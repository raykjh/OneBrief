from pathlib import Path

import pytest

from onebrief.agent_registry import AgentType, ApprovedModel
from onebrief.budget_guard import BudgetExceeded
from onebrief.model_policy import (
    ModelBudgetStatus,
    ModelExecutionPolicy,
    ModelPolicyGateway,
    evaluate_model_budget,
    persist_model_approval,
)
from onebrief.producer import estimate_budget
from onebrief.schemas import IntakeRequest, InternalSource, RequirementsAnalysis, SourcePriority

from team_plan_support import minimal_team_plan


def _requirements() -> RequirementsAnalysis:
    return RequirementsAnalysis(
        supported=True,
        support_reason="The supplied source is sufficient.",
        normalized_goal="Create a grounded guide.",
        deliverables=["Guide"],
        mandatory_information=[],
        optional_information=[],
        acceptance_criteria=["Every claim cites evidence."],
        assumptions=[],
        consolidated_questions=[],
        ready_for_estimate=True,
    )


def _estimate():
    source = InternalSource(
        name="policy.md", priority=SourcePriority.MANDATORY, content="The approved rule."
    )
    intake = IntakeRequest(goal="Create a grounded guide.")
    return estimate_budget(intake.model_copy(update={"internal_sources": [source]}), _requirements())


def test_producer_reprices_owner_model_choices_and_builds_policy() -> None:
    estimate = _estimate()
    plan = minimal_team_plan("job-models")
    analyst = next(member for member in plan.members if member.agent_type == AgentType.ANALYST)
    analyst.model = ApprovedModel.GEMINI_3_5_FLASH_LITE
    analyst.model_selection_reason = "Bounded extraction can use the lower-cost approved model."

    decision, policy = evaluate_model_budget(
        estimate, plan, approved_usd=estimate.recommended_approval_usd
    )

    assert decision.status == ModelBudgetStatus.APPROVED
    assert policy.stage_models["evidence_analysis"] == ApprovedModel.GEMINI_3_5_FLASH_LITE
    selected = next(stage for stage in decision.selected_stages if stage.stage == "evidence_analysis")
    original = next(stage for stage in estimate.stages if stage.stage == "evidence_analysis")
    assert selected.recommended_cost_usd < original.recommended_cost_usd
    assert policy.stage_models["team_planning"] == ApprovedModel.GEMINI_3_1_PRO_PREVIEW


def test_price_card_reserves_pro_for_decision_critical_stages() -> None:
    estimate = _estimate()
    by_stage = {stage.stage: stage for stage in estimate.stages}

    assert by_stage["team_planning"].model == "gemini-3.1-pro-preview"
    assert by_stage["independent_verification"].model == "gemini-3.1-pro-preview"
    assert by_stage["policy_guard"].model == "gemini-3.1-pro-preview"
    assert by_stage["final_approval"].model == "gemini-3.1-pro-preview"
    assert by_stage["evidence_analysis"].model == "gemini-3.5-flash"
    assert by_stage["long_form_draft"].model == "gemini-3.5-flash"


def test_producer_rejects_models_that_do_not_fit_approved_minimum() -> None:
    with pytest.raises(BudgetExceeded, match="need at least"):
        evaluate_model_budget(_estimate(), minimal_team_plan("job-models"), approved_usd=0.000001)


class RecordingGateway:
    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    def generate_json(self, *, stage: str, model: str, **_: object):
        self.calls.append((stage, model))
        return {"ok": True}

    def generate_adk_response(self, *, stage: str, model: str, **_: object):
        self.calls.append((stage, model))
        return {"adk": True}


def _policy() -> ModelExecutionPolicy:
    return ModelExecutionPolicy(
        project_id="job-models",
        team_plan_sha256="a" * 64,
        price_card_version="test",
        approved_usd=0.10,
        stage_models={
            "team_planning": ApprovedModel.GEMINI_3_5_FLASH,
            "evidence_analysis": ApprovedModel.GEMINI_3_5_FLASH_LITE,
        },
    )


def test_gateway_allows_only_exact_stage_model_binding() -> None:
    raw = RecordingGateway()
    gateway = ModelPolicyGateway(raw, _policy())
    gateway.generate_json(
        stage="evidence_analysis", model="gemini-3.5-flash-lite", schema=dict
    )
    with pytest.raises(PermissionError, match="blocked before provider call"):
        gateway.generate_json(
            stage="evidence_analysis", model="gemini-3.5-flash", schema=dict
        )
    assert raw.calls == [("evidence_analysis", "gemini-3.5-flash-lite")]


def test_gateway_applies_the_same_model_policy_to_native_adk_turns() -> None:
    raw = RecordingGateway()
    policy = _policy().model_copy(update={
        "stage_models": {
            **_policy().stage_models,
            "long_form_draft": ApprovedModel.GEMINI_3_5_FLASH,
        }
    })
    gateway = ModelPolicyGateway(raw, policy)

    assert gateway.generate_adk_response(
        stage="long_form_draft", model="gemini-3.5-flash", contents=[]
    ) == {"adk": True}
    assert gateway.generate_adk_response(
        stage="long_form_draft_revision_r5",
        model="gemini-3.5-flash",
        contents=[],
    ) == {"adk": True}
    with pytest.raises(PermissionError, match="blocked before provider call"):
        gateway.generate_adk_response(
            stage="long_form_draft", model="gemini-3.5-flash-lite", contents=[]
        )
    assert raw.calls == [
        ("long_form_draft", "gemini-3.5-flash"),
        ("long_form_draft_revision_r5", "gemini-3.5-flash"),
    ]


@pytest.mark.parametrize(
    "stage",
    [
        "evidence_analysis_r1",
        "evidence_analysis_compact_retry",
        "evidence_analysis_verification_retry",
        "evidence_analysis_verification_retry_compact_retry",
    ],
)
def test_gateway_maps_revision_and_retry_stages_to_the_approved_base_model(stage: str) -> None:
    raw = RecordingGateway()
    gateway = ModelPolicyGateway(raw, _policy())

    gateway.generate_json(stage=stage, model="gemini-3.5-flash-lite", schema=dict)

    assert raw.calls == [(stage, "gemini-3.5-flash-lite")]


def test_gateway_maps_research_refinement_to_approved_investigator_model() -> None:
    raw = RecordingGateway()
    policy = _policy().model_copy(update={
        "stage_models": {
            **_policy().stage_models,
            "public_research": ApprovedModel.GEMINI_3_5_FLASH,
        }
    })
    gateway = ModelPolicyGateway(raw, policy)

    gateway.generate_json(
        stage="public_research_refinement_r2",
        model="gemini-3.5-flash",
        schema=dict,
    )

    assert raw.calls == [("public_research_refinement_r2", "gemini-3.5-flash")]


def test_model_approval_records_are_immutable(tmp_path: Path) -> None:
    estimate = _estimate()
    decision, policy = evaluate_model_budget(
        estimate,
        minimal_team_plan("job-models"),
        approved_usd=estimate.recommended_approval_usd,
    )
    persist_model_approval(
        run_dir=tmp_path / "run",
        project_dir=tmp_path / "project",
        decision=decision,
        policy=policy,
    )
    persist_model_approval(
        run_dir=tmp_path / "run",
        project_dir=tmp_path / "project",
        decision=decision,
        policy=policy,
    )
    assert (tmp_path / "run" / "model_execution_policy.json").exists()
    changed = policy.model_copy(update={"approved_usd": policy.approved_usd + 1})
    with pytest.raises(FileExistsError, match="refusing to overwrite"):
        persist_model_approval(
            run_dir=tmp_path / "run",
            project_dir=tmp_path / "project",
            decision=decision,
            policy=changed,
        )
