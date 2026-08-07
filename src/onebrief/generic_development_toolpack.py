"""Approved imported-project editing in a disposable Git clone."""

from __future__ import annotations

import hashlib
import json
import re
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath

from pydantic import BaseModel, Field, field_validator, model_validator

from onebrief.development_toolpack import (
    BLOCKED_PARTS,
    DANGEROUS_RUNTIME,
    MAX_CHANGE_BYTES,
    MAX_CHANGE_FILES,
    MAX_CONTEXT_BYTES,
    MAX_CONTEXT_FILE_BYTES,
    CommandRunner,
    DevelopmentRun,
    RepositoryContextFile,
    RepositoryInspection,
    _default_runner,
)
from onebrief.schemas import InternalSource, SourcePriority
from onebrief.toolpack_lifecycle import AdapterId, ProjectToolPackLifecycle


def generic_safe_relative(value: str) -> PurePosixPath:
    path = PurePosixPath(value.replace("\\", "/"))
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("change path must be a safe repository-relative path")
    lowered = {part.casefold() for part in path.parts}
    if lowered & BLOCKED_PARTS or any(part.startswith(".env") for part in lowered):
        raise ValueError(f"change path is blocked by policy: {value}")
    return path


class ProjectFileChange(BaseModel):
    path: str
    base_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    content: str = Field(max_length=MAX_CHANGE_BYTES)
    reason: str = Field(min_length=3, max_length=500)

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return generic_safe_relative(value).as_posix()

    @model_validator(mode="after")
    def block_host_runtime_access(self) -> "ProjectFileChange":
        if DANGEROUS_RUNTIME.search(self.content):
            raise ValueError("change content requests a prohibited host-runtime capability")
        return self


class ProjectCodeChangeSet(BaseModel):
    schema_version: str = "onebrief-project-code-change-set-v1"
    summary: str = Field(min_length=3, max_length=1000)
    changes: list[ProjectFileChange] = Field(min_length=1, max_length=MAX_CHANGE_FILES)

    @model_validator(mode="after")
    def validate_set(self) -> "ProjectCodeChangeSet":
        paths = [item.path.casefold() for item in self.changes]
        if len(paths) != len(set(paths)):
            raise ValueError("a change set cannot edit the same path twice")
        if sum(len(item.content.encode("utf-8")) for item in self.changes) > MAX_CHANGE_BYTES:
            raise ValueError("change set exceeds the total text-size limit")
        return self


class ApprovedProjectDevelopmentToolPack:
    """Runs only an approved generated profile against an isolated local clone."""

    def __init__(self, project_id: str, registry_root: Path | None = None, runner: CommandRunner | None = None):
        self.lifecycle = ProjectToolPackLifecycle(project_id, registry_root)
        self.project_id = project_id
        self.runner = runner or _default_runner
        self.root = Path(self.lifecycle._record().manifest.project_root).resolve()

    def _git(self, *args: str, cwd: Path | None = None, timeout: int = 120) -> str:
        completed = subprocess.run(
            ["git", *args], cwd=cwd or self.root, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout, shell=False, check=False,
        )
        if completed.returncode:
            raise RuntimeError((completed.stderr or completed.stdout).strip()[:2000])
        return completed.stdout

    def _profile(self):
        state = self.lifecycle.state()
        if state.status != "approved" or not state.execution_ready or state.generated is None:
            detail = "; ".join(state.execution_blockers) or "ToolPack is not approved"
            raise PermissionError(detail)
        return state.generated

    def _validate_root(self) -> tuple[object, str]:
        profile = self._profile()
        if self.root != Path(profile.project_root).resolve():
            raise PermissionError("approved project root changed")
        ignored = {"ONEBRIEF_PROJECT.json"}
        dirty = [line for line in self._git("status", "--porcelain").splitlines() if line[3:].replace("\\", "/") not in ignored]
        if dirty:
            raise RuntimeError("development requires a clean source repository")
        head = self._git("rev-parse", "HEAD").strip()
        if head != profile.repository_head_sha:
            raise RuntimeError("repository HEAD changed after ToolPack approval")
        return profile, head

    def approved_edit_path(self, value: str) -> str | None:
        try:
            pure = generic_safe_relative(value)
            profile = self._profile()
        except (ValueError, PermissionError):
            return None
        normalized = pure.as_posix()
        if pure.suffix.casefold() not in {item.casefold() for item in profile.allowed_suffixes}:
            return None
        if not any(normalized.startswith(prefix) for prefix in profile.allowed_write_prefixes):
            return None
        return normalized

    def _blob(self, head: str, relative: str) -> bytes:
        completed = subprocess.run(
            ["git", "show", f"{head}:{relative}"], cwd=self.root,
            capture_output=True, timeout=120, shell=False, check=False,
        )
        if completed.returncode:
            raise RuntimeError(completed.stderr.decode("utf-8", errors="replace")[:1000])
        return completed.stdout

    def inspect(
        self, output_dir: Path, focus_text: str = ""
    ) -> tuple[RepositoryInspection, list[InternalSource]]:
        profile, head = self._validate_root()
        allowed_suffixes = {item.casefold() for item in profile.allowed_suffixes}
        read_prefixes = tuple(profile.allowed_read_prefixes)
        write_prefixes = tuple(profile.allowed_write_prefixes)
        tracked = [item for item in self._git("ls-files", "-z").split("\0") if item]

        focus_terms = {
            item for item in re.findall(r"[a-z0-9][a-z0-9_-]{2,}", focus_text.casefold())
            if item not in {
                "the", "and", "for", "with", "from", "this", "that", "project",
                "existing", "safely", "improve", "complete", "result", "unity",
            }
        }
        if any(marker in focus_text.casefold() for marker in ("???", "??", "??", "localization", "language")):
            focus_terms.update({"localization", "language", "locale", "i18n", "string", "translation"})
        intrinsic = ("localization", "language", "locale", "i18n", "string", "translation")
        localization_focus = any(term in focus_text.casefold() for term in intrinsic)
        code_suffixes = {".cs", ".js", ".jsx", ".mjs", ".py", ".ts", ".tsx"}

        def rank(relative: str) -> tuple[int, int, int, int, int, str]:
            lowered = relative.casefold()
            hits = sum(term in lowered for term in focus_terms)
            editable = any(relative.startswith(prefix) for prefix in write_prefixes)
            intrinsic_hits = sum(term in lowered for term in intrinsic) if localization_focus else 0
            suffix = PurePosixPath(relative).suffix.casefold()
            filename_length = len(PurePosixPath(relative).name)
            return (-intrinsic_hits, 0 if suffix in code_suffixes else 1, filename_length, -hits, 0 if editable else 1, lowered)

        tracked.sort(key=rank)
        records: list[RepositoryContextFile] = []
        sources: list[InternalSource] = []
        total = 0
        for relative in tracked:
            pure = generic_safe_relative(relative)
            normalized = pure.as_posix()
            if not any(normalized.startswith(prefix) for prefix in read_prefixes):
                continue
            if pure.suffix.casefold() not in allowed_suffixes:
                continue
            source_path = (self.root / Path(*pure.parts)).resolve()
            if (
                not source_path.is_relative_to(self.root)
                or source_path.is_symlink()
                or not source_path.is_file()
            ):
                continue
            size = source_path.stat().st_size
            if size > MAX_CONTEXT_FILE_BYTES or total + size > MAX_CONTEXT_BYTES:
                continue
            data = self._blob(head, normalized)
            destination = output_dir / "repository_context" / Path(*pure.parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
            digest = hashlib.sha256(data).hexdigest()
            records.append(RepositoryContextFile(path=normalized, size_bytes=len(data), sha256=digest))
            sources.append(InternalSource(
                name=f"project-source/{normalized}", priority=SourcePriority.MANDATORY,
                requirement_keys=["project_development"],
                summary="Committed source from the approved imported-project ToolPack.",
                content=data.decode("utf-8", errors="replace"), media_type="text/plain",
                size_bytes=len(data), sha256=digest,
            ))
            total += len(data)
            if len(records) >= 48 or total >= int(MAX_CONTEXT_BYTES * 0.95):
                break
        inspection = RepositoryInspection(
            repository_name=self.root.name, head_sha=head, context_files=records,
            safety_boundary=[
                "exact-hash approved project and clean Git HEAD",
                "bounded committed text context prioritized by the approved goal",
                "all edits occur in a disposable local clone",
                "only generated fixed validation adapters may execute",
                "no install, deploy, push, credentials, accounts, or arbitrary commands",
            ],
        )
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "repository_inspection.json").write_text(inspection.model_dump_json(indent=2) + "\n", encoding="utf-8")
        return inspection, sources

    def _commands(self, profile, clone: Path) -> list[tuple[str, list[str], int]]:
        commands: list[tuple[str, list[str], int]] = []
        npm = "npm.cmd" if os.name == "nt" else "npm"
        for adapter in profile.adapters:
            if not adapter.enabled or adapter.adapter_id == AdapterId.REPOSITORY_SNAPSHOT:
                continue
            if adapter.adapter_id == AdapterId.NODE_SCRIPT:
                commands.append((f"node_{adapter.parameter}", [npm, "run", str(adapter.parameter)], 300))
            elif adapter.adapter_id == AdapterId.PYTHON_TESTS:
                commands.append(("python_tests", [sys.executable, "-m", "pytest"], 300))
            elif adapter.adapter_id in {AdapterId.UNITY_COMPILE, AdapterId.UNITY_EDITMODE_TESTS}:
                editor = self.lifecycle._unity_editor(self.root)
                if editor is None or str(editor) != adapter.evidence:
                    raise PermissionError("the approved Unity Editor binding is unavailable or changed")
                log = clone / ("onebrief-editmode.log" if adapter.adapter_id == AdapterId.UNITY_EDITMODE_TESTS else "onebrief-compile.log")
                argv = [str(editor), "-batchmode", "-quit", "-projectPath", str(clone), "-logFile", str(log)]
                if adapter.adapter_id == AdapterId.UNITY_EDITMODE_TESTS:
                    argv += ["-runTests", "-testPlatform", "EditMode", "-testResults", str(clone / "onebrief-test-results.xml")]
                commands.append((adapter.adapter_id.value, argv, 900))
        return commands


    def bind_change_set_to_inspection(
        self,
        change_set: ProjectCodeChangeSet,
        inspection: RepositoryInspection,
    ) -> ProjectCodeChangeSet:
        """Bind model-proposed edits to hashes from the trusted inspection."""
        _profile, head = self._validate_root()
        if inspection.head_sha != head:
            raise RuntimeError("repository inspection HEAD is stale")
        inspected = {item.path: item.sha256 for item in inspection.context_files}
        rebound: list[ProjectFileChange] = []
        for change in change_set.changes:
            try:
                self._blob(head, change.path)
                exists = True
            except RuntimeError:
                exists = False
            if exists and change.path not in inspected:
                raise PermissionError(
                    f"existing file was not included in approved model context: {change.path}"
                )
            rebound.append(change.model_copy(update={
                "base_sha256": inspected[change.path] if exists else None,
            }))
        return change_set.model_copy(update={"changes": rebound})
    def apply_and_verify(self, change_set: ProjectCodeChangeSet, output_dir: Path) -> DevelopmentRun:
        profile, head = self._validate_root()
        for change in change_set.changes:
            if self.approved_edit_path(change.path) is None:
                raise PermissionError(f"path is outside the approved project source area: {change.path}")
        output_dir = output_dir.resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="onebrief_project_dev_") as temporary:
            clone = Path(temporary) / "repository"
            self._git("clone", "--local", "--no-hardlinks", str(self.root), str(clone), cwd=Path(temporary))
            new_paths: list[str] = []
            for change in change_set.changes:
                pure = generic_safe_relative(change.path)
                target = (clone / Path(*pure.parts)).resolve()
                if not target.is_relative_to(clone) or target.is_symlink():
                    raise PermissionError(f"unsafe development path: {change.path}")
                if target.exists():
                    committed_sha = hashlib.sha256(self._blob(head, change.path)).hexdigest()
                    if change.base_sha256 != committed_sha:
                        raise RuntimeError(f"stale or missing base hash: {change.path}")
                elif change.base_sha256 is not None:
                    raise RuntimeError(f"new file cannot declare a base hash: {change.path}")
                else:
                    new_paths.append(change.path)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(change.content, encoding="utf-8", newline="\n")
            if new_paths:
                self._git("add", "-N", "--", *new_paths, cwd=clone)
            approved_paths = [item.path for item in change_set.changes]
            try:
                self._git("diff", "--check", "--", *approved_paths, cwd=clone)
            except RuntimeError as exc:
                raise RuntimeError(f"development patch hygiene failed: {exc}") from exc
            results = [self.runner(command_id, argv, clone, timeout) for command_id, argv, timeout in self._commands(profile, clone)]
            patch = self._git("diff", "--binary", "--no-ext-diff", "--", *approved_paths, cwd=clone)
            if not patch.strip():
                raise ValueError("development change set produced no repository diff")
            patch_path = output_dir / "changes.patch"
            patch_path.write_text(patch, encoding="utf-8", newline="\n")
            for change in change_set.changes:
                source = clone / Path(*PurePosixPath(change.path).parts)
                destination = output_dir / "changed_files" / Path(*PurePosixPath(change.path).parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
            run = DevelopmentRun(
                status="verified", repository_name=self.root.name, base_head_sha=head,
                summary=change_set.summary, changed_paths=[item.path for item in change_set.changes],
                commands=results, patch_path=patch_path.relative_to(output_dir.parent).as_posix(),
                safety_boundary=[
                    "original repository remained read-only",
                    "exact approved HEAD and per-file base hashes were enforced",
                    "edits were limited to approved prefixes and text suffixes",
                    "only fixed generated validation adapters executed in the clone",
                    "no install, deploy, push, credentials, accounts, or arbitrary commands",
                ],
            )
            (output_dir / "development_run.json").write_text(run.model_dump_json(indent=2) + "\n", encoding="utf-8")
            (output_dir / "change_set.json").write_text(change_set.model_dump_json(indent=2) + "\n", encoding="utf-8")
            return run
