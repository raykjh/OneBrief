"""Deterministic, reproducible evaluation metrics for OneBrief executions."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, Field

from onebrief.budget_guard import CallStatus, CostLedger, micros_to_dollars
from onebrief.completion_ledger import CompletionLedger
from onebrief.jobs import JobRecord, JobStatus
from onebrief.phase_execution import PhaseAttemptLedger, phase_for_stage


class ExecutionEvaluation(BaseModel):
    schema_version: str = "onebrief-execution-evaluation-v1"
    job_id: str
    benchmark_variant: str
    terminal_status: JobStatus
    completion_proven: bool
    required_criteria_total: int = Field(ge=0)
    required_criteria_passed: int = Field(ge=0)
    completion_rate: float = Field(ge=0, le=1)
    revision_rounds: int = Field(ge=0)
    model_calls: int = Field(ge=0)
    denied_calls: int = Field(ge=0)
    actual_input_tokens: int = Field(ge=0)
    actual_output_tokens: int = Field(ge=0)
    actual_cost_usd: float = Field(ge=0)
    approved_cost_usd: float = Field(ge=0)
    budget_utilization: float = Field(ge=0, le=1)
    phase_costs_usd: dict[str, float] = Field(default_factory=dict)
    deterministic_attempts_by_phase: dict[str, int] = Field(default_factory=dict)
    adk_event_count: int = Field(ge=0)
    distinct_agent_authors: list[str]
    user_supplement_count: int = Field(ge=0)
    elapsed_seconds: float = Field(ge=0)
    evaluated_at: str


class ABComparison(BaseModel):
    schema_version: str = "onebrief-ab-comparison-v1"
    case_id: str
    goal_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    baseline: ExecutionEvaluation
    onebrief: ExecutionEvaluation
    completion_rate_delta: float
    user_supplement_delta: int
    cost_delta_usd: float
    elapsed_delta_seconds: float


def _load(path: Path, model: type[BaseModel]) -> BaseModel | None:
    if not path.is_file():
        return None
    return model.model_validate_json(path.read_text(encoding="utf-8"))


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def build_execution_evaluation(job_dir: Path, status: JobStatus) -> ExecutionEvaluation:
    job_dir = job_dir.resolve()
    record = JobRecord.model_validate_json((job_dir / "job.json").read_text(encoding="utf-8"))
    ledger = _load(job_dir / "run" / "cost_ledger.json", CostLedger)
    completion = _load(job_dir / "work" / "completion_ledger.json", CompletionLedger)
    trace_path = job_dir / "work" / "adk_convergence_trace.json"
    trace = json.loads(trace_path.read_text(encoding="utf-8")) if trace_path.is_file() else {}
    events = trace.get("events", []) if isinstance(trace, dict) else []
    source_path = job_dir / "inputs" / "sources.json"
    sources = json.loads(source_path.read_text(encoding="utf-8")) if source_path.is_file() else []
    settled = [
        item for item in (ledger.entries if ledger else []) if item.status == CallStatus.SETTLED
    ]
    denied = [
        item for item in (ledger.entries if ledger else []) if item.status == CallStatus.DENIED
    ]
    phase_costs_micros: dict[str, int] = {}
    for item in settled:
        phase = phase_for_stage(item.stage).value
        phase_costs_micros[phase] = (
            phase_costs_micros.get(phase, 0) + item.actual_usd_micros
        )
    attempt_ledger = _load(
        job_dir / "run" / "phase_attempts.json", PhaseAttemptLedger
    )
    deterministic_attempts: dict[str, int] = {}
    if isinstance(attempt_ledger, PhaseAttemptLedger):
        for item in attempt_ledger.attempts:
            deterministic_attempts[item.phase.value] = (
                deterministic_attempts.get(item.phase.value, 0) + 1
            )
    approved = ledger.approval.approved_usd_micros if ledger else 0
    actual = ledger.actual_usd_micros if ledger else 0
    required_total = completion.required_total if completion else 0
    required_passed = completion.required_passed if completion else 0
    evaluated_at = datetime.now(UTC)
    elapsed = max(0.0, (evaluated_at - _parse_time(record.created_at)).total_seconds())
    return ExecutionEvaluation(
        job_id=record.job_id,
        benchmark_variant=record.benchmark_variant,
        terminal_status=status,
        completion_proven=bool(completion and completion.complete),
        required_criteria_total=required_total,
        required_criteria_passed=required_passed,
        completion_rate=(required_passed / required_total if required_total else 0.0),
        revision_rounds=completion.revision_rounds if completion else 0,
        model_calls=len(settled),
        denied_calls=len(denied),
        actual_input_tokens=sum(item.actual_input_tokens for item in settled),
        actual_output_tokens=sum(item.actual_output_tokens for item in settled),
        actual_cost_usd=micros_to_dollars(actual),
        approved_cost_usd=micros_to_dollars(approved),
        budget_utilization=(actual / approved if approved else 0.0),
        phase_costs_usd={
            phase: micros_to_dollars(cost)
            for phase, cost in sorted(phase_costs_micros.items())
        },
        deterministic_attempts_by_phase=dict(sorted(deterministic_attempts.items())),
        adk_event_count=len(events),
        distinct_agent_authors=sorted({
            str(item.get("author")) for item in events if item.get("author")
        }),
        user_supplement_count=sum(
            1 for item in sources
            if isinstance(item, dict) and str(item.get("name", "")).startswith("user-supplement-")
        ),
        elapsed_seconds=round(elapsed, 3),
        evaluated_at=evaluated_at.isoformat(),
    )


def persist_execution_evaluation(job_dir: Path, status: JobStatus) -> ExecutionEvaluation:
    evaluation = build_execution_evaluation(job_dir, status)
    target = job_dir / "work" / "evaluation_metrics.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(f".json.{uuid4().hex}.tmp")
    temporary.write_text(evaluation.model_dump_json(indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, target)
    return evaluation


def compare_executions(
    *, case_id: str, goal_sha256: str, baseline: ExecutionEvaluation,
    onebrief: ExecutionEvaluation,
) -> ABComparison:
    return ABComparison(
        case_id=case_id,
        goal_sha256=goal_sha256,
        baseline=baseline,
        onebrief=onebrief,
        completion_rate_delta=round(onebrief.completion_rate - baseline.completion_rate, 6),
        user_supplement_delta=(
            onebrief.user_supplement_count - baseline.user_supplement_count
        ),
        cost_delta_usd=round(onebrief.actual_cost_usd - baseline.actual_cost_usd, 6),
        elapsed_delta_seconds=round(onebrief.elapsed_seconds - baseline.elapsed_seconds, 3),
    )
