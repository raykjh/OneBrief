from onebrief.contract_integrity import enforce_contract_integrity
from onebrief.schemas import IntakeRequest, RequirementsAnalysis, SixSensePlan


def _requirements() -> RequirementsAnalysis:
    return RequirementsAnalysis(
        supported=True,
        support_reason="Supported.",
        normalized_goal="Create a comparison report.",
        deliverables=["Comparison report"],
        mandatory_information=[],
        optional_information=[],
        acceptance_criteria=["Every candidate is compared."],
        assumptions=[],
        consolidated_questions=[],
        ready_for_estimate=True,
        sixsense=SixSensePlan(
            standard_profile="A market research expert guideline.", questions=[]
        ),
    )


def test_missing_explicit_exclusion_is_copied_into_completion_contract() -> None:
    intake = IntakeRequest(
        goal="Compare candidate services. Exclude any candidate with an existing equivalent."
    )
    result = enforce_contract_integrity(intake, _requirements())

    descriptions = [item.description for item in result.completion_contract.quality_criteria]
    assert any("Exclude any candidate" in item for item in descriptions)
    assert result.completion_contract.quality_criteria[-1].criterion_id == "Q02"


def test_model_persona_is_replaced_with_a_disclosed_standard_profile() -> None:
    intake = IntakeRequest(goal="Create a comparison report.")
    result = enforce_contract_integrity(intake, _requirements())

    assert "expert" not in result.sixsense.standard_profile.casefold()
    assert "official standards" in result.sixsense.standard_profile


def test_delegated_discovery_does_not_return_candidate_choice_to_user() -> None:
    requirements = _requirements()
    requirements.sixsense = SixSensePlan.model_validate({
        "standard_profile": "Use official standards.",
        "questions": [{
            "question_id": "S02",
            "dimension": "functional scope",
            "prompt": "Which category should be selected?",
            "reason": "This narrows the search.",
            "options": [
                {"option_id": "first", "label": "First category", "decision": "Use candidate A and candidate B.", "recommended": True},
                {"option_id": "second", "label": "Second category", "decision": "Use candidate C."},
            ],
        }],
    })
    intake = IntakeRequest(
        goal="건강에 도움을 줄 수 있는 성분을 찾은 다음 제품 콘셉트를 정해줘.",
        public_research_allowed=True,
    )

    result = enforce_contract_integrity(intake, requirements)

    assert result.sixsense.questions == []


def test_explicit_request_to_ask_questions_keeps_sixsense_choices() -> None:
    requirements = _requirements()
    requirements.sixsense = SixSensePlan.model_validate({
        "standard_profile": "Use official standards.",
        "questions": [{
            "question_id": "S02",
            "dimension": "audience",
            "prompt": "Who is the audience?",
            "reason": "This changes the result.",
            "options": [
                {"option_id": "new", "label": "New users", "decision": "Prioritize new users.", "recommended": True},
                {"option_id": "existing", "label": "Existing users", "decision": "Prioritize existing users."},
            ],
        }],
    })
    intake = IntakeRequest(goal="후보를 찾아 추천하되 필요한 취향은 나에게 질문해줘.")

    result = enforce_contract_integrity(intake, requirements)

    assert len(result.sixsense.questions) == 1
