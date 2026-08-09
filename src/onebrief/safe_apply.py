"""Apply a verified OneBrief development result with backup and automatic rollback."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Callable, Literal
from uuid import uuid4

from filelock import FileLock
from pydantic import BaseModel, Field

from onebrief.development_toolpack import CommandRunner, DevelopmentCommandResult
from onebrief.generic_development_toolpack import (
    ApprovedProjectDevelopmentToolPack,
    ProjectCodeChangeSet,
    generic_safe_relative,
)
from onebrief.project_catalog import ProjectCatalog


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class SafeApplyReceipt(BaseModel):
    schema_version: Literal["onebrief-safe-apply-v1"] = "onebrief-safe-apply-v1"
    apply_id: str
    project_id: str
    result_package_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_head_sha: str = Field(pattern=r"^[a-f0-9]{40}$")
    status: Literal["applied", "rolled_back"]
    changed_paths: list[str]
    backup_path: str
    commands: list[DevelopmentCommandResult] = Field(default_factory=list)
    applied_at: str
    message: str


def _verify_package(result_root: Path) -> str:
    manifest_path = result_root / "package_manifest.json"
    if not manifest_path.is_file():
        raise RuntimeError("result package manifest is missing")
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    if payload.get("status") != "complete":
        raise RuntimeError("only a complete result package can be applied")
    files = payload.get("files")
    if not isinstance(files, list) or not files:
        raise RuntimeError("result package file manifest is empty")
    for item in files:
        if not isinstance(item, dict):
            raise RuntimeError("result package contains an invalid file record")
        pure = generic_safe_relative(str(item.get("path", "")))
        target = (result_root / Path(*pure.parts)).resolve()
        if not target.is_relative_to(result_root) or not target.is_file():
            raise RuntimeError(f"result package file is missing: {pure.as_posix()}")
        if target.stat().st_size != item.get("size_bytes") or _sha256(target) != item.get("sha256"):
            raise RuntimeError(f"result package file hash mismatch: {pure.as_posix()}")
    return _sha256(manifest_path)


def _require_pass(result_root: Path) -> None:
    artifacts = result_root / "artifacts"
    for relative in ("final_verification.json", "final_approval.json"):
        path = artifacts / relative
        if not path.is_file():
            raise RuntimeError(f"verified result is missing {relative}")
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("verdict") != "PASS":
            raise RuntimeError(f"verified result did not pass {relative}")


def _atomic_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{uuid4().hex}.tmp")
    temporary.write_bytes(payload)
    os.replace(temporary, path)


def _git_diff_check(root: Path, paths: list[str]) -> None:
    completed = subprocess.run(
        ["git", "diff", "--check", "--", *paths], cwd=root,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=120, shell=False, check=False,
    )
    if completed.returncode:
        detail = "\n".join(part.strip() for part in (completed.stdout, completed.stderr) if part.strip())
        raise RuntimeError(f"applied patch hygiene failed: {detail[:4000]}")


def apply_verified_project_result(
    project_id: str,
    result_root: Path,
    backup_root: Path,
    *,
    runner: CommandRunner | None = None,
) -> SafeApplyReceipt:
    """Apply one immutable PASS package to its unchanged source repository."""
    result_root = result_root.resolve()
    backup_root = backup_root.resolve()
    package_sha = _verify_package(result_root)
    _require_pass(result_root)
    receipt_path = backup_root / project_id / package_sha / "receipt.json"
    if receipt_path.is_file():
        receipt = SafeApplyReceipt.model_validate_json(receipt_path.read_text(encoding="utf-8"))
        if receipt.status == "applied":
            return receipt
    project = ProjectCatalog().get(project_id)
    if project.origin != "imported":
        raise PermissionError("safe apply requires an imported project with an approved ToolPack")
    restore_evidence = result_root / "artifacts" / "project_snapshot" / "restore_evidence.json"
    if not restore_evidence.is_file():
        raise RuntimeError("result package has no verified project snapshot provenance")
    provenance = json.loads(restore_evidence.read_text(encoding="utf-8"))
    source_head = str(provenance.get("source_head_sha", ""))
    if provenance.get("project_id") != project_id or provenance.get("status") != "verified_and_approved":
        raise RuntimeError("result package project provenance is invalid")
    development = result_root / "artifacts" / "development"
    change_set = ProjectCodeChangeSet.model_validate_json(
        (development / "change_set.json").read_text(encoding="utf-8")
    )
    pack = ApprovedProjectDevelopmentToolPack(project_id, runner=runner)
    profile, current_head = pack._validate_root()
    if current_head != source_head:
        raise RuntimeError("the project changed after this Cloud result was created")
    changed_paths = [item.path for item in change_set.changes]
    commands = pack._commands(profile, pack.root, change_set.summary)
    apply_dir = receipt_path.parent
    lock_path = backup_root / project_id / ".apply.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    apply_id = package_sha[:16]
    backup_records: list[tuple[Path, Path | None]] = []
    results: list[DevelopmentCommandResult] = []
    with FileLock(lock_path, timeout=1):
        apply_dir.mkdir(parents=True, exist_ok=True)
        originals = apply_dir / "original"
        try:
            for change in change_set.changes:
                if pack.approved_edit_path(change.path) is None:
                    raise PermissionError(f"result path is outside the approved write boundary: {change.path}")
                pure = generic_safe_relative(change.path)
                target = (pack.root / Path(*pure.parts)).resolve()
                if not target.is_relative_to(pack.root) or target.is_symlink():
                    raise PermissionError(f"unsafe result path: {change.path}")
                changed_file = (development / "changed_files" / Path(*pure.parts)).resolve()
                if not changed_file.is_relative_to(development) or not changed_file.is_file():
                    raise RuntimeError(f"changed-file artifact is missing: {change.path}")
                normalized = pack._normalize_safe_generated_text(change.path, change.content)
                payload = normalized.encode("utf-8")
                if changed_file.read_bytes() != payload:
                    raise RuntimeError(f"changed-file artifact differs from its approved change set: {change.path}")
                backup = None
                if target.exists():
                    if change.base_sha256 is None or _sha256(target) != change.base_sha256:
                        raise RuntimeError(f"source file changed after Cloud inspection: {change.path}")
                    backup = originals / Path(*pure.parts)
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(target, backup)
                elif change.base_sha256 is not None:
                    raise RuntimeError(f"expected source file is missing: {change.path}")
                backup_records.append((target, backup))
                _atomic_bytes(target, payload)
            _git_diff_check(pack.root, changed_paths)
            results = [
                pack.runner(command_id, argv, pack.root, timeout)
                for command_id, argv, timeout in commands
            ]
            receipt = SafeApplyReceipt(
                apply_id=apply_id,
                project_id=project_id,
                result_package_sha256=package_sha,
                source_head_sha=source_head,
                status="applied",
                changed_paths=changed_paths,
                backup_path=str(apply_dir),
                commands=results,
                applied_at=_now(),
                message="Verified Cloud changes were applied and passed local validation.",
            )
            receipt_path.write_text(receipt.model_dump_json(indent=2) + "\n", encoding="utf-8")
            return receipt
        except Exception as exc:
            for target, backup in reversed(backup_records):
                if backup is None:
                    target.unlink(missing_ok=True)
                elif backup.is_file():
                    _atomic_bytes(target, backup.read_bytes())
            receipt = SafeApplyReceipt(
                apply_id=apply_id,
                project_id=project_id,
                result_package_sha256=package_sha,
                source_head_sha=source_head,
                status="rolled_back",
                changed_paths=changed_paths,
                backup_path=str(apply_dir),
                commands=results,
                applied_at=_now(),
                message=f"Application failed and original files were restored: {type(exc).__name__}: {exc}",
            )
            receipt_path.write_text(receipt.model_dump_json(indent=2) + "\n", encoding="utf-8")
            raise RuntimeError(receipt.message) from exc
