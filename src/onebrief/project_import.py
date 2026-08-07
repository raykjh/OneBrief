"""Safe import contracts and read-only discovery for external OneBrief projects."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator, model_validator


MANIFEST_NAME = "ONEBRIEF_PROJECT.json"
MANIFEST_VERSION = "onebrief-project-v1"
MAX_MANIFEST_BYTES = 100_000
IGNORED_PARTS = {
    ".git", ".idea", ".vs", ".vscode", "__pycache__", "library", "temp",
    "obj", "build", "dist", "node_modules", "logs",
}


def _now() -> str:
    return datetime.now(UTC).isoformat()


def default_project_registry_root() -> Path:
    configured = os.environ.get("ONEBRIEF_PROJECTS_ROOT")
    if configured:
        return Path(configured).expanduser().resolve()
    local = os.environ.get("LOCALAPPDATA")
    base = Path(local) if local else Path.home() / ".onebrief"
    return (base / "OneBrief" / "projects").resolve()


class ProjectManifest(BaseModel):
    schema_version: Literal["onebrief-project-v1"] = MANIFEST_VERSION
    project_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,63}$")
    name: str = Field(min_length=1, max_length=120)
    project_type: str = Field(min_length=2, max_length=120)
    project_root: str = Field(min_length=3, max_length=1000)
    canonical_goal: str = Field(min_length=3, max_length=8000)
    summary: str = Field(default="", max_length=1000)
    authoritative_documents: list[str] = Field(default_factory=list, max_length=24)

    @field_validator("authoritative_documents")
    @classmethod
    def validate_document_paths(cls, values: list[str]) -> list[str]:
        clean: list[str] = []
        for value in values:
            path = Path(value)
            if path.is_absolute() or ".." in path.parts or not value.strip():
                raise ValueError("authoritative document paths must stay relative to the project root")
            clean.append(value.replace("\\", "/"))
        return clean

    @model_validator(mode="after")
    def forbid_capability_fields(self) -> "ProjectManifest":
        return self


class ProjectInventory(BaseModel):
    schema_version: str = "onebrief-project-inventory-v1"
    project_id: str
    root_path: str
    inspected_at: str
    git_repository: bool
    branch: str | None = None
    head_sha: str | None = Field(default=None, pattern=r"^[a-f0-9]{40}$")
    worktree_status: str
    file_count: int = Field(ge=0)
    sampled_files: list[str] = Field(default_factory=list, max_length=200)
    detected_ecosystems: list[str] = Field(default_factory=list)
    detected_markers: list[str] = Field(default_factory=list)


class ToolPackPreparation(BaseModel):
    schema_version: str = "onebrief-toolpack-preparation-v1"
    project_id: str
    status: Literal["needs_generation"] = "needs_generation"
    allowed_project_root: str
    candidate_capabilities: list[str]
    validation_candidates: list[str]
    excluded_boundaries: list[str]
    approval_required: bool = True
    notes: list[str] = Field(default_factory=list)


class ImportedProjectRecord(BaseModel):
    schema_version: str = "onebrief-imported-project-v1"
    imported_at: str
    manifest_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    manifest: ProjectManifest
    inventory: ProjectInventory
    toolpack_preparation: ToolPackPreparation


class ProjectImportResult(BaseModel):
    record: ImportedProjectRecord
    registry_path: str


def _git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=20,
        shell=False,
        check=False,
    )
    return completed.stdout.strip() if completed.returncode == 0 else ""


def _safe_root(value: str) -> Path:
    root = Path(value).expanduser().resolve(strict=True)
    if not root.is_dir():
        raise ValueError("project_root must be an existing directory")
    home = Path.home().resolve()
    anchor = Path(root.anchor).resolve()
    if root in {home, anchor}:
        raise ValueError("a home directory or drive root cannot be imported as one project")
    return root


def _project_files(root: Path, git_repository: bool) -> list[str]:
    if git_repository:
        return [
            value.replace("\\", "/")
            for value in _git(root, "ls-files").splitlines()
            if value
        ][:5000]
    found: list[str] = []
    for path in root.rglob("*"):
        if len(found) >= 5000:
            break
        try:
            relative = path.relative_to(root)
        except ValueError:
            continue
        if path.is_symlink() or any(part.casefold() in IGNORED_PARTS for part in relative.parts):
            continue
        if path.is_file():
            found.append(relative.as_posix())
    return found


def inspect_project(manifest: ProjectManifest) -> ProjectInventory:
    root = _safe_root(manifest.project_root)
    git_repository = (root / ".git").is_dir()
    files = _project_files(root, git_repository)
    lowered = {item.casefold() for item in files}
    markers: list[str] = []
    ecosystems: list[str] = []
    checks = [
        ("ProjectSettings/ProjectVersion.txt", "unity"),
        ("package.json", "node"),
        ("pyproject.toml", "python"),
        ("requirements.txt", "python"),
        ("Cargo.toml", "rust"),
        ("go.mod", "go"),
    ]
    for marker, ecosystem in checks:
        if marker.casefold() in lowered:
            markers.append(marker)
            if ecosystem not in ecosystems:
                ecosystems.append(ecosystem)
    if any(item.endswith((".sln", ".csproj")) for item in lowered):
        ecosystems.append("dotnet")
    branch = _git(root, "branch", "--show-current") or None if git_repository else None
    head = _git(root, "rev-parse", "HEAD") if git_repository else ""
    porcelain = "\n".join(
        line for line in _git(root, "status", "--porcelain").splitlines()
        if line[3:].replace("\\", "/") != MANIFEST_NAME
    ) if git_repository else ""
    return ProjectInventory(
        project_id=manifest.project_id,
        root_path=str(root),
        inspected_at=_now(),
        git_repository=git_repository,
        branch=branch,
        head_sha=head if len(head) == 40 else None,
        worktree_status=("modified" if porcelain else "clean") if git_repository else "unversioned",
        file_count=len(files),
        sampled_files=files[:200],
        detected_ecosystems=list(dict.fromkeys(ecosystems)),
        detected_markers=markers,
    )


def prepare_toolpack(manifest: ProjectManifest, inventory: ProjectInventory) -> ToolPackPreparation:
    capabilities = ["read project inventory", "read approved project documents", "propose isolated source changes"]
    validations = ["verify changed-file boundaries", "compare repository base hashes"]
    if "unity" in inventory.detected_ecosystems:
        capabilities += ["prepare Unity compile adapter", "prepare Unity test adapter"]
        validations += ["Unity batch-mode compilation", "Unity EditMode or project-specific tests"]
    if "node" in inventory.detected_ecosystems:
        validations += ["package-defined test", "package-defined build"]
    if "python" in inventory.detected_ecosystems:
        validations += ["project-defined Python tests"]
    return ToolPackPreparation(
        project_id=manifest.project_id,
        allowed_project_root=inventory.root_path,
        candidate_capabilities=capabilities,
        validation_candidates=list(dict.fromkeys(validations)),
        excluded_boundaries=[
            "original repository writes before capability approval",
            "credentials, secret stores, accounts, payments, and personal data",
            "push, deployment, release, publishing, and destructive Git operations",
            "commands declared by the manifest itself",
        ],
        notes=[
            "The manifest identifies the project but grants no execution authority.",
            "A Guardian must qualify generated commands and paths before the ToolPack can run.",
        ],
    )


class ExternalProjectImporter:
    def __init__(self, registry_root: Path | None = None):
        self.registry_root = (registry_root or default_project_registry_root()).resolve()

    @staticmethod
    def parse(payload: bytes) -> ProjectManifest:
        if not payload or len(payload) > MAX_MANIFEST_BYTES:
            raise ValueError("project manifest is empty or too large")
        try:
            raw = json.loads(payload.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("project manifest must be valid UTF-8 JSON") from exc
        if not isinstance(raw, dict):
            raise ValueError("project manifest must be a JSON object")
        forbidden = {"commands", "argv", "allowed_commands", "toolpack", "secrets", "environment"}
        if forbidden.intersection(raw):
            raise ValueError("project manifest cannot grant commands, ToolPacks, secrets, or environment access")
        return ProjectManifest.model_validate(raw)

    def import_bytes(self, payload: bytes) -> ProjectImportResult:
        manifest = self.parse(payload)
        root = _safe_root(manifest.project_root)
        resident = root / MANIFEST_NAME
        if not resident.is_file() or resident.read_bytes() != payload:
            raise ValueError(
                f"the uploaded manifest must be the exact {MANIFEST_NAME} file stored in project_root"
            )
        inventory = inspect_project(manifest)
        preparation = prepare_toolpack(manifest, inventory)
        digest = hashlib.sha256(payload).hexdigest()
        record = ImportedProjectRecord(
            imported_at=_now(),
            manifest_sha256=digest,
            manifest=manifest,
            inventory=inventory,
            toolpack_preparation=preparation,
        )
        project_dir = self.registry_root / manifest.project_id
        record_path = project_dir / "project.json"
        if record_path.is_file():
            existing = ImportedProjectRecord.model_validate_json(
                record_path.read_text(encoding="utf-8")
            )
            if Path(existing.manifest.project_root).resolve() != root:
                raise ValueError("project_id is already registered to a different root")
        for relative in ("memory", "knowledge", "skills", "toolpacks", "evidence", "runs"):
            (project_dir / relative).mkdir(parents=True, exist_ok=True)
        self._write(project_dir / "memory" / "canonical_goal.md", manifest.canonical_goal + "\n")
        self._write(project_dir / "inventory.json", inventory.model_dump_json(indent=2) + "\n")
        self._write(
            project_dir / "toolpacks" / "preparation.json",
            preparation.model_dump_json(indent=2) + "\n",
        )
        self._write(record_path, record.model_dump_json(indent=2) + "\n")
        return ProjectImportResult(record=record, registry_path=str(project_dir))

    @staticmethod
    def _write(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + f".{uuid4().hex}.tmp")
        temporary.write_text(content, encoding="utf-8")
        os.replace(temporary, path)

