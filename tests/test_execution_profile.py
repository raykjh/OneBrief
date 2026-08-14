from onebrief.execution_profile import (
    compact_work_contract,
    effective_revision_rounds,
    requires_full_csv_preservation,
)
from onebrief.schemas import IntakeRequest, OutputTarget, RequirementsAnalysis


def _requirements(criteria: list[str]) -> RequirementsAnalysis:
    return RequirementsAnalysis(
        supported=True,
        support_reason="bounded task",
        normalized_goal="Create a bounded artifact.",
        deliverables=["one artifact"],
        mandatory_information=[], optional_information=[],
        acceptance_criteria=criteria,
        assumptions=[], consolidated_questions=[], ready_for_estimate=True,
    )


def test_small_document_caps_revisions_and_compacts_duplicate_contract_text() -> None:
    intake = IntakeRequest(
        goal="Create an incident playbook.",
        output_target=OutputTarget.DOCUMENT,
        max_revision_rounds=6,
    )
    requirements = _requirements(["Use supplied rules only."])
    assert effective_revision_rounds(intake, requirements) == 2
    compact = compact_work_contract({
        "goal": intake.goal,
        "normalized_goal": requirements.normalized_goal,
        "deliverables": requirements.deliverables,
        "acceptance_criteria": requirements.acceptance_criteria,
        "completion_contract": requirements.completion_contract.model_dump(mode="json"),
        "assumptions": [],
        "output_target": "document",
    }, enabled=True)
    assert set(compact) == {"goal", "output_target", "completion_contract", "assumptions"}


def test_csv_preservation_is_contract_activated() -> None:
    assert requires_full_csv_preservation(_requirements(["Every CSV row is preserved."]))
    assert not requires_full_csv_preservation(_requirements(["Use CSV examples as guidance."]))
