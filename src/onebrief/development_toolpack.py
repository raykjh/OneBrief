"""Isolated, policy-bounded source editing and deterministic project verification."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path, PurePosixPath
from typing import Callable

from pydantic import BaseModel, Field, field_validator, model_validator

from onebrief.schemas import InternalSource, SourcePriority


EDITABLE_SUFFIXES = {
    ".css", ".html", ".js", ".json", ".jsx", ".md", ".mjs", ".py",
    ".toml", ".ts", ".tsx", ".txt", ".yaml", ".yml",
}
EDITABLE_PREFIXES = ("web/app/", "web/components/", "web/lib/", "web/src/", "web/public/")
EDITABLE_EXACT_PATHS = {"web/index.html"}
DANGEROUS_RUNTIME = re.compile(
    r"(?:child_process|node:(?:fs|net|http|https|tls|dgram|cluster|worker_threads)|"
    r"require\s*\(\s*['\"](?:fs|net|http|https|child_process)['\"]|"
    r"process\s*\.\s*(?:env|binding|mainModule)|"
    r"(?:eval|Function)\s*\()",
    re.IGNORECASE,
)

CONTEXT_SUFFIXES = EDITABLE_SUFFIXES | {".cjs"}
BLOCKED_PARTS = {
    ".env", ".git", ".github", ".ssh", "credentials", "node_modules",
    "secrets", "service-account", "service_account",
}
MAX_CHANGE_FILES = 12
MAX_CHANGE_BYTES = 64_000
MAX_CONTEXT_FILE_BYTES = 120_000
MAX_CONTEXT_BYTES = 120_000


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _safe_relative(value: str) -> PurePosixPath:
    normalized = value.replace("\\", "/")
    path = PurePosixPath(normalized)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("change path must be a safe repository-relative path")
    lowered = {part.casefold() for part in path.parts}
    if lowered & BLOCKED_PARTS or any(part.startswith(".env") for part in lowered):
        raise ValueError(f"change path is blocked by policy: {value}")
    normalized_path = path.as_posix()
    if normalized_path not in EDITABLE_EXACT_PATHS and not normalized_path.startswith(EDITABLE_PREFIXES):
        raise ValueError(f"path is outside the approved Exchange web source area: {value}")
    if path.suffix.casefold() not in EDITABLE_SUFFIXES:
        raise ValueError(f"file type is not editable by this ToolPack: {value}")
    return path

def approved_edit_path(value: str) -> str | None:
    """Return the canonical editable path, or None for evidence-only repository files."""
    try:
        return _safe_relative(value).as_posix()
    except ValueError:
        return None


def _context_priority(value: str) -> tuple[int, str]:
    normalized = value.replace("\\", "/")
    if approved_edit_path(normalized) is not None:
        return (0, normalized.casefold())
    if normalized in {"package.json", "web/package.json"}:
        return (1, normalized.casefold())
    return (2 if normalized.startswith("web/") else 3, normalized.casefold())





class FileChange(BaseModel):
    path: str
    base_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    content: str = Field(max_length=MAX_CHANGE_BYTES)
    reason: str = Field(min_length=3, max_length=500)

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return _safe_relative(value).as_posix()

    @model_validator(mode="after")
    def block_dangerous_runtime_access(self) -> "FileChange":
        if DANGEROUS_RUNTIME.search(self.content):
            raise ValueError("change content requests a prohibited host-runtime capability")
        return self



class CodeChangeSet(BaseModel):
    schema_version: str = "onebrief-code-change-set-v1"
    summary: str = Field(min_length=3, max_length=1000)
    changes: list[FileChange] = Field(min_length=1, max_length=MAX_CHANGE_FILES)

    @model_validator(mode="after")
    def unique_paths_and_size(self) -> "CodeChangeSet":
        paths = [item.path.casefold() for item in self.changes]
        if len(paths) != len(set(paths)):
            raise ValueError("a change set cannot edit the same path twice")
        total = sum(len(item.content.encode("utf-8")) for item in self.changes)
        if total > MAX_CHANGE_BYTES:
            raise ValueError("change set exceeds the total text-size limit")
        return self


class DevelopmentCommandResult(BaseModel):
    command_id: str
    argv: list[str]
    exit_code: int
    duration_seconds: float = Field(ge=0)
    output_tail: str


class RepositoryContextFile(BaseModel):
    path: str
    size_bytes: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class RepositoryInspection(BaseModel):
    schema_version: str = "onebrief-development-inspection-v1"
    repository_name: str
    head_sha: str = Field(pattern=r"^[a-f0-9]{40}$")
    context_files: list[RepositoryContextFile]
    safety_boundary: list[str]


class DevelopmentRun(BaseModel):
    schema_version: str = "onebrief-development-run-v1"
    status: str
    repository_name: str
    base_head_sha: str = Field(pattern=r"^[a-f0-9]{40}$")
    summary: str
    changed_paths: list[str]
    commands: list[DevelopmentCommandResult]
    patch_path: str
    safety_boundary: list[str]


CommandRunner = Callable[[str, list[str], Path, int], DevelopmentCommandResult]


def _default_runner(
    command_id: str, argv: list[str], cwd: Path, timeout_seconds: int
) -> DevelopmentCommandResult:
    started = time.monotonic()
    environment = {
        key: value for key, value in os.environ.items()
        if key.casefold() in {
            "path", "systemroot", "temp", "tmp", "comspec", "pathext",
            "userprofile", "appdata", "localappdata", "programdata",
            "homedrive", "homepath",
            "allusersprofile", "commonprogramfiles", "commonprogramfiles(x86)",
            "commonprogramw6432", "computername", "number_of_processors", "os",
            "processor_architecture", "programfiles", "programfiles(x86)",
            "programw6432", "public", "systemdrive", "username", "userdomain",
            "windir",
        }
    }
    environment.update({"CI": "1", "NO_COLOR": "1"})
    completed = subprocess.run(
        argv, cwd=cwd, env=environment, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=timeout_seconds,
        shell=False, check=False,
    )
    output = (completed.stdout + "\n" + completed.stderr).strip()
    if "-logFile" in argv:
        index = argv.index("-logFile") + 1
        if index < len(argv):
            log_path = Path(argv[index])
            if log_path.is_file():
                log_text = log_path.read_text(encoding="utf-8", errors="replace")
                if log_text.strip():
                    output = (output + "\n" + log_text[-20_000:]).strip()
                    signals = [
                        line for line in log_text.splitlines()
                        if any(marker in line.casefold() for marker in (
                            "error", "failed", "exception", "compilation", "package manager"
                        ))
                    ]
                    if signals:
                        output = (output + "\nVERIFICATION SIGNALS\n" + "\n".join(signals[-60:])).strip()
    result = DevelopmentCommandResult(
        command_id=command_id,
        argv=argv,
        exit_code=completed.returncode,
        duration_seconds=round(time.monotonic() - started, 3),
        output_tail=output[-20_000:],
    )
    if result.exit_code != 0:
        detail = result.output_tail[-4_000:].strip()
        suffix = f"\n{detail}" if detail else ""
        raise RuntimeError(f"development verification failed: {command_id}{suffix}")
    return result


class ExchangeDevelopmentToolPack:
    """Develop Exchange in an isolated clone and return only verified review artifacts."""

    def __init__(self, root: Path | None = None, runner: CommandRunner | None = None):
        configured = root or Path(os.environ.get("ONEBRIEF_EXCHANGE_ROOT", r"C:\exchange"))
        self.root = configured.resolve()
        self.runner = runner or _default_runner

    def _git(self, *args: str, cwd: Path | None = None, timeout: int = 120) -> str:
        completed = subprocess.run(
            ["git", *args], cwd=cwd or self.root, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
            shell=False, check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError((completed.stderr or completed.stdout).strip()[:1000])
        return completed.stdout

    def _validate_root(self) -> str:
        required = [self.root / ".git", self.root / "package.json", self.root / "web" / "package.json"]
        if not self.root.is_dir() or any(not item.exists() for item in required):
            raise FileNotFoundError("approved Exchange development repository is unavailable")
        product_changes = [
            line for line in self._git("status", "--porcelain").splitlines()
            if not line[3:].replace("\\", "/").startswith(".onebrief/")
        ]
        if product_changes:
            raise RuntimeError("development requires a clean source repository")
        head = self._git("rev-parse", "HEAD").strip()
        if len(head) != 40:
            raise RuntimeError("repository HEAD could not be verified")
        return head

    def _git_blob(self, head: str, relative: str, timeout: int = 120) -> bytes:
        """Read the committed bytes that a clean verification clone will receive."""
        completed = subprocess.run(
            ["git", "show", f"{head}:{relative}"], cwd=self.root,
            capture_output=True, timeout=timeout, shell=False, check=False,
        )
        if completed.returncode != 0:
            message = (completed.stderr or completed.stdout).decode("utf-8", errors="replace")
            raise RuntimeError(message.strip()[:1000])
        return completed.stdout


    def _tracked_files(self) -> list[str]:
        output = self._git("ls-files", "-z")
        tracked = [item for item in output.split("\0") if item]
        return sorted(tracked, key=_context_priority)

    def inspect(self, output_dir: Path) -> tuple[RepositoryInspection, list[InternalSource]]:
        head = self._validate_root()
        context_dir = output_dir / "repository_context"
        records: list[RepositoryContextFile] = []
        sources: list[InternalSource] = []
        total = 0
        for relative in self._tracked_files():
            pure = PurePosixPath(relative)
            lowered = {part.casefold() for part in pure.parts}
            source = (self.root / Path(*pure.parts)).resolve()
            if lowered & BLOCKED_PARTS or source.suffix.casefold() not in CONTEXT_SUFFIXES:
                continue
            if not source.is_relative_to(self.root) or source.is_symlink() or not source.is_file():
                continue
            committed = self._git_blob(head, pure.as_posix())
            size = len(committed)
            if size > MAX_CONTEXT_FILE_BYTES or total + size > MAX_CONTEXT_BYTES:
                continue
            destination = context_dir / Path(*pure.parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(committed)
            digest = hashlib.sha256(committed).hexdigest()
            records.append(RepositoryContextFile(path=pure.as_posix(), size_bytes=size, sha256=digest))
            content = destination.read_text(encoding="utf-8", errors="replace")
            sources.append(InternalSource(
                name=f"exchange-source/{pure.as_posix()}",
                priority=SourcePriority.MANDATORY,
                requirement_keys=["exchange_development"],
                summary="Tracked source file from the approved Exchange repository.",
                content=content,
                media_type="text/plain",
                size_bytes=size,
                sha256=digest,
            ))
            total += size
        inspection = RepositoryInspection(
            repository_name=self.root.name,
            head_sha=head,
            context_files=records,
            safety_boundary=[
                "fixed approved repository root",
                "clean tracked source snapshot only",
                "secret, credential, dependency, and binary paths excluded",
                "no deployment, push, account access, or external side effects",
            ],
        )
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "repository_inspection.json").write_text(
            inspection.model_dump_json(indent=2) + "\n", encoding="utf-8"
        )
        return inspection, sources

    def _attach_dependencies(self, clone: Path) -> list[Path]:
        attached: list[Path] = []
        source = self.root / "web" / "node_modules"
        target = clone / "web" / "node_modules"
        if not source.is_dir() or target.exists():
            return attached
        if os.name == "nt":
            completed = subprocess.run(
                ["cmd.exe", "/c", "mklink", "/J", str(target), str(source)],
                capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
            )
            if completed.returncode != 0:
                raise RuntimeError("approved dependency junction could not be created")
        else:
            target.symlink_to(source, target_is_directory=True)
        attached.append(target)
        return attached

    @staticmethod
    def _detach_dependencies(paths: list[Path]) -> None:
        for path in paths:
            if os.name == "nt":
                subprocess.run(["cmd.exe", "/c", "rmdir", str(path)], check=False)
            elif path.is_symlink():
                path.unlink()

    def apply_and_verify(self, change_set: CodeChangeSet, output_dir: Path) -> DevelopmentRun:
        head = self._validate_root()
        output_dir = output_dir.resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="onebrief_dev_") as temporary:
            clone = Path(temporary) / "repository"
            self._git("clone", "--local", "--no-hardlinks", str(self.root), str(clone), cwd=Path(temporary))
            new_paths: list[str] = []
            for change in change_set.changes:
                pure = _safe_relative(change.path)
                target = (clone / Path(*pure.parts)).resolve()
                if not target.is_relative_to(clone) or target.is_symlink():
                    raise PermissionError(f"unsafe development path: {change.path}")
                if target.exists():
                    committed_sha = hashlib.sha256(self._git_blob(head, change.path)).hexdigest()
                    if change.base_sha256 is None or committed_sha != change.base_sha256:
                        raise RuntimeError(f"stale or missing base hash: {change.path}")
                elif change.base_sha256 is not None:
                    raise RuntimeError(f"new file cannot declare a base hash: {change.path}")
                else:
                    new_paths.append(change.path)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(change.content, encoding="utf-8", newline="\n")

            if new_paths:
                self._git("add", "-N", "--", *new_paths, cwd=clone)
            attached = self._attach_dependencies(clone)
            try:
                npm = "npm.cmd" if os.name == "nt" else "npm"
                commands = [
                    self.runner("repository_tests", [npm, "test"], clone, 240),
                    self.runner("web_build", [npm, "--prefix", "web", "run", "build"], clone, 300),
                    self.runner("web_tests", [npm, "--prefix", "web", "test"], clone, 240),
                    self.runner("production_http", [npm, "--prefix", "web", "run", "test:production"], clone, 240),
                ]
            finally:
                self._detach_dependencies(attached)

            approved_paths = [item.path for item in change_set.changes]
            patch = self._git("diff", "--binary", "--no-ext-diff", "--", *approved_paths, cwd=clone)
            if not patch.strip():
                raise ValueError("development change set produced no repository diff")
            patch_path = output_dir / "changes.patch"
            patch_path.write_text(patch, encoding="utf-8", newline="\n")
            changed_dir = output_dir / "changed_files"
            for change in change_set.changes:
                source = clone / Path(*PurePosixPath(change.path).parts)
                destination = changed_dir / Path(*PurePosixPath(change.path).parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
            run = DevelopmentRun(
                status="verified",
                repository_name=self.root.name,
                base_head_sha=head,
                summary=change_set.summary,
                changed_paths=[item.path for item in change_set.changes],
                commands=commands,
                patch_path=patch_path.relative_to(output_dir.parent).as_posix(),
                safety_boundary=[
                    "all edits occurred in an isolated local clone",
                    "only bounded text paths with matching base hashes were accepted",
                    "only fixed test, build, and production HTTP smoke commands were executed",
                    "static host-runtime capability scan passed before execution",
                    "original repository, Git remotes, deployment, and accounts were not changed",
                ],
            )
            (output_dir / "development_run.json").write_text(
                run.model_dump_json(indent=2) + "\n", encoding="utf-8"
            )
            (output_dir / "change_set.json").write_text(
                change_set.model_dump_json(indent=2) + "\n", encoding="utf-8"
            )
            return run
