import json
from pathlib import Path

import pytest

from onebrief.budget_guard import BudgetExceeded, BudgetStore, RunStatus
from onebrief.execution_pipeline import ExecutionPipeline
from onebrief.execution_schemas import (
    AnalysisPackage,
    DraftArtifact,
    PipelineStatus,
    RevisionArtifact,
    VerificationReport,
)
from onebrief.producer import estimate_budget
from onebrief.schemas import IntakeRequest, InternalSource, RequirementsAnalysis, SourcePriority


class FakeGateway:
    def __init__(self, outputs: list[object]):
        self.outputs = outputs
        self.calls: list[tuple[str, str]] = []

    def generate_json(self, *, stage: str, model: str, schema: type, **_: object):
        self.calls.append((stage, model))
        value = self.outputs.pop(0)
        assert isinstance(value, schema)
        return value


class BlockingGateway:
    def generate_json(self, **_: object):
        raise BudgetExceeded("blocked before generation")


def _requirements() -> RequirementsAnalysis:
    return RequirementsAnalysis(
        supported=True,
        support_reason="Complete internal inputs were supplied.",
        normalized_goal="Create a grounded guide.",
        deliverables=["Grounded guide"],
        mandatory_information=[],
        optional_information=[],
        acceptance_criteria=["Every material claim cites supplied evidence."],
        assumptions=[],
        consolidated_questions=[],
        ready_for_estimate=True,
    )


def _source() -> InternalSource:
    return InternalSource(
        name="policy.md",
        priority=SourcePriority.MANDATORY,
        requirement_keys=["policy"],
        content="The policy requires manager approval for remote work.",
    )


def _analysis() -> AnalysisPackage:
    return AnalysisPackage(
        objective="Create a grounded guide.",
        findings=[
            {
                "finding_id": "F01",
                "source_name": "policy.md",
                "evidence": "Manager approval is required.",
                "implication": "The guide must instruct employees to request approval.",
            }
        ],
        recommended_structure=["Rule", "Procedure"],
        constraints=["Use only the policy."],
        risks=[],
    )


def _draft(text: str = "Employees must request manager approval before remote work. [F01]") -> DraftArtifact:
    return DraftArtifact(
        title="Remote Work Guide",
        body_markdown=text,
        cited_finding_ids=["F01"],
        drafting_decisions=["Used the mandatory policy."],
    )


def _verification(verdict: str) -> VerificationReport:
    revise = verdict == "REVISE"
    return VerificationReport(
        verdict=verdict,
        criterion_checks=[
            {
                "criterion": "Every material claim cites evidence.",
                "passed": not revise,
                "evidence": "F01 is present." if not revise else "The procedure is incomplete.",
            }
        ],
        blocking_issues=["Add the approval request procedure."] if revise else [],
        revision_instructions=["Add a clear approval request step."] if revise else [],
        missing_information=[],
    )


def _approve(tmp_path: Path, intake: IntakeRequest, source: InternalSource) -> Path:
    run_dir = tmp_path / "run"
    estimate = estimate_budget(
        intake.model_copy(update={"internal_sources": [source]}),
        _requirements(),
    )
    BudgetStore(run_dir).approve(estimate, estimate.recommended_approval_usd)
    return run_dir


def test_revision_is_always_reverified_through_same_gateway(tmp_path: Path) -> None:
    intake = IntakeRequest(goal="Create a guide.", max_revision_rounds=2)
    source = _source()
    run_dir = _approve(tmp_path, intake, source)
    gateway = FakeGateway(
        [
            _analysis(),
            _draft(),
            _verification("REVISE"),
            RevisionArtifact(
                title="Remote Work Guide",
                revised_body_markdown=(
                    "Employees must submit a request and receive manager approval before remote work. [F01]"
                ),
                addressed_issues=["Added the approval request procedure."],
                cited_finding_ids=["F01"],
                temperament_decisions=[
                    {
                        "agent": "reviser",
                        "agent_type": "TFL",
                        "options": ["targeted correction", "full rewrite"],
                        "selected": "targeted correction",
                        "deciding_axis": "scope",
                        "reason": "Both met the contract; Local favored the smaller correction.",
                    }
                ],
            ),
            _verification("PASS"),
        ]
    )
    result = ExecutionPipeline(run_dir, gateway=gateway).run(
        intake=intake,
        requirements=_requirements(),
        sources=[source],
        output_dir=tmp_path / "output",
    )
    assert result.status == PipelineStatus.COMPLETE
    assert [stage for stage, _ in gateway.calls] == [
        "evidence_analysis",
        "long_form_draft",
        "independent_verification_r0",
        "revision_r1",
        "independent_verification_r1",
    ]
    assert BudgetStore(run_dir).read().status == RunStatus.COMPLETE
    audit = json.loads(
        (tmp_path / "output" / "temperament_decisions.json").read_text(encoding="utf-8")
    )
    assert len(audit) == 1
    assert audit[0]["agent_type"] == "TFL"


def test_budget_block_writes_resumable_checkpoint(tmp_path: Path) -> None:
    intake = IntakeRequest(goal="Create a guide.")
    source = _source()
    run_dir = _approve(tmp_path, intake, source)
    output_dir = tmp_path / "blocked"
    with pytest.raises(BudgetExceeded):
        ExecutionPipeline(run_dir, gateway=BlockingGateway()).run(
            intake=intake,
            requirements=_requirements(),
            sources=[source],
            output_dir=output_dir,
        )
    checkpoint = (output_dir / "execution_checkpoint.json").read_text(encoding="utf-8")
    assert '"status": "needs_budget"' in checkpoint



def test_resume_reuses_completed_analysis_without_a_new_model_call(tmp_path: Path) -> None:
    intake = IntakeRequest(goal="Create a guide.")
    source = _source()
    run_dir = _approve(tmp_path, intake, source)
    output_dir = tmp_path / "resumed"
    output_dir.mkdir()
    (output_dir / "analysis.json").write_text(
        _analysis().model_dump_json(indent=2),
        encoding="utf-8",
    )
    gateway = FakeGateway([_draft(), _verification("PASS")])

    result = ExecutionPipeline(run_dir, gateway=gateway).run(
        intake=intake,
        requirements=_requirements(),
        sources=[source],
        output_dir=output_dir,
    )

    assert result.status == PipelineStatus.COMPLETE
    assert [stage for stage, _ in gateway.calls] == [
        "long_form_draft",
        "independent_verification_r0",
    ]
    final_text = (output_dir / "final.md").read_text(encoding="utf-8")
    assert "Remote Work Guide" in final_text
    assert json.loads((output_dir / "temperament_decisions.json").read_text(encoding="utf-8")) == []
