import zipfile
from pathlib import Path

from onebrief.budget_guard import BudgetStore
from onebrief.jobs import create_job
from onebrief.producer import estimate_budget
from onebrief.public_research import (
    PublicResearchResult,
    merge_public_sources,
    resolve_public_source,
    web_source,
)
from onebrief.schemas import IntakeRequest, RequirementsAnalysis
from onebrief.workbook_export import export_workbook, markdown_tables


def _requirements() -> RequirementsAnalysis:
    return RequirementsAnalysis(
        supported=True,
        support_reason="Current public facts can be researched.",
        normalized_goal="Research current candidates and create an Excel list.",
        deliverables=["Candidate list", "Excel workbook"],
        mandatory_information=[],
        optional_information=[],
        acceptance_criteria=["Every candidate includes a source and checked date."],
        assumptions=[],
        consolidated_questions=[],
        ready_for_estimate=True,
    )


def _intake() -> IntakeRequest:
    return IntakeRequest(
        goal="Find current public candidates.",
        desired_output="Excel list",
        public_research_allowed=True,
        budget_limit_usd=0.5,
        max_revision_rounds=0,
    )


def test_public_research_is_estimated_and_source_less_job_is_allowed(tmp_path: Path) -> None:
    intake = _intake()
    estimate = estimate_budget(intake, _requirements())
    research = next(stage for stage in estimate.stages if stage.stage == "public_research")
    assert research.stage == "public_research"
    assert research.model == "gemini-3.5-flash"
    assert research.fixed_cost_usd_per_call == 0.035
    job = create_job(
        jobs_dir=tmp_path / "jobs",
        intake=intake,
        requirements=_requirements(),
        sources=[],
        estimate=estimate,
        approved_usd=estimate.recommended_approval_usd,
    )
    assert (job / "inputs" / "sources.json").read_text(encoding="utf-8").strip() == "[]"


def test_grounded_prompt_fixed_fee_is_reserved_and_settled(tmp_path: Path) -> None:
    intake = _intake()
    estimate = estimate_budget(intake, _requirements())
    store = BudgetStore(tmp_path)
    store.approve(estimate, 0.5)
    call = store.reserve_call(
        stage="public_research",
        model="gemini-3.5-flash",
        input_token_cap=100,
        output_token_cap=100,
        fixed_cost_usd=0.035,
    )
    assert call.fixed_cost_cap_micros == 35_000
    ledger = store.settle_call(
        call.call_id,
        input_tokens=50,
        output_tokens=50,
        fixed_cost_usd=0.035,
    )
    assert ledger.actual_usd_micros >= 35_000
    assert ledger.actual_usd_micros <= ledger.approval.approved_usd_micros


def test_google_grounding_redirect_is_resolved_to_observed_public_url(monkeypatch) -> None:
    class Response:
        def __init__(self, status_code, url, location=None):
            self.status_code = status_code
            self.url = url
            self.headers = {"location": location} if location else {}

    class Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def head(self, url):
            if "grounding-api-redirect" in url:
                return Response(302, url, "https://vendor.example/products/42")
            return Response(200, url)

    monkeypatch.setattr("onebrief.public_research._public_host", lambda _host: True)
    monkeypatch.setattr("onebrief.public_research.httpx.Client", Client)
    source = web_source(
        "W01", "vendor.example",
        "https://vertexaisearch.cloud.google.com/grounding-api-redirect/abc",
    )

    resolved = resolve_public_source(source)

    assert resolved.resolved_url == "https://vendor.example/products/42"
    assert resolved.http_status == 200
    internal = PublicResearchResult(
        query="test", answer_markdown="answer", sources=[resolved]
    ).as_internal_source()
    assert "https://vendor.example/products/42 [HTTP 200]" in internal.content


def test_head_not_allowed_falls_back_to_bounded_get(monkeypatch) -> None:
    class Response:
        def __init__(self, status_code, url):
            self.status_code = status_code
            self.url = url
            self.headers = {}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    class Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def head(self, url):
            return Response(405, url)

        def stream(self, method, url, headers):
            assert method == "GET"
            assert headers == {"Range": "bytes=0-0"}
            return Response(200, url)

    monkeypatch.setattr("onebrief.public_research._public_host", lambda _host: True)
    monkeypatch.setattr("onebrief.public_research.httpx.Client", Client)
    source = web_source("W01", "vendor.example", "https://vendor.example/products/42")

    resolved = resolve_public_source(source)

    assert resolved.http_status == 200


def test_refinement_source_merge_preserves_prior_observed_evidence() -> None:
    first = web_source("W01", "a.example", "https://a.example/products/1").model_copy(
        update={"resolved_url": "https://a.example/products/1", "http_status": 200}
    )
    repeated = first.model_copy(update={"source_id": "W09"})
    second = web_source("W01", "b.example", "https://b.example/products/2")

    merged = merge_public_sources([first], [repeated, second])

    assert [item.source_id for item in merged] == ["W01", "W02"]
    assert [item.url for item in merged] == [
        "https://a.example/products/1",
        "https://b.example/products/2",
    ]


def test_refinement_source_merge_upgrades_failed_observation() -> None:
    stale = web_source("W01", "a.example", "https://google.example/redirect").model_copy(
        update={"resolved_url": "https://a.example/products/1", "http_status": 405}
    )
    observed = stale.model_copy(update={"source_id": "W07", "http_status": 200})

    merged = merge_public_sources([stale], [observed])

    assert len(merged) == 1
    assert merged[0].source_id == "W01"
    assert merged[0].http_status == 200


def test_markdown_table_becomes_real_xlsx_with_sources(tmp_path: Path) -> None:
    markdown = """# 결과

| 단지 | 전용면적 | 가격 | 출처 |
|---|---:|---:|---|
| 예시단지 | 84㎡ | 9억원 | https://example.com/listing |
"""
    research = PublicResearchResult(
        query="test",
        answer_markdown=markdown,
        sources=[
            {
                "source_id": "W01",
                "title": "Example listing",
                "url": "https://example.com/listing",
                "domain": "example.com",
            }
        ],
    )
    assert markdown_tables(markdown)[0][1][0] == "예시단지"
    output = tmp_path / "result.xlsx"
    export_workbook(markdown, output, research)
    assert output.read_bytes().startswith(b"PK")
    with zipfile.ZipFile(output) as archive:
        workbook_xml = archive.read("xl/workbook.xml").decode("utf-8")
        assert "결과" in workbook_xml
        assert "공개출처" in workbook_xml
