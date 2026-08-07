import json
import subprocess
from pathlib import Path

from onebrief.project_catalog import ProjectCatalog
from onebrief.project_continuity import ProjectContinuityStore
from onebrief.schemas import IntakeRequest, OutputTarget, RequirementsAnalysis


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def test_continuation_restores_goal_and_classifies_previous_work(tmp_path: Path) -> None:
    root = tmp_path / "exchange"
    root.mkdir()
    (root / "README.md").write_text("# Exchange\nLive FX analysis app.", encoding="utf-8")
    (root / "app.ts").write_text("export const ready = true;", encoding="utf-8")
    _git(root, "init")
    _git(root, "config", "user.name", "OneBrief Test")
    _git(root, "config", "user.email", "onebrief@example.invalid")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "Create exchange app")

    jobs = tmp_path / "jobs"
    job = jobs / "job-1"
    (job / "inputs").mkdir(parents=True)
    (job / "work").mkdir()
    intake = IntakeRequest(
        goal="Finish the Exchange application.",
        output_target=OutputTarget.EXISTING_PROJECT,
        existing_project_id="exchange",
    )
    requirements = RequirementsAnalysis(
        supported=True,
        support_reason="Supported.",
        normalized_goal="Complete the live FX decision-support application.",
        deliverables=["Working application"],
        mandatory_information=[],
        optional_information=[],
        acceptance_criteria=["Build and tests pass."],
        assumptions=[],
        consolidated_questions=[],
        ready_for_estimate=True,
    )
    (job / "inputs" / "intake.json").write_text(intake.model_dump_json(), encoding="utf-8")
    (job / "inputs" / "requirements.json").write_text(
        requirements.model_dump_json(), encoding="utf-8"
    )
    (job / "job.json").write_text(json.dumps({
        "job_id": "job-1",
        "status": "complete",
        "updated_at": "2026-08-06T01:00:00+00:00",
        "current_stage": "finished",
        "message": "Complete",
        "result_package": "packages/result-v001",
    }), encoding="utf-8")
    (job / "work" / "execution_graph_state.json").write_text(json.dumps({
        "nodes": {
            "public_research": {"status": "complete"},
            "long_form_draft": {"status": "complete"},
            "final_approval": {"status": "pending"},
        }
    }), encoding="utf-8")

    project = ProjectCatalog(exchange_root=root).get("exchange")
    context = ProjectContinuityStore(project, jobs).context()

    assert context.state.canonical_goal == "Finish the Exchange application."
    assert "public_research" in context.state.completed_work
    assert "final_approval" in context.state.pending_work
    assert context.state.last_run_id == "job-1"
    assert "README.md" in context.tracked_documents
    assert "app.ts" in context.tracked_code
    assert context.as_internal_source().requirement_keys == ["existing_project_continuation"]
    assert (root / ".onebrief" / "project_state.json").is_file()
    assert ProjectCatalog(exchange_root=root).get("exchange").ready_for_isolated_edit is True
