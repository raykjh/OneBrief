import json
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from onebrief.project_catalog import ProjectCatalog
from onebrief.project_continuity import ProjectContinuityStore
from onebrief.project_import import ExternalProjectImporter, MANIFEST_NAME
from onebrief.schemas import RequirementsAnalysis
from onebrief.web_service import app


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def _manifest(root: Path) -> bytes:
    payload = {
        "schema_version": "onebrief-project-v1",
        "project_id": "sample-unity",
        "name": "Sample Unity",
        "project_type": "unity_game",
        "project_root": str(root.resolve()),
        "canonical_goal": "Maintain and improve the sample Unity game.",
        "summary": "Imported Unity project.",
        "authoritative_documents": ["README.md"],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")


def _unity_project(root: Path) -> bytes:
    (root / "ProjectSettings").mkdir(parents=True)
    (root / "Assets" / "Scripts").mkdir(parents=True)
    (root / "ProjectSettings" / "ProjectVersion.txt").write_text(
        "m_EditorVersion: 6000.3.11f1", encoding="utf-8"
    )
    (root / "Assets" / "Scripts" / "Game.cs").write_text(
        "public class Game {}", encoding="utf-8"
    )
    (root / "README.md").write_text("# Sample Unity", encoding="utf-8")
    payload = _manifest(root)
    (root / MANIFEST_NAME).write_bytes(payload)
    _git(root, "init")
    _git(root, "config", "user.name", "OneBrief Test")
    _git(root, "config", "user.email", "onebrief@example.invalid")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "Create sample Unity project")
    return payload


def test_external_project_import_creates_sidecar_workspace_and_catalog_entry(tmp_path: Path) -> None:
    root = tmp_path / "sample"
    payload = _unity_project(root)
    registry = tmp_path / "registry"

    result = ExternalProjectImporter(registry).import_bytes(payload)

    assert result.record.inventory.detected_ecosystems == ["unity"]
    assert result.record.toolpack_preparation.status == "needs_generation"
    assert result.record.toolpack_preparation.approval_required is True
    workspace = Path(result.registry_path)
    for relative in ("memory", "knowledge", "skills", "toolpacks", "evidence", "runs"):
        assert (workspace / relative).is_dir()
    assert (workspace / "memory" / "canonical_goal.md").read_text(
        encoding="utf-8"
    ).strip() == "Maintain and improve the sample Unity game."
    assert not (root / ".onebrief").exists()

    project = ProjectCatalog(
        exchange_root=tmp_path / "missing-exchange",
        registry_root=registry,
    ).get("sample-unity")
    assert project.origin == "imported"
    assert project.toolpack_id is None
    assert project.toolpack_status == "needs_generation"
    assert project.ready_for_isolated_edit is False

    state = ProjectContinuityStore(project, tmp_path / "jobs").load_or_bootstrap()
    assert state.canonical_goal == "Maintain and improve the sample Unity game."
    assert (workspace / "memory" / "project_state.json").is_file()
    assert not (root / ".onebrief").exists()


def test_project_inventory_ignores_only_onebrief_resident_control_files(tmp_path: Path) -> None:
    root = tmp_path / "sample"
    payload = _unity_project(root)
    control = root / ".onebrief" / "project_state.json"
    control.parent.mkdir()
    control.write_text('{"status":"incomplete"}\n', encoding="utf-8")

    result = ExternalProjectImporter(tmp_path / "registry").import_bytes(payload)

    assert result.record.inventory.worktree_status == "clean"

    (root / "Assets" / "Scripts" / "Game.cs").write_text(
        "public class Game { public int Value = 1; }", encoding="utf-8"
    )
    refreshed = ExternalProjectImporter(tmp_path / "registry-2").import_bytes(payload)
    assert refreshed.record.inventory.worktree_status == "modified"

def test_manifest_cannot_grant_commands_or_point_without_resident_proof(tmp_path: Path) -> None:
    root = tmp_path / "sample"
    root.mkdir()
    payload = json.loads(_manifest(root))
    payload["commands"] = [["dangerous"]]
    with pytest.raises(ValueError, match="cannot grant commands"):
        ExternalProjectImporter(tmp_path / "registry").import_bytes(
            json.dumps(payload).encode("utf-8")
        )

    clean = _manifest(root)
    with pytest.raises(ValueError, match="exact ONEBRIEF_PROJECT.json"):
        ExternalProjectImporter(tmp_path / "registry").import_bytes(clean)


def test_web_import_registers_project_for_the_dropdown(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "sample"
    payload = _unity_project(root)
    registry = tmp_path / "registry"
    monkeypatch.setenv("ONEBRIEF_PROJECTS_ROOT", str(registry))
    monkeypatch.setattr(
        "onebrief.web_service.inspect_requirements",
        AsyncMock(return_value=RequirementsAnalysis(
            supported=True,
            support_reason="Supported.",
            normalized_goal="Improve localization.",
            deliverables=["Improved Unity project"],
            mandatory_information=[],
            optional_information=[],
            acceptance_criteria=["The project compiles and localization is verified."],
            assumptions=[],
            consolidated_questions=[],
            ready_for_estimate=True,
        )),
    )

    response = TestClient(app).post(
        "/api/projects/import",
        files={"manifest": (MANIFEST_NAME, payload, "application/json")},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["project"]["project_id"] == "sample-unity"
    assert body["project"]["toolpack_status"] == "needs_generation"
    assert body["inventory"]["detected_ecosystems"] == ["unity"]
    listed = TestClient(app).get("/api/projects?q=sample-unity").json()["projects"]
    assert listed[0]["project_id"] == "sample-unity"
    inspected = TestClient(app).post(
        "/api/inspect",
        data={
            "goal": "Improve localization.",
            "output_target": "existing_project",
            "existing_project_id": "sample-unity",
        },
    )
    assert inspected.status_code == 200
    prepared = inspected.json()["preparation"]
    assert prepared["ready_for_authorization"] is True
    assert prepared["permission_manifest"]["toolpack_sha256"]
    assert "Assets/" in prepared["permission_manifest"]["allowed_write_prefixes"]
    lifecycle = TestClient(app).get(
        "/api/projects/sample-unity/toolpack"
    ).json()["state"]
    assert lifecycle["status"] == "validated"
    assert lifecycle["approval"] is None

