import json
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from onebrief.project_catalog import ProjectCatalog
from onebrief.project_import import ExternalProjectImporter, MANIFEST_NAME
from onebrief.toolpack_lifecycle import ProjectToolPackLifecycle
from onebrief.web_service import app


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def _registered_unity(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    root = tmp_path / "game"
    registry = tmp_path / "registry"
    (root / "ProjectSettings").mkdir(parents=True)
    (root / "Packages").mkdir()
    (root / "Assets" / "Scripts").mkdir(parents=True)
    (root / "ProjectSettings" / "ProjectVersion.txt").write_text(
        "m_EditorVersion: 6000.3.11f1", encoding="utf-8"
    )
    (root / "Packages" / "manifest.json").write_text(
        json.dumps({
            "dependencies": {"com.unity.test-framework": "1.6.0"}
        }),
        encoding="utf-8",
    )
    (root / "Assets" / "Scripts" / "Game.cs").write_text(
        "public class Game {}", encoding="utf-8"
    )
    _git(root, "init")
    _git(root, "config", "user.name", "OneBrief Test")
    _git(root, "config", "user.email", "onebrief@example.invalid")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "Create Unity project")
    manifest = {
        "schema_version": "onebrief-project-v1",
        "project_id": "toolpack-game",
        "name": "ToolPack Game",
        "project_type": "unity_project",
        "project_root": str(root.resolve()),
        "canonical_goal": "Safely improve the game.",
        "summary": "Test Unity project.",
        "authoritative_documents": ["ProjectSettings/ProjectVersion.txt"],
    }
    payload = json.dumps(manifest, indent=2).encode("utf-8")
    (root / MANIFEST_NAME).write_bytes(payload)
    ExternalProjectImporter(registry).import_bytes(payload)
    editor = tmp_path / "Unity.exe"
    editor.write_bytes(b"fake editor")
    monkeypatch.setenv("ONEBRIEF_UNITY_EDITOR", str(editor))
    return root, registry


def test_generated_toolpack_is_qualified_and_exact_hash_approved(tmp_path: Path, monkeypatch) -> None:
    _root, registry = _registered_unity(tmp_path, monkeypatch)
    lifecycle = ProjectToolPackLifecycle("toolpack-game", registry)

    generated = lifecycle.generate_and_qualify()

    assert generated.status == "validated"
    assert generated.qualification is not None
    assert all(item.passed for item in generated.qualification.checks)
    assert generated.generated is not None
    assert generated.generated.allowed_write_prefixes == ["Assets/", "Packages/"]
    assert any(item.adapter_id.value == "unity_compile" and item.enabled for item in generated.generated.adapters)
    assert any(
        item.adapter_id.value == "unity_layout_diagnostics" and item.enabled
        for item in generated.generated.adapters
    )
    assert [item.pack_id.value for item in generated.generated.capability_packs] == [
        "repository-control",
        "unity-control",
        "unity-layout-diagnostics",
    ]
    with pytest.raises(ValueError, match="changed after review"):
        lifecycle.approve("0" * 64)

    assert any(
        item.adapter_id.value == "unity_playmode_visual_tests" and item.enabled
        for item in generated.generated.adapters
    )
    approved = lifecycle.approve(generated.qualification.toolpack_sha256)

    assert approved.status == "approved"
    assert approved.approval is not None
    assert approved.execution_ready is True
    assert approved.execution_blockers == []
    project = ProjectCatalog(
        exchange_root=tmp_path / "missing-exchange",
        registry_root=registry,
    ).get("toolpack-game")
    assert project.toolpack_status == "approved"
    assert project.ready_for_isolated_edit is True
    assert project.toolpack_id.value == "project_development"


def test_regeneration_invalidates_approval_when_repository_head_changes(tmp_path: Path, monkeypatch) -> None:
    root, registry = _registered_unity(tmp_path, monkeypatch)
    lifecycle = ProjectToolPackLifecycle("toolpack-game", registry)
    first = lifecycle.generate_and_qualify()
    lifecycle.approve(first.qualification.toolpack_sha256)
    (root / "Assets" / "Scripts" / "Added.cs").write_text(
        "public class Added {}", encoding="utf-8"
    )
    _git(root, "add", ".")
    _git(root, "commit", "-m", "Change repository head")

    regenerated = lifecycle.generate_and_qualify()

    assert regenerated.status == "validated"
    assert regenerated.approval is None
    assert regenerated.generated.repository_head_sha != first.generated.repository_head_sha


def test_stale_capability_binding_keeps_project_visible_for_regeneration(
    tmp_path: Path, monkeypatch
) -> None:
    _root, registry = _registered_unity(tmp_path, monkeypatch)
    lifecycle = ProjectToolPackLifecycle("toolpack-game", registry)
    generated = lifecycle.generate_and_qualify()
    lifecycle.approve(generated.qualification.toolpack_sha256)
    path = registry / "toolpack-game" / "toolpacks" / "generated.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["capability_packs"][0]["definition_sha256"] = "0" * 64
    path.write_text(json.dumps(payload), encoding="utf-8")

    stale = lifecycle.state()
    project = ProjectCatalog(
        exchange_root=tmp_path / "missing-exchange",
        registry_root=registry,
    ).get("toolpack-game")

    assert stale.status == "needs_generation"
    assert stale.execution_ready is False
    assert "stale" in stale.execution_blockers[0]
    assert project.project_id == "toolpack-game"
    assert project.toolpack_status == "needs_generation"
    assert lifecycle.generate_and_qualify().status == "approved"


def test_toolpack_web_generate_review_and_approve(tmp_path: Path, monkeypatch) -> None:
    _root, registry = _registered_unity(tmp_path, monkeypatch)
    monkeypatch.setenv("ONEBRIEF_PROJECTS_ROOT", str(registry))
    client = TestClient(app)

    initial = client.get("/api/projects/toolpack-game/toolpack")
    generated = client.post("/api/projects/toolpack-game/toolpack/generate")
    body = generated.json()["state"]
    approved = client.post(
        "/api/projects/toolpack-game/toolpack/approve",
        json={"toolpack_sha256": body["qualification"]["toolpack_sha256"]},
    )

    assert initial.status_code == 200
    assert initial.json()["state"]["status"] == "needs_generation"
    assert generated.status_code == 200
    assert body["status"] == "validated"
    assert approved.status_code == 200
    assert approved.json()["state"]["status"] == "approved"
    assert approved.json()["state"]["execution_ready"] is True
    assert approved.json()["state"]["execution_blockers"] == []


def test_node_toolpack_reads_fixed_scripts_without_granting_write_access(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "web"
    registry = tmp_path / "registry"
    (root / "src").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "scripts").mkdir()
    (root / "web" / "app").mkdir(parents=True)
    (root / "src" / "index.html").write_text("<!doctype html>\n", encoding="utf-8")
    (root / "tests" / "page.test.mjs").write_text("// fixed test\n", encoding="utf-8")
    (root / "scripts" / "verify.mjs").write_text("// fixed runner\n", encoding="utf-8")
    (root / "package.json").write_text(
        '{"scripts":{"test":"node scripts/verify.mjs"}}', encoding="utf-8"
    )
    (root / "web" / "package.json").write_text(
        '{"scripts":{"test":"node --test","build":"node --check app/page.js",'
        '"start":"node app/page.js"}}', encoding="utf-8"
    )
    (root / "web" / "app" / "page.js").write_text(
        "console.log('ready');\n", encoding="utf-8"
    )
    _git(root, "init")
    _git(root, "config", "user.name", "OneBrief Test")
    _git(root, "config", "user.email", "onebrief@example.invalid")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "Create Node project")
    payload = json.dumps({
        "schema_version": "onebrief-project-v1",
        "project_id": "toolpack-web",
        "name": "ToolPack Web",
        "project_type": "node_application",
        "project_root": str(root.resolve()),
        "canonical_goal": "Safely improve the website.",
        "summary": "Test Node project.",
    }).encode("utf-8")
    (root / MANIFEST_NAME).write_bytes(payload)
    ExternalProjectImporter(registry).import_bytes(payload)

    state = ProjectToolPackLifecycle("toolpack-web", registry).generate_and_qualify()

    assert "scripts/" in state.generated.allowed_read_prefixes
    assert "scripts/" not in state.generated.allowed_write_prefixes
    assert "tests/" in state.generated.allowed_read_prefixes
    assert "tests/" not in state.generated.allowed_write_prefixes
    parameters = {
        item.parameter for item in state.generated.adapters
        if item.adapter_id.value == "node_script"
    }
    assert {"test", "web::test", "web::build"}.issubset(parameters)
