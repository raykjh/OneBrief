from onebrief.preparation import build_preparation_plan
from onebrief.producer import estimate_budget
from onebrief.schemas import (
    AssuranceSelection,
    AssuranceUse,
    IntakeRequest,
    RequirementsAnalysis,
    SixSensePlan,
)


def _requirements(criterion: str = "Every requested feature is proven.") -> RequirementsAnalysis:
    return RequirementsAnalysis(
        supported=True,
        support_reason="Supported.",
        normalized_goal="Finish a bounded deliverable.",
        deliverables=["Finished deliverable"],
        mandatory_information=[],
        optional_information=[],
        acceptance_criteria=[criterion],
        assumptions=[],
        consolidated_questions=[],
        ready_for_estimate=True,
    )


def test_stage_one_binds_completion_permissions_and_budget_to_one_approval() -> None:
    intake = IntakeRequest(
        goal="Finish a bounded deliverable.",
        public_research_allowed=True,
    )
    requirements = _requirements()
    budget = estimate_budget(intake, requirements)

    plan = build_preparation_plan(intake, requirements, budget)

    assert plan is not None
    assert plan.stage == "definition_and_authorization"
    assert plan.canonical_goal == intake.goal
    assert plan.output_target == "auto"
    assert plan.ready_for_authorization is True
    assert "perform bounded Google Search research" in " ".join(
        plan.permission_manifest.capabilities
    )
    assert plan.minimum_cost_usd <= plan.recommended_cost_usd <= plan.maximum_cost_usd
    assert len(plan.authorization_sha256) == 64
    assert plan.authorization_envelope.actor_id == "local_user"
    assert plan.authorization_envelope.executor_id == "onebrief_worker"
    assert plan.authorization_envelope.maximum_budget_usd == budget.maximum_cost_usd


def test_changed_completion_contract_requires_a_new_authorization_hash() -> None:
    intake = IntakeRequest(goal="Finish a bounded deliverable.")
    first_requirements = _requirements("The output opens successfully.")
    second_requirements = _requirements("The output opens and passes its tests.")

    first = build_preparation_plan(
        intake, first_requirements, estimate_budget(intake, first_requirements)
    )
    second = build_preparation_plan(
        intake, second_requirements, estimate_budget(intake, second_requirements)
    )

    assert first is not None and second is not None
    assert first.authorization_sha256 != second.authorization_sha256


def test_changed_goal_requires_a_new_authorization_even_with_same_contract() -> None:
    requirements = _requirements()
    first_intake = IntakeRequest(goal="Finish the English product page.")
    second_intake = IntakeRequest(goal="Finish the Korean product page.")

    first = build_preparation_plan(
        first_intake, requirements, estimate_budget(first_intake, requirements)
    )
    second = build_preparation_plan(
        second_intake, requirements, estimate_budget(second_intake, requirements)
    )

    assert first is not None and second is not None
    assert first.authorization_sha256 != second.authorization_sha256


def test_changed_intended_use_requires_a_new_authorization_hash() -> None:
    intake = IntakeRequest(goal="Prepare a product document.")
    proposal = _requirements().model_copy(update={
        "sixsense": SixSensePlan(
            standard_profile="Use proposal conventions.",
            assurance=AssuranceSelection(
                intended_use=AssuranceUse.COMMERCIAL_PROPOSAL,
                rationale="The document is an OEM proposal.",
            ),
        )
    })
    marketing = proposal.model_copy(update={
        "sixsense": proposal.sixsense.model_copy(update={
            "assurance": AssuranceSelection(
                intended_use=AssuranceUse.PUBLIC_MARKETING,
                rationale="The document will be published as marketing.",
            )
        })
    })

    first = build_preparation_plan(intake, proposal, estimate_budget(intake, proposal))
    second = build_preparation_plan(intake, marketing, estimate_budget(intake, marketing))

    assert first is not None and second is not None
    assert first.authorization_envelope.assurance_profile_sha256 != second.authorization_envelope.assurance_profile_sha256
    assert first.authorization_sha256 != second.authorization_sha256
