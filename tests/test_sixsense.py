from onebrief.schemas import IntakeRequest, RequirementsAnalysis, SixSenseOption, SixSensePlan, SixSenseQuestion
from onebrief.sixsense import apply_sixsense_policy


def _analysis() -> RequirementsAnalysis:
    return RequirementsAnalysis(
        supported=True,
        support_reason="Supported.",
        normalized_goal="Improve the homepage.",
        deliverables=["Improved homepage"],
        mandatory_information=[],
        optional_information=[],
        acceptance_criteria=["The homepage is usable."],
        assumptions=[],
        consolidated_questions=[],
        ready_for_estimate=True,
        sixsense=SixSensePlan(
            standard_profile="Use a commercial game homepage standard.",
            questions=[SixSenseQuestion(
                question_id="S02",
                dimension="visual_style",
                prompt="Which visual style should be used?",
                reason="The choice changes the visual result.",
                options=[
                    SixSenseOption(option_id="dark", label="Dark", decision="Use dark styling.", recommended=True),
                    SixSenseOption(option_id="light", label="Light", decision="Use light styling."),
                ],
            )],
        ),
    )


def test_vague_web_upgrade_gets_five_ordered_material_decisions() -> None:
    result = apply_sixsense_policy(
        IntakeRequest(goal="줄패 홈페이지를 정식 게임 서비스 수준으로 더 발전시켜줘"),
        _analysis(),
    )
    questions = result.sixsense.questions
    assert [question.question_id for question in questions] == ["S02", "S03", "S04", "S05", "S06"]
    assert [question.dimension for question in questions[:4]] == [
        "Functional scope", "Information structure", "Primary audience", "Service breadth"
    ]
    assert questions[-1].dimension == "visual_style"
    assert all(sum(option.recommended for option in question.options) == 1 for question in questions)


def test_specific_non_upgrade_goal_keeps_model_questions_unchanged() -> None:
    analysis = _analysis()
    result = apply_sixsense_policy(
        IntakeRequest(goal="Add a verified CSV export button to the dashboard."),
        analysis,
    )
    assert result is analysis
