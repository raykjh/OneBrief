from pathlib import Path

from onebrief.budget_guard import BudgetStore
from onebrief.execution_pipeline import ExecutionPipeline
from onebrief.execution_schemas import AnalysisPackage, DraftArtifact, VerificationReport
from onebrief.producer import estimate_budget
from onebrief.public_research import PublicResearchResult
from onebrief.schemas import (
    CompletionContract, IntakeRequest, InternalSource, QualityCriterion,
    RequirementsAnalysis, SourcePriority,
)


class FakeGateway:
    def __init__(self) -> None:
        self.outputs = [
            AnalysisPackage(
                objective="Create a current candidate list.",
                findings=[{
                    "finding_id": "F01", "source_name": "public_research.md",
                    "source_refs": ["W01"],
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
                    "criterion_id": "Q01",
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


def test_research_reentry_refines_evidence_before_rebuilding_analysis(
    monkeypatch, tmp_path: Path
) -> None:
    intake = IntakeRequest(
        goal="Compare current candidates.", public_research_allowed=True,
        max_revision_rounds=0,
    )
    requirements = RequirementsAnalysis(
        supported=True,
        support_reason="Public research is allowed.",
        normalized_goal=intake.goal,
        deliverables=["Comparison"],
        mandatory_information=[], optional_information=[],
        acceptance_criteria=["Compare at least three named products."],
        completion_contract=CompletionContract(
            target_state="A sourced comparison is complete.",
            quality_criteria=[QualityCriterion(
                criterion_id="Q01",
                description="현재 유사 제품 조사",
                evidence_required="유사 제품 3개 이상의 비교 분석 테이블",
            )],
        ),
        assumptions=[], consolidated_questions=[], ready_for_estimate=True,
    )
    initial = PublicResearchResult(
        query=intake.goal,
        answer_markdown="| 제품 | 기능 |\n|---|---|\n| Alpha | A |\n| Beta | B |\n| Gamma | C |",
        sources=[{"source_id": "W01", "title": "Catalog", "url": "https://example.com/catalog/items", "domain": "example.com"}],
    )
    invalid = initial.model_copy()
    valid = initial.model_copy(update={
        "answer_markdown": (
            "| 제품 | 기능 | 직접 출처 |\n|---|---|---|\n"
            "| Alpha | A | https://example.com/products/a |\n"
            "| Beta | B | https://example.com/products/b |\n"
            "| Gamma | C | https://example.com/products/c |"
        ),
        "sources": [
            {"source_id": "W01", "title": "Alpha", "url": "https://example.com/products/a", "domain": "example.com"},
            {"source_id": "W02", "title": "Beta", "url": "https://example.com/products/b", "domain": "example.com"},
            {"source_id": "W03", "title": "Gamma", "url": "https://example.com/products/c", "domain": "example.com"},
        ],
    })
    calls = []

    def fake_research(*_args, **kwargs):
        calls.append(kwargs["stage"])
        return invalid if len(calls) == 1 else valid

    monkeypatch.setattr("onebrief.execution_pipeline.run_grounded_research", fake_research)
    estimate = estimate_budget(intake, requirements)
    run_dir = tmp_path / "run"
    BudgetStore(run_dir).approve(estimate, estimate.recommended_approval_usd)
    output = tmp_path / "output"
    output.mkdir()
    (output / "public_research.json").write_text(initial.model_dump_json(), "utf-8")
    (output / "public_research.md").write_text(initial.answer_markdown, "utf-8")
    (output / "research_reentry_request.json").write_text(
        '{"blocking_issues":["item URLs missing"],"max_refinement_calls":2}', "utf-8"
    )

    gateway = FakeGateway()
    gateway.outputs[1] = DraftArtifact(
        title="Candidates",
        body_markdown=(
            "# Candidates\n\n| Name | Source |\n|---|---|\n"
            "| Alpha | https://example.com/products/a |\n"
            "| Beta | https://example.com/products/b |\n"
            "| Gamma | https://example.com/products/c |\n\n[F01]"
        ),
        cited_finding_ids=["F01"],
        drafting_decisions=["Used refined public evidence."],
    )
    gateway.outputs[2] = VerificationReport(
        verdict="PASS",
        criterion_checks=[{
            "criterion_id": "Q01",
            "criterion": "현재 유사 제품 조사",
            "passed": True,
            "evidence": "Three named rows carry direct URLs.",
        }],
        blocking_issues=[], revision_instructions=[], missing_information=[],
    )
    result = ExecutionPipeline(run_dir, gateway=gateway).run(
        intake=intake, requirements=requirements, sources=[], output_dir=output,
    )

    assert result.status == "complete"
    assert calls == ["public_research_refinement_r1", "public_research_refinement_r2"]
    status = __import__("json").loads(
        (output / "research_reentry_status.json").read_text("utf-8")
    )
    assert status["resolved"] is True
    assert PublicResearchResult.model_validate_json(
        (output / "public_research.json").read_text("utf-8")
    ).answer_markdown == valid.answer_markdown
