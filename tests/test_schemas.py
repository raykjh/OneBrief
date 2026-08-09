import pytest
from pydantic import ValidationError

from onebrief.schemas import (
    EvaluationMode,
    InformationRequirement,
    RequirementsAnalysis,
    SixSenseOption,
    SixSenseQuestion,
)


def _gap() -> InformationRequirement:
    return InformationRequirement(
        key="authoritative_policy",
        request="Provide the authoritative internal policy.",
        reason="The evaluation must follow the organization's own rules.",
        acceptable_evidence=["policy document", "approved written rules"],
    )


def test_mandatory_gap_blocks_estimation() -> None:
    with pytest.raises(ValidationError):
        RequirementsAnalysis(
            supported=True,
            support_reason="This is supported analytical work.",
            normalized_goal="Evaluate candidates against company rules.",
            deliverables=["Candidate evaluation report"],
            mandatory_information=[_gap()],
            optional_information=[],
            acceptance_criteria=["Each score cites a supplied rule."],
            assumptions=[],
            consolidated_questions=["Please provide the authoritative policy."],
            ready_for_estimate=True,
        )


def test_complete_supported_intake_can_be_estimated() -> None:
    result = RequirementsAnalysis(
        supported=True,
        support_reason="This is supported drafting work.",
        normalized_goal="Draft a treatment from the supplied world bible.",
        deliverables=["Five-scene treatment"],
        mandatory_information=[],
        optional_information=[],
        acceptance_criteria=["Every scene conforms to the supplied canon."],
        assumptions=["The requested language is Korean."],
        consolidated_questions=[],
        ready_for_estimate=True,
    )
    assert result.ready_for_estimate is True
    assert result.completion_contract is not None
    assert result.completion_contract.quality_criteria[0].criterion_id == "Q01"
    assert (
        result.completion_contract.quality_criteria[0].evaluation_mode
        == EvaluationMode.INDEPENDENT_REVIEW
    )


def test_sixsense_question_requires_one_disclosed_recommendation() -> None:
    with pytest.raises(ValidationError):
        SixSenseQuestion(
            question_id="S02",
            dimension="audience",
            prompt="Who should this serve first?",
            reason="The answer changes the information hierarchy.",
            options=[
                SixSenseOption(
                    option_id="new_users",
                    label="New users",
                    decision="Prioritize onboarding.",
                ),
                SixSenseOption(
                    option_id="experts",
                    label="Experts",
                    decision="Prioritize advanced controls.",
                ),
            ],
        )

