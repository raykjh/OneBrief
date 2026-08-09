import json
import subprocess
import zipfile
from pathlib import Path

import pytest

from onebrief.jobs import create_job, verify_input_snapshot
from onebrief.producer import estimate_budget
from onebrief.project_import import ExternalProjectImporter, MANIFEST_NAME
from onebrief.project_snapshot import (
    SNAPSHOT_ARCHIVE,
    SNAPSHOT_MANIFEST,
    create_project_snapshot,
    restore_project_snapshot,
)
from onebrief.toolpack_lifecycle import ProjectToolPackLifecycle
from onebrief.schemas import (
    IntakeRequest,
    InternalSource,
    OutputTarget,
    RequirementsAnalysis,
    SourcePriority,
    ToolPackId,
)


def _git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True,
        text=True, encoding="utf-8",
    )
    return completed.stdout.strip()


def _approved_python_project(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    root = tmp_path / "project"
    registry = tmp_path / "source-registry"
    (root / "src" / "sample").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "docs").mkdir()
    (root / "src" / "sample" / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / "tests" / "test_sample.py").write_text(
        "from sample import VALUE\n\ndef test_value():\n    assert VALUE == 1\n",
        encoding="utf-8",
    )
    (root / "docs" / "TASK.md").write_text("Keep the sample tested.\n", encoding="utf-8")
    (root / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\npythonpath = ['src']\ntestpaths = ['tests']\n",
        encoding="utf-8",
    )
    manifest = {
        "schema_version": "onebrief-project-v1",
        "project_id": "snapshot-python",
        "name": "Snapshot Python",
        "project_type": "python_library",
        "project_root": str(root.resolve()),
        "canonical_goal": "Keep the sample tested.",
        "summary": "Remote snapshot fixture.",
        "authoritative_documents": ["docs/TASK.md"],
    }
    (root / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    _git(root, "init")
    _git(root, "config", "user.name", "OneBrief Test")
    _git(root, "config", "user.email", "onebrief@example.invalid")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "Create snapshot fixture")
    monkeypatch.setenv("ONEBRIEF_PROJECTS_ROOT", str(registry))
    ExternalProjectImporter(registry).import_bytes((root / MANIFEST_NAME).read_bytes())
    lifecycle = ProjectToolPackLifecycle("snapshot-python", registry)
    state = lifecycle.generate_and_qualify()
    lifecycle.approve(state.qualification.toolpack_sha256)
    return root, registry


def test_snapshot_restores_only_current_approved_tree_and_rebuilds_toolpack(
    tmp_path: Path, monkeypatch
) -> None:
    root, _registry = _approved_python_project(tmp_path, monkeypatch)
    inputs = tmp_path / "job" / "inputs"

    manifest = create_project_snapshot("snapshot-python", inputs)

    with zipfile.ZipFile(inputs / SNAPSHOT_ARCHIVE) as archive:
        names = set(archive.namelist())
    assert "repository/.git/config" not in names
    assert "repository/src/sample/__init__.py" in names
    assert manifest.source_head_sha == _git(root, "rev-parse", "HEAD")

    restored = restore_project_snapshot(tmp_path / "job", "snapshot-python")

    assert restored is not None
    assert (restored / "src" / "sample" / "__init__.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    evidence = json.loads(
        (tmp_path / "job" / "work" / "project_snapshot" / "restore_evidence.json").read_text(
            encoding="utf-8"
        )
    )
    assert evidence["status"] == "verified_and_approved"
    assert ProjectToolPackLifecycle(
        "snapshot-python", tmp_path / "job" / "work" / "project_snapshot" / "registry"
    ).state().execution_ready


def test_snapshot_tampering_is_rejected_before_restore(tmp_path: Path, monkeypatch) -> None:
    _approved_python_project(tmp_path, monkeypatch)
    inputs = tmp_path / "job" / "inputs"
    create_project_snapshot("snapshot-python", inputs)
    archive = inputs / SNAPSHOT_ARCHIVE
    archive.write_bytes(archive.read_bytes() + b"tampered")

    with pytest.raises(RuntimeError, match="archive hash or size changed"):
        restore_project_snapshot(tmp_path / "job", "snapshot-python")


def test_snapshot_manifest_is_a_standalone_immutable_job_input(tmp_path: Path, monkeypatch) -> None:
    _approved_python_project(tmp_path, monkeypatch)
    inputs = tmp_path / "job" / "inputs"
    create_project_snapshot("snapshot-python", inputs)

    payload = json.loads((inputs / SNAPSHOT_MANIFEST).read_text(encoding="utf-8"))

    assert payload["schema_version"] == "onebrief-project-snapshot-v1"
    assert payload["project_id"] == "snapshot-python"
    assert payload["files"]


def test_project_development_job_embeds_snapshot_in_immutable_inputs(
    tmp_path: Path, monkeypatch
) -> None:
    _approved_python_project(tmp_path, monkeypatch)
    source = InternalSource(
        name="task.md",
        priority=SourcePriority.MANDATORY,
        requirement_keys=["task_contract"],
        content="Keep the public API and pass the tests.",
    )
    intake = IntakeRequest(
        goal="Improve the approved Python project.",
        output_target=OutputTarget.EXISTING_PROJECT,
        existing_project_id="snapshot-python",
        internal_sources=[source],
        toolpack_ids=[ToolPackId.PROJECT_DEVELOPMENT],
    )
    requirements = RequirementsAnalysis(
        supported=True,
        support_reason="The task and deterministic tests are available.",
        normalized_goal=intake.goal,
        deliverables=["Verified source patch"],
        mandatory_information=[],
        optional_information=[],
        acceptance_criteria=["The approved tests pass."],
        assumptions=[],
        consolidated_questions=[],
        ready_for_estimate=True,
    )
    estimate = estimate_budget(intake, requirements)

    job = create_job(
        jobs_dir=tmp_path / "jobs",
        intake=intake,
        requirements=requirements,
        sources=[source],
        estimate=estimate,
        approved_usd=estimate.recommended_approval_usd,
    )

    assert (job / "inputs" / SNAPSHOT_ARCHIVE).is_file()
    assert (job / "inputs" / SNAPSHOT_MANIFEST).is_file()
    verify_input_snapshot(job)
