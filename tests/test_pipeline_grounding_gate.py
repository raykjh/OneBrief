import json
from pathlib import Path

from onebrief.budget_guard import BudgetStore
from onebrief.execution_pipeline import ExecutionPipeline
from onebrief.execution_schemas import (
    AnalysisPackage,
    DraftArtifact,
    PipelineStatus,
    VerificationReport,
    Verdict,
)
from onebrief.producer import estimate_budget
from onebrief.schemas import IntakeRequest, InternalSource, RequirementsAnalysis, SourcePriority


class FakeGateway:
    def __init__(self, outputs: list[object]):
        self.outputs = outputs
        self.calls: list[dict[str, object]] = []

    def generate_json(self, **kwargs):
        self.calls.append(kwargs)
        value = self.outputs.pop(0)
        assert isinstance(value, kwargs["schema"])
        return value


def test_deterministic_grounding_gate_overrides_model_pass(tmp_path: Path) -> None:
    source = InternalSource(
        name="records.csv",
        priority=SourcePriority.MANDATORY,
        requirement_keys=["records"],
        content="record_id,years,level\nA1,3,strong\n",
        media_type="text/csv",
    )
    requirements = RequirementsAnalysis(
        supported=True,
        support_reason="Inputs are present.",
        normalized_goal="Create a grounded evaluation.",
        deliverables=["Evaluation table"],
        mandatory_information=[],
        optional_information=[],
        acceptance_criteria=["Preserve every CSV field."],
        assumptions=[],
        consolidated_questions=[],
        ready_for_estimate=True,
    )
    intake = IntakeRequest(goal="Create a grounded evaluation.", max_revision_rounds=2)
    estimate = estimate_budget(intake.model_copy(update={"internal_sources": [source]}), requirements)
    run_dir = tmp_path / "run"
    BudgetStore(run_dir).approve(estimate, estimate.maximum_cost_usd)

    analysis = AnalysisPackage(
        objective="Create a grounded evaluation.",
        findings=[
            {
                "finding_id": "F01",
                "source_name": "records.csv",
                "evidence": "A1 has 3 years and strong level.",
                "implication": "Preserve the row exactly.",
            }
        ],
        recommended_structure=["Table"],
        constraints=["Do not invent values."],
        risks=[],
    )
    draft = DraftArtifact(
        title="Evaluation",
        body_markdown=(
            "# Evaluation\n\n- at least 5 years: 40 points\n\n"
            "| record_id | years | level | score |\n"
            "|---|---:|---|---:|\n"
            "| A1 | 6 years | strong | 40 points |"
        ),
        cited_finding_ids=["F01"],
        drafting_decisions=[],
    )
    model_pass = VerificationReport(
        verdict=Verdict.PASS,
        criterion_checks=[
            {"criterion": "Preserve every CSV field.", "passed": True, "evidence": "Looks correct."}
        ],
        blocking_issues=[],
        revision_instructions=[],
        missing_information=[],
    )
    gateway = FakeGateway([analysis, draft, model_pass])
    output_dir = tmp_path / "output"

    result = ExecutionPipeline(run_dir, gateway=gateway).run(
        intake=intake,
        requirements=requirements,
        sources=[source],
        output_dir=output_dir,
    )

    assert result.status == PipelineStatus.NEEDS_INFORMATION
    assert result.final_verdict == Verdict.NEEDS_INFORMATION
    final_report = VerificationReport.model_validate_json(
        (output_dir / "verification_r0.json").read_text(encoding="utf-8")
    )
    assert final_report.verdict == Verdict.NEEDS_INFORMATION
    grounding = json.loads(
        (output_dir / "deterministic_verification_r0.json").read_text(encoding="utf-8")
    )
    assert grounding["verdict_override"] == "NEEDS_INFORMATION"
    assert {issue["kind"] for issue in grounding["issues"]} == {
        "csv_value_mismatch",
        "unsupported_scoring_rule",
    }
    assert len(gateway.calls) == 3
    assert "records.csv" in str(gateway.calls[1]["contents"])
    assert "records.csv" in str(gateway.calls[2]["contents"])
