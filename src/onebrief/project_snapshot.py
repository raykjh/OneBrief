"""Immutable, least-privilege project snapshots for remote development jobs."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import zipfile
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from onebrief.project_import import ExternalProjectImporter, MANIFEST_NAME, ProjectManifest
from onebrief.toolpack_lifecycle import ProjectToolPackLifecycle


SNAPSHOT_ARCHIVE = "project_snapshot.zip"
SNAPSHOT_MANIFEST = "project_snapshot.json"
MAX_SNAPSHOT_FILES = 5_000
# A remote Unity verification snapshot must preserve committed fonts, scene data,
# audio, and small runtime media or its compile/render evidence is not faithful.
# Keep the transfer bounded, but size it for a medium client rather than a source-
# only Python project. Julpae's largest committed file is ~16.4 MB and its exact
# approved tree is ~295 MB.
MAX_SNAPSHOT_FILE_BYTES = 20_000_000
MAX_SNAPSHOT_BYTES = 350_000_000
BLOCKED_PARTS = {
    ".git", ".ssh", "credentials", "library", "logs", "node_modules",
    "secrets", "service-account", "service_account", "temp", "usersettings",
}
BLOCKED_SECRET_SUFFIXES = {
    ".jks", ".key", ".keystore", ".p12", ".pfx", ".pem", ".pkcs12",
}
BLOCKED_SECRET_NAME_MARKERS = {
    "client_secret", "private_key", "release-key", "service-account-key",
}


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=120, shell=False, check=False,
    )
    if completed.returncode:
        detail = "\n".join(part.strip() for part in (completed.stdout, completed.stderr) if part.strip())
        # Git can emit hundreds of CRLF notices before the actionable fatal
        # line. Preserve the tail so a durable failure record names the cause.
        raise RuntimeError(detail[-4000:] or f"git {' '.join(args)} failed")
    return completed.stdout


def _git_blobs(root: Path, relatives: list[str]) -> dict[str, bytes]:
    """Read many committed blobs exactly through one persistent Git process."""
    process = subprocess.Popen(
        ["git", "cat-file", "--batch"], cwd=root, stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    if process.stdin is None or process.stdout is None:
        process.kill()
        raise RuntimeError("cannot open Git batch blob reader")
    blobs: dict[str, bytes] = {}
    try:
        for relative in relatives:
            process.stdin.write(f"HEAD:{relative}\n".encode("utf-8"))
            process.stdin.flush()
            header = process.stdout.readline().decode("utf-8", errors="replace").strip()
            parts = header.rsplit(" ", 2)
            if len(parts) != 3 or parts[1] != "blob" or not parts[2].isdigit():
                raise RuntimeError(f"cannot read committed file {relative}: {header}")
            size = int(parts[2])
            data = process.stdout.read(size)
            terminator = process.stdout.read(1)
            if len(data) != size or terminator != b"\n":
                raise RuntimeError(f"truncated committed file from Git: {relative}")
            blobs[relative] = data
        process.stdin.close()
        if process.wait(timeout=30):
            detail = (process.stderr.read() if process.stderr else b"").decode(
                "utf-8", errors="replace"
            ).strip()
            raise RuntimeError(detail[:4000] or "Git batch blob reader failed")
    finally:
        if process.poll() is None:
            process.kill()
    return blobs


def _safe_relative(value: str) -> PurePosixPath:
    path = PurePosixPath(value.replace("\\", "/"))
    lowered = {part.casefold() for part in path.parts}
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"unsafe snapshot path: {value}")
    if lowered & BLOCKED_PARTS or any(part.startswith(".env") for part in lowered):
        raise ValueError(f"blocked snapshot path: {value}")
    return path


def _is_sensitive_snapshot_path(value: str) -> bool:
    path = PurePosixPath(value.replace("\\", "/"))
    lowered_parts = {part.casefold() for part in path.parts}
    filename = path.name.casefold()
    return bool(
        lowered_parts & BLOCKED_PARTS
        or any(part.startswith(".env") for part in lowered_parts)
        or any(filename.endswith(suffix) for suffix in BLOCKED_SECRET_SUFFIXES)
        or any(marker in filename for marker in BLOCKED_SECRET_NAME_MARKERS)
    )


class SnapshotFile(BaseModel):
    path: str
    size_bytes: int = Field(ge=0, le=MAX_SNAPSHOT_FILE_BYTES)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class ProjectSnapshotManifest(BaseModel):
    schema_version: Literal["onebrief-project-snapshot-v1"] = "onebrief-project-snapshot-v1"
    project_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,63}$")
    source_head_sha: str = Field(pattern=r"^[a-f0-9]{40}$")
    approved_toolpack_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    approved_read_prefixes: list[str]
    approved_write_prefixes: list[str]
    approved_suffixes: list[str]
    approved_adapters: list[str]
    created_at: str
    archive_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    archive_size_bytes: int = Field(gt=0, le=MAX_SNAPSHOT_BYTES)
    files: list[SnapshotFile] = Field(min_length=1, max_length=MAX_SNAPSHOT_FILES)

    @model_validator(mode="after")
    def validate_unique_paths(self) -> "ProjectSnapshotManifest":
        paths = [item.path.casefold() for item in self.files]
        if len(paths) != len(set(paths)):
            raise ValueError("project snapshot contains duplicate file paths")
        if sum(item.size_bytes for item in self.files) > MAX_SNAPSHOT_BYTES:
            raise ValueError("project snapshot exceeds the uncompressed size limit")
        return self


def _selected_files(root: Path, read_prefixes: list[str]) -> list[tuple[str, bytes]]:
    """Return a faithful, secret-filtered copy of the committed repository.

    ``read_prefixes`` still limits which files are exposed to the maker's model
    context.  It must not trim the filesystem supplied to deterministic build
    and test adapters: tests commonly depend on committed fixtures, reports,
    lockfiles, or generated reference data outside the editable source tree.
    """
    if not read_prefixes:
        raise ValueError("approved project snapshot has no readable source prefixes")
    tracked = [item for item in _git(root, "ls-files", "-z").split("\0") if item]
    tracked_set = set(tracked)
    # Folder registration deliberately creates the resident OneBrief manifest
    # without making a Git commit on the user's behalf.  It is nevertheless an
    # exact-hash-qualified root input required to restore the remote ToolPack,
    # so include that one generated control file even when it is untracked.
    resident = root / MANIFEST_NAME
    if resident.is_file() and MANIFEST_NAME not in tracked:
        tracked.append(MANIFEST_NAME)
    committed_blobs = _git_blobs(root, sorted(tracked_set))
    selected: list[tuple[str, bytes]] = []
    for relative in tracked:
        if _is_sensitive_snapshot_path(relative):
            continue
        pure = _safe_relative(relative)
        normalized = pure.as_posix()
        source = (root / Path(*pure.parts)).resolve()
        if not source.is_relative_to(root) or source.is_symlink() or not source.is_file():
            raise ValueError(f"snapshot source must be a regular in-repository file: {normalized}")
        data = committed_blobs[relative] if relative in tracked_set else source.read_bytes()
        size = len(data)
        if size > MAX_SNAPSHOT_FILE_BYTES:
            raise ValueError(f"snapshot file is too large: {normalized}")
        selected.append((normalized, data))
    if not selected:
        raise ValueError("approved project snapshot contains no files")
    if len(selected) > MAX_SNAPSHOT_FILES:
        raise ValueError("project snapshot contains too many files")
    if sum(len(data) for _, data in selected) > MAX_SNAPSHOT_BYTES:
        raise ValueError("project snapshot exceeds the uncompressed size limit")
    return sorted(selected)


def create_project_snapshot(project_id: str, inputs_dir: Path) -> ProjectSnapshotManifest:
    """Create an immutable current-tree snapshot after exact ToolPack approval."""
    lifecycle = ProjectToolPackLifecycle(project_id)
    state = lifecycle.state()
    if not state.execution_ready or state.generated is None or state.approval is None:
        detail = "; ".join(state.execution_blockers) or "ToolPack is not approved"
        raise PermissionError(detail)
    root = Path(state.generated.project_root).resolve()
    head = _git(root, "rev-parse", "HEAD").strip()
    if head != state.generated.repository_head_sha:
        raise RuntimeError("project HEAD changed after ToolPack approval")
    files = _selected_files(root, state.generated.allowed_read_prefixes)
    inputs_dir.mkdir(parents=True, exist_ok=True)
    archive = inputs_dir / SNAPSHOT_ARCHIVE
    records: list[SnapshotFile] = []
    with zipfile.ZipFile(archive, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as bundle:
        for relative, data in files:
            bundle.writestr(f"repository/{relative}", data)
            records.append(SnapshotFile(
                path=relative,
                size_bytes=len(data),
                sha256=hashlib.sha256(data).hexdigest(),
            ))
    if archive.stat().st_size > MAX_SNAPSHOT_BYTES:
        archive.unlink(missing_ok=True)
        raise ValueError("compressed project snapshot exceeds the size limit")
    manifest = ProjectSnapshotManifest(
        project_id=project_id,
        source_head_sha=head,
        approved_toolpack_sha256=state.generated.sha256,
        approved_read_prefixes=state.generated.allowed_read_prefixes,
        approved_write_prefixes=state.generated.allowed_write_prefixes,
        approved_suffixes=state.generated.allowed_suffixes,
        approved_adapters=sorted(
            f"{item.adapter_id.value}:{item.parameter or ''}"
            for item in state.generated.adapters if item.enabled
        ),
        created_at=_now(),
        archive_sha256=_sha256(archive),
        archive_size_bytes=archive.stat().st_size,
        files=records,
    )
    (inputs_dir / SNAPSHOT_MANIFEST).write_text(
        manifest.model_dump_json(indent=2) + "\n", encoding="utf-8"
    )
    return manifest


def record_local_project_provenance(job_dir: Path, project_id: str) -> Path:
    """Bind a local-only job to the exact approved repository without copying assets.

    Large Unity repositories are validated in a disposable local Git clone. Shipping every
    committed binary asset into the job would add hundreds of megabytes without improving
    isolation, while the approved ToolPack already binds the source HEAD and permissions.
    """

    lifecycle = ProjectToolPackLifecycle(project_id)
    state = lifecycle.state()
    if not state.execution_ready or state.generated is None or state.approval is None:
        detail = "; ".join(state.execution_blockers) or "ToolPack is not approved"
        raise PermissionError(detail)
    generated = state.generated
    root = Path(generated.project_root).resolve()
    head = _git(root, "rev-parse", "HEAD").strip()
    if head != generated.repository_head_sha:
        raise RuntimeError("project HEAD changed after ToolPack approval")
    workspace = job_dir / "work" / "project_snapshot"
    workspace.mkdir(parents=True, exist_ok=True)
    evidence = {
        "schema_version": "onebrief-local-project-provenance-v1",
        "mode": "local_verified_clone",
        "project_id": project_id,
        "source_head_sha": head,
        "source_toolpack_sha256": generated.sha256,
        "status": "verified_and_approved",
        "verified_at": _now(),
    }
    path = workspace / "restore_evidence.json"
    path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def _extract_verified(archive: Path, destination: Path, manifest: ProjectSnapshotManifest) -> None:
    if archive.stat().st_size != manifest.archive_size_bytes or _sha256(archive) != manifest.archive_sha256:
        raise RuntimeError("project snapshot archive hash or size changed")
    expected = {f"repository/{item.path}": item for item in manifest.files}
    destination.mkdir(parents=True, exist_ok=False)
    with zipfile.ZipFile(archive) as bundle:
        observed = {item.filename: item for item in bundle.infolist() if not item.is_dir()}
        if set(observed) != set(expected):
            raise RuntimeError("project snapshot archive file set does not match its manifest")
        for archive_name, recorded in expected.items():
            info = observed[archive_name]
            mode = info.external_attr >> 16
            if stat.S_ISLNK(mode):
                raise RuntimeError(f"project snapshot contains a symbolic link: {archive_name}")
            if info.file_size != recorded.size_bytes:
                raise RuntimeError(f"project snapshot file size changed: {recorded.path}")
            pure = _safe_relative(recorded.path)
            target = (destination / Path(*pure.parts)).resolve()
            if not target.is_relative_to(destination):
                raise RuntimeError(f"project snapshot path escaped its destination: {recorded.path}")
            target.parent.mkdir(parents=True, exist_ok=True)
            digest = hashlib.sha256()
            written = 0
            with bundle.open(info, "r") as source, target.open("xb") as output:
                while chunk := source.read(1024 * 1024):
                    digest.update(chunk)
                    output.write(chunk)
                    written += len(chunk)
            if written != recorded.size_bytes or digest.hexdigest() != recorded.sha256:
                target.unlink(missing_ok=True)
                raise RuntimeError(f"project snapshot file hash changed: {recorded.path}")


def restore_project_snapshot(job_dir: Path, project_id: str) -> Path | None:
    """Restore and re-approve an immutable snapshot inside one ephemeral job workspace."""
    inputs = job_dir / "inputs"
    manifest_path = inputs / SNAPSHOT_MANIFEST
    archive = inputs / SNAPSHOT_ARCHIVE
    if not manifest_path.exists() and not archive.exists():
        return None
    if not manifest_path.is_file() or not archive.is_file():
        raise RuntimeError("project snapshot input is incomplete")
    manifest = ProjectSnapshotManifest.model_validate_json(
        manifest_path.read_text(encoding="utf-8")
    )
    if manifest.project_id != project_id:
        raise RuntimeError("project snapshot identity does not match the requested project")
    workspace = job_dir / "work" / "project_snapshot"
    repository = workspace / "repository"
    registry = workspace / "registry"
    _extract_verified(archive, repository, manifest)
    resident = repository / MANIFEST_NAME
    if not resident.is_file():
        raise RuntimeError(f"project snapshot is missing {MANIFEST_NAME}")
    original = ProjectManifest.model_validate_json(resident.read_text(encoding="utf-8"))
    if original.project_id != project_id:
        raise RuntimeError("resident project manifest identity mismatch")
    adjusted = original.model_copy(update={"project_root": str(repository.resolve())})
    resident.write_text(adjusted.model_dump_json(indent=2) + "\n", encoding="utf-8")
    _git(repository, "init")
    _git(repository, "config", "user.name", "OneBrief Cloud Snapshot")
    _git(repository, "config", "user.email", "onebrief-snapshot@example.invalid")
    _git(repository, "config", "core.longpaths", "true")
    _git(repository, "config", "core.autocrlf", "false")
    _git(repository, "add", "-A")
    _git(repository, "commit", "-m", f"Restore approved snapshot from {manifest.source_head_sha}")
    ExternalProjectImporter(registry).import_bytes(resident.read_bytes())
    lifecycle = ProjectToolPackLifecycle(project_id, registry)
    state = lifecycle.generate_and_qualify()
    generated = state.generated
    if state.qualification is None or state.qualification.status != "passed" or generated is None:
        failed_checks = [
            f"{item.check_id}: {item.message}"
            for item in (state.qualification.checks if state.qualification else [])
            if not item.passed
        ]
        detail = "; ".join(failed_checks) or "; ".join(state.execution_blockers)
        raise RuntimeError(
            "restored project ToolPack qualification failed"
            + (f": {detail}" if detail else "")
        )
    adapters = sorted(
        f"{item.adapter_id.value}:{item.parameter or ''}"
        for item in generated.adapters if item.enabled
    )
    capability_differences = {
        key: {"approved": approved, "restored": restored}
        for key, approved, restored in (
            ("read_prefixes", manifest.approved_read_prefixes, generated.allowed_read_prefixes),
            ("write_prefixes", manifest.approved_write_prefixes, generated.allowed_write_prefixes),
            ("suffixes", manifest.approved_suffixes, generated.allowed_suffixes),
            ("adapters", manifest.approved_adapters, adapters),
        )
        if approved != restored
    }
    if capability_differences:
        detail = json.dumps(capability_differences, ensure_ascii=False, sort_keys=True)
        raise RuntimeError(
            "restored ToolPack capabilities differ from the locally approved snapshot: "
            + detail
        )
    approved = lifecycle.approve(state.qualification.toolpack_sha256)
    if not approved.execution_ready:
        raise RuntimeError("restored project ToolPack is not execution-ready")
    evidence = {
        "schema_version": "onebrief-project-snapshot-restore-v1",
        "project_id": project_id,
        "source_head_sha": manifest.source_head_sha,
        "source_toolpack_sha256": manifest.approved_toolpack_sha256,
        "snapshot_archive_sha256": manifest.archive_sha256,
        "restored_head_sha": generated.repository_head_sha,
        "restored_toolpack_sha256": generated.sha256,
        "file_count": len(manifest.files),
        "total_bytes": sum(item.size_bytes for item in manifest.files),
        "status": "verified_and_approved",
    }
    (workspace / "restore_evidence.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return repository
