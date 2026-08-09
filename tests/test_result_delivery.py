import json
from pathlib import Path

import pytest

from onebrief.development_toolpack import DevelopmentRun
from onebrief.project_catalog import RegisteredProject
from onebrief.result_delivery import ExchangePreviewManager


def _project(tmp_path: Path) -> RegisteredProject:
    return RegisteredProject(
        project_id="sample-project",
        name="Sample",
        canonical_goal="Improve the sample project.",
        summary="Sample project.",
        root_path=str(tmp_path),
        project_type="node_application",
        worktree_status="clean",
        ready_for_isolated_edit=True,
        toolpack_id=None,
        origin="imported",
    )


def _run() -> DevelopmentRun:
    return DevelopmentRun(
        status="verified",
        repository_name="repository",
        base_head_sha="b" * 40,
        summary="Verified snapshot result.",
        changed_paths=["src/index.html"],
        patch_path="development/changes.patch",
        commands=[],
        safety_boundary=["isolated verification"],
    )


def test_preview_binds_snapshot_result_to_verified_source_head(tmp_path: Path) -> None:
    package = tmp_path / "package"
    evidence = package / "artifacts" / "project_snapshot" / "restore_evidence.json"
    evidence.parent.mkdir(parents=True)
    evidence.write_text(json.dumps({
        "project_id": "sample-project",
        "source_head_sha": "a" * 40,
        "restored_head_sha": "b" * 40,
        "status": "verified_and_approved",
    }), encoding="utf-8")
    manager = ExchangePreviewManager(_project(tmp_path), tmp_path / "jobs")

    rebound = manager._bind_run_to_source_head(package, _run())

    assert rebound.base_head_sha == "a" * 40


def test_preview_rejects_mismatched_snapshot_provenance(tmp_path: Path) -> None:
    package = tmp_path / "package"
    evidence = package / "artifacts" / "project_snapshot" / "restore_evidence.json"
    evidence.parent.mkdir(parents=True)
    evidence.write_text(json.dumps({
        "project_id": "another-project",
        "source_head_sha": "a" * 40,
        "status": "verified_and_approved",
    }), encoding="utf-8")
    manager = ExchangePreviewManager(_project(tmp_path), tmp_path / "jobs")

    with pytest.raises(RuntimeError, match="provenance is invalid"):
        manager._bind_run_to_source_head(package, _run())
