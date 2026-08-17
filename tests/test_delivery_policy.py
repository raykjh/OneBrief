from onebrief.delivery_policy import apply_standard_first_delivery_policy
from onebrief.schemas import IntakeRequest, OutputTarget, RequirementsAnalysis


def _requirements() -> RequirementsAnalysis:
    return RequirementsAnalysis(
        supported=True,
        support_reason="Supported.",
        normalized_goal="Build a useful product.",
        deliverables=["Runnable product"],
        mandatory_information=[],
        optional_information=[],
        acceptance_criteria=["The requested behavior is proven."],
        assumptions=[],
        consolidated_questions=[],
        ready_for_estimate=True,
    )


def test_first_product_delivery_defaults_to_standard_design() -> None:
    intake = IntakeRequest(
        goal="환율 분석 웹 프로그램을 만들어줘.",
        output_target=OutputTarget.WEB_APP,
    )
    result = apply_standard_first_delivery_policy(intake, _requirements())

    assert any("conventional commercial design" in item for item in result.assumptions)


def test_explicit_design_improvement_is_not_overridden() -> None:
    intake = IntakeRequest(
        goal="기존 웹 프로그램의 디자인을 개선하고 고유한 시각 체계를 만들어줘.",
        output_target=OutputTarget.EXISTING_PROJECT,
    )
    result = apply_standard_first_delivery_policy(intake, _requirements())

    assert result.assumptions == []
