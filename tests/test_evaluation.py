from __future__ import annotations

import json
from pathlib import Path

from onebrief.budget_guard import BudgetStore
from onebrief.evaluation import build_execution_evaluation, compare_executions
from onebrief.jobs import JobRecord, JobStatus
from onebrief.producer import PRICE_CARD_VERSION
from onebrief.schemas import BudgetEnvelope, BudgetStatus, StageEstimate


def _write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def test_execution_evaluation_reads_durable_evidence(tmp_path: Path) -> None:
    job = JobRecord(
        job_id="job-1",
        status=JobStatus.RUNNING,
        created_at="2026-08-10T00:00:00+00:00",
        updated_at="2026-08-10T00:00:00+00:00",
        attempts=1,
        run_id="run-1",
        benchmark_variant="single_agent_baseline",
    )
    _write(tmp_path / "job.json", job.model_dump(mode="json"))
    estimate = BudgetEnvelope(
        price_card_version=PRICE_CARD_VERSION,
        price_source_url="https://example.test/pricing",
        endpoint="global-standard",
        estimated_source_tokens=100,
        estimated_contract_tokens=100,
        minimum_cost_usd=0.001,
        recommended_cost_usd=0.1,
        maximum_cost_usd=0.2,
        recommended_approval_usd=0.1,
        budget_limit_usd=1.0,
        status=BudgetStatus.WITHIN_BUDGET,
        estimated_minutes_minimum=1,
        estimated_minutes_recommended=1,
        estimated_minutes_maximum=1,
        notes=[],
        stages=[StageEstimate(
            stage="analysis", model="gemini-3.5-flash-lite",
            input_tokens_per_call=100, output_tokens_per_call=100,
            estimated_minutes_per_call=1,
            minimum_calls=1, recommended_calls=1, maximum_calls=1,
            minimum_cost_usd=0.001, recommended_cost_usd=0.002,
            maximum_cost_usd=0.003,
        )],
    )
    BudgetStore(tmp_path / "run").approve(estimate, 0.1)
    _write(tmp_path / "inputs" / "sources.json", [{"name": "user-supplement-1"}])
    _write(tmp_path / "work" / "completion_ledger.json", {
        "target_state": "done", "pass_condition": "all", "criteria": [],
        "required_total": 2, "required_passed": 1, "complete": False,
        "latest_verdict": "REVISE", "revision_rounds": 1,
    })
    _write(tmp_path / "work" / "adk_convergence_trace.json", {
        "events": [{"author": "maker"}, {"author": "verifier"}],
    })
    result = build_execution_evaluation(tmp_path, JobStatus.PARTIAL)
    assert result.benchmark_variant == "single_agent_baseline"
    assert result.completion_rate == 0.5
    assert result.user_supplement_count == 1
    assert result.distinct_agent_authors == ["maker", "verifier"]


def test_comparison_uses_same_goal_hash(tmp_path: Path) -> None:
    # Reuse one deterministic metric as both sides; deltas must be zero.
    test_execution_evaluation_reads_durable_evidence(tmp_path)
    metric = build_execution_evaluation(tmp_path, JobStatus.PARTIAL)
    comparison = compare_executions(
        case_id="exchange-release",
        goal_sha256="a" * 64,
        baseline=metric,
        onebrief=metric,
    )
    assert comparison.completion_rate_delta == 0
    assert comparison.cost_delta_usd == 0
