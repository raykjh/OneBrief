"""Deterministic generation, qualification, and approval of imported-project ToolPacks."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator

from onebrief.capability_packs import (
    CapabilityPackRef,
    capability_pack_refs_for_adapter_ids,
    validate_capability_pack_refs,
)
from onebrief.project_import import (
    MANIFEST_NAME,
    ImportedProjectRecord,
    ProjectInventory,
    default_project_registry_root,
    inspect_project,
)


BLOCKED_PARTS = {
    ".env", ".git", ".ssh", "credentials", "library", "logs", "node_modules",
    "secrets", "service-account", "service_account", "temp", "userSettings",
}
SAFE_SUFFIXES = {
    ".asmdef", ".asset", ".cs", ".css", ".html", ".ini", ".js", ".json",
    ".jsx", ".md", ".mjs", ".prefab", ".py", ".shader", ".toml", ".ts",
    ".tsx", ".txt", ".unity", ".uss", ".uxml", ".xml", ".yaml", ".yml",
    ".gd", ".godot", ".tscn", ".tres",
}


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _canonical_hash(value: BaseModel) -> str:
    canonical = value.model_dump(mode="json")
    canonical.pop("generated_at", None)
    payload = json.dumps(
        canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _safe_prefix(value: str) -> str:
    normalized = value.replace("\\", "/").strip("/") + "/"
    path = PurePosixPath(normalized)
    lowered = {part.casefold() for part in path.parts}
    if path.is_absolute() or ".." in path.parts or lowered & {item.casefold() for item in BLOCKED_PARTS}:
        raise ValueError(f"unsafe ToolPack prefix: {value}")
    return normalized


class AdapterId(StrEnum):
    REPOSITORY_SNAPSHOT = "repository_snapshot"
    UNITY_COMPILE = "unity_compile"
    UNITY_EDITMODE_TESTS = "unity_editmode_tests"
    UNITY_PLAYMODE_VISUAL_TESTS = "unity_playmode_visual_tests"
    UNITY_LAYOUT_DIAGNOSTICS = "unity_layout_diagnostics"
    GODOT_HEADLESS_PROBE = "godot_headless_probe"
    NODE_SCRIPT = "node_script"
    NODE_WEB_OBSERVATION = "node_web_observation"
    PYTHON_TESTS = "python_tests"


class ToolAdapter(BaseModel):
    adapter_id: AdapterId
    label: str = Field(min_length=3, max_length=120)
    enabled: bool = True
    parameter: str | None = Field(default=None, max_length=120)
    evidence: str = Field(min_length=1, max_length=1000)


class ApprovedRuntimeArgument(BaseModel):
    """One project-owned, non-secret argv binding emitted by trusted discovery."""

    adapter_id: Literal[AdapterId.UNITY_PLAYMODE_VISUAL_TESTS]
    argument: Literal["--julpae-recording-profile"]
    value: Literal["onebrief-evidence"]
    source_paths: list[str] = Field(min_length=2, max_length=2)
    source_digest_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    authentication_selector: Literal["DevPanel/TestAccountDropdown"] | None = None
    authentication_submit: Literal["DevPanel/DirectEnterButton"] | None = None

    @model_validator(mode="after")
    def validate_allowlisted_sources(self) -> "ApprovedRuntimeArgument":
        expected = [
            "Assets/JULPAE/Scripts/Common/JulpaeRecordingProfile.cs",
            "Assets/JULPAE/Scripts/Login/LoginSceneController.cs",
        ]
        if self.source_paths != expected:
            raise ValueError("runtime argument sources are not on the trusted allowlist")
        if (self.authentication_selector is None) != (self.authentication_submit is None):
            raise ValueError("runtime authentication controls must be bound as one complete pair")
        return self


class ApprovedHostExecutable(BaseModel):
    """Exact local runtime identity included in the approved ToolPack digest."""

    adapter_id: Literal[AdapterId.GODOT_HEADLESS_PROBE]
    executable_path: str = Field(min_length=3, max_length=1000)
    executable_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    executable_size: int = Field(gt=0)

    @model_validator(mode="after")
    def require_absolute_executable(self) -> "ApprovedHostExecutable":
        if not Path(self.executable_path).is_absolute():
            raise ValueError("approved host executable path must be absolute")
        return self


class GeneratedProjectToolPack(BaseModel):
    schema_version: Literal[
        "onebrief-generated-toolpack-v1", "onebrief-generated-toolpack-v2"
    ] = "onebrief-generated-toolpack-v2"
    pack_kind: Literal["project_profile"] = "project_profile"
    project_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,63}$")
    project_root: str
    manifest_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    repository_head_sha: str | None = Field(default=None, pattern=r"^[a-f0-9]{40}$")
    generated_at: str
    capabilities: list[str] = Field(min_length=1, max_length=20)
    allowed_read_prefixes: list[str] = Field(min_length=1, max_length=20)
    allowed_write_prefixes: list[str] = Field(max_length=20)
    allowed_suffixes: list[str] = Field(min_length=1, max_length=80)
    adapters: list[ToolAdapter] = Field(min_length=1, max_length=12)
    runtime_arguments: list[ApprovedRuntimeArgument] = Field(default_factory=list, max_length=8)
    approved_host_executables: list[ApprovedHostExecutable] = Field(
        default_factory=list, max_length=8
    )
    trusted_component_digests: dict[str, str] = Field(default_factory=dict, max_length=16)
    capability_packs: list[CapabilityPackRef] = Field(default_factory=list, max_length=12)
    blocked_boundaries: list[str] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def validate_boundaries(self) -> "GeneratedProjectToolPack":
        self.allowed_read_prefixes = [_safe_prefix(item) for item in self.allowed_read_prefixes]
        self.allowed_write_prefixes = [_safe_prefix(item) for item in self.allowed_write_prefixes]
        if not set(self.allowed_write_prefixes).issubset(set(self.allowed_read_prefixes)):
            raise ValueError("write prefixes must be a subset of read prefixes")
        suffixes = {item.casefold() for item in self.allowed_suffixes}
        if not suffixes or not suffixes.issubset(SAFE_SUFFIXES):
            raise ValueError("ToolPack requested an unsupported editable suffix")
        if len(self.adapters) != len({(item.adapter_id, item.parameter) for item in self.adapters}):
            raise ValueError("ToolPack adapters must be unique")
        enabled_adapters = {item.adapter_id for item in self.adapters if item.enabled}
        if any(item.adapter_id not in enabled_adapters for item in self.runtime_arguments):
            raise ValueError("runtime arguments require their exact enabled adapter")
        if len(self.runtime_arguments) != len({
            (item.adapter_id, item.argument) for item in self.runtime_arguments
        }):
            raise ValueError("ToolPack runtime arguments must be unique")
        host_adapters = {item.adapter_id for item in self.approved_host_executables}
        if len(host_adapters) != len(self.approved_host_executables):
            raise ValueError("approved host executable bindings must be unique")
        if any(item.adapter_id not in enabled_adapters for item in self.approved_host_executables):
            raise ValueError("approved host executable requires its exact enabled adapter")
        required_host_adapters = enabled_adapters & {AdapterId.GODOT_HEADLESS_PROBE}
        if host_adapters != required_host_adapters:
            raise ValueError("every enabled approved-host adapter requires one executable binding")
        if AdapterId.GODOT_HEADLESS_PROBE in enabled_adapters:
            digest = self.trusted_component_digests.get("godot_topology_compiler")
            if digest is None or not re.fullmatch(r"[a-f0-9]{64}", digest):
                raise ValueError("Godot adapter requires the trusted compiler implementation digest")
        if self.schema_version == "onebrief-generated-toolpack-v2":
            errors = validate_capability_pack_refs(
                self.capability_packs,
                [item.adapter_id.value for item in self.adapters],
            )
            if errors:
                raise ValueError("; ".join(errors))
        return self

    @property
    def sha256(self) -> str:
        return _canonical_hash(self)


class QualificationCheck(BaseModel):
    check_id: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    passed: bool
    message: str = Field(min_length=1, max_length=1000)


class ToolPackQualification(BaseModel):
    schema_version: Literal["onebrief-toolpack-qualification-v1"] = "onebrief-toolpack-qualification-v1"
    project_id: str
    toolpack_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    qualified_at: str
    status: Literal["passed", "failed"]
    checks: list[QualificationCheck] = Field(min_length=1, max_length=30)


class ToolPackApproval(BaseModel):
    schema_version: Literal["onebrief-toolpack-approval-v1"] = "onebrief-toolpack-approval-v1"
    project_id: str
    toolpack_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    approved_at: str
    approved_by: Literal["local_user"] = "local_user"
    status: Literal["approved"] = "approved"
    constraints: list[str] = Field(min_length=1, max_length=20)


class ToolPackLifecycleState(BaseModel):
    project_id: str
    status: Literal["needs_generation", "generated", "validation_failed", "validated", "approved"]
    execution_ready: bool = False
    execution_blockers: list[str] = Field(default_factory=list)
    generated: GeneratedProjectToolPack | None = None
    qualification: ToolPackQualification | None = None
    approval: ToolPackApproval | None = None


class ToolPackApprovalRequest(BaseModel):
    toolpack_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class ProjectToolPackLifecycle:
    def __init__(self, project_id: str, registry_root: Path | None = None):
        self.registry_root = (registry_root or default_project_registry_root()).resolve()
        self.project_dir = self.registry_root / project_id
        self.project_id = project_id
        self.toolpack_dir = self.project_dir / "toolpacks"

    def _record(self) -> ImportedProjectRecord:
        path = self.project_dir / "project.json"
        if not path.is_file():
            raise FileNotFoundError(f"imported project is not registered: {self.project_id}")
        record = ImportedProjectRecord.model_validate_json(path.read_text(encoding="utf-8"))
        if record.manifest.project_id != self.project_id:
            raise ValueError("project registry identity mismatch")
        return record

    @staticmethod
    def _atomic(path: Path, model: BaseModel) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + f".{uuid4().hex}.tmp")
        temporary.write_text(model.model_dump_json(indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)

    def _load(self, name: str, model_type):
        path = self.toolpack_dir / name
        if not path.is_file():
            return None
        return model_type.model_validate_json(path.read_text(encoding="utf-8"))

    @staticmethod
    def _prefixes(inventory: ProjectInventory) -> tuple[list[str], list[str]]:
        systems = set(inventory.detected_ecosystems)
        if "unity" in systems:
            return ["Assets/", "Packages/", "ProjectSettings/"], ["Assets/", "Packages/"]
        if "godot" in systems:
            return ["game/", "docs/"], ["game/"]
        if "node" in systems:
            writable = ["app/", "src/", "web/", "public/"]
            return [*writable, "tests/", "scripts/"], writable
        if "python" in systems:
            return ["src/", "tests/", "docs/"], ["src/", "tests/", "docs/"]
        if "dotnet" in systems:
            return ["src/", "tests/", "docs/"], ["src/", "tests/", "docs/"]
        return ["docs/"], ["docs/"]

    @staticmethod
    def _node_adapters(root: Path) -> list[ToolAdapter]:
        adapters: list[ToolAdapter] = []
        packages = [
            root / "package.json",
            *(root / scope / "package.json" for scope in ("app", "web", "src", "public")),
        ]
        for package in packages[:4]:
            if not package.is_file() or package.parent.name == "node_modules":
                continue
            try:
                scripts = json.loads(package.read_text(encoding="utf-8")).get("scripts", {})
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(scripts, dict):
                continue
            scope = package.parent.relative_to(root).as_posix()
            parameter_prefix = "" if scope == "." else f"{scope}::"
            label_prefix = "" if scope == "." else f"{scope} "
            for name in ("lint", "test", "build"):
                if isinstance(scripts.get(name), str):
                    adapters.append(ToolAdapter(
                        adapter_id=AdapterId.NODE_SCRIPT,
                        label=f"Run {label_prefix}package script: {name}",
                        parameter=f"{parameter_prefix}{name}",
                        evidence=(
                            f"{package.relative_to(root).as_posix()} defines the {name!r} script."
                        ),
                    ))
            if not (
                isinstance(scripts.get("start"), str)
                and isinstance(scripts.get("build"), str)
            ):
                continue
            chrome_available = any(path.is_file() for path in (
                Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
                Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
            )) if os.name == "nt" else bool(shutil.which("google-chrome") or shutil.which("chromium"))
            adapters.append(ToolAdapter(
                adapter_id=AdapterId.NODE_WEB_OBSERVATION,
                label="Run web UI observer v7 with locale preservation checks",
                enabled=chrome_available,
                parameter=scope,
                evidence=(
                    f"{package.relative_to(root).as_posix()} defines build/start scripts and a local headless Chrome runtime is available."
                    if chrome_available else
                    "A local headless Chrome runtime was not found."
                ),
            ))
        return adapters[:11]

    @staticmethod
    def _unity_editor(root: Path) -> Path | None:
        configured = os.environ.get("ONEBRIEF_UNITY_EDITOR")
        candidates: list[Path] = [Path(configured)] if configured else []
        if os.name == "nt":
            version_file = root / "ProjectSettings" / "ProjectVersion.txt"
            version = ""
            if version_file.is_file():
                for line in version_file.read_text(encoding="utf-8", errors="replace").splitlines():
                    if line.startswith("m_EditorVersion:"):
                        version = line.split(":", 1)[1].strip()
                        break
            if version:
                candidates.append(Path(r"C:\Program Files\Unity\Hub\Editor") / version / "Editor" / "Unity.exe")
            candidates.extend(sorted(Path(r"C:\Program Files\Unity\Hub\Editor").glob("*/Editor/Unity.exe"), reverse=True))
        return next((item.resolve() for item in candidates if item and item.is_file()), None)

    @staticmethod
    def _godot_executable() -> Path | None:
        configured = (
            os.environ.get("KHALINOS_GODOT_EXECUTABLE")
            or os.environ.get("ONEBRIEF_GODOT_EXECUTABLE")
        )
        candidates = [Path(configured)] if configured else []
        for name in ("godot", "godot4"):
            discovered = shutil.which(name)
            if discovered:
                candidates.append(Path(discovered))
        return next((item.resolve() for item in candidates if item.is_file()), None)

    @staticmethod
    def _host_executable_binding(
        adapter_id: AdapterId, executable: Path | None
    ) -> list[ApprovedHostExecutable]:
        if executable is None:
            return []
        digest = hashlib.sha256()
        size = 0
        with executable.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
                size += len(block)
        return [ApprovedHostExecutable(
            adapter_id=adapter_id,
            executable_path=str(executable),
            executable_sha256=digest.hexdigest(),
            executable_size=size,
        )]

    @classmethod
    def _host_binding_matches(cls, binding: ApprovedHostExecutable) -> bool:
        try:
            current = cls._host_executable_binding(
                binding.adapter_id, Path(binding.executable_path)
            )
        except OSError:
            return False
        return len(current) == 1 and current[0] == binding

    @staticmethod
    def _trusted_component_digests(adapters: list[ToolAdapter]) -> dict[str, str]:
        enabled = {item.adapter_id for item in adapters if item.enabled}
        if AdapterId.GODOT_HEADLESS_PROBE not in enabled:
            return {}
        compiler = Path(__file__).with_name("godot_topology.py")
        return {"godot_topology_compiler": hashlib.sha256(compiler.read_bytes()).hexdigest()}

    @staticmethod
    def _approved_runtime_arguments(root: Path, head_sha: str | None) -> list[ApprovedRuntimeArgument]:
        """Discover only fixed argv contracts proven by committed project source."""

        if head_sha is None:
            return []
        source_paths = [
            "Assets/JULPAE/Scripts/Common/JulpaeRecordingProfile.cs",
            "Assets/JULPAE/Scripts/Login/LoginSceneController.cs",
        ]
        blobs: list[bytes] = []
        for relative in source_paths:
            completed = subprocess.run(
                ["git", "show", f"{head_sha}:{relative}"],
                cwd=root,
                capture_output=True,
            )
            if completed.returncode != 0:
                return []
            blobs.append(completed.stdout)
        recording_source = blobs[0].decode("utf-8", errors="replace")
        login_source = blobs[1].decode("utf-8", errors="replace")
        if (
            'private const string ProfileArg = "--julpae-recording-profile"' not in recording_source
            or "JulpaeRecordingProfile.HasDevOrRecordingCommandLineArgs()" not in login_source
        ):
            return []
        digest = hashlib.sha256()
        for relative, blob in zip(source_paths, blobs, strict=True):
            digest.update(relative.encode("utf-8"))
            digest.update(b"\0")
            digest.update(blob)
            digest.update(b"\0")
        authentication_controls = (
            (
                "DevPanel/TestAccountDropdown",
                "DevPanel/DirectEnterButton",
            )
            if all(token in login_source for token in (
                '"DevPanel/TestAccountDropdown"',
                '"DevPanel/DirectEnterButton"',
            ))
            else (None, None)
        )
        return [ApprovedRuntimeArgument(
            adapter_id=AdapterId.UNITY_PLAYMODE_VISUAL_TESTS,
            argument="--julpae-recording-profile",
            value="onebrief-evidence",
            source_paths=source_paths,
            source_digest_sha256=digest.hexdigest(),
            authentication_selector=authentication_controls[0],
            authentication_submit=authentication_controls[1],
        )]

    def generate_and_qualify(self) -> ToolPackLifecycleState:
        record = self._record()
        manifest = record.manifest
        inventory = inspect_project(manifest)
        root = Path(inventory.root_path)
        read_prefixes, write_prefixes = self._prefixes(inventory)
        read_prefixes = [item for item in read_prefixes if (root / item.rstrip("/")).exists()]
        write_prefixes = [item for item in write_prefixes if item in read_prefixes]
        if not read_prefixes:
            read_prefixes = ["docs/"]
            write_prefixes = []
        adapters = [ToolAdapter(
            adapter_id=AdapterId.REPOSITORY_SNAPSHOT,
            label="Verify repository identity and create an isolated snapshot",
            evidence="The imported project inventory records a Git repository and immutable HEAD." if inventory.git_repository else "No Git repository was detected.",
            enabled=inventory.git_repository and inventory.head_sha is not None,
        )]
        systems = set(inventory.detected_ecosystems)
        approved_host_executables: list[ApprovedHostExecutable] = []
        if "unity" in systems:
            editor = self._unity_editor(root)
            has_editmode_tests = any(
                "test" in path.name.casefold()
                for path in root.glob("Assets/**/*.asmdef")
            )
            package_manifest = root / "Packages" / "manifest.json"
            has_test_framework = False
            if package_manifest.is_file():
                try:
                    dependencies = json.loads(
                        package_manifest.read_text(encoding="utf-8")
                    ).get("dependencies", {})
                    has_test_framework = "com.unity.test-framework" in dependencies
                except (OSError, json.JSONDecodeError):
                    has_test_framework = False

            adapters.extend([
                ToolAdapter(
                    adapter_id=AdapterId.UNITY_COMPILE,
                    label="Compile the isolated Unity project in batch mode",
                    enabled=editor is not None,
                    evidence=str(editor) if editor else "A compatible Unity Editor was not found.",
                ),
                ToolAdapter(
                    adapter_id=AdapterId.UNITY_EDITMODE_TESTS,
                    label="Run Unity EditMode tests when the project exposes them",
                    enabled=editor is not None and has_editmode_tests,
                    evidence=(
                        str(editor) if editor is not None and has_editmode_tests
                        else "No compatible Unity Editor and EditMode test assembly pair was found."
                    ),
                ),
                ToolAdapter(
                    adapter_id=AdapterId.UNITY_PLAYMODE_VISUAL_TESTS,
                    label="Run bounded OneBrief.Visual PlayMode tests and collect runtime evidence",
                    enabled=editor is not None and has_test_framework,
                    evidence=(
                        str(editor) if editor is not None and has_test_framework
                        else "A compatible Unity Editor and Unity Test Framework were not both found."
                    ),
                ),
                ToolAdapter(
                    adapter_id=AdapterId.UNITY_LAYOUT_DIAGNOSTICS,
                    label="Inspect Canvas and RectTransform hierarchy without editing the project",
                    enabled=editor is not None,
                    evidence=(
                        str(editor) if editor is not None
                        else "A compatible Unity Editor was not found."
                    ),
                ),
            ])
        if "godot" in systems:
            godot = self._godot_executable()
            adapters.append(ToolAdapter(
                adapter_id=AdapterId.GODOT_HEADLESS_PROBE,
                label="Run the trusted Godot topology probe headlessly",
                enabled=godot is not None,
                parameter="game",
                evidence=str(godot) if godot else "An approved Godot executable was not found.",
            ))
            approved_host_executables.extend(self._host_executable_binding(
                AdapterId.GODOT_HEADLESS_PROBE, godot
            ))
        adapters.extend(self._node_adapters(root))
        if "python" in systems:
            adapters.append(ToolAdapter(
                adapter_id=AdapterId.PYTHON_TESTS,
                label="Run project Python tests",
                enabled=(root / "tests").is_dir(),
                evidence="tests/ directory detected" if (root / "tests").is_dir() else "tests/ directory not found",
            ))
        resident = root / MANIFEST_NAME
        manifest_digest = hashlib.sha256(resident.read_bytes()).hexdigest()
        capability_packs = capability_pack_refs_for_adapter_ids(
            [item.adapter_id.value for item in adapters]
        )
        runtime_arguments = self._approved_runtime_arguments(root, inventory.head_sha)
        generated = GeneratedProjectToolPack(
            project_id=self.project_id,
            project_root=str(root),
            manifest_sha256=manifest_digest,
            repository_head_sha=inventory.head_sha,
            generated_at=_now(),
            capabilities=[
                "read bounded project context",
                "prepare changes only in an isolated repository snapshot",
                "verify exact base hashes before applying changes",
                "restore Node dependencies only from package manifests with lifecycle scripts disabled",
                "run only approved deterministic validation adapters",
                "return a reviewable patch without modifying the source repository",
            ],
            allowed_read_prefixes=read_prefixes,
            allowed_write_prefixes=write_prefixes,
            allowed_suffixes=sorted(SAFE_SUFFIXES),
            adapters=adapters,
            runtime_arguments=runtime_arguments,
            approved_host_executables=approved_host_executables,
            trusted_component_digests=self._trusted_component_digests(adapters),
            capability_packs=capability_packs,
            blocked_boundaries=[
                "source repository writes",
                "credentials, secret stores, accounts, payments, and personal data",
                "network deployment, release, publishing, and Git push",
                "arbitrary dependency commands, dependency lifecycle scripts, and arbitrary shell commands",
                "destructive Git operations and generated build directories",
            ],
        )
        self._atomic(self.toolpack_dir / "generated.json", generated)
        qualification = self._qualify(generated, record, inventory)
        self._atomic(self.toolpack_dir / "qualification.json", qualification)
        approval_path = self.toolpack_dir / "approval.json"
        previous = self._load("approval.json", ToolPackApproval)
        if previous is not None and previous.toolpack_sha256 != generated.sha256:
            approval_path.unlink(missing_ok=True)
        return self.state()

    def _qualify(
        self,
        generated: GeneratedProjectToolPack,
        record: ImportedProjectRecord,
        inventory: ProjectInventory,
    ) -> ToolPackQualification:
        root = Path(generated.project_root).resolve()
        resident = root / MANIFEST_NAME
        checks = [
            QualificationCheck(
                check_id="registry_root_match",
                passed=root == Path(record.manifest.project_root).resolve(),
                message="Generated ToolPack root matches the imported project registry.",
            ),
            QualificationCheck(
                check_id="resident_manifest_match",
                passed=resident.is_file() and hashlib.sha256(resident.read_bytes()).hexdigest() == generated.manifest_sha256,
                message="Resident project manifest matches the generated ToolPack identity.",
            ),
            QualificationCheck(
                check_id="bounded_write_paths",
                passed=bool(generated.allowed_write_prefixes) and set(generated.allowed_write_prefixes).issubset(set(generated.allowed_read_prefixes)),
                message="Write paths are non-empty, relative, bounded, and included in the read boundary.",
            ),
            QualificationCheck(
                check_id="isolated_snapshot_adapter",
                passed=any(item.adapter_id == AdapterId.REPOSITORY_SNAPSHOT and item.enabled for item in generated.adapters),
                message="A Git-backed isolated snapshot adapter is available.",
            ),
            QualificationCheck(
                check_id="repository_head_match",
                passed=inventory.head_sha is not None and inventory.head_sha == generated.repository_head_sha,
                message="The current repository HEAD matches the generated ToolPack base.",
            ),
            QualificationCheck(
                check_id="validation_adapter_available",
                passed=any(item.enabled and item.adapter_id != AdapterId.REPOSITORY_SNAPSHOT for item in generated.adapters),
                message="At least one deterministic project validation adapter is available.",
            ),
            QualificationCheck(
                check_id="capability_pack_integrity",
                passed=not validate_capability_pack_refs(
                    generated.capability_packs,
                    [item.adapter_id.value for item in generated.adapters],
                ),
                message=(
                    "Every adapter is supplied by an exact-version reusable capability pack, "
                    "and each component digest is current."
                ),
            ),
            QualificationCheck(
                check_id="runtime_argument_source_integrity",
                passed=(
                    generated.runtime_arguments
                    == self._approved_runtime_arguments(root, inventory.head_sha)
                ),
                message=(
                    "Every project-specific runtime argument is allowlisted and bound to "
                    "the exact committed source that activates it."
                ),
            ),
            QualificationCheck(
                check_id="approved_host_executable_integrity",
                passed=all(
                    self._host_binding_matches(binding)
                    for binding in generated.approved_host_executables
                ),
                message=(
                    "Every approved-host executable remains at the exact path, size, and SHA-256 "
                    "included in the ToolPack approval digest."
                ),
            ),
            QualificationCheck(
                check_id="trusted_component_integrity",
                passed=(
                    generated.trusted_component_digests
                    == self._trusted_component_digests(generated.adapters)
                ),
                message=(
                    "Trusted compiler implementations match the digests included in the "
                    "ToolPack approval."
                ),
            ),
        ]
        return ToolPackQualification(
            project_id=self.project_id,
            toolpack_sha256=generated.sha256,
            qualified_at=_now(),
            status="passed" if all(item.passed for item in checks) else "failed",
            checks=checks,
        )

    def approve(self, toolpack_sha256: str) -> ToolPackLifecycleState:
        generated = self._load("generated.json", GeneratedProjectToolPack)
        qualification = self._load("qualification.json", ToolPackQualification)
        if generated is None or qualification is None:
            raise ValueError("ToolPack must be generated and qualified before approval")
        if generated.sha256 != toolpack_sha256 or qualification.toolpack_sha256 != toolpack_sha256:
            raise ValueError("ToolPack changed after review; regenerate and review it again")
        if qualification.status != "passed" or not all(item.passed for item in qualification.checks):
            raise ValueError("ToolPack qualification did not pass")
        approval = ToolPackApproval(
            project_id=self.project_id,
            toolpack_sha256=toolpack_sha256,
            approved_at=_now(),
            constraints=generated.blocked_boundaries,
        )
        self._atomic(self.toolpack_dir / "approval.json", approval)
        return self.state()

    def state(self) -> ToolPackLifecycleState:
        try:
            generated = self._load("generated.json", GeneratedProjectToolPack)
        except ValueError:
            # A capability-pack implementation/version change intentionally makes
            # the persisted composition stale. Keep the imported project visible
            # so it can be regenerated, but never expose the old approval as ready.
            return ToolPackLifecycleState(
                project_id=self.project_id,
                status="needs_generation",
                execution_ready=False,
                execution_blockers=[
                    "The stored ToolPack component binding is stale; regenerate and approve the new exact hash."
                ],
            )
        qualification = self._load("qualification.json", ToolPackQualification)
        approval = self._load("approval.json", ToolPackApproval)
        if generated is None:
            return ToolPackLifecycleState(project_id=self.project_id, status="needs_generation")
        status = "generated"
        if qualification is not None:
            status = "validated" if qualification.status == "passed" else "validation_failed"
        valid_approval = (
            approval is not None
            and qualification is not None
            and qualification.status == "passed"
            and approval.toolpack_sha256 == generated.sha256 == qualification.toolpack_sha256
        )
        if valid_approval:
            status = "approved"
        record = self._record()
        inventory = inspect_project(record.manifest)
        blockers: list[str] = []
        if not valid_approval:
            blockers.append("ToolPack generation, qualification, and exact-hash approval are incomplete.")
        if inventory.worktree_status != "clean":
            blockers.append("The source repository has uncommitted changes that must be preserved or isolated.")
        if inventory.head_sha != generated.repository_head_sha:
            blockers.append("The repository HEAD changed after ToolPack generation.")
        if not generated.allowed_write_prefixes:
            blockers.append("The generated ToolPack has no approved write boundary.")
        if not any(
            item.enabled and item.adapter_id != AdapterId.REPOSITORY_SNAPSHOT
            for item in generated.adapters
        ):
            blockers.append("The generated ToolPack has no approved deterministic validation adapter.")
        return ToolPackLifecycleState(
            project_id=self.project_id,
            status=status,
            execution_ready=not blockers,
            execution_blockers=blockers,
            generated=generated,
            qualification=qualification,
            approval=approval if valid_approval else None,
        )
