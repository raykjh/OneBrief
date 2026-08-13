import hashlib
import json
import os
import subprocess
import zipfile
from pathlib import Path

import pytest

from onebrief.jobs import JobStatus, build_result_package, create_job, verify_input_snapshot
from onebrief.producer import estimate_budget
from onebrief.project_import import ExternalProjectImporter, MANIFEST_NAME
from onebrief.project_snapshot import (
    MAX_SNAPSHOT_BYTES,
    MAX_SNAPSHOT_FILE_BYTES,
    SNAPSHOT_ARCHIVE,
    SNAPSHOT_MANIFEST,
    create_project_snapshot,
    record_local_project_provenance,
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
    (root / "reports").mkdir()
    (root / "src" / "sample" / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / "tests" / "test_sample.py").write_text(
        "from sample import VALUE\n\ndef test_value():\n    assert VALUE == 1\n",
        encoding="utf-8",
    )
    (root / "docs" / "TASK.md").write_text("Keep the sample tested.\n", encoding="utf-8")
    (root / "reports" / "baseline.json").write_text(
        '{"status": "approved-fixture"}\n', encoding="utf-8"
    )
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
    # Deterministic adapters receive the complete committed tree even though
    # maker context remains restricted to the approved read prefixes.
    assert "repository/reports/baseline.json" in names
    committed = subprocess.run(
        ["git", "show", "HEAD:src/sample/__init__.py"], cwd=root,
        check=True, capture_output=True,
    ).stdout
    with zipfile.ZipFile(inputs / SNAPSHOT_ARCHIVE) as archive:
        assert archive.read("repository/src/sample/__init__.py") == committed
    record = next(item for item in manifest.files if item.path == "src/sample/__init__.py")
    assert record.sha256 == hashlib.sha256(committed).hexdigest()
    assert manifest.source_head_sha == _git(root, "rev-parse", "HEAD")

    original_registry = os.environ["ONEBRIEF_PROJECTS_ROOT"]
    restored = restore_project_snapshot(tmp_path / "job", "snapshot-python")

    assert restored is not None
    assert os.environ["ONEBRIEF_PROJECTS_ROOT"] == original_registry
    assert _git(restored, "config", "--get", "core.longpaths") == "true"
    assert _git(restored, "config", "--get", "core.autocrlf") == "false"
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


def test_snapshot_includes_the_registration_manifest_without_committing_it(
    tmp_path: Path, monkeypatch
) -> None:
    root, registry = _approved_python_project(tmp_path, monkeypatch)
    _git(root, "rm", "--cached", MANIFEST_NAME)
    _git(root, "commit", "-m", "Keep OneBrief registration metadata outside source history")
    lifecycle = ProjectToolPackLifecycle("snapshot-python", registry)
    state = lifecycle.generate_and_qualify()
    lifecycle.approve(state.qualification.toolpack_sha256)

    inputs = tmp_path / "job" / "inputs"
    manifest = create_project_snapshot("snapshot-python", inputs)

    assert any(item.path == MANIFEST_NAME for item in manifest.files)
    restored = restore_project_snapshot(tmp_path / "job", "snapshot-python")
    assert restored is not None
    assert (restored / MANIFEST_NAME).is_file()


def test_snapshot_bounds_cover_a_medium_unity_client_without_becoming_unbounded(
    tmp_path: Path, monkeypatch
) -> None:
    root, registry = _approved_python_project(tmp_path, monkeypatch)
    fixture = root / "reports" / "medium-runtime-asset.bin"
    fixture.write_bytes(b"0" * 6_000_000)
    _git(root, "add", "reports/medium-runtime-asset.bin")
    _git(root, "commit", "-m", "Add a medium runtime asset")
    lifecycle = ProjectToolPackLifecycle("snapshot-python", registry)
    state = lifecycle.generate_and_qualify()
    lifecycle.approve(state.qualification.toolpack_sha256)

    manifest = create_project_snapshot("snapshot-python", tmp_path / "job" / "inputs")

    record = next(item for item in manifest.files if item.path == fixture.relative_to(root).as_posix())
    assert record.size_bytes == 6_000_000
    assert 16_500_000 <= MAX_SNAPSHOT_FILE_BYTES <= 25_000_000
    assert 300_000_000 <= MAX_SNAPSHOT_BYTES <= 400_000_000


def test_snapshot_tampering_is_rejected_before_restore(tmp_path: Path, monkeypatch) -> None:
    _approved_python_project(tmp_path, monkeypatch)
    inputs = tmp_path / "job" / "inputs"
    create_project_snapshot("snapshot-python", inputs)
    archive = inputs / SNAPSHOT_ARCHIVE
    archive.write_bytes(archive.read_bytes() + b"tampered")

    with pytest.raises(RuntimeError, match="archive hash or size changed"):
        restore_project_snapshot(tmp_path / "job", "snapshot-python")


def test_snapshot_restore_streams_entries_without_whole_file_reads(
    tmp_path: Path, monkeypatch
) -> None:
    _approved_python_project(tmp_path, monkeypatch)
    inputs = tmp_path / "job" / "inputs"
    create_project_snapshot("snapshot-python", inputs)

    def reject_whole_entry_read(*_args, **_kwargs):
        raise AssertionError("snapshot restore must stream zip entries")

    monkeypatch.setattr(zipfile.ZipFile, "read", reject_whole_entry_read)

    restored = restore_project_snapshot(tmp_path / "job", "snapshot-python")

    assert restored is not None
    assert (restored / "src" / "sample" / "__init__.py").is_file()


def test_snapshot_manifest_is_a_standalone_immutable_job_input(tmp_path: Path, monkeypatch) -> None:
    _approved_python_project(tmp_path, monkeypatch)
    inputs = tmp_path / "job" / "inputs"
    create_project_snapshot("snapshot-python", inputs)

    payload = json.loads((inputs / SNAPSHOT_MANIFEST).read_text(encoding="utf-8"))

    assert payload["schema_version"] == "onebrief-project-snapshot-v1"
    assert payload["project_id"] == "snapshot-python"
    assert payload["files"]


def test_cloud_image_contains_the_approved_web_verification_runtime() -> None:
    dockerfile = (Path(__file__).parents[1] / "Dockerfile").read_text(encoding="utf-8")

    for package in (
        "FROM node:24-bookworm-slim AS node_runtime",
        "COPY --from=node_runtime /usr/local/bin/node",
        "chromium",
        "fonts-noto-cjk",
        "npm-cli.js",
    ):
        assert package in dockerfile


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

    private_git = job / "work" / "project_snapshot" / "repository" / ".git" / "config"
    private_git.parent.mkdir(parents=True)
    private_git.write_text("synthetic git metadata", encoding="utf-8")
    milestone_private = job / "work" / "milestone_workspace" / "repository" / ".git" / "config"
    milestone_private.parent.mkdir(parents=True)
    milestone_private.write_text("ephemeral milestone clone", encoding="utf-8")
    milestone_receipt = job / "work" / "milestone_state" / "receipt.json"
    milestone_receipt.parent.mkdir(parents=True)
    milestone_receipt.write_text('{"status":"passed"}', encoding="utf-8")
    evidence = job / "work" / "project_snapshot" / "restore_evidence.json"
    evidence.write_text('{"status":"verified_and_approved"}', encoding="utf-8")
    package, _digest = build_result_package(job, status=JobStatus.COMPLETE, attempt=1)

    assert not (package / "artifacts" / "project_snapshot" / "repository").exists()
    assert not (package / "artifacts" / "milestone_workspace" / "repository").exists()
    assert (package / "artifacts" / "project_snapshot" / "restore_evidence.json").is_file()
    assert (package / "artifacts" / "milestone_state" / "receipt.json").is_file()


def test_local_project_job_uses_exact_provenance_without_copying_large_assets(
    tmp_path: Path, monkeypatch
) -> None:
    root, _registry = _approved_python_project(tmp_path, monkeypatch)
    source = InternalSource(
        name="task.md",
        priority=SourcePriority.MANDATORY,
        requirement_keys=["task_contract"],
        content="Improve the approved source in an isolated local clone.",
    )
    intake = IntakeRequest(
        goal="Improve the approved Python project locally.",
        output_target=OutputTarget.EXISTING_PROJECT,
        existing_project_id="snapshot-python",
        internal_sources=[source],
        toolpack_ids=[ToolPackId.PROJECT_DEVELOPMENT],
    )
    requirements = RequirementsAnalysis(
        supported=True,
        support_reason="The local project and validation contract are available.",
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
        embed_project_snapshot=False,
    )

    assert not (job / "inputs" / SNAPSHOT_ARCHIVE).exists()
    evidence_path = record_local_project_provenance(job, "snapshot-python")
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert evidence["mode"] == "local_verified_clone"
    assert evidence["source_head_sha"] == _git(root, "rev-parse", "HEAD")
    assert evidence["status"] == "verified_and_approved"
