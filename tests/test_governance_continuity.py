from __future__ import annotations

import json
from pathlib import Path

from onebrief.governance import StopReason, build_decision_request, external_apply_decision
from onebrief.jobs import create_job
from onebrief.lineage import build_run_lineage
from onebrief.preparation import build_preparation_plan
from onebrief.producer import estimate_budget
from onebrief.resume_capsule import build_resume_capsule
from onebrief.schemas import IntakeRequest, RequirementsAnalysis


def requirements() -> RequirementsAnalysis:
    return RequirementsAnalysis(
        supported=True,
        support_reason="Supported.",
        normalized_goal="Produce a verified artifact.",
        deliverables=["Artifact"],
        mandatory_information=[],
        optional_information=[],
        acceptance_criteria=["The artifact is independently verified."],
        assumptions=[],
        consolidated_questions=[],
        ready_for_estimate=True,
    )


def test_decision_request_is_deterministic_and_distinguishes_stop_reasons() -> None:
    first = build_decision_request(
        status="failed", stage="safe_apply", message="stale source changed after approval"
    )
    repeated = build_decision_request(
        status="failed", stage="safe_apply", message="stale source changed after approval"
    )
    conflict = build_decision_request(
        status="failed", stage="merge", message="conflict detected"
    )

    assert first is not None and repeated is not None and conflict is not None
    assert first.request_id == repeated.request_id
    assert first.reason == StopReason.STALE_REVISION
    assert conflict.reason == StopReason.CONFLICT_DETECTED
    assert first.autonomous_retry_allowed is False
    apply = external_apply_decision(
        authorization_sha256="a" * 64,
        base_source_revision="b" * 40,
        project_id="sample",
    )
    assert apply.reason == StopReason.EXTERNAL_SIDE_EFFECT_APPROVAL
    assert apply.autonomous_retry_allowed is False


def test_preparation_binds_actor_expiry_contract_budget_and_permissions() -> None:
    intake = IntakeRequest(goal="Produce a verified artifact.", public_research_allowed=True)
    req = requirements()
    budget = estimate_budget(intake, req)
    plan = build_preparation_plan(
        intake, req, budget,
        issued_at="2026-08-11T00:00:00+00:00",
        actor_id="owner:local",
    )

    assert plan is not None
    assert plan.authorization_envelope.actor_id == "owner:local"
    assert plan.authorization_envelope.maximum_budget_usd == budget.maximum_cost_usd
    assert plan.authorization_envelope.completion_contract_sha256
    changed = build_preparation_plan(
        intake, req, budget,
        issued_at="2026-08-11T00:00:00+00:00",
        actor_id="owner:other",
    )
    assert changed is not None
    assert changed.authorization_sha256 != plan.authorization_sha256


def test_lineage_and_l0_capsule_are_deterministic(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("onebrief.jobs.create_project_snapshot", lambda *_args: None)
    intake = IntakeRequest(goal="Produce a verified artifact.", public_research_allowed=True)
    req = requirements()
    estimate = estimate_budget(intake, req)
    job = create_job(
        jobs_dir=tmp_path,
        intake=intake,
        requirements=req,
        sources=[],
        estimate=estimate,
        approved_usd=estimate.recommended_approval_usd,
    )

    lineage = build_run_lineage(job)
    first = build_resume_capsule(
        job, status="needs_information", stage="verification", message="missing evidence"
    )
    second = build_resume_capsule(
        job, status="needs_information", stage="verification", message="missing evidence"
    )

    assert lineage.job_ids == [lineage.current_job_id]
    assert lineage.depth == 0
    assert first.capsule_digest == second.capsule_digest
    assert first.authorization_status == "requires_user_decision"
    assert first.next_action
