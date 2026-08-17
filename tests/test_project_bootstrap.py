import json
import subprocess
from pathlib import Path

from fastapi.testclient import TestClient

from onebrief.project_bootstrap import (
    FolderRegistrationRequest,
    draft_project_folder,
    register_project_folder,
)
from onebrief.project_import import MANIFEST_NAME
from onebrief.web_service import app


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def _unity_root(root: Path) -> None:
    (root / "ProjectSettings").mkdir(parents=True)
    (root / "Packages").mkdir()
    (root / "Assets" / "Scripts").mkdir(parents=True)
    (root / "ProjectSettings" / "ProjectVersion.txt").write_text(
        "m_EditorVersion: 6000.3.11f1", encoding="utf-8"
    )
    (root / "Packages" / "manifest.json").write_text("{}", encoding="utf-8")
    (root / "Assets" / "Scripts" / "Game.cs").write_text(
        "public class Game {}", encoding="utf-8"
    )
    _git(root, "init")
    _git(root, "config", "user.name", "OneBrief Test")
    _git(root, "config", "user.email", "onebrief@example.invalid")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "Create Unity project")


def test_folder_draft_detects_project_without_user_authored_json(tmp_path: Path) -> None:
    root = tmp_path / "My_Unity_Game"
    _unity_root(root)

    draft = draft_project_folder(root)

    assert draft.existing_manifest is False
    assert draft.manifest.project_id == "my_unity_game"
    assert draft.manifest.name == "My Unity Game"
    assert draft.manifest.project_type == "unity_project"
    assert draft.inventory.detected_ecosystems == ["unity"]
    assert "ProjectSettings/ProjectVersion.txt" in draft.manifest.authoritative_documents
    assert "Packages/manifest.json" in draft.manifest.authoritative_documents
    assert not (root / MANIFEST_NAME).exists()


def test_folder_draft_namespaces_a_builtin_project_id(tmp_path: Path) -> None:
    root = tmp_path / "exchange"
    _unity_root(root)

    draft = draft_project_folder(root)

    assert draft.manifest.project_id == "exchange-project"


def test_folder_draft_detects_godot_project(tmp_path: Path) -> None:
    root = tmp_path / "Puzzle_Godot"
    root.mkdir()
    (root / "project.godot").write_text(
        '[application]\nconfig/name="Puzzle"\n', encoding="utf-8"
    )
    _git(root, "init")
    _git(root, "config", "user.name", "OneBrief Test")
    _git(root, "config", "user.email", "onebrief@example.invalid")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "Create Godot project")

    draft = draft_project_folder(root)

    assert draft.manifest.project_type == "godot_project"
    assert draft.inventory.detected_ecosystems == ["godot"]
    assert "project.godot" in draft.manifest.authoritative_documents


def test_confirmation_creates_manifest_and_registers_sidecar(tmp_path: Path) -> None:
    root = tmp_path / "My_Unity_Game"
    _unity_root(root)
    registry = tmp_path / "registry"
    request = FolderRegistrationRequest(
        project_root=str(root),
        name="My Game",
        canonical_goal="Finish the game without breaking its established rules.",
    )

    result = register_project_folder(request, registry)

    resident = root / MANIFEST_NAME
    assert resident.is_file()
    payload = json.loads(resident.read_text(encoding="utf-8"))
    assert payload["name"] == "My Game"
    assert payload["canonical_goal"] == request.canonical_goal
    assert result.record.manifest.project_id == "my_unity_game"
    assert (registry / "my_unity_game" / "project.json").is_file()
    second = draft_project_folder(root)
    assert second.existing_manifest is True
    assert second.editable_fields == []


def test_web_folder_picker_returns_draft_and_registers(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "My_Unity_Game"
    _unity_root(root)
    registry = tmp_path / "registry"
    monkeypatch.setenv("ONEBRIEF_PROJECTS_ROOT", str(registry))
    monkeypatch.setattr("onebrief.web_service.choose_project_folder", lambda: str(root))
    client = TestClient(app)

    picked = client.post("/api/projects/pick-folder")

    assert picked.status_code == 200
    draft = picked.json()["draft"]
    assert draft["manifest"]["project_type"] == "unity_project"
    registered = client.post(
        "/api/projects/register-folder",
        json={
            "project_root": str(root),
            "name": "Confirmed Game",
            "canonical_goal": "Complete the confirmed game safely.",
        },
    )
    assert registered.status_code == 200
    assert registered.json()["project"]["name"] == "Confirmed Game"
    assert registered.json()["project"]["toolpack_status"] == "needs_generation"
