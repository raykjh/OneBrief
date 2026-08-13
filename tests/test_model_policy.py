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
    assert policy.stage_model_ladders["long_form_draft"] == [
        ApprovedModel.GEMINI_3_5_FLASH,
        ApprovedModel.GEMINI_3_1_PRO_PREVIEW,
    ]
    assert decision.maximum_cost_usd >= decision.recommended_cost_usd


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


def test_bounded_continuation_reprices_only_unfinished_model_stages() -> None:
    estimate = _estimate()
    plan = minimal_team_plan("job-models")
    full, _ = evaluate_model_budget(
        estimate, plan, approved_usd=estimate.maximum_cost_usd
    )
    continuation, policy = evaluate_model_budget(
        estimate,
        plan,
        approved_usd=estimate.maximum_cost_usd,
        cost_stage_names={
            "long_form_draft", "independent_verification", "final_approval"
        },
    )

    assert {item.stage for item in continuation.selected_stages} == {
        "long_form_draft", "independent_verification", "final_approval"
    }
    assert continuation.minimum_cost_usd < full.minimum_cost_usd
    assert policy.stage_models["team_planning"] == ApprovedModel.GEMINI_3_1_PRO_PREVIEW


class RecordingGateway:
    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    def generate_json(self, *, stage: str, model: str, **_: object):
        self.calls.append((stage, model))
        return {"ok": True}

    def generate_json_with_images(self, *, stage: str, model: str, **_: object):
        self.calls.append((stage, model))
        return {"vision": True}

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


def _escalation_policy() -> ModelExecutionPolicy:
    return _policy().model_copy(update={
        "stage_models": {
            **_policy().stage_models,
            "long_form_draft": ApprovedModel.GEMINI_3_5_FLASH,
        },
        "stage_model_ladders": {
            "long_form_draft": [
                ApprovedModel.GEMINI_3_5_FLASH,
                ApprovedModel.GEMINI_3_1_PRO_PREVIEW,
            ]
        },
    })


def test_complex_semantic_failure_escalates_same_maker_to_approved_pro_rung() -> None:
    policy = _escalation_policy()

    decision = policy.select_after_failure(
        "long_form_draft",
        failure_text=(
            "Independent Unity semantic visual observation failed: rendered UI defect in mobile lobby"
        ),
        difficulty="complex",
    )

    assert decision.escalated is True
    assert decision.base_model == ApprovedModel.GEMINI_3_5_FLASH
    assert decision.selected_model == ApprovedModel.GEMINI_3_1_PRO_PREVIEW
    assert decision.call_stage == "long_form_draft_reasoning_escalation_r1"
    assert policy.model_for(decision.call_stage) == "gemini-3.1-pro-preview"
    assert policy.model_for(decision.call_stage + "_compact_retry") == "gemini-3.1-pro-preview"


def test_unchanged_unity_locale_escalates_the_same_maker_to_pro() -> None:
    decision = _escalation_policy().select_after_failure(
        "long_form_draft",
        failure_text="Unity visual scenario locale_es did not visibly change any text",
        difficulty="complex",
    )

    assert decision.escalated is True
    assert decision.selected_model == ApprovedModel.GEMINI_3_1_PRO_PREVIEW
    assert decision.call_stage == "long_form_draft_reasoning_escalation_r1"


def test_incomplete_atomic_unity_evidence_bundle_escalates_same_maker() -> None:
    decision = _escalation_policy().select_after_failure(
        "long_form_draft",
        failure_text=(
            "Unity visual test contract: the evidence harness is an atomic bundle and the "
            "proposal omitted its sibling TestAssemblies asmdef"
        ),
        difficulty="complex",
    )

    assert decision.escalated is True
    assert decision.selected_model == ApprovedModel.GEMINI_3_1_PRO_PREVIEW


def test_first_missing_evidence_harness_stays_on_flash() -> None:
    decision = _escalation_policy().select_after_failure(
        "long_form_draft",
        failure_text=(
            "development verification failed: Unity visual test contract: add a "
            "discoverable Unity PlayMode test whose namespace begins with OneBrief.Visual"
        ),
        difficulty="complex",
    )

    assert decision.escalated is False
    assert decision.selected_model == ApprovedModel.GEMINI_3_5_FLASH


@pytest.mark.parametrize("failure", [
    "Invalid JSON: EOF while parsing",
    "stale or missing base hash: web/app/page.tsx",
    "edit anchors could not rediscover one approved source range: Assets/UI.cs",
    "provider returned status code 503",
])
def test_non_reasoning_failures_do_not_buy_a_stronger_model(failure: str) -> None:
    decision = _escalation_policy().select_after_failure(
        "long_form_draft", failure_text=failure, difficulty="complex"
    )

    assert decision.escalated is False
    assert decision.selected_model == ApprovedModel.GEMINI_3_5_FLASH
    assert decision.call_stage == "long_form_draft"


def test_escalated_model_is_blocked_without_an_approved_ladder() -> None:
    with pytest.raises(PermissionError, match="no approved reasoning escalation"):
        _policy().model_for("team_planning_reasoning_escalation_r1")


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


def test_phase_qualified_runtime_stage_uses_approved_logical_binding() -> None:
    policy = _escalation_policy().model_copy(update={
        "stage_models": {
            **_escalation_policy().stage_models,
            "independent_verification": ApprovedModel.GEMINI_3_1_PRO_PREVIEW,
        }
    })

    assert policy.model_for(
        "product_implementation::long_form_draft"
    ) == "gemini-3.5-flash"
    assert policy.model_for(
        "evidence_construction::repair::long_form_draft"
    ) == "gemini-3.5-flash"
    assert policy.model_for(
        "final_verification::independent_verification"
    ) == "gemini-3.1-pro-preview"
    assert policy.model_for(
        "product_implementation::repair::long_form_draft_reasoning_escalation_r2"
    ) == "gemini-3.1-pro-preview"


def test_unknown_phase_stage_namespace_cannot_smuggle_a_model_binding() -> None:
    policy = _escalation_policy()

    with pytest.raises(PermissionError, match="no approved model binding"):
        policy.model_for("product_implementation::unapproved::long_form_draft")
    with pytest.raises(PermissionError, match="no approved model binding"):
        policy.model_for("unapproved_phase::long_form_draft")


def test_gateway_applies_model_policy_to_multimodal_observation() -> None:
    raw = RecordingGateway()
    policy = _policy().model_copy(update={
        "stage_models": {
            **_policy().stage_models,
            "independent_verification": ApprovedModel.GEMINI_3_1_PRO_PREVIEW,
        }
    })
    gateway = ModelPolicyGateway(raw, policy)

    assert gateway.generate_json_with_images(
        stage="independent_verification",
        model="gemini-3.1-pro-preview",
        image_paths=[],
    ) == {"vision": True}
    with pytest.raises(PermissionError, match="blocked before provider call"):
        gateway.generate_json_with_images(
            stage="independent_verification",
            model="gemini-3.5-flash",
            image_paths=[],
        )
    assert raw.calls == [("independent_verification", "gemini-3.1-pro-preview")]


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
