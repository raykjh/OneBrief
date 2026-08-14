"""Local folder discovery and safe manifest bootstrap for external projects."""

from __future__ import annotations

import base64
import hashlib
import os
import re
import subprocess
from pathlib import Path

from pydantic import BaseModel, Field

from onebrief.project_import import (
    MANIFEST_NAME,
    ExternalProjectImporter,
    ProjectImportResult,
    ProjectInventory,
    ProjectManifest,
    _project_files,
    _safe_root,
    inspect_project,
)


class ProjectFolderDraft(BaseModel):
    manifest: ProjectManifest
    inventory: ProjectInventory
    existing_manifest: bool = False
    editable_fields: list[str] = Field(default_factory=lambda: ["name", "canonical_goal"])
    warnings: list[str] = Field(default_factory=list)


class FolderRegistrationRequest(BaseModel):
    project_root: str = Field(min_length=3, max_length=1000)
    name: str = Field(min_length=1, max_length=120)
    canonical_goal: str = Field(min_length=3, max_length=8000)


def _slug(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9_-]+", "-", value.casefold()).strip("-_")
    if len(normalized) < 2:
        normalized = "project-" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]
    if not normalized[0].isalpha():
        normalized = "project-" + normalized
    # Built-in project IDs are routing keys, not names external folders may
    # silently replace. Give a same-named imported folder its own namespace.
    if normalized == "exchange":
        normalized = "exchange-project"
    return normalized[:64].rstrip("-_")


def _display_name(root: Path) -> str:
    return re.sub(r"[_-]+", " ", root.name).strip() or "Imported project"


def _project_type(ecosystems: list[str]) -> str:
    detected = set(ecosystems)
    if "unity" in detected:
        return "unity_project"
    if "node" in detected:
        return "node_application"
    if "python" in detected:
        return "python_application"
    if "dotnet" in detected:
        return "dotnet_application"
    if "rust" in detected:
        return "rust_application"
    if "go" in detected:
        return "go_application"
    return "general_project"


def _authoritative_documents(root: Path, git_repository: bool) -> list[str]:
    files = _project_files(root, git_repository)
    by_lower = {item.casefold(): item for item in files}
    exact_candidates = [
        "README.md",
        "README.txt",
        "docs/README.md",
        "ProjectSettings/ProjectVersion.txt",
        "Packages/manifest.json",
        "package.json",
        "pyproject.toml",
        "requirements.txt",
        "Cargo.toml",
        "go.mod",
    ]
    selected = [by_lower[item.casefold()] for item in exact_candidates if item.casefold() in by_lower]
    return list(dict.fromkeys(selected))[:12]


def draft_project_folder(project_root: str | Path) -> ProjectFolderDraft:
    root = _safe_root(str(project_root))
    resident = root / MANIFEST_NAME
    if resident.is_file():
        manifest = ExternalProjectImporter.parse(resident.read_bytes())
        if _safe_root(manifest.project_root) != root:
            raise ValueError(f"existing {MANIFEST_NAME} points to a different project root")
        return ProjectFolderDraft(
            manifest=manifest,
            inventory=inspect_project(manifest),
            existing_manifest=True,
            editable_fields=[],
            warnings=["An existing manifest was found and will not be overwritten."],
        )

    provisional = ProjectManifest(
        project_id=_slug(root.name),
        name=_display_name(root),
        project_type="general_project",
        project_root=str(root),
        canonical_goal=f"Safely continue, improve, and complete {_display_name(root)} without regressing existing behavior.",
        summary="External project discovered by OneBrief.",
    )
    inventory = inspect_project(provisional)
    ecosystem_text = ", ".join(inventory.detected_ecosystems) or "general"
    manifest = provisional.model_copy(
        update={
            "project_type": _project_type(inventory.detected_ecosystems),
            "summary": f"Existing {ecosystem_text} project discovered from its repository structure.",
            "authoritative_documents": _authoritative_documents(root, inventory.git_repository),
        }
    )
    warnings: list[str] = []
    if inventory.worktree_status == "modified":
        warnings.append("Uncommitted changes were detected; editing stays blocked until they are preserved or isolated.")
    if not inventory.git_repository:
        warnings.append("No Git repository was detected; version-safe editing needs an additional approval step.")
    return ProjectFolderDraft(manifest=manifest, inventory=inventory, warnings=warnings)


def register_project_folder(
    request: FolderRegistrationRequest,
    registry_root: Path | None = None,
) -> ProjectImportResult:
    draft = draft_project_folder(request.project_root)
    root = _safe_root(request.project_root)
    resident = root / MANIFEST_NAME
    if draft.existing_manifest:
        if request.name != draft.manifest.name or request.canonical_goal != draft.manifest.canonical_goal:
            raise ValueError("an existing manifest must be registered unchanged")
        return ExternalProjectImporter(registry_root).import_bytes(resident.read_bytes())

    manifest = draft.manifest.model_copy(
        update={"name": request.name.strip(), "canonical_goal": request.canonical_goal.strip()}
    )
    payload = (manifest.model_dump_json(indent=2) + "\n").encode("utf-8")
    try:
        with resident.open("xb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
    except FileExistsError as exc:
        raise ValueError(f"{MANIFEST_NAME} appeared during confirmation; nothing was overwritten") from exc
    return ExternalProjectImporter(registry_root).import_bytes(payload)


def choose_project_folder() -> str | None:
    if os.name != "nt":
        raise RuntimeError("native project folder selection is available only in the local Windows app")
    script = r'''
Add-Type -AssemblyName System.Windows.Forms
$dialog = New-Object System.Windows.Forms.FolderBrowserDialog
$dialog.Description = 'OneBrief? ??? ???? ??? ?????.'
$dialog.ShowNewFolderButton = $false
$result = $dialog.ShowDialog()
if ($result -ne [System.Windows.Forms.DialogResult]::OK) { exit 2 }
[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($dialog.SelectedPath))
'''
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-STA", "-Command", script],
        capture_output=True,
        timeout=600,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if completed.returncode == 2:
        return None
    if completed.returncode != 0:
        raise RuntimeError("the Windows project folder picker could not be opened")
    encoded = completed.stdout.decode("ascii", errors="ignore").strip().splitlines()
    if not encoded:
        return None
    try:
        return base64.b64decode(encoded[-1]).decode("utf-8")
    except (ValueError, UnicodeDecodeError) as exc:
        raise RuntimeError("the selected project path could not be decoded") from exc
