from onebrief.requirements_gate import apply_requirements_gate
from onebrief.schemas import IntakeRequest, OutputTarget, RequirementsAnalysis


def _analysis() -> RequirementsAnalysis:
    return RequirementsAnalysis(
        supported=True,
        support_reason="The task is supported.",
        normalized_goal="Improve the existing exchange product.",
        deliverables=["Improved product"],
        mandatory_information=[],
        optional_information=[],
        acceptance_criteria=["Existing behavior remains available."],
        assumptions=[],
        consolidated_questions=[],
        ready_for_estimate=True,
    )


def test_auto_target_leaves_agent_deliverables_unchanged() -> None:
    result = apply_requirements_gate(IntakeRequest(goal="Improve the product."), _analysis())
    assert result.deliverables == ["Improved product"]


def test_existing_project_target_cannot_be_replaced_by_a_report() -> None:
    intake = IntakeRequest(
        goal="Improve the completed Exchange project.",
        output_target=OutputTarget.EXISTING_PROJECT,
        desired_output="Keep it as a runnable web application.",
    )
    result = apply_requirements_gate(intake, _analysis())
    assert any("기존 프로젝트의 원래 실행 형태" in item for item in result.deliverables)
    assert any("보고서나 요약 파일로 대체하지 않는다" in item for item in result.acceptance_criteria)
