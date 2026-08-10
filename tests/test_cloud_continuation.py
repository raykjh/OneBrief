from __future__ import annotations

import json
from pathlib import Path

import pytest

from onebrief.budget_guard import BudgetStore
from onebrief.cloud_continuation import create_budget_preserving_continuation
from onebrief.jobs import JobStatus, JobStore, create_job
from onebrief.team_planning import TeamPlan
from onebrief.schemas import (
    BudgetEnvelope,
    BudgetStatus,
    IntakeRequest,
    InternalSource,
    RequirementsAnalysis,
    SourcePriority,
    StageEstimate,
)
from team_plan_support import minimal_team_plan


class ReuseSource:
    def download_reusable_artifacts(self, destination: Path) -> list[str]:
        destination.mkdir(parents=True, exist_ok=True)
        (destination / "analysis.json").write_text('{"ok":true}', "utf-8")
        return ["analysis.json"]


def _model(path: Path, model):
    return model.model_validate_json(path.read_text("utf-8"))


def _intake(source: InternalSource) -> IntakeRequest:
    return IntakeRequest(goal="Create a grounded guide.", internal_sources=[source])


def _requirements() -> RequirementsAnalysis:
    return RequirementsAnalysis(
        supported=True,
        support_reason="The supplied source supports the guide.",
        normalized_goal="Create a grounded guide.",
        deliverables=["Guide"],
        mandatory_information=[],
        optional_information=[],
        acceptance_criteria=["Every claim cites the source."],
        assumptions=[],
        consolidated_questions=[],
        ready_for_estimate=True,
    )


def _estimate() -> BudgetEnvelope:
    return BudgetEnvelope(
        price_card_version="google-agent-platform-global-standard-search-2026-08-05",
        price_source_url="https://example.test/pricing",
        endpoint="global-standard",
        estimated_source_tokens=100,
        estimated_contract_tokens=100,
        stages=[StageEstimate(
            stage="draft", model="gemini-3.5-flash",
            input_tokens_per_call=100, output_tokens_per_call=100,
            minimum_calls=1, recommended_calls=1, maximum_calls=1,
            minimum_cost_usd=0.001, recommended_cost_usd=0.002,
            maximum_cost_usd=0.003, estimated_minutes_per_call=1,
        )],
        minimum_cost_usd=0.001,
        recommended_cost_usd=0.10,
        maximum_cost_usd=0.20,
        recommended_approval_usd=0.10,
        budget_limit_usd=1.0,
        status=BudgetStatus.WITHIN_BUDGET,
        estimated_minutes_minimum=1,
        estimated_minutes_recommended=1,
        estimated_minutes_maximum=1,
        notes=[],
    )


def test_continuation_inherits_only_remaining_approval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("onebrief.jobs.create_project_snapshot", lambda *_args: None)
    source = InternalSource(name="rules", priority=SourcePriority.MANDATORY, content="truth", size_bytes=5)
    estimate = _estimate()
    source_dir = create_job(
        jobs_dir=tmp_path / "source-jobs",
        intake=_intake(source),
        requirements=_requirements(),
        sources=[source],
        estimate=estimate,
        approved_usd=estimate.recommended_cost_usd,
    )
    store = JobStore(source_dir)
    record = store.read()
    store.finish(JobStatus.FAILED, stage="test", message="expected failure")

    child, actual, approved, reused = create_budget_preserving_continuation(
        source_job_dir=source_dir,
        source_job_uri="gs://bucket/jobs/source",
        jobs_dir=tmp_path / "children",
        reusable_source=ReuseSource(),
        reusable_team_plan=minimal_team_plan("parent-job"),
    )

    parent_ledger = BudgetStore(source_dir / "run").read()
    child_ledger = BudgetStore(child / "run").read()
    assert actual == 0
    assert child_ledger.approval.approved_usd_micros == parent_ledger.approval.approved_usd_micros
    assert approved == parent_ledger.approval.approved_usd_micros / 1_000_000
    assert reused == ("analysis.json", "team_plan.json")
    manifest = json.loads((child / "work" / "continuation_manifest.json").read_text("utf-8"))
    assert manifest["source_job_id"] == record.job_id
    assert manifest["aggregate_approval_ceiling_usd"] == approved
    child_id = JobStore(child).read().job_id
    plan_path = (
        child / "work" / "workspace" / "projects" / child_id
        / "02_plan_and_teams" / "team_plan.json"
    )
    assert _model(plan_path, TeamPlan).project_id == child_id
    assert plan_path.read_text("utf-8") == json.dumps(
        _model(plan_path, TeamPlan).model_dump(mode="json"),
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ) + "\n"


def test_continuation_rejects_nonterminal_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("onebrief.jobs.create_project_snapshot", lambda *_args: None)
    source = InternalSource(name="rules", priority=SourcePriority.MANDATORY, content="truth", size_bytes=5)
    estimate = _estimate()
    source_dir = create_job(
        jobs_dir=tmp_path / "source-jobs",
        intake=_intake(source),
        requirements=_requirements(),
        sources=[source],
        estimate=estimate,
        approved_usd=estimate.recommended_cost_usd,
    )
    with pytest.raises(RuntimeError, match="terminal resumable"):
        create_budget_preserving_continuation(
            source_job_dir=source_dir,
            source_job_uri="gs://bucket/jobs/source",
            jobs_dir=tmp_path / "children",
            reusable_source=ReuseSource(),
        )
