from __future__ import annotations

import json
from pathlib import Path

import pytest

from onebrief.budget_guard import BudgetStore
from onebrief.cloud_continuation import (
    _targeted_repair_estimate,
    create_budget_preserving_continuation,
)
from onebrief.jobs import JobStatus, JobStore, create_job
from onebrief.lineage import build_run_lineage
from onebrief.team_planning import TeamPlan
from onebrief.schemas import (
    BudgetEnvelope,
    BudgetStatus,
    ExecutionPhase,
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


class SupplementalReuseSource:
    def download_reusable_artifacts(self, destination: Path) -> list[str]:
        destination.mkdir(parents=True, exist_ok=True)
        (destination / "development_verification_failure.txt").write_text(
            "latest failure", "utf-8"
        )
        return ["development_verification_failure.txt"]


class MilestoneReuseSource:
    def download_reusable_artifacts(self, destination: Path) -> list[str]:
        milestone = destination / "milestones" / "M01"
        milestone.mkdir(parents=True, exist_ok=True)
        (milestone / "code_change_set.json").write_text('{"changes":[]}', "utf-8")
        return ["milestones/M01/code_change_set.json"]


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
        price_card_version="google-agent-platform-global-standard-search-2026-08-12",
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
        supplemental_reusable_source=SupplementalReuseSource(),
        reusable_team_plan=minimal_team_plan("parent-job"),
    )

    parent_ledger = BudgetStore(source_dir / "run").read()
    child_ledger = BudgetStore(child / "run").read()
    assert actual == 0
    assert child_ledger.approval.approved_usd_micros == parent_ledger.approval.approved_usd_micros
    assert approved == parent_ledger.approval.approved_usd_micros / 1_000_000
    assert reused == (
        "analysis.json",
        "development_verification_failure.txt",
        "team_plan.json",
    )
    assert (child / "work" / "development_verification_failure.txt").read_text(
        "utf-8"
    ) == "latest failure"
    manifest = json.loads((child / "work" / "continuation_manifest.json").read_text("utf-8"))
    assert manifest["source_job_id"] == record.job_id
    assert manifest["aggregate_approval_ceiling_usd"] == approved
    assert manifest["ancestor_lineage"]["job_ids"] == [record.job_id]
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
    child_lineage = build_run_lineage(child)
    assert child_lineage.root_job_id == record.job_id
    assert child_lineage.job_ids == [record.job_id, child_id]
    assert child_lineage.depth == 1


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


def test_reverification_marker_reaches_active_milestone_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("onebrief.jobs.create_project_snapshot", lambda *_args: None)
    source = InternalSource(
        name="rules", priority=SourcePriority.MANDATORY, content="truth", size_bytes=5
    )
    source_dir = create_job(
        jobs_dir=tmp_path / "source-jobs",
        intake=_intake(source), requirements=_requirements(), sources=[source],
        estimate=_estimate(), approved_usd=0.10,
    )
    JobStore(source_dir).finish(JobStatus.FAILED, stage="test", message="expected")

    child, *_ = create_budget_preserving_continuation(
        source_job_dir=source_dir,
        source_job_uri="gs://bucket/jobs/source",
        jobs_dir=tmp_path / "children",
        reusable_source=MilestoneReuseSource(),
        reverify_existing_candidate=True,
    )

    for marker in ("reverify_existing_candidate.json", "continuation_manifest.json"):
        assert (child / "work" / marker).is_file()
        assert (child / "work" / "milestones" / "M01" / marker).is_file()


def test_authorization_gate_can_resume_under_explicit_remaining_approval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("onebrief.jobs.create_project_snapshot", lambda *_args: None)
    source = InternalSource(
        name="rules", priority=SourcePriority.MANDATORY, content="truth", size_bytes=5
    )
    source_dir = create_job(
        jobs_dir=tmp_path / "source-jobs",
        intake=_intake(source),
        requirements=_requirements(),
        sources=[source],
        estimate=_estimate(),
        approved_usd=0.10,
    )
    JobStore(source_dir).finish(
        JobStatus.NEEDS_AUTHORIZATION,
        stage="authorization_gate",
        message="A dynamic repair stage needs the existing approved model binding.",
    )

    child, _actual, approved, _reused = create_budget_preserving_continuation(
        source_job_dir=source_dir,
        source_job_uri="gs://bucket/jobs/source",
        jobs_dir=tmp_path / "children",
        reusable_source=ReuseSource(),
        explicit_child_approval_usd=0.05,
    )

    assert approved == 0.05
    assert JobStore(child).read().status == JobStatus.QUEUED


def test_explicit_reauthorization_uses_only_new_approval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("onebrief.jobs.create_project_snapshot", lambda *_args: None)
    source = InternalSource(
        name="rules", priority=SourcePriority.MANDATORY,
        content="truth", size_bytes=5,
    )
    estimate = _estimate()
    source_dir = create_job(
        jobs_dir=tmp_path / "source-jobs",
        intake=_intake(source),
        requirements=_requirements(),
        sources=[source],
        estimate=estimate,
        approved_usd=estimate.recommended_cost_usd,
    )
    JobStore(source_dir).finish(
        JobStatus.FAILED, stage="test", message="expected failure"
    )

    child, _actual, approved, _reused = create_budget_preserving_continuation(
        source_job_dir=source_dir,
        source_job_uri="gs://bucket/jobs/source",
        jobs_dir=tmp_path / "children",
        reusable_source=ReuseSource(),
        explicit_child_approval_usd=0.15,
    )

    assert approved == 0.15
    assert BudgetStore(child / "run").read().approval.approved_usd_micros == 150_000
    manifest = json.loads((child / "work" / "continuation_manifest.json").read_text("utf-8"))
    assert manifest["authorization_kind"] == "explicit_additional_user_approval"
    assert manifest["aggregate_approval_ceiling_usd"] == 0.15
    assert manifest["source_unused_usd_abandoned"] == 0.1


def test_research_reentry_request_is_bound_to_the_new_continuation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("onebrief.jobs.create_project_snapshot", lambda *_args: None)
    source = InternalSource(
        name="rules", priority=SourcePriority.MANDATORY, content="truth", size_bytes=5
    )
    source_dir = create_job(
        jobs_dir=tmp_path / "source-jobs",
        intake=_intake(source),
        requirements=_requirements(),
        sources=[source],
        estimate=_estimate(),
        approved_usd=0.10,
    )
    JobStore(source_dir).finish(JobStatus.PARTIAL, stage="verification", message="evidence gap")

    class ResearchReuseSource:
        def download_reusable_artifacts(self, destination: Path) -> list[str]:
            destination.mkdir(parents=True, exist_ok=True)
            (destination / "public_research.json").write_text('{"sources":[]}', "utf-8")
            (destination / "analysis.json").write_text('{"stale":true}', "utf-8")
            (destination / "draft_r0.json").write_text('{"stale":true}', "utf-8")
            return ["public_research.json", "analysis.json", "draft_r0.json"]

    child, _actual, _approved, reused = create_budget_preserving_continuation(
        source_job_dir=source_dir,
        source_job_uri="gs://bucket/jobs/source",
        jobs_dir=tmp_path / "children",
        reusable_source=ResearchReuseSource(),
        explicit_child_approval_usd=0.15,
        research_reentry=True,
        research_blocking_issues=["Three comparison rows lack direct URLs."],
    )

    request = json.loads(
        (child / "work" / "research_reentry_request.json").read_text("utf-8")
    )
    manifest = json.loads((child / "work" / "continuation_manifest.json").read_text("utf-8"))
    assert request["max_refinement_calls"] == 2
    assert request["blocking_issues"] == ["Three comparison rows lack direct URLs."]
    assert manifest["research_reentry"] is True
    assert (child / "work" / "public_research.json").is_file()
    assert not (child / "work" / "analysis.json").exists()
    assert not (child / "work" / "draft_r0.json").exists()
    assert reused == ("public_research.json",)


def test_targeted_repair_estimate_charges_only_remaining_work() -> None:
    base = _estimate()
    template = base.stages[0]
    estimate = base.model_copy(update={
        "stages": [
            template.model_copy(update={"stage": stage})
            for stage in (
                "long_form_draft",
                "independent_verification",
                "final_approval",
            )
        ],
    })

    targeted = _targeted_repair_estimate(estimate)

    assert [item.stage for item in targeted.stages] == [
        "long_form_draft", "independent_verification", "final_approval"
    ]
    assert targeted.minimum_cost_usd < 0.25
    calls = {item.stage: (item.minimum_calls, item.maximum_calls) for item in targeted.stages}
    assert calls == {
        "long_form_draft": (1, 2),
        "independent_verification": (1, 1),
        "final_approval": (1, 1),
    }
    assert targeted.maximum_cost_usd > targeted.recommended_cost_usd
    phase_budgets = {item.phase: item for item in targeted.phase_budgets}
    assert {
        ExecutionPhase.PRODUCT_IMPLEMENTATION,
        ExecutionPhase.EVIDENCE_CONSTRUCTION,
        ExecutionPhase.FINAL_VERIFICATION,
    } == set(phase_budgets)
    assert phase_budgets[ExecutionPhase.EVIDENCE_CONSTRUCTION].minimum_cost_usd == 0
    assert phase_budgets[ExecutionPhase.EVIDENCE_CONSTRUCTION].recommended_cost_usd == 0
    assert phase_budgets[ExecutionPhase.EVIDENCE_CONSTRUCTION].maximum_cost_usd > 0
    assert phase_budgets[ExecutionPhase.PRODUCT_IMPLEMENTATION].max_ai_repair_calls == 2
    assert phase_budgets[ExecutionPhase.EVIDENCE_CONSTRUCTION].max_ai_repair_calls == 2

    low_cost = _targeted_repair_estimate(estimate, low_cost_models=True)

    assert all(item.model == "gemini-3.5-flash-lite" for item in low_cost.stages)
    assert low_cost.minimum_cost_usd < 0.098586
