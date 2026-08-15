from onebrief.assurance import ClaimMode, resolve_assurance_policy
from onebrief.schemas import (
    AssuranceSelection,
    AssuranceUse,
    RequirementsAnalysis,
    SixSensePlan,
)


def _requirements(intended_use: AssuranceUse) -> RequirementsAnalysis:
    return RequirementsAnalysis(
        supported=True,
        support_reason="Supported.",
        normalized_goal="Prepare the requested artifact.",
        deliverables=["Artifact"],
        mandatory_information=[],
        optional_information=[],
        acceptance_criteria=["The artifact is usable."],
        sixsense=SixSensePlan(
            standard_profile="Use professional defaults.",
            assurance=AssuranceSelection(
                intended_use=intended_use,
                rationale="The delivery use determines the evidence and expression policy.",
            ),
        ),
        assumptions=[],
        consolidated_questions=[],
        ready_for_estimate=True,
    )


def test_commercial_proposal_allows_bounded_inference_but_keeps_truth_floor() -> None:
    policy = resolve_assurance_policy(_requirements(AssuranceUse.COMMERCIAL_PROPOSAL))

    assert policy.claim_mode == ClaimMode.BOUNDED_INFERENCE
    assert policy.allow_labeled_estimates is True
    assert policy.allow_bounded_inference is True
    assert policy.require_direct_evidence_for_health_claims is True
    assert any("invented" in item.casefold() for item in policy.immutable_truth_floor)


def test_public_marketing_and_regulatory_submission_cannot_inherit_proposal_freedom() -> None:
    marketing = resolve_assurance_policy(_requirements(AssuranceUse.PUBLIC_MARKETING))
    regulatory = resolve_assurance_policy(_requirements(AssuranceUse.REGULATORY_SUBMISSION))

    assert marketing.allow_bounded_inference is False
    assert marketing.allow_labeled_estimates is False
    assert regulatory.claim_mode == ClaimMode.VALIDATED_SUBMISSION
    assert regulatory.require_deterministic_artifact_verification is True
