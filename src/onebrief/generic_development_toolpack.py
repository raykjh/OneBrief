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
    DevelopmentCommandResult,
    DevelopmentRun,
    RepositoryContextFile,
    RepositoryInspection,
    _default_runner,
)
from onebrief.unity_runtime_evidence import (
    requested_ui_surfaces,
    requested_ui_transition,
    validate_and_copy_unity_visual_evidence,
)
from onebrief.unity_layout_diagnostics import (
    install_unity_layout_diagnostic_source,
    package_unity_layout_diagnostics,
)
from onebrief.web_runtime_evidence import (
    observe_web_application,
    validate_preserved_language_states,
)
from onebrief.schemas import InternalSource, SourcePriority
from onebrief.toolpack_lifecycle import AdapterId, ProjectToolPackLifecycle


# Provider-authored edits remain capped by MAX_CHANGE_BYTES (64 KiB).  Trusted
# promotion may apply one small exact edit to an already-approved mature source
# file that is larger than that.  Keep that materialized candidate bounded, but
# do not force the model to rewrite or split the existing file just to satisfy a
# transport-oriented response cap.
MAX_BOUND_PROJECT_CHANGE_BYTES = 256_000
MAX_REPAIR_ANCHOR_CHARS = 1_000


def _bounded_repair_anchor(value: object, *, keep: str) -> str | None:
    """Keep a deterministic boundary when a provider copies an entire range.

    The shortened selector is still untrusted: promotion must find it uniquely
    in the approved candidate before any isolated clone can be changed.
    """

    if value is None:
        return None
    text = str(value)
    if len(text) <= MAX_REPAIR_ANCHOR_CHARS:
        return text
    if keep == "start":
        return text[:MAX_REPAIR_ANCHOR_CHARS]
    return text[-MAX_REPAIR_ANCHOR_CHARS:]


def _csharp_code_only(value: str) -> str:
    """Remove C# comments and string literals before structural identifier checks."""
    token = re.compile(
        r'(?:\$@|@\$|@)"(?:""|[^"])*"|(?:\$)?"(?:\\.|[^"\\])*"|//[^\r\n]*|/\*.*?\*/',
        re.DOTALL,
    )
    return token.sub(" ", value)


def _declared_unity_viewports(source: str) -> list[tuple[int, int]]:
    """Extract literal or paired-array viewport declarations from a PlayMode test."""

    measured = [
        (int(width), int(height))
        for width, height in re.findall(
            r"viewport_width[^0-9]{0,32}(\d{2,5})[^\n]{0,120}?"
            r"viewport_height[^0-9]{0,32}(\d{2,5})",
            source,
            re.IGNORECASE,
        )
    ]
    structural = _csharp_code_only(source)
    measured.extend(
        (int(width), int(height))
        for width, height in re.findall(
            r"\b[A-Za-z_][A-Za-z0-9_]*(?:capture|screenshot)[A-Za-z0-9_]*\s*\(\s*"
            r'"[^"\r\n]*\.png"\s*,\s*(\d{2,5})\s*,\s*(\d{2,5})\s*\)',
            source,
            re.IGNORECASE,
        )
    )
    if "screen.setresolution" not in structural.casefold():
        return measured
    measured.extend(
        (int(width), int(height))
        for width, height in re.findall(
            r"screen\.setresolution\s*\(\s*(\d{2,5})\s*,\s*(\d{2,5})\s*,",
            structural,
            re.IGNORECASE,
        )
    )
    arrays: dict[str, list[int]] = {}
    for name, values in re.findall(
        r"\bint\s*\[\s*\]\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*\{([^}]*)\}",
        structural,
        re.IGNORECASE | re.DOTALL,
    ):
        numbers = [int(item) for item in re.findall(r"\b\d{2,5}\b", values)]
        if numbers:
            arrays[name] = numbers
    width_arrays = [
        (name, values) for name, values in arrays.items()
        if "width" in name.casefold() and re.search(rf"\b{re.escape(name)}\s*\[", structural)
    ]
    height_arrays = [
        (name, values) for name, values in arrays.items()
        if "height" in name.casefold() and re.search(rf"\b{re.escape(name)}\s*\[", structural)
    ]
    for _width_name, widths in width_arrays:
        for _height_name, heights in height_arrays:
            if len(widths) == len(heights):
                measured.extend(zip(widths, heights, strict=True))
    return measured


def _uses_screen_sized_render_target(structural_source: str) -> bool:
    """Detect batchmode capture targets derived from Screen.width/height."""

    if re.search(
        r"new\s+(?:unityengine\.)?rendertexture\s*\(\s*"
        r"(?:unityengine\.)?screen\.width\s*,\s*(?:unityengine\.)?"
        r"screen\.height\s*,",
        structural_source,
    ):
        return True
    width_vars = re.findall(
        r"\b(?:int|var)\s+([a-z_][a-z0-9_]*)\s*=\s*"
        r"(?:unityengine\.)?screen\.width\b",
        structural_source,
    )
    height_vars = re.findall(
        r"\b(?:int|var)\s+([a-z_][a-z0-9_]*)\s*=\s*"
        r"(?:unityengine\.)?screen\.height\b",
        structural_source,
    )
    return any(
        re.search(
            rf"new\s+(?:unityengine\.)?rendertexture\s*\(\s*"
            rf"{re.escape(width)}\s*,\s*{re.escape(height)}\s*,",
            structural_source,
        )
        for width in width_vars
        for height in height_vars
    )


def _unity_literal_scenarios(source: str) -> list[tuple[str, str, int, int]]:
    """Extract literal observed-state/interaction claims and source spans.

    The model may interpolate numeric measurements, but state and interaction
    labels must remain literal so they can be bound to the code that precedes
    each evidence row before the expensive PlayMode run begins.
    """

    results: list[tuple[str, str, int, int]] = []
    previous_end = 0
    for match in re.finditer(
        r"(?:scenarios|evidenceRows|evidence_rows)\s*\.\s*Add\s*\((.*?)\)\s*;",
        source,
        re.IGNORECASE | re.DOTALL,
    ):
        body = match.group(1)

        def field(name: str) -> str | None:
            found = re.search(
                rf'\\?"{name}\\?"\s*:\s*\\?"([^"\\]+)',
                body,
                re.IGNORECASE,
            )
            return found.group(1) if found else None

        state = field("observed_state")
        interaction = field("interaction")
        if state and interaction:
            results.append((state, interaction, previous_end, match.start()))
        previous_end = match.end()
    return results


def _contains_ordered_literals(observed: list[str], requested: list[str]) -> bool:
    if not requested:
        return True
    aliases = {
        "login": ("login", "sign in", "로그인"),
        "lobby": ("lobby", "main menu", "로비"),
        "settings": ("settings", "setting", "options", "설정"),
    }
    cursor = 0
    for value in observed:
        lowered = value.casefold()
        surface = next(
            (
                name for name, markers in aliases.items()
                if any(marker in lowered for marker in markers)
            ),
            None,
        )
        if surface == requested[cursor]:
            cursor += 1
            if cursor == len(requested):
                return True
    return False


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
    content: str = Field(max_length=MAX_BOUND_PROJECT_CHANGE_BYTES)
    reason: str = Field(min_length=3, max_length=500)

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return generic_safe_relative(value).as_posix()

    @field_validator("base_sha256", mode="before")
    @classmethod
    def discard_untrusted_invalid_base_hash(cls, value: object) -> object:
        if value is None:
            return None
        candidate = str(value).strip().casefold()
        if re.fullmatch(r"[a-f0-9]{64}", candidate):
            return candidate
        # Model-provided hashes are provenance hints only. The trusted
        # inspection binds the real Git blob hash before any edit can run.
        return None

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
        if sum(len(item.content.encode("utf-8")) for item in self.changes) > MAX_BOUND_PROJECT_CHANGE_BYTES:
            raise ValueError("change set exceeds the total text-size limit")
        return self


class ProposedProjectFileChange(BaseModel):
    """Untrusted provider proposal; authority and runtime checks happen on promotion."""

    path: str
    base_sha256: str | None = None
    content: str | None = Field(default=None, max_length=MAX_CHANGE_BYTES)
    # Exact edits remain bounded by the same aggregate 60 KiB change-set limit.
    # Mature single-file applications can legitimately contain a component
    # larger than 20k, so a smaller per-field cap would make safe exact edits
    # impossible even though the promoted result still satisfies the total cap.
    search: str | None = Field(default=None, min_length=1, max_length=MAX_CHANGE_BYTES)
    replace: str | None = Field(default=None, max_length=MAX_CHANGE_BYTES)
    anchor_id: str | None = Field(default=None, pattern=r"^A[0-9a-f]{12}$")
    start_anchor: str | None = Field(default=None, min_length=1, max_length=4000)
    end_anchor: str | None = Field(default=None, min_length=1, max_length=4000)
    reason: str = Field(min_length=3, max_length=500)

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return generic_safe_relative(value).as_posix()

    @model_validator(mode="after")
    def validate_edit_mode(self) -> "ProposedProjectFileChange":
        full_file = self.content is not None
        catalog_edit = (
            self.content is None
            and self.anchor_id is not None
            and self.search is None
            and self.replace is not None
            and self.start_anchor is None
            and self.end_anchor is None
        )
        exact_edit = (
            self.anchor_id is None
            and self.search is not None
            and self.replace is not None
        )
        anchored_edit = (
            self.anchor_id is None
            and self.start_anchor is not None
            and self.end_anchor is not None
            and self.replace is not None
            and self.search is None
        )
        if sum((full_file, catalog_edit, exact_edit, anchored_edit)) != 1:
            raise ValueError(
                "provide complete content, catalog anchor, exact search/replace, or anchored range replacement"
            )
        return self


class ProposedProjectCodeChangeSet(BaseModel):
    schema_version: str = "onebrief-project-code-change-set-v1"
    summary: str = Field(min_length=3, max_length=1000)
    changes: list[ProposedProjectFileChange] = Field(min_length=1, max_length=MAX_CHANGE_FILES)


class CompactProposedProjectFileChange(BaseModel):
    """Bounded repair delta, including a small new file when evidence requires one."""

    path: str
    base_sha256: str | None = None
    # The provider sees every optional field in the JSON schema and may emit
    # both ``content`` and ``replace`` before our narrower-mode validator can
    # discard one. Keep the schema itself below a single model response cap so
    # a repair cannot be truncated before validation.
    content: str | None = Field(default=None, max_length=8000)
    search: str | None = Field(default=None, min_length=1, max_length=8000)
    replace: str | None = Field(default=None, max_length=8000)
    anchor_id: str | None = Field(default=None, pattern=r"^A[0-9a-f]{12}$")
    start_anchor: str | None = Field(default=None, min_length=1, max_length=1000)
    end_anchor: str | None = Field(default=None, min_length=1, max_length=1000)
    reason: str = Field(min_length=3, max_length=500)

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return generic_safe_relative(value).as_posix()

    @field_validator("reason", mode="before")
    @classmethod
    def bound_explanatory_reason(cls, value: object) -> str:
        # ``reason`` is audit metadata, not executable content or authority.
        # Preserve the useful prefix instead of discarding an otherwise valid
        # bounded edit because a provider over-explained it.
        return str(value)[:500]

    @field_validator("start_anchor", mode="before")
    @classmethod
    def bound_start_anchor(cls, value: object) -> str | None:
        return _bounded_repair_anchor(value, keep="start")

    @field_validator("end_anchor", mode="before")
    @classmethod
    def bound_end_anchor(cls, value: object) -> str | None:
        return _bounded_repair_anchor(value, keep="end")

    @model_validator(mode="after")
    def validate_edit_mode(self) -> "CompactProposedProjectFileChange":
        # Gemini occasionally includes a redundant ``content`` field while also
        # returning the requested bounded edit. Prefer the narrower edit mode
        # deterministically; trusted promotion still verifies the catalog/search
        # anchor against the approved source before any clone is changed.
        if self.replace is not None:
            if self.anchor_id is not None:
                self.content = None
                self.search = None
                self.start_anchor = None
                self.end_anchor = None
            elif self.search is not None:
                self.content = None
                self.start_anchor = None
                self.end_anchor = None
            elif self.start_anchor is not None and self.end_anchor is not None:
                self.content = None
        full_file = (
            self.content is not None
            and self.search is None
            and self.replace is None
            and self.anchor_id is None
            and self.start_anchor is None
            and self.end_anchor is None
        )
        catalog_edit = (
            self.content is None
            and self.anchor_id is not None
            and self.search is None
            and self.start_anchor is None
            and self.end_anchor is None
            and self.replace is not None
        )
        exact_edit = (
            self.content is None
            and self.anchor_id is None
            and self.search is not None
            and self.start_anchor is None
            and self.end_anchor is None
            and self.replace is not None
        )
        anchored_edit = (
            self.content is None
            and self.anchor_id is None
            and self.search is None
            and self.start_anchor is not None
            and self.end_anchor is not None
            and self.replace is not None
        )
        if sum((full_file, catalog_edit, exact_edit, anchored_edit)) != 1:
            raise ValueError(
                "provide one bounded new file, catalog anchor, exact edit, or anchored edit"
            )
        if full_file and self.base_sha256 is not None:
            raise ValueError("a compact full-file repair must be a new file with a null base hash")
        return self


class CompactProposedProjectCodeChangeSet(BaseModel):
    schema_version: str = "onebrief-project-code-change-set-v1"
    summary: str = Field(min_length=3, max_length=1000)
    # A finite repair normally touches one file, but some executable evidence
    # topologies are an indivisible pair (for example a Unity PlayMode test and
    # its test-only asmdef). Keep the repair compact while allowing that
    # smallest coherent pair to prevent an impossible one-file repair loop.
    changes: list[CompactProposedProjectFileChange] = Field(min_length=1, max_length=2)


class ExactRepairProjectFileChange(BaseModel):
    """One existing-candidate edit; full-file output is structurally impossible."""

    path: str
    base_sha256: str | None = None
    content: str | None = Field(default=None, max_length=8000)
    search: str | None = Field(default=None, min_length=1, max_length=8000)
    replace: str | None = Field(default=None, max_length=8000)
    anchor_id: str | None = Field(default=None, pattern=r"^A[0-9a-f]{12}$")
    start_anchor: str | None = Field(default=None, min_length=1, max_length=1000)
    end_anchor: str | None = Field(default=None, min_length=1, max_length=1000)
    reason: str = Field(min_length=3, max_length=500)

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return generic_safe_relative(value).as_posix()

    @field_validator("reason", mode="before")
    @classmethod
    def bound_explanatory_reason(cls, value: object) -> str:
        return str(value)[:500]

    @field_validator("start_anchor", mode="before")
    @classmethod
    def bound_start_anchor(cls, value: object) -> str | None:
        return _bounded_repair_anchor(value, keep="start")

    @field_validator("end_anchor", mode="before")
    @classmethod
    def bound_end_anchor(cls, value: object) -> str | None:
        return _bounded_repair_anchor(value, keep="end")

    @model_validator(mode="after")
    def validate_edit_mode(self) -> "ExactRepairProjectFileChange":
        # Prefer the most deterministic selector if a provider redundantly
        # fills several optional selector fields in structured output.
        if self.anchor_id is not None:
            self.content = None
            self.search = None
            self.start_anchor = None
            self.end_anchor = None
        elif self.search is not None:
            self.start_anchor = None
            self.end_anchor = None
        full = (
            self.content is not None
            and self.search is None
            and self.replace is None
            and self.anchor_id is None
        )
        catalog = (
            self.content is None
            and self.anchor_id is not None
            and self.search is None
            and self.replace is not None
        )
        exact = (
            self.content is None
            and self.anchor_id is None
            and self.search is not None
            and self.replace is not None
        )
        anchored = (
            self.content is None
            and self.anchor_id is None
            and self.search is None
            and self.start_anchor is not None
            and self.end_anchor is not None
            and self.replace is not None
        )
        if sum((full, catalog, exact, anchored)) != 1:
            raise ValueError(
                "provide exactly one bounded candidate-file, catalog, exact, or anchored repair edit"
            )
        if full and self.base_sha256 is not None:
            raise ValueError("a full candidate-file repair must retain a null base hash")
        return self


class ExactRepairProjectCodeChangeSet(BaseModel):
    schema_version: str = "onebrief-project-code-change-set-v1"
    summary: str = Field(min_length=3, max_length=500)
    changes: list[ExactRepairProjectFileChange] = Field(min_length=1, max_length=1)


class AnchoredRangeRepairProjectFileChange(BaseModel):
    """One unambiguous range replacement for a larger coherent repair slice."""

    path: str
    base_sha256: str | None = None
    start_anchor: str = Field(min_length=1, max_length=1000)
    end_anchor: str = Field(min_length=1, max_length=1000)
    replace: str = Field(max_length=12_000)
    reason: str = Field(min_length=3, max_length=500)

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return generic_safe_relative(value).as_posix()

    @field_validator("reason", mode="before")
    @classmethod
    def bound_explanatory_reason(cls, value: object) -> str:
        return str(value)[:500]

    @field_validator("start_anchor", mode="before")
    @classmethod
    def bound_start_anchor(cls, value: object) -> str | None:
        return _bounded_repair_anchor(value, keep="start")

    @field_validator("end_anchor", mode="before")
    @classmethod
    def bound_end_anchor(cls, value: object) -> str | None:
        return _bounded_repair_anchor(value, keep="end")


class AnchoredRangeRepairProjectCodeChangeSet(BaseModel):
    schema_version: str = "onebrief-project-code-change-set-v1"
    summary: str = Field(min_length=3, max_length=500)
    changes: list[AnchoredRangeRepairProjectFileChange] = Field(min_length=1, max_length=1)


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
            # Git may emit harmless platform warnings (for example LF -> CRLF)
            # on stderr while placing the actionable diff failure on stdout.
            detail = "\n".join(
                part.strip() for part in (completed.stdout, completed.stderr) if part.strip()
            )
            raise RuntimeError(detail[:4000])
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
        dirty = [
            line for line in self._git("status", "--porcelain").splitlines()
            if (
                (normalized := line[3:].replace("\\", "/")) != "ONEBRIEF_PROJECT.json"
                and not normalized.startswith(".onebrief/")
            )
        ]
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

    @staticmethod
    def _normalize_safe_generated_text(path: str, content: str) -> str:
        """Repair deterministic generated-source contracts without widening access."""
        pure = PurePosixPath(path)
        suffix = pure.suffix.casefold()
        lowered_parts = {part.casefold() for part in pure.parts}
        if (
            suffix == ".asmdef"
            and "tests" in lowered_parts
            and "playmode" in lowered_parts
        ):
            try:
                payload = json.loads(content)
            except (TypeError, json.JSONDecodeError):
                return content
            if isinstance(payload, dict):
                optional = payload.get("optionalUnityReferences")
                if not isinstance(optional, list):
                    optional = []
                if "TestAssemblies" not in optional:
                    optional.append("TestAssemblies")
                payload["optionalUnityReferences"] = optional
                # Unity's TestAssemblies marker already supplies both runner
                # assemblies. Keeping explicit runner references as well makes
                # Unity reject the asmdef as having duplicate references.
                references = payload.get("references")
                if isinstance(references, list):
                    duplicate_test_runners = {
                        "UnityEngine.TestRunner",
                        "UnityEditor.TestRunner",
                    }
                    payload["references"] = [
                        item for item in references
                        if item not in duplicate_test_runners
                    ]
                return json.dumps(payload, ensure_ascii=False, indent=4) + "\n"
            return content
        if suffix != ".cs":
            normalized_text: list[str] = []
            for line in content.splitlines(keepends=True):
                body = line.rstrip("\r\n")
                newline = line[len(body):]
                normalized_text.append(body.rstrip(" \t") + newline)
            return "".join(normalized_text).replace("\r\n", "\n").replace("\r", "\n")
        normalized: list[str] = []
        generated_test_source = "tests" in lowered_parts and "playmode" in lowered_parts
        for line in content.splitlines(keepends=True):
            body = line.rstrip("\r\n")
            newline = line[len(body):]
            if generated_test_source:
                # Models occasionally emit JavaScript-style `${"..."}` while
                # constructing a C# interpolated JSON string.  In a generated,
                # isolated PlayMode test this is an unambiguous one-character
                # syntax repair; production sources remain untouched.
                body = body.replace('${"', '$"')
            # Trailing horizontal whitespace never changes C# semantics and
            # causes deterministic patch-hygiene rejection. Normalize it for
            # every generated candidate, not only files already under Tests.
            body = body.rstrip(" \t")
            if re.match(r"^\s*using\s+(?:static\s+)?[A-Za-z_][A-Za-z0-9_.]*(?:\s*=\s*[A-Za-z_][A-Za-z0-9_.]*)?;[ \t]+$", body):
                body = body.rstrip(" \t")
            normalized.append(body + newline)
        result = "".join(normalized).replace("\r\n", "\n").replace("\r", "\n")
        if (
            generated_test_source
            and re.search(r"\[(?:UnityTest|Test)\]", result)
            and not re.search(r"\bnamespace\s+OneBrief\.Visual(?:\b|\.)", result)
            and not re.search(r"\bnamespace\s+[A-Za-z_]", result)
        ):
            # The namespace is part of OneBrief's fixed discovery contract, not
            # product behavior. Normalize a newly generated, namespace-free
            # PlayMode test deterministically so the repair loop can address the
            # substantive runtime evidence instead of repeating this wrapper fix.
            using_end = 0
            for match in re.finditer(r"(?m)^using\s+[^;]+;\s*$", result):
                using_end = match.end()
            prefix = result[:using_end].rstrip()
            body = result[using_end:].strip()
            if prefix and body:
                result = f"{prefix}\n\nnamespace OneBrief.Visual\n{{\n{body}\n}}\n"
        return result

    def _blob(self, head: str, relative: str) -> bytes:
        completed = subprocess.run(
            ["git", "show", f"{head}:{relative}"], cwd=self.root,
            capture_output=True, timeout=120, shell=False, check=False,
        )
        if completed.returncode:
            raise RuntimeError(completed.stderr.decode("utf-8", errors="replace")[:1000])
        return completed.stdout

    def _unity_scene_catalog(
        self,
        head: str,
        tracked: list[str],
        read_prefixes: tuple[str, ...],
        focus_text: str,
    ) -> bytes | None:
        """Return a bounded, read-only map of real Unity scenes and object names.

        Unity scene YAML is commonly hundreds of kilobytes or several
        megabytes and is intentionally outside the editable text context. The
        maker still needs exact scene names and real object anchors to avoid
        inventing a synthetic canvas. This catalog exposes only committed
        names and never grants scene write authority.
        """
        scene_paths = [
            item for item in tracked
            if item.casefold().endswith(".unity")
            and any(item.startswith(prefix) for prefix in read_prefixes)
        ]
        if not scene_paths:
            return None
        focus = focus_text.casefold()
        surface_terms = {
            term for term in (
                "login", "lobby", "settings", "setting", "option", "audio",
                "sound", "language", "menu", "popup", "canvas", "panel",
            ) if term in focus
        }

        def scene_rank(path: str) -> tuple[int, str]:
            lowered = path.casefold()
            return (-sum(term in lowered for term in surface_terms), lowered)

        scenes: list[dict[str, object]] = []
        for relative in sorted(scene_paths, key=scene_rank)[:12]:
            text = self._blob(head, relative).decode("utf-8", errors="replace")
            names: list[str] = []
            seen: set[str] = set()
            for raw_name in re.findall(r"(?m)^\s*m_Name:\s*(.*?)\s*$", text):
                name = raw_name.strip().strip('"')
                if not name or name in seen or len(name) > 120:
                    continue
                seen.add(name)
                names.append(name)
            names.sort(key=lambda name: (
                -sum(term in name.casefold() for term in surface_terms),
                name.casefold(),
            ))
            scenes.append({
                "scene_path": relative,
                "scene_name": PurePosixPath(relative).stem,
                "object_names": names[:30],
            })
        payload = {
            "schema_version": "onebrief-unity-scene-catalog-v1",
            "source_revision": head,
            "authority": "read_only_committed_scene_metadata",
            "scenes": scenes,
        }
        return (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")

    def inspect(
        self, output_dir: Path, focus_text: str = ""
    ) -> tuple[RepositoryInspection, list[InternalSource]]:
        profile, head = self._validate_root()
        allowed_suffixes = {item.casefold() for item in profile.allowed_suffixes}
        read_prefixes = tuple(profile.allowed_read_prefixes)
        write_prefixes = tuple(profile.allowed_write_prefixes)
        tracked = [item for item in self._git("ls-files", "-z").split("\0") if item]
        unity_scene_catalog = self._unity_scene_catalog(
            head, tracked, read_prefixes, focus_text
        )

        focus_terms = {
            item for item in re.findall(r"[a-z0-9가-힣][a-z0-9가-힣_-]{1,}", focus_text.casefold())
            if item not in {
                "the", "and", "for", "with", "from", "this", "that", "project",
                "existing", "safely", "improve", "complete", "result", "unity",
            }
        }
        concept_terms = {
            ("localization", "language", "locale", "i18n", "translation", "언어", "다국어", "번역", "현지화"):
                {"localization", "language", "locale", "i18n", "string", "translation"},
            ("login", "signin", "auth", "account", "로그인", "인증", "계정"):
                {"login", "signin", "auth", "account", "start"},
            ("lobby", "home", "main menu", "로비", "메인 메뉴"):
                {"lobby", "home", "main", "menu"},
            ("settings", "setting", "option", "preference", "설정", "옵션"):
                {"settings", "setting", "option", "preference", "config"},
            ("audio", "sound", "volume", "music", "bgm", "sfx", "음향", "소리", "볼륨", "배경음", "효과음"):
                {"audio", "sound", "volume", "music", "bgm", "sfx"},
            ("navigation", "transition", "route", "flow", "scene", "화면 이동", "전환", "이동"):
                {"navigation", "transition", "router", "route", "flow", "scene"},
            ("ui", "ux", "screen", "visual", "layout", "responsive", "화면", "시각", "디자인", "반응형"):
                {"ui", "ux", "screen", "visual", "layout", "responsive", "canvas", "panel", "popup", "style"},
        }
        lowered_focus = focus_text.casefold()
        active_concepts: list[set[str]] = []
        for markers, related in concept_terms.items():
            if any(marker in lowered_focus for marker in markers):
                focus_terms.update(related)
                active_concepts.append(related)
        code_suffixes = {".cs", ".js", ".jsx", ".mjs", ".py", ".ts", ".tsx"}

        def path_terms(relative: str) -> set[str]:
            separated = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", relative)
            return {
                token.casefold()
                for token in re.findall(r"[A-Za-z0-9가-힣]+", separated)
                if len(token) > 1
            }

        vendor_markers = {
            "editor", "externaldependencymanager", "generatedlocalrepo",
            "googlemobileads", "googleplaygames", "packages", "plugins",
            "samples", "thirdparty", "vendor",
        }

        def rank(relative: str) -> tuple[int, int, int, int, int, int, str]:
            lowered = relative.casefold()
            tokens = path_terms(relative)
            hits = sum(term in tokens for term in focus_terms)
            concept_hits = sum(bool(tokens & terms) for terms in active_concepts)
            vendor = bool(tokens & vendor_markers)
            editable = any(relative.startswith(prefix) for prefix in write_prefixes)
            suffix = PurePosixPath(relative).suffix.casefold()
            filename_length = len(PurePosixPath(relative).name)
            # Goal fit must dominate generic filename length.  The previous
            # order let a past localization task monopolize a later UI task's
            # bounded context merely because those filenames were short.
            return (-concept_hits, -hits, 1 if vendor else 0, 0 if suffix in code_suffixes else 1, 0 if editable else 1, filename_length, lowered)

        tracked.sort(key=rank)
        # Preserve cross-surface coverage in a small context window. A broad
        # goal such as login + lobby + settings + audio previously spent the
        # entire 120 KiB allowance on several near-identical files from the
        # first matching directory. Greedily cover each active goal concept
        # before filling the remaining space by ordinary relevance.
        eligible: list[str] = []
        for relative in tracked:
            pure = PurePosixPath(relative)
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
                or source_path.stat().st_size > MAX_CONTEXT_FILE_BYTES
            ):
                continue
            eligible.append(relative)
        if active_concepts:
            # Reserve one compact source slot for every independently requested
            # surface before globally ranking the remainder. A broad Unity goal
            # must not spend the complete context allowance on visual or
            # localization files while omitting login, settings, audio, or
            # navigation entirely.
            diversified: list[str] = []
            remaining = list(eligible)
            compact_seed_limit = min(
                MAX_CONTEXT_FILE_BYTES,
                max(24_000, MAX_CONTEXT_BYTES // max(len(active_concepts), 4)),
            )
            primary_surface_roles = {"controller", "layout", "manager", "router"}
            secondary_surface_roles = {"service", "settings", "audio", "language"}
            for terms in active_concepts:
                candidates = [
                    relative for relative in remaining
                    if path_terms(relative) & terms
                ]
                if not candidates:
                    continue
                non_vendor = [
                    relative for relative in candidates
                    if not (path_terms(relative) & vendor_markers)
                ]
                candidates = non_vendor or candidates
                compact_candidates = [
                    relative for relative in candidates
                    if (self.root / Path(*PurePosixPath(relative).parts)).stat().st_size
                    <= compact_seed_limit
                ]
                # A huge monolithic controller can consume the entire context
                # before later requested surfaces receive even one source.
                # Prefer a compact adapter/binder for this surface and leave
                # the large file untouched; exact committed scene metadata and
                # additive production files remain available to the maker.
                if compact_candidates:
                    candidates = compact_candidates
                else:
                    continue
                if "localization" in terms:
                    preferred_surface_terms = {"language", "localization", "dropdown"}
                elif "login" in terms:
                    preferred_surface_terms = {"login", "controller"}
                elif "lobby" in terms:
                    preferred_surface_terms = {"lobby", "controller", "layout"}
                elif "settings" in terms:
                    preferred_surface_terms = {"settings", "controller"}
                elif "audio" in terms:
                    preferred_surface_terms = {"audio", "manager"}
                elif "scene" in terms:
                    preferred_surface_terms = {"scene", "router", "navigation"}
                else:
                    preferred_surface_terms = {"ui", "layout", "controller"}

                def domain_fit(relative: str) -> int:
                    tokens = path_terms(relative)
                    lowered = "/" + relative.casefold().strip("/") + "/"
                    if "localization" in terms:
                        return 0 if "/localization/" in lowered and tokens & {
                            "language", "localization", "dropdown", "settings",
                        } else 1
                    if "login" in terms:
                        if "login" in tokens and "scene" in tokens:
                            return 0
                        if "/login/" in lowered:
                            return 1
                        return 2 if "login" in tokens else 3
                    if "lobby" in terms:
                        if "lobby" in tokens and tokens & {"layout", "responsive"}:
                            return 0
                        if "lobby" in tokens and tokens & {"mobile", "menu", "home"}:
                            return 1
                        return 2
                    if "settings" in terms:
                        return 0 if "settings" in tokens else 1
                    if "audio" in terms:
                        return 0 if "audio" in tokens else 1
                    if "scene" in terms:
                        return 0 if tokens & {"scene", "router", "navigation"} else 1
                    return 0 if tokens & {"ui", "layout", "responsive", "screen"} else 1

                best = min(
                    candidates,
                    key=lambda relative: (
                        0 if PurePosixPath(relative).suffix.casefold() in code_suffixes else 1,
                        0 if any(relative.startswith(prefix) for prefix in write_prefixes) else 1,
                        domain_fit(relative),
                        0 if path_terms(relative) & preferred_surface_terms else 1,
                        (
                            0 if path_terms(relative) & primary_surface_roles else
                            1 if path_terms(relative) & secondary_surface_roles else 2
                        ),
                        -len(path_terms(relative) & terms),
                        0 if (self.root / Path(*PurePosixPath(relative).parts)).stat().st_size
                        <= compact_seed_limit else 1,
                        (self.root / Path(*PurePosixPath(relative).parts)).stat().st_size,
                        rank(relative),
                    ),
                )
                diversified.append(best)
                remaining.remove(best)
            remaining.sort(key=lambda relative: (
                1 if path_terms(relative) & vendor_markers else 0,
                rank(relative),
            ))
            tracked = diversified + remaining
        else:
            tracked = eligible
        records: list[RepositoryContextFile] = []
        sources: list[InternalSource] = []
        total = 0
        if unity_scene_catalog is not None:
            digest = hashlib.sha256(unity_scene_catalog).hexdigest()
            sources.append(InternalSource(
                name="project-source/unity-scene-catalog.json",
                priority=SourcePriority.MANDATORY,
                requirement_keys=["project_development"],
                summary=(
                    "Read-only committed Unity scene names and GameObject anchors. "
                    "Use these exact real scenes for runtime tests; this catalog is not editable authority."
                ),
                content=unity_scene_catalog.decode("utf-8"),
                media_type="application/json",
                size_bytes=len(unity_scene_catalog),
                sha256=digest,
            ))
            total += len(unity_scene_catalog)
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
                "only lockfile-bound dependency restore and generated fixed validation adapters may execute",
                "dependency lifecycle scripts, deploy, push, credentials, accounts, and arbitrary commands are blocked",
            ],
        )
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "repository_inspection.json").write_text(inspection.model_dump_json(indent=2) + "\n", encoding="utf-8")
        return inspection, sources

    def _commands(
        self, profile, clone: Path, goal_text: str
    ) -> list[tuple[str, list[str], int]]:
        commands: list[tuple[str, list[str], int]] = []
        npm = "npm.cmd" if os.name == "nt" else "npm"
        needs_visual_runtime = self._requires_unity_visual_runtime(goal_text)
        for adapter in profile.adapters:
            if not adapter.enabled or adapter.adapter_id == AdapterId.REPOSITORY_SNAPSHOT:
                continue
            if (
                adapter.adapter_id == AdapterId.UNITY_PLAYMODE_VISUAL_TESTS
                and not needs_visual_runtime
            ):
                continue
            if (
                adapter.adapter_id == AdapterId.UNITY_LAYOUT_DIAGNOSTICS
                and not needs_visual_runtime
            ):
                continue
            if adapter.adapter_id == AdapterId.NODE_SCRIPT:
                parameter = str(adapter.parameter)
                if "::" in parameter:
                    scope, script = parameter.split("::", 1)
                    argv = [npm, "--prefix", scope, "run", script]
                    command_id = f"node_{scope.replace('/', '_')}_{script}"
                else:
                    argv = [npm, "run", parameter]
                    command_id = f"node_{parameter}"
                commands.append((command_id, argv, 300))
            elif adapter.adapter_id == AdapterId.PYTHON_TESTS:
                commands.append(("python_tests", [sys.executable, "-m", "pytest"], 300))
            elif adapter.adapter_id in {
                AdapterId.UNITY_COMPILE,
                AdapterId.UNITY_EDITMODE_TESTS,
                AdapterId.UNITY_PLAYMODE_VISUAL_TESTS,
                AdapterId.UNITY_LAYOUT_DIAGNOSTICS,
            }:
                editor = self.lifecycle._unity_editor(self.root)
                if editor is None or str(editor) != adapter.evidence:
                    raise PermissionError("the approved Unity Editor binding is unavailable or changed")
                log_names = {
                    AdapterId.UNITY_COMPILE: "onebrief-compile.log",
                    AdapterId.UNITY_EDITMODE_TESTS: "onebrief-editmode.log",
                    AdapterId.UNITY_PLAYMODE_VISUAL_TESTS: "onebrief-playmode-visual.log",
                    AdapterId.UNITY_LAYOUT_DIAGNOSTICS: "onebrief-layout-diagnostics.log",
                }
                log = clone / log_names[adapter.adapter_id]
                argv = [str(editor), "-batchmode"]
                if adapter.adapter_id == AdapterId.UNITY_COMPILE:
                    argv.append("-quit")
                argv += ["-projectPath", str(clone), "-logFile", str(log)]
                if adapter.adapter_id == AdapterId.UNITY_EDITMODE_TESTS:
                    argv += [
                        "-runTests", "-testPlatform", "EditMode", "-testResults",
                        str(clone / "onebrief-editmode-test-results.xml"),
                    ]
                elif adapter.adapter_id == AdapterId.UNITY_PLAYMODE_VISUAL_TESTS:
                    argv += [
                        "-runTests", "-testPlatform", "PlayMode",
                        "-testFilter", "OneBrief.Visual",
                        "-testResults", str(clone / "onebrief-playmode-visual-results.xml"),
                    ]
                elif adapter.adapter_id == AdapterId.UNITY_LAYOUT_DIAGNOSTICS:
                    argv += [
                        "-quit", "-executeMethod",
                        "OneBriefDiagnostics.LayoutDiagnostics.Export",
                    ]
                commands.append((adapter.adapter_id.value, argv, 900))
        def priority(item: tuple[str, list[str], int]) -> tuple[int, str]:
            command_id = item[0]
            if "lint" in command_id or command_id == AdapterId.UNITY_COMPILE.value:
                return (10, command_id)
            if "build" in command_id or "compile" in command_id:
                return (20, command_id)
            if command_id == AdapterId.UNITY_LAYOUT_DIAGNOSTICS.value:
                return (25, command_id)
            if "test" in command_id:
                return (30, command_id)
            return (25, command_id)

        # Some package-defined tests import the built server bundle. Preserve a
        # deterministic validation lifecycle instead of trusting manifest or
        # filesystem discovery order.
        return sorted(commands, key=priority)

    def _dependency_commands(
        self, profile, clone: Path
    ) -> list[tuple[str, list[str], int]]:
        """Restore Node tools from committed lockfiles without lifecycle scripts."""
        npm = "npm.cmd" if os.name == "nt" else "npm"
        scopes: set[str] = set()
        for adapter in profile.adapters:
            if not adapter.enabled or adapter.adapter_id != AdapterId.NODE_SCRIPT:
                continue
            parameter = str(adapter.parameter)
            scopes.add(parameter.split("::", 1)[0] if "::" in parameter else ".")
        commands: list[tuple[str, list[str], int]] = []
        for scope in sorted(scopes):
            package_dir = clone if scope == "." else clone / Path(*PurePosixPath(scope).parts)
            package_json = package_dir / "package.json"
            lockfile = package_dir / "package-lock.json"
            if not package_json.is_file():
                raise RuntimeError(f"node adapter scope has no package.json: {scope}")
            package = json.loads(package_json.read_text(encoding="utf-8"))
            has_dependencies = any(package.get(key) for key in ("dependencies", "devDependencies"))
            if not lockfile.is_file():
                if has_dependencies:
                    raise RuntimeError(
                        f"node dependency bootstrap requires a committed package-lock.json: {scope}"
                    )
                continue
            argv = [npm]
            if scope != ".":
                argv += ["--prefix", scope]
            argv += ["ci", "--ignore-scripts", "--no-audit", "--no-fund"]
            command_id = (
                "node_dependencies" if scope == "."
                else f"node_{scope.replace('/', '_')}_dependencies"
            )
            commands.append((command_id, argv, 600))
        return commands

    def _restore_node_dependencies(
        self, profile, clone: Path
    ) -> tuple[list[DevelopmentCommandResult], list[str]]:
        """Run clean installs with one bounded runtime retry.

        A transient registry/process failure is a runtime responsibility and must
        not consume a maker revision. Lock mismatches use the stricter manifest-
        only repair path; every other failure gets exactly one identical retry.
        """
        results: list[DevelopmentCommandResult] = []
        repaired_locks: list[str] = []
        for command_id, argv, timeout in self._dependency_commands(profile, clone):
            try:
                results.append(self.runner(command_id, argv, clone, timeout))
                continue
            except RuntimeError as exc:
                message = str(exc)
                lock_mismatch = "npm ci" in message and any(marker in message for marker in (
                    "EUSAGE", "Clean install a project", "lock file", "package-lock",
                ))
                if not lock_mismatch:
                    results.append(
                        self.runner(f"{command_id}_retry", argv, clone, timeout)
                    )
                    continue
            prefix = argv[:-4]
            repair_argv = prefix + [
                "install", "--package-lock-only", "--ignore-scripts", "--no-audit", "--no-fund"
            ]
            results.append(self.runner(f"{command_id}_lock_repair", repair_argv, clone, timeout))
            results.append(self.runner(command_id, argv, clone, timeout))
            if "--prefix" in argv:
                scope = argv[argv.index("--prefix") + 1]
                repaired_locks.append(f"{scope}/package-lock.json")
            else:
                repaired_locks.append("package-lock.json")
        return results, repaired_locks

    @staticmethod
    def _requires_unity_visual_runtime(goal_text: str) -> bool:
        return bool(re.search(
            r"(?:\bui\b|screen|visual|render|dropdown|locali[sz]ation|language|"
            r"multilingual|화면|시각|드롭다운|다국어|언어|번역)",
            goal_text,
            re.IGNORECASE,
        ))

    def _unity_visual_contract_issues(
        self,
        profile,
        clone: Path,
        goal_text: str,
        changed_paths: list[str] | None = None,
    ) -> list[str]:
        if not self._requires_unity_visual_runtime(goal_text):
            return []
        if not any(
            item.enabled and item.adapter_id == AdapterId.UNITY_PLAYMODE_VISUAL_TESTS
            for item in profile.adapters
        ):
            return []

        test_sources: list[str] = []
        for path in (clone / "Assets").rglob("*.cs"):
            if path.is_symlink() or not path.is_file():
                continue
            content = path.read_text(encoding="utf-8", errors="replace")
            lowered = content.casefold()
            if "onebrief.visual" in lowered and (
                "[unitytest]" in lowered or "[test]" in lowered
            ):
                test_sources.append(content)

        issues: list[str] = []
        if changed_paths and re.search(
            r"(?:\bui\b|screen|visual|layout|responsive|로그인|로비|설정|화면)",
            goal_text,
            re.IGNORECASE,
        ):
            production_paths = [
                path for path in changed_paths
                if "/tests/" not in f"/{path.casefold()}/"
                and not path.casefold().endswith(".asmdef")
            ]
            if not production_paths:
                issues.append(
                    "the change set contains only verification code; add an actual production UI implementation "
                    "under the approved project source before claiming UI modernization"
                )
            else:
                changed_scene_assets = any(
                    Path(path).suffix.casefold() in {".unity", ".prefab"}
                    for path in production_paths
                )
                changed_production_source = "\n".join(
                    path.read_text(encoding="utf-8", errors="replace")
                    for relative in production_paths
                    if Path(relative).suffix.casefold() == ".cs"
                    and (path := clone / Path(*PurePosixPath(relative).parts)).is_file()
                )
                for relative in production_paths:
                    if Path(relative).suffix.casefold() != ".cs":
                        continue
                    source = clone / Path(*PurePosixPath(relative).parts)
                    original = self.root / Path(*PurePosixPath(relative).parts)
                    if original.is_file() or not source.is_file():
                        continue
                    content = source.read_text(encoding="utf-8", errors="replace")
                    class_match = re.search(
                        r"\bclass\s+([A-Za-z_][A-Za-z0-9_]*)\s*:\s*[^\n{]*\bMonoBehaviour\b",
                        content,
                    )
                    if not class_match:
                        continue
                    class_name = class_match.group(1)
                    runtime_entry = any(marker in content for marker in (
                        "RuntimeInitializeOnLoadMethod", "InitializeOnLoadMethod", "ExecuteAlways",
                    ))
                    referenced_by_other_production = changed_production_source.count(class_name) > 1
                    if not (
                        runtime_entry or changed_scene_assets or referenced_by_other_production
                    ):
                        issues.append(
                            f"new Unity UI MonoBehaviour {class_name} is not attached to a changed scene/prefab and "
                            "has no runtime initialization entrypoint; an inert source file does not implement the UI"
                        )
        if not test_sources:
            issues.append(
                "add a discoverable Unity PlayMode test whose namespace/full name begins "
                "with OneBrief.Visual"
            )
        else:
            combined_source = "\n".join(test_sources)
            combined = combined_source.casefold()
            structural = _csharp_code_only(combined_source).casefold()
            if '${"' in combined or "${'" in combined:
                issues.append("use valid C# interpolation ($\"...\"), never JavaScript-style ${...}")
            if "runtime-evidence.json" not in combined:
                issues.append("the OneBrief.Visual test must write onebrief-evidence/runtime-evidence.json")
            elif (
                "onebrief-unity-visual-evidence-v1" not in combined
                or "scenarios" not in combined
            ):
                issues.append(
                    "runtime-evidence.json must use schema_version onebrief-unity-visual-evidence-v1 and contain "
                    "a scenarios array derived from the executed UI states"
                )
            elif not all(field in combined for field in (
                "scenario_id", "observed_state", "interaction", "assertion_count",
                "viewport_width", "viewport_height", "screenshot_path",
            )):
                issues.append(
                    "each general UI evidence scenario must contain scenario_id, observed_state, interaction, "
                    "assertion_count, viewport_width, viewport_height, and screenshot_path"
                )
            if re.search(
                r'\\?"viewport_(?:width|height)\\?"\s*:\s*\d+', combined_source,
                re.IGNORECASE,
            ):
                issues.append(
                    "runtime evidence must record the actual captured PNG texture dimensions, not hard-code "
                    "requested viewport values"
                )
            unity_test_methods = re.findall(
                r"\[\s*unitytest\s*\]\s*(?:public\s+)?(?:ienumerator|void)\s+([A-Za-z_][A-Za-z0-9_]*)",
                structural,
            )
            duplicate_test_methods = sorted({
                name for name in unity_test_methods if unity_test_methods.count(name) > 1
            })
            if duplicate_test_methods:
                issues.append(
                    "the OneBrief.Visual source contains duplicate UnityTest methods after repair: "
                    + ", ".join(duplicate_test_methods)
                )
            if ".png" not in combined and "capturescreenshot" not in combined:
                issues.append("the OneBrief.Visual test must capture PNG runtime evidence")
            if "screencapture.capturescreenshot" in structural and not all(
                token in structural for token in ("readpixels", "encodetopng", "file.writeallbytes")
            ):
                issues.append(
                    "Unity batchmode must not rely on asynchronous ScreenCapture.CaptureScreenshot; "
                    "render the real scene UI to a RenderTexture, read pixels, EncodeToPNG, and "
                    "File.WriteAllBytes synchronously"
                )
            if "waitforendofframe" in structural:
                issues.append(
                    "Unity batchmode does not evoke WaitForEndOfFrame; use a batch-safe yield or "
                    "synchronous render/readback instead"
                )
            if (
                "rendertexture" in structural
                and ".render()" in structural
                and not (
                    "screenspacecamera" in structural
                    and "worldcamera" in structural
                    and "rendermode" in structural
                )
            ):
                issues.append(
                    "a camera RenderTexture does not capture ScreenSpaceOverlay UI; temporarily route the real "
                    "active Canvas through ScreenSpaceCamera/worldCamera, render it, then restore its prior state"
                )
            responsive_requested = (
                ("mobile" in goal_text.casefold() or "모바일" in goal_text)
                and ("desktop" in goal_text.casefold() or "데스크톱" in goal_text)
            )
            if responsive_requested:
                measured = _declared_unity_viewports(combined_source)
                if not any(width < height for width, height in measured) or not any(
                    width >= height for width, height in measured
                ):
                    issues.append(
                        "responsive Unity visual evidence must define and capture both a measured "
                        "mobile/portrait viewport and a desktop/landscape viewport before PlayMode execution"
                    )
                if (
                    "screen.setresolution" in structural
                    and _uses_screen_sized_render_target(structural)
                ):
                    issues.append(
                        "responsive Unity batchmode evidence must pass each requested viewport width and height "
                        "directly into the synchronous RenderTexture capture; Screen.SetResolution plus "
                        "Screen.width/Screen.height can reuse one batchmode render size"
                    )
            if "scenemanager.loadscene" not in structural and "scenemanager.loadsceneasync" not in structural:
                issues.append(
                    "the OneBrief.Visual test must load and exercise an actual project scene, not an empty test scene"
                )
            broad_named_button_fallbacks = re.findall(
                r"([a-z][a-z0-9_]*button)\s*=\s*[^;]*getcomponentinchildren\s*<\s*"
                r"(?:[a-z0-9_.]+\.)?button\s*>\s*\(\s*true\s*\)",
                structural,
            )
            if broad_named_button_fallbacks:
                issues.append(
                    "a named UI action must not fall back to the first arbitrary child Button; rediscover "
                    "the real control by exact hierarchy or a semantic name match: "
                    + ", ".join(sorted(set(broad_named_button_fallbacks)))
                )
            local_control_declarations = re.findall(
                r"\bvar\s+([a-z_][a-z0-9_]*(?:button|btn))\s*=\s*([^;]+);",
                structural,
            )
            duplicate_control_declarations = sorted({
                name
                for name, expression in local_control_declarations
                if sum(
                    1
                    for other_name, other_expression in local_control_declarations
                    if other_name == name and other_expression == expression
                ) > 1
            })
            if duplicate_control_declarations:
                issues.append(
                    "the Unity visual test contains an identical duplicate local UI-control declaration; "
                    "remove the repeated block instead of appending the same navigation again: "
                    + ", ".join(duplicate_control_declarations)
                )
            if re.search(
                r"(?:\.text|\.options\s*\[[^\]]+\]\s*\.text)\s*=",
                structural,
            ):
                issues.append(
                    "a runtime evidence test must observe product text, not rewrite visible labels or "
                    "dropdown option text to hide a localization or glyph defect"
                )
            if re.search(
                r"\.(?:uiscalemode|referenceresolution|matchwidthorheight)\s*=",
                structural,
            ):
                issues.append(
                    "a runtime evidence test must observe the shipped responsive layout, not configure "
                    "CanvasScaler properties during verification; repair the production UI instead"
                )
            requested_scenes = re.findall(
                r"SceneManager\.LoadScene(?:Async)?\s*\(\s*\"([^\"]+)\"",
                combined_source,
            )
            project_scenes = {
                path.stem: path for path in (clone / "Assets").rglob("*.unity") if path.is_file()
            }
            if project_scenes and requested_scenes:
                missing_scenes = [name for name in requested_scenes if name not in project_scenes]
                if missing_scenes:
                    issues.append(
                        "the OneBrief.Visual test names a scene that does not exist; use an exact project scene name: "
                        + ", ".join(missing_scenes)
                        + "; available: "
                        + ", ".join(sorted(project_scenes)[:12])
                    )
                settings_scenes = {
                    name for name, path in project_scenes.items()
                    if "LanguageDropdown" in path.read_text(encoding="utf-8", errors="replace")
                }
                verified_navigation_to_settings_scene = bool(
                    any(name in combined_source for name in settings_scenes)
                    and re.search(r"\bSceneManager\s*\.\s*GetActiveScene\s*\(", combined_source)
                    and re.search(r"\bAssert\s*\.", combined_source)
                    and any(token in structural for token in (
                        ".onclick.invoke", "executeevents.execute", "pointerclickevent",
                    ))
                )
                if (
                    settings_scenes
                    and not any(name in settings_scenes for name in requested_scenes)
                    and not verified_navigation_to_settings_scene
                ):
                    issues.append(
                        "the OneBrief.Visual test must load a scene containing the real LanguageDropdown: "
                        + ", ".join(sorted(settings_scenes))
                        + "; alternatively reach it through a real UI action and assert the active scene"
                    )
            requested_surfaces = requested_ui_surfaces(goal_text)
            if len(requested_surfaces) >= 2 and not any(token in structural for token in (
                ".onclick.invoke", "executeevents.execute", ".setactive(true)",
                "pointerclickevent", "submitEvent",
            )):
                issues.append(
                    "the OneBrief.Visual test must perform a real UI interaction to move between requested "
                    "screens; loading scenes and asserting object presence alone does not prove the transition"
                )
            literal_scenarios = _unity_literal_scenarios(combined_source)
            if len(requested_surfaces) >= 2 and literal_scenarios:
                if not re.search(r"\bAssert\s*\.", structural, re.IGNORECASE):
                    issues.append(
                        "multi-screen runtime evidence must execute real Assert checks for discovered controls and "
                        "observed destinations; a hard-coded assertion_count is not proof"
                    )
                action_markers = (
                    "click", "submit", "start", "login", "setting", "open", "back", "return",
                    "선택", "열기", "뒤로", "복귀", "로그인", "설정",
                )
                action_tokens = (
                    ".onclick.invoke", "executeevents.execute", "pointerclickevent", "submitevent",
                )
                for state, interaction, start, end in literal_scenarios:
                    if not any(marker in interaction.casefold() for marker in action_markers):
                        continue
                    segment = combined_source[start:end]
                    segment_structural = _csharp_code_only(segment).casefold()
                    if not any(token in segment_structural for token in action_tokens):
                        issues.append(
                            f"runtime evidence labels {state}/{interaction} as a UI action but no real control "
                            "was invoked before that evidence row; do not relabel a direct scene load as a click"
                        )
                        break
            requested_transition = requested_ui_transition(goal_text)
            if requested_transition and literal_scenarios and not _contains_ordered_literals(
                [state for state, _interaction, _start, _end in literal_scenarios],
                requested_transition,
            ):
                issues.append(
                    "the OneBrief.Visual test must record the requested ordered UI journey, including repeated "
                    "return destinations: " + " -> ".join(requested_transition)
                )
            if re.search(r"new\s+(?:unityengine\.)?gameobject", structural) and re.search(
                r"addcomponent\s*<[^>]*(?:canvas|tmp_|text|image|button|recttransform)[^>]*>",
                structural,
            ):
                issues.append(
                    "the OneBrief.Visual test must not construct synthetic UI; find and interact with the real scene UI"
                )
            if not any(token in structural for token in (
                "findobjectsbytype", "findobjectsoftype", "findfirstobjectbytype",
                "findanyobjectbytype", "findobjectoftype", "gameobject.find", "getcomponent<tmp_",
            )):
                issues.append(
                    "the OneBrief.Visual test must inspect and interact with visible UI objects from the loaded scene"
                )
            language_requested = bool(re.search(
                r"(?:language|locali[sz]ation|다국어|언어)", goal_text, re.IGNORECASE
            ))
            dropdown_discovered = bool(
                re.search(r"GameObject\.Find\s*\(\s*\"LanguageDropdown\"", combined_source)
                or "getcomponent<tmp_dropdown" in structural
                or "findobjectsoftypeall<tmp_dropdown" in structural
                or re.search(
                    r"(?:findfirstobjectbytype|findanyobjectbytype|findobjectsbytype|"
                    r"findobjectsoftype)\s*<\s*(?:tmpro\.)?tmp_dropdown",
                    structural,
                )
            )
            dropdown_operated = bool(re.search(
                r"(?:\b[a-z_][a-z0-9_]*dropdown\b|\blangdropdown\b)\s*\.\s*"
                r"(?:value\s*=|setvaluewithoutnotify\s*\(|onvaluechanged\s*\.\s*invoke\s*\()",
                structural,
            ))
            if language_requested and not dropdown_discovered:
                issues.append(
                    "the OneBrief.Visual test must discover the real LanguageDropdown control from the loaded scene"
                )
            elif language_requested and not dropdown_operated:
                issues.append(
                    "the OneBrief.Visual test must select a real LanguageDropdown value and dispatch its change"
                )
            if language_requested and re.search(
                r"findobjectsoftype\s*<\s*(?:tmpro\.)?tmp_dropdown\s*>\s*\(\s*true\s*\)",
                structural,
            ) and not any(token in structural for token in (
                ".isactiveandenabled", ".activeinhierarchy",
            )):
                issues.append(
                    "the OneBrief.Visual test searches inactive LanguageDropdown objects but never proves the selected "
                    "control is active and visible; filter by isActiveAndEnabled/activeInHierarchy before interaction"
                )
            if language_requested and dropdown_discovered and not re.search(
                r"assert\s*\.\s*[a-z0-9_]+\s*\([^;\n]*dropdown",
                structural,
                re.IGNORECASE,
            ):
                issues.append(
                    "the OneBrief.Visual test must Assert that the selected real LanguageDropdown exists and is visible "
                    "instead of silently skipping its evidence"
                )
            if re.search(r"observed_locale\s*=\s*(?:lang|languages\s*\[)", structural):
                issues.append(
                    "observed_locale must come from the running localization state (for example reflected GetLanguage), "
                    "not the requested loop variable"
                )
            if re.search(r"changed_visible_text_count\s*=\s*\w+(?:\.length|\.count)", structural):
                issues.append(
                    "changed_visible_text_count must compare visible text before and after the language interaction"
                )
            locale_change_marker = structural.find("changedvisibletextcount")
            locale_interactions = list(re.finditer(
                r"(?:\b[a-z_][a-z0-9_]*dropdown\b|\blangdropdown\b)\s*\.\s*"
                r"onvaluechanged\s*\.\s*invoke\s*\(",
                structural,
            ))
            if language_requested and locale_change_marker >= 0 and locale_interactions:
                interaction_end = locale_interactions[-1].end()
                measurement_window = structural[interaction_end:locale_change_marker]
                left_surface_early = bool(re.search(
                    r"\b[a-z0-9_]*(?:close|back|return)[a-z0-9_]*\s*\.\s*"
                    r"onclick\s*\.\s*invoke\s*\(",
                    measurement_window,
                ))
                has_before_snapshot = bool(re.search(
                    r"(?:before[a-z0-9_]*(?:text|snapshot)|(?:text|snapshot)[a-z0-9_]*before)",
                    structural,
                ))
                has_after_snapshot = bool(re.search(
                    r"(?:after[a-z0-9_]*(?:text|snapshot)|(?:text|snapshot)[a-z0-9_]*after)",
                    structural,
                ))
                compares_snapshot_to_live_text = bool(
                    re.search(
                        r"before[a-z0-9_]*(?:text|snapshot)[a-z0-9_]*\.trygetvalue\s*\(",
                        structural,
                    )
                    and re.search(
                        r"before[a-z0-9_]*text\s*!=\s*[a-z0-9_]+\.text",
                        structural,
                    )
                )
                valid_before_after_comparison = bool(
                    has_before_snapshot
                    and (has_after_snapshot or compares_snapshot_to_live_text)
                )
                if left_surface_early or not valid_before_after_comparison:
                    issues.append(
                        "locale changed_visible_text_count must compare before/after visible text snapshots while "
                        "the localized target surface remains active; measure and capture the locale scenario before "
                        "invoking any Close, Back, or Return navigation, and never rewrite product labels in the test"
                    )
            if (
                "textinfo.characterinfo" not in structural
                and ".hascharacter" not in structural
                and "getmissingcharacters" not in structural
            ):
                issues.append(
                    "missing_glyph_count must inspect TMP font glyph availability, not only search rendered text for a box character"
                )

            test_assembly_references: set[str] = set()
            for asmdef in (clone / "Assets").rglob("*.asmdef"):
                parts = {part.casefold() for part in asmdef.relative_to(clone).parts}
                if "tests" not in parts or not asmdef.is_file():
                    continue
                try:
                    payload = json.loads(asmdef.read_text(encoding="utf-8", errors="replace"))
                except json.JSONDecodeError:
                    continue
                test_assembly_references.update(
                    str(item) for item in payload.get("references", []) if isinstance(item, str)
                )

            direct_type_violations: list[str] = []
            test_text = _csharp_code_only(combined_source)
            assets_root = clone / "Assets"
            for path in assets_root.rglob("*.cs"):
                relative_parts = {part.casefold() for part in path.relative_to(clone).parts}
                if "tests" in relative_parts or path.is_symlink() or not path.is_file():
                    continue
                source = path.read_text(encoding="utf-8", errors="replace")
                declared_types = re.findall(
                    r"\b(?:class|struct|interface|enum)\s+([A-Z][A-Za-z0-9_]*)",
                    source,
                )
                referenced_types = [
                    name for name in declared_types
                    if re.search(rf"\b{re.escape(name)}\b", test_text)
                ]
                if not referenced_types:
                    continue
                owning_assembly: str | None = None
                parent = path.parent
                while parent.is_relative_to(assets_root):
                    asmdefs = list(parent.glob("*.asmdef"))
                    if asmdefs:
                        try:
                            payload = json.loads(asmdefs[0].read_text(encoding="utf-8", errors="replace"))
                            owning_assembly = str(payload.get("name") or "") or None
                        except json.JSONDecodeError:
                            owning_assembly = None
                        break
                    if parent == assets_root:
                        break
                    parent = parent.parent
                for name in referenced_types:
                    if owning_assembly is None:
                        direct_type_violations.append(f"{name} (Assembly-CSharp)")
                    elif owning_assembly not in test_assembly_references:
                        direct_type_violations.append(
                            f"{name} (missing test reference to {owning_assembly})"
                        )
            if direct_type_violations:
                issues.append(
                    "the PlayMode test cannot directly reference production types outside its assembly; "
                    "interact through the running scene/public UI or reflection instead: "
                    + ", ".join(sorted(set(direct_type_violations))[:8])
                )

        test_assembly = False
        unsafe_test_assemblies: list[str] = []
        for path in (clone / "Assets").rglob("*.asmdef"):
            if path.is_symlink() or not path.is_file():
                continue
            content = path.read_text(encoding="utf-8", errors="replace").casefold()
            if "testassemblies" in content:
                relative_parts = [part.casefold() for part in path.relative_to(clone).parts]
                if "tests" not in relative_parts:
                    unsafe_test_assemblies.append(path.relative_to(clone).as_posix())
                else:
                    test_assembly = True
        if unsafe_test_assemblies:
            issues.append(
                "test .asmdef files must live under a dedicated Tests/PlayMode directory and must "
                "never be placed above production scripts: " + ", ".join(unsafe_test_assemblies)
            )
        if not test_assembly:
            issues.append(
                "add a Unity test .asmdef with optionalUnityReferences containing TestAssemblies"
            )
        return issues


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
                committed = self._blob(head, change.path)
                exists = True
            except RuntimeError:
                committed = b""
                exists = False
            if exists and change.path not in inspected:
                committed_sha256 = hashlib.sha256(committed).hexdigest()
                if change.base_sha256 != committed_sha256:
                    raise PermissionError(
                        f"existing file was not included in approved model context: {change.path}"
                    )
                # A bounded continuation merges its new delta with the prior
                # rejected candidate. Goal retargeting may legitimately evict
                # an unchanged prior file from the new 120 KiB inspection.
                # Preserve it only when its already-bound hash still matches
                # the exact approved HEAD; a new or stale unseen edit remains
                # blocked above.
                rebound.append(change)
                continue
            rebound.append(change.model_copy(update={
                "base_sha256": inspected[change.path] if exists else None,
            }))
        return change_set.model_copy(update={"changes": rebound})
    def apply_and_verify(
        self,
        change_set: ProjectCodeChangeSet,
        output_dir: Path,
        verification_goal: str = "",
    ) -> DevelopmentRun:
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
                target.write_text(
                    self._normalize_safe_generated_text(change.path, change.content),
                    encoding="utf-8",
                    newline="\n",
                )
            candidate_hasher = hashlib.sha256()
            candidate_hasher.update(head.encode("ascii"))
            for relative in sorted(item.path for item in change_set.changes):
                target = clone / Path(*PurePosixPath(relative).parts)
                candidate_hasher.update(relative.encode("utf-8"))
                candidate_hasher.update(b"\0")
                candidate_hasher.update(target.read_bytes())
                candidate_hasher.update(b"\0")
            candidate_sha256 = candidate_hasher.hexdigest()
            if new_paths:
                self._git("add", "-N", "--", *new_paths, cwd=clone)
            approved_paths = [item.path for item in change_set.changes]
            goal_text = verification_goal or "\n".join([
                change_set.summary,
                *(item.reason for item in change_set.changes),
            ])
            hygiene_failure: RuntimeError | None = None
            try:
                self._git("diff", "--check", "--", *approved_paths, cwd=clone)
            except RuntimeError as exc:
                hygiene_failure = exc
            visual_issues = self._unity_visual_contract_issues(
                profile, clone, goal_text, approved_paths
            )
            if visual_issues:
                details = [f"Unity visual test contract: {item}" for item in visual_issues]
                if hygiene_failure is not None:
                    details.append(f"Patch hygiene: {hygiene_failure}")
                raise RuntimeError("development verification failed: " + " | ".join(details))
            if hygiene_failure is not None:
                raise RuntimeError(
                    f"development patch hygiene failed: {hygiene_failure}"
                ) from hygiene_failure
            if any(
                item.enabled and item.adapter_id == AdapterId.UNITY_LAYOUT_DIAGNOSTICS
                for item in profile.adapters
            ) and self._requires_unity_visual_runtime(goal_text):
                install_unity_layout_diagnostic_source(clone)
            dependency_results, repaired_locks = self._restore_node_dependencies(profile, clone)
            for repaired in repaired_locks:
                if self.approved_edit_path(repaired) is None:
                    raise PermissionError(
                        f"repaired lockfile is outside the approved project source area: {repaired}"
                    )
                if repaired not in approved_paths:
                    approved_paths.append(repaired)
            commands = self._commands(profile, clone, goal_text)
            results = dependency_results + [
                self.runner(command_id, argv, clone, timeout)
                for command_id, argv, timeout in commands
            ]
            evidence_paths: list[str] = []
            if any(
                item.command_id == AdapterId.UNITY_LAYOUT_DIAGNOSTICS.value
                for item in results
            ):
                evidence_dir = output_dir / "unity_layout_diagnostics"
                package_unity_layout_diagnostics(
                    clone / "onebrief-evidence" / "unity-layout-diagnostics.json",
                    evidence_dir,
                    source_revision=head,
                    candidate_sha256=candidate_sha256,
                )
                evidence_paths.append(
                    evidence_dir.relative_to(output_dir.parent).as_posix()
                )
            web_observer = next((
                item for item in profile.adapters
                if item.adapter_id == AdapterId.NODE_WEB_OBSERVATION and item.enabled
            ), None)
            if web_observer is not None:
                web_scope = str(web_observer.parameter or ".")
                evidence_dir = output_dir / "web_observation_evidence"
                observation_command, receipt = observe_web_application(
                    clone, evidence_dir, application_subdir=web_scope
                )
                if re.search(r"(?:\bpreserv(?:e|es|ed|ing)\b|보존|유지)", goal_text, re.IGNORECASE):
                    baseline = Path(temporary) / "baseline"
                    self._git(
                        "clone", "--local", "--no-hardlinks", str(self.root), str(baseline),
                        cwd=Path(temporary),
                    )
                    expected_build = "build" if web_scope == "." else f"{web_scope}::build"
                    build_adapter = next((
                        item for item in profile.adapters
                        if item.enabled
                        and item.adapter_id == AdapterId.NODE_SCRIPT
                        and str(item.parameter) == expected_build
                    ), None)
                    if build_adapter is None:
                        raise RuntimeError(
                            "web content preservation requires an approved baseline build adapter"
                        )
                    npm = "npm.cmd" if os.name == "nt" else "npm"
                    baseline_argv = (
                        [npm, "run", "build"] if web_scope == "."
                        else [npm, "--prefix", web_scope, "run", "build"]
                    )
                    baseline_dependencies, _ = self._restore_node_dependencies(profile, baseline)
                    baseline_result = self.runner(
                        "baseline_node_build", baseline_argv, baseline, 300
                    )
                    baseline_evidence = output_dir / "baseline_web_observation_evidence"
                    baseline_observation, _ = observe_web_application(
                        baseline, baseline_evidence, application_subdir=web_scope
                    )
                    validate_preserved_language_states(
                        baseline_evidence, evidence_dir
                    )
                    results.extend([*baseline_dependencies, baseline_result, baseline_observation])
                results.append(observation_command)
                observation_dir = output_dir.parent / "independent_observations"
                observation_dir.mkdir(parents=True, exist_ok=True)
                (observation_dir / "web_ui_observation.json").write_text(
                    receipt.model_dump_json(indent=2) + "\n", encoding="utf-8"
                )
                evidence_paths.append(
                    evidence_dir.relative_to(output_dir.parent).as_posix()
                )
            if any(
                item.command_id == AdapterId.UNITY_PLAYMODE_VISUAL_TESTS.value
                for item in results
            ):
                evidence_dir = output_dir / "unity_visual_evidence"
                validate_and_copy_unity_visual_evidence(
                    clone,
                    clone / "onebrief-playmode-visual-results.xml",
                    evidence_dir,
                    goal_text,
                )
                evidence_paths.append(evidence_dir.relative_to(output_dir.parent).as_posix())
            patch = self._git("diff", "--binary", "--no-ext-diff", "--", *approved_paths, cwd=clone)
            if not patch.strip():
                raise ValueError("development change set produced no repository diff")
            patch_path = output_dir / "changes.patch"
            patch_path.write_text(patch, encoding="utf-8", newline="\n")
            for relative in approved_paths:
                source = clone / Path(*PurePosixPath(relative).parts)
                destination = output_dir / "changed_files" / Path(*PurePosixPath(relative).parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
            run = DevelopmentRun(
                status="verified", repository_name=self.root.name, base_head_sha=head,
                summary=change_set.summary, changed_paths=approved_paths,
                commands=results, patch_path=patch_path.relative_to(output_dir.parent).as_posix(),
                evidence_paths=evidence_paths,
                safety_boundary=[
                    "original repository remained read-only",
                    "exact approved HEAD and per-file base hashes were enforced",
                    "edits were limited to approved prefixes and text suffixes",
                    "only lockfile-bound dependency restore and fixed validation adapters executed in the clone",
                    "dependency lifecycle scripts, deploy, push, credentials, accounts, and arbitrary commands were blocked",
                ],
            )
            (output_dir / "development_run.json").write_text(run.model_dump_json(indent=2) + "\n", encoding="utf-8")
            (output_dir / "change_set.json").write_text(change_set.model_dump_json(indent=2) + "\n", encoding="utf-8")
            return run
