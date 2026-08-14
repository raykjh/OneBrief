import json
from pathlib import Path

import pytest

from onebrief.project_closure import (
    CandidateState,
    ClosureFile,
    PackKind,
    PackRegistry,
    ProjectClosureManager,
    ReuseCandidate,
    ReviewState,
)
from onebrief.workspaces import WorkspaceManager


def _exchange_run(work: Path) -> None:
    evidence = work / "toolpacks" / "exchange" / "evidence" / "report.json"
    evidence.parent.mkdir(parents=True)
    evidence.write_text('{"status":"verified"}\n', encoding="utf-8")
    manifest = work / "toolpacks" / "toolpack_execution.json"
    manifest.write_text(json.dumps({
        "runs": [{
            "toolpack_id": "exchange",
            "status": "passed",
            "commands": [{"command_id": "tests", "exit_code": 0}],
            "evidence": [{"source_path": "report.json"}],
            "safety_boundary": ["read only", "no trading"],
        }]
    }), encoding="utf-8")


def test_complete_project_closes_and_registers_candidate(tmp_path: Path) -> None:
    work = tmp_path / "work"
    workspace = work / "workspace"
    project = WorkspaceManager(workspace).create_project("fx-1", "FX decision support")
    _exchange_run(work)
    report = ProjectClosureManager(workspace / "pack_registry").close(
        project_id="fx-1", project_dir=project, work_dir=work, terminal_status="complete"
    )
    assert report.reusable_candidates == ["fx-1-exchange-toolpack"]
    candidate_path = workspace / "pack_registry/candidates/fx-1-exchange-toolpack/candidate.json"
    candidate = ReuseCandidate.model_validate_json(candidate_path.read_text(encoding="utf-8"))
    assert candidate.privacy_review == ReviewState.PASSED
    assert candidate.state == CandidateState.CANDIDATE
    assert (project / "09_closure" / "closure_report.json").is_file()


def test_noncomplete_project_does_not_extract_reusable_candidate(tmp_path: Path) -> None:
    work = tmp_path / "work"
    workspace = work / "workspace"
    project = WorkspaceManager(workspace).create_project("fx-2", "FX decision support")
    _exchange_run(work)
    report = ProjectClosureManager(workspace / "pack_registry").close(
        project_id="fx-2", project_dir=project, work_dir=work, terminal_status="partial"
    )
    assert report.reusable_candidates == []


def test_promotion_requires_reviews_and_two_project_uses(tmp_path: Path) -> None:
    file = ClosureFile(
        path="source.json",
        size_bytes=2,
        sha256="44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a",
    )
    candidate = ReuseCandidate(
        candidate_id="exchange-candidate",
        project_id="fx-1",
        kind=PackKind.TOOL,
        display_name="exchange",
        source_files=[file],
        activation_conditions=["FX evidence requested"],
        capability_summary="Verified FX evidence",
        safety_boundary=["read only"],
        validation_evidence=["tests passed"],
        successful_project_uses=["fx-1"],
    )
    registry = PackRegistry(tmp_path / "registry")
    registry.register_candidate(candidate)
    with pytest.raises(ValueError, match="reviews"):
        registry.promote(candidate.candidate_id, version="1.0.0")

    approved = candidate.model_copy(update={
        "privacy_review": ReviewState.PASSED,
        "provenance_review": ReviewState.PASSED,
        "independent_review": ReviewState.PASSED,
        "successful_project_uses": ["fx-1", "fx-2"],
    })
    candidate_path = tmp_path / "registry/candidates/exchange-candidate/candidate.json"
    candidate_path.write_text(approved.model_dump_json(indent=2), encoding="utf-8")
    release = registry.promote(candidate.candidate_id, version="1.0.0")
    assert release.pack_id == "tool-exchange"
    assert (tmp_path / "registry/releases/tool-exchange/1.0.0/release.json").is_file()

