import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from onebrief.generic_development_toolpack import (
    ApprovedProjectDevelopmentToolPack,
    ProjectCodeChangeSet,
)
from onebrief.development_toolpack import DevelopmentCommandResult
from onebrief.project_import import ExternalProjectImporter, MANIFEST_NAME
from onebrief.schemas import ToolPackId
from onebrief.toolpack_lifecycle import ProjectToolPackLifecycle
from onebrief.toolpacks import execute_toolpacks


def _git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True,
        text=True, encoding="utf-8",
    )
    return completed.stdout.strip()


def _approved_node_project(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "node-project"
    registry = tmp_path / "registry"
    (root / "src").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "src" / "app.js").write_text("export const answer = 40;\n", encoding="utf-8")
    (root / "src" / "localization-manager.js").write_text(
        "export const language = 'en';\n", encoding="utf-8"
    )

    (root / "tests" / "app.test.js").write_text(
        "import test from 'node:test';\n"
        "import assert from 'node:assert/strict';\n"
        "import { answer } from '../src/app.js';\n"
        "test('answer remains valid', () => assert.ok(answer >= 40));\n",
        encoding="utf-8",
    )
    (root / "package.json").write_text(json.dumps({
        "name": "isolated-fixture",
        "version": "1.0.0",
        "type": "module",
        "scripts": {"test": "node --test", "build": "node --check src/app.js"},
    }), encoding="utf-8")
    _git(root, "init")
    _git(root, "config", "user.name", "OneBrief Test")
    _git(root, "config", "user.email", "onebrief@example.invalid")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "Create fixture")
    manifest = {
        "schema_version": "onebrief-project-v1",
        "project_id": "generic-node",
        "name": "Generic Node",
        "project_type": "node_app",
        "project_root": str(root.resolve()),
        "canonical_goal": "Safely improve this Node application.",
        "summary": "Generic isolated development fixture.",
        "authoritative_documents": ["package.json"],
    }
    payload = json.dumps(manifest, indent=2).encode("utf-8")
    (root / MANIFEST_NAME).write_bytes(payload)
    ExternalProjectImporter(registry).import_bytes(payload)
    lifecycle = ProjectToolPackLifecycle("generic-node", registry)
    state = lifecycle.generate_and_qualify()
    assert state.qualification is not None
    approved = lifecycle.approve(state.qualification.toolpack_sha256)
    assert approved.execution_ready
    return root, registry


@pytest.mark.skipif(shutil.which("npm.cmd" if __import__("os").name == "nt" else "npm") is None, reason="npm unavailable")
def test_approved_generic_runner_edits_only_clone_and_returns_verified_patch(tmp_path: Path) -> None:
    root, registry = _approved_node_project(tmp_path)
    original = (root / "src" / "app.js").read_bytes()
    committed = subprocess.run(
        ["git", "show", "HEAD:src/app.js"], cwd=root, check=True, capture_output=True
    ).stdout
    base_sha = hashlib.sha256(committed).hexdigest()
    change_set = ProjectCodeChangeSet(
        summary="Update the tested answer.",
        changes=[{
            "path": "src/app.js",
            "base_sha256": base_sha,
            "content": "export const answer = 42;\n",
            "reason": "Implement the approved fixture change.",
        }],
    )
    output = tmp_path / "result" / "development"

    run = ApprovedProjectDevelopmentToolPack(
        "generic-node", registry
    ).apply_and_verify(change_set, output)

    assert run.status == "verified"
    assert {item.command_id for item in run.commands} == {"node_test", "node_build"}
    assert (root / "src" / "app.js").read_bytes() == original
    assert "answer = 42" in (output / "changed_files" / "src" / "app.js").read_text(encoding="utf-8")
    assert "src/app.js" in (output / "changes.patch").read_text(encoding="utf-8")


def test_generic_runner_rejects_unapproved_path_and_stale_base(tmp_path: Path) -> None:
    root, registry = _approved_node_project(tmp_path)
    pack = ApprovedProjectDevelopmentToolPack("generic-node", registry)
    with pytest.raises(PermissionError, match="outside the approved"):
        pack.apply_and_verify(ProjectCodeChangeSet(
            summary="Unsafe edit.",
            changes=[{
                "path": "package.json", "base_sha256": None,
                "content": "{}", "reason": "Attempt policy bypass.",
            }],
        ), tmp_path / "unsafe")
    with pytest.raises(RuntimeError, match="stale or missing base hash"):
        pack.apply_and_verify(ProjectCodeChangeSet(
            summary="Stale edit.",
            changes=[{
                "path": "src/app.js", "base_sha256": "0" * 64,
                "content": "export const answer = 42;\n", "reason": "Use stale source.",
            }],
        ), tmp_path / "stale")
    assert (root / "src" / "app.js").read_text(encoding="utf-8") == "export const answer = 40;\n"

def test_generic_toolpack_execution_packages_context_and_resumes_with_editable_names(tmp_path: Path, monkeypatch) -> None:
    _root, registry = _approved_node_project(tmp_path)
    monkeypatch.setenv("ONEBRIEF_PROJECTS_ROOT", str(registry))
    output = tmp_path / "toolpacks"

    first_runs, first_sources = execute_toolpacks(
        [ToolPackId.PROJECT_DEVELOPMENT], output, "generic-node"
    )
    resumed_runs, resumed_sources = execute_toolpacks(
        [ToolPackId.PROJECT_DEVELOPMENT], output, "generic-node"
    )

    assert first_runs[0].status == "ready"
    assert resumed_runs[0].toolpack_id == ToolPackId.PROJECT_DEVELOPMENT
    assert any(item.name == "project-source/src/app.js" for item in first_sources)
    assert any(item.name == "project-source/src/app.js" for item in resumed_sources)


def test_generic_inspection_prioritizes_goal_relevant_context(tmp_path: Path) -> None:
    _root, registry = _approved_node_project(tmp_path)
    inspection, sources = ApprovedProjectDevelopmentToolPack(
        "generic-node", registry
    ).inspect(tmp_path / "focused", "Add multilingual localization and language switching")

    assert inspection.context_files
    assert "localization" in inspection.context_files[0].path
    assert sources[0].name.endswith("localization-manager.js")


def test_change_set_hashes_are_bound_to_trusted_inspection(tmp_path: Path) -> None:
    _root, registry = _approved_node_project(tmp_path)
    pack = ApprovedProjectDevelopmentToolPack("generic-node", registry)
    inspection, _sources = pack.inspect(tmp_path / "inspection", "localization language")
    proposed = ProjectCodeChangeSet(
        summary="Update inspected localization and add a locale file.",
        changes=[
            {
                "path": "src/localization-manager.js",
                "base_sha256": "0" * 64,
                "content": "export const language = 'ja';\n",
                "reason": "Add a tested locale default.",
            },
            {
                "path": "src/locales.js",
                "base_sha256": "f" * 64,
                "content": "export const locales = ['en', 'ja'];\n",
                "reason": "Add supported locale metadata.",
            },
        ],
    )

    bound = pack.bind_change_set_to_inspection(proposed, inspection)

    expected = next(
        item.sha256 for item in inspection.context_files
        if item.path == "src/localization-manager.js"
    )
    assert bound.changes[0].base_sha256 == expected
    assert bound.changes[1].base_sha256 is None
    assert proposed.changes[0].base_sha256 == "0" * 64


def test_change_set_cannot_edit_existing_file_omitted_from_context(tmp_path: Path) -> None:
    _root, registry = _approved_node_project(tmp_path)
    pack = ApprovedProjectDevelopmentToolPack("generic-node", registry)
    inspection, _sources = pack.inspect(tmp_path / "inspection", "localization language")
    inspection = inspection.model_copy(update={
        "context_files": [
            item for item in inspection.context_files if item.path != "src/app.js"
        ]
    })
    proposed = ProjectCodeChangeSet(
        summary="Attempt an unseen edit.",
        changes=[{
            "path": "src/app.js",
            "base_sha256": None,
            "content": "export const answer = 42;\n",
            "reason": "Edit a file outside model context.",
        }],
    )

    with pytest.raises(PermissionError, match="not included in approved model context"):
        pack.bind_change_set_to_inspection(proposed, inspection)


def test_patch_includes_new_files_and_excludes_validator_side_effects(tmp_path: Path) -> None:
    root, registry = _approved_node_project(tmp_path)
    committed = subprocess.run(
        ["git", "show", "HEAD:src/app.js"], cwd=root, check=True, capture_output=True
    ).stdout

    def side_effect_runner(command_id: str, argv: list[str], cwd: Path, timeout: int):
        (cwd / "package.json").write_text('{"validator":"side-effect"}\n', encoding="utf-8")
        return DevelopmentCommandResult(
            command_id=command_id, argv=argv, exit_code=0,
            duration_seconds=0.01, output_tail="ok",
        )

    change_set = ProjectCodeChangeSet(
        summary="Update one file and add another.",
        changes=[
            {
                "path": "src/app.js",
                "base_sha256": hashlib.sha256(committed).hexdigest(),
                "content": "export const answer = 42;\n",
                "reason": "Update approved source.",
            },
            {
                "path": "src/new-locales.js",
                "base_sha256": None,
                "content": "export const locales = ['ja'];\n",
                "reason": "Add approved locale source.",
            },
        ],
    )

    output = tmp_path / "result" / "development"
    ApprovedProjectDevelopmentToolPack(
        "generic-node", registry, runner=side_effect_runner
    ).apply_and_verify(change_set, output)
    patch = (output / "changes.patch").read_text(encoding="utf-8")
    assert "src/new-locales.js" in patch
    assert "package.json" not in patch


def test_generic_runner_rejects_patch_whitespace_before_project_validation(tmp_path: Path) -> None:
    root, registry = _approved_node_project(tmp_path)
    committed = subprocess.run(
        ["git", "show", "HEAD:src/app.js"], cwd=root, check=True, capture_output=True
    ).stdout
    validation_called = False

    def runner(command_id: str, argv: list[str], cwd: Path, timeout: int) -> DevelopmentCommandResult:
        nonlocal validation_called
        validation_called = True
        return DevelopmentCommandResult(
            command_id=command_id, argv=argv, exit_code=0,
            duration_seconds=0.01, output_tail="ok",
        )

    change_set = ProjectCodeChangeSet(
        summary="Introduce an invalid whitespace-only code line.",
        changes=[{
            "path": "src/app.js",
            "base_sha256": hashlib.sha256(committed).hexdigest(),
            "content": "export const answer = 42;  \n",
            "reason": "Exercise deterministic patch hygiene.",
        }],
    )

    with pytest.raises(RuntimeError, match="development patch hygiene failed"):
        ApprovedProjectDevelopmentToolPack(
            "generic-node", registry, runner=runner
        ).apply_and_verify(change_set, tmp_path / "whitespace")
    assert validation_called is False
