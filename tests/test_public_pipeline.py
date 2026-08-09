from pathlib import Path

from onebrief.budget_guard import BudgetStore
from onebrief.execution_pipeline import ExecutionPipeline
from onebrief.execution_schemas import AnalysisPackage, DraftArtifact, VerificationReport
from onebrief.producer import estimate_budget
from onebrief.public_research import PublicResearchResult
from onebrief.schemas import (
    IntakeRequest, InternalSource, RequirementsAnalysis, SourcePriority,
)


class FakeGateway:
    def __init__(self) -> None:
        self.outputs = [
            AnalysisPackage(
                objective="Create a current candidate list.",
                findings=[{
                    "finding_id": "F01", "source_name": "public_research.md",
                    "evidence": "Example candidate is publicly listed.",
                    "implication": "Include it with its source.",
                }],
                recommended_structure=["Candidate table"], constraints=[], risks=[],
            ),
            DraftArtifact(
                title="Candidates",
                body_markdown=(
                    "# Candidates\n\n| Name | Source |\n|---|---|\n"
                    "| Example | https://example.com/item |\n\n[F01]"
                ),
                cited_finding_ids=["F01"], drafting_decisions=["Used public evidence."],
            ),
            VerificationReport(
                verdict="PASS",
                criterion_checks=[{
                    "criterion": "Every candidate has a source.",
                    "passed": True,
                    "evidence": "The row contains a source URL.",
                }],
                blocking_issues=[], revision_instructions=[], missing_information=[],
            ),
        ]

    def generate_json(self, *, schema: type, **_: object):
        value = self.outputs.pop(0)
        assert isinstance(value, schema)
        return value


def test_public_research_flows_into_agents_and_xlsx(monkeypatch, tmp_path: Path) -> None:
    intake = IntakeRequest(
        goal="Find current candidates.", desired_output="Excel",
        public_research_allowed=True, max_revision_rounds=0,
    )
    requirements = RequirementsAnalysis(
        supported=True, support_reason="Public research is allowed.",
        normalized_goal="Find current candidates.", deliverables=["Excel list"],
        mandatory_information=[], optional_information=[],
        acceptance_criteria=["Every candidate has a source."], assumptions=[],
        consolidated_questions=[], ready_for_estimate=True,
    )
    research = PublicResearchResult(
        query=intake.goal,
        answer_markdown=(
            "| Name | Source |\n|---|---|\n| Example | https://example.com/item |"
        ),
        sources=[{
            "source_id": "W01", "title": "Example", "url": "https://example.com/item",
            "domain": "example.com",
        }],
    )
    monkeypatch.setattr(
        "onebrief.execution_pipeline.run_grounded_research",
        lambda *_args, **_kwargs: research,
    )
    estimate = estimate_budget(intake, requirements)
    run_dir = tmp_path / "run"
    BudgetStore(run_dir).approve(estimate, estimate.recommended_approval_usd)
    output = tmp_path / "output"
    result = ExecutionPipeline(run_dir, gateway=FakeGateway()).run(
        intake=intake, requirements=requirements, sources=[], output_dir=output,
    )
    assert result.status == "complete"
    assert (output / "public_research.json").exists()
    assert (output / "result.xlsx").read_bytes().startswith(b"PK")


def test_missing_search_sources_fall_back_only_when_internal_authority_exists(
    monkeypatch, tmp_path: Path
) -> None:
    intake = IntakeRequest(
        goal="Complete the supplied product page.",
        public_research_allowed=True,
        max_revision_rounds=0,
    )
    requirements = RequirementsAnalysis(
        supported=True,
        support_reason="The product brief is authoritative.",
        normalized_goal=intake.goal,
        deliverables=["Product page"],
        mandatory_information=[],
        optional_information=[],
        acceptance_criteria=["Use only supplied product facts."],
        assumptions=[],
        consolidated_questions=[],
        ready_for_estimate=True,
    )
    monkeypatch.setattr(
        "onebrief.execution_pipeline.run_grounded_research",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ValueError("Google Search returned no grounded source URLs.")
        ),
    )
    estimate = estimate_budget(intake, requirements)
    run_dir = tmp_path / "run"
    BudgetStore(run_dir).approve(estimate, estimate.recommended_approval_usd)
    output = tmp_path / "output"

    result = ExecutionPipeline(run_dir, gateway=FakeGateway()).run(
        intake=intake,
        requirements=requirements,
        sources=[InternalSource(
            name="product-brief.md",
            priority=SourcePriority.MANDATORY,
            content="The approved product fact.",
        )],
        output_dir=output,
    )

    assert result.status == "complete"
    assert (output / "public_research_unavailable.json").is_file()
    assert not (output / "public_research.json").exists()
