from onebrief.requirements_gate import apply_requirements_gate
from onebrief.schemas import (
    InformationRequirement,
    IntakeRequest,
    InternalSource,
    RequirementsAnalysis,
    SourcePriority,
)


def _source() -> InternalSource:
    return InternalSource(
        name="canon.md",
        priority=SourcePriority.MANDATORY,
        content="The supplied fictional canon.",
        size_bytes=31,
    )


def _analysis(request: str, key: str = "creative_direction") -> RequirementsAnalysis:
    requirement = InformationRequirement(
        key=key,
        request=request,
        reason="This would improve the creative direction.",
        acceptable_evidence=["User preference"],
    )
    return RequirementsAnalysis(
        supported=True,
        support_reason="Creative drafting is supported.",
        normalized_goal="Write a screenplay grounded in the canon.",
        deliverables=["Screenplay"],
        mandatory_information=[requirement],
        optional_information=[],
        acceptance_criteria=["The screenplay follows the canon."],
        assumptions=[],
        consolidated_questions=[request],
        ready_for_estimate=False,
    )


def test_open_ended_screenplay_preferences_become_optional() -> None:
    intake = IntakeRequest(
        goal="A.R.E.S 세계관을 기반으로 영화 각본을 작성해줘.",
        internal_sources=[_source()],
    )

    gated = apply_requirements_gate(
        intake,
        _analysis("줄거리, 주인공, 장르, 결말과 분량을 알려주세요."),
    )

    assert gated.ready_for_estimate is True
    assert gated.mandatory_information == []
    assert [item.key for item in gated.optional_information] == ["creative_direction"]
    assert gated.consolidated_questions == []


def test_missing_canon_is_not_auto_cleared() -> None:
    intake = IntakeRequest(goal="A.R.E.S 세계관을 기반으로 영화 각본을 작성해줘.")

    gated = apply_requirements_gate(intake, _analysis("세계관 정본을 제공해 주세요.", "canon"))

    assert gated.ready_for_estimate is False
    assert [item.key for item in gated.mandatory_information] == ["canon"]


def test_explicit_adaptation_preferences_are_not_auto_cleared() -> None:
    intake = IntakeRequest(
        goal="기존 작품을 각색한 영화 각본을 작성해줘.",
        internal_sources=[_source()],
    )

    gated = apply_requirements_gate(intake, _analysis("기존 작품의 결말 보존 여부를 알려주세요."))

    assert gated.ready_for_estimate is False
    assert gated.mandatory_information
