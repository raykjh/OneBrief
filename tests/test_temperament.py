import pytest
from pydantic import ValidationError

from onebrief.execution_agents import AnalystAgent, RevisionAgent, VerifierAgent, WriterAgent
from onebrief.execution_schemas import (
    AnalysisPackage,
    DraftArtifact,
    RevisionArtifact,
    VerificationReport,
)
from onebrief.temperament import (
    ANALYST_PROFILE,
    EXECUTION_PROFILES,
    REVISION_PROFILE,
    VERIFIER_PROFILE,
    WRITER_PROFILE,
    TemperamentDecision,
    enforce_temperament_audit,
)


class CaptureGateway:
    def __init__(self, outputs: list[object]):
        self.outputs = outputs
        self.calls: list[dict[str, object]] = []

    def generate_json(self, **kwargs):
        self.calls.append(kwargs)
        value = self.outputs.pop(0)
        assert isinstance(value, kwargs["schema"])
        return value


def _analysis(**updates) -> AnalysisPackage:
    data = {
        "objective": "Create a grounded guide.",
        "findings": [
            {
                "finding_id": "F01",
                "source_name": "policy.md",
                "evidence": "Approval is required.",
                "implication": "State the approval rule.",
            }
        ],
        "recommended_structure": ["Rule"],
        "constraints": ["Use only supplied evidence."],
        "risks": [],
    }
    data.update(updates)
    return AnalysisPackage(**data)


def _draft() -> DraftArtifact:
    return DraftArtifact(
        title="Guide",
        body_markdown=(
            "# Guide\n\nManager approval is required before remote work begins. [F01] "
            "Employees must follow the supplied internal policy and retain human authority."
        ),
        cited_finding_ids=["F01"],
        drafting_decisions=[],
    )


def _verification() -> VerificationReport:
    return VerificationReport(
        verdict="PASS",
        criterion_checks=[
            {"criterion": "Cite evidence.", "passed": True, "evidence": "F01 cited."}
        ],
        blocking_issues=[],
        revision_instructions=[],
        missing_information=[],
    )


def _revision() -> RevisionArtifact:
    return RevisionArtifact(
        title="Guide",
        revised_body_markdown=(
            "# Guide\n\nManager approval is required before remote work begins. [F01] "
            "Employees must follow the supplied internal policy and retain human authority."
        ),
        addressed_issues=["Clarified the approval requirement."],
        cited_finding_ids=["F01"],
    )


def test_onebrief_uses_only_the_three_apt3_axes() -> None:
    assert {name: profile.code for name, profile in EXECUTION_PROFILES.items()} == {
        "analyst": "TFG",
        "writer": "TNL",
        "verifier": "TFL",
        "reviser": "TFL",
    }
    assert all(len(profile.code) == 3 for profile in EXECUTION_PROFILES.values())


def test_agent_prompts_apply_apt3_only_as_a_tie_breaker() -> None:
    gateway = CaptureGateway([_analysis(), _draft(), _verification(), _revision()])
    contract = {"goal": "Create a guide."}
    analysis = AnalystAgent(gateway).run(contract, [{"name": "policy.md"}])
    draft = WriterAgent(gateway).run(contract, analysis)
    report = VerifierAgent(gateway).run(contract, analysis, draft, 0)
    RevisionAgent(gateway).run(contract, analysis, draft, report, 1)

    expected = [
        ANALYST_PROFILE.code,
        WRITER_PROFILE.code,
        VERIFIER_PROFILE.code,
        REVISION_PROFILE.code,
    ]
    for call, code in zip(gateway.calls, expected, strict=True):
        instruction = str(call["system_instruction"])
        assert f"APT-3 discretion profile: {code}" in instruction
        assert "only when two or more choices remain equally valid" in instruction
        assert "Never force a temperament choice" in instruction


def test_no_tie_means_no_forced_temperament_record() -> None:
    result = enforce_temperament_audit(_analysis(), ANALYST_PROFILE)
    assert result.temperament_decisions == []


def test_wrong_agent_type_is_rejected_after_structured_output() -> None:
    decision = TemperamentDecision(
        agent="analyst",
        agent_type="TNL",
        options=["outline A", "outline B"],
        selected="outline A",
        deciding_axis="scope",
        reason="Both outlines met higher-priority rules.",
    )
    with pytest.raises(ValueError, match="does not match 'TFG'"):
        enforce_temperament_audit(
            _analysis(temperament_decisions=[decision]),
            ANALYST_PROFILE,
        )


def test_audit_must_record_a_real_candidate_choice() -> None:
    with pytest.raises(ValidationError, match="selected option"):
        TemperamentDecision(
            agent="writer",
            agent_type="TNL",
            options=["opening A", "opening B"],
            selected="unlisted opening",
            deciding_axis="orientation",
            reason="Both listed openings were otherwise valid.",
        )
