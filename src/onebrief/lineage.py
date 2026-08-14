"""Aggregate a continuation family without double-counting executions."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field

from onebrief.budget_guard import CostLedger, CallStatus, micros_to_dollars
from onebrief.jobs import JobRecord


class RunLineageSummary(BaseModel):
    schema_version: Literal["onebrief-run-lineage-v1"] = "onebrief-run-lineage-v1"
    root_job_id: str
    current_job_id: str
    job_ids: list[str] = Field(min_length=1)
    depth: int = Field(ge=0)
    actual_cost_usd: float = Field(ge=0)
    approved_cost_usd: float = Field(ge=0)
    model_calls: int = Field(ge=0)
    denied_calls: int = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    revision_rounds: int = Field(ge=0)
    elapsed_seconds: float = Field(ge=0)


def _json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def _prefix(job_dir: Path) -> dict[str, object]:
    manifest = _json(job_dir / "work" / "continuation_manifest.json")
    value = manifest.get("ancestor_lineage")
    return value if isinstance(value, dict) else {}


def build_run_lineage(job_dir: Path) -> RunLineageSummary:
    job_dir = job_dir.resolve()
    record = JobRecord.model_validate_json((job_dir / "job.json").read_text("utf-8"))
    ledger = CostLedger.model_validate_json((job_dir / "run" / "cost_ledger.json").read_text("utf-8"))
    evaluation = _json(job_dir / "work" / "evaluation_metrics.json")
    prefix = _prefix(job_dir)
    prior_ids = [str(item) for item in prefix.get("job_ids", [])]
    job_ids = list(dict.fromkeys([*prior_ids, record.job_id]))
    settled = [entry for entry in ledger.entries if entry.status == CallStatus.SETTLED]
    denied = [entry for entry in ledger.entries if entry.status == CallStatus.DENIED]
    return RunLineageSummary(
        root_job_id=str(prefix.get("root_job_id") or job_ids[0]),
        current_job_id=record.job_id,
        job_ids=job_ids,
        depth=len(job_ids) - 1,
        actual_cost_usd=round(float(prefix.get("actual_cost_usd", 0)) + micros_to_dollars(ledger.actual_usd_micros), 6),
        approved_cost_usd=round(float(prefix.get("approved_cost_usd", 0)) + micros_to_dollars(ledger.approval.approved_usd_micros), 6),
        model_calls=int(prefix.get("model_calls", 0)) + len(settled),
        denied_calls=int(prefix.get("denied_calls", 0)) + len(denied),
        input_tokens=int(prefix.get("input_tokens", 0)) + sum(item.actual_input_tokens for item in settled),
        output_tokens=int(prefix.get("output_tokens", 0)) + sum(item.actual_output_tokens for item in settled),
        revision_rounds=int(prefix.get("revision_rounds", 0)) + int(evaluation.get("revision_rounds", 0)),
        elapsed_seconds=round(float(prefix.get("elapsed_seconds", 0)) + float(evaluation.get("elapsed_seconds", 0)), 3),
    )


def persist_run_lineage(job_dir: Path) -> RunLineageSummary:
    summary = build_run_lineage(job_dir)
    target = job_dir / "work" / "lineage_summary.json"
    temp = target.with_suffix(f".json.{uuid4().hex}.tmp")
    temp.write_text(summary.model_dump_json(indent=2) + "\n", encoding="utf-8")
    os.replace(temp, target)
    return summary


def ancestor_lineage_payload(source_job_dir: Path) -> dict[str, object]:
    existing = source_job_dir / "work" / "lineage_summary.json"
    if existing.is_file():
        return RunLineageSummary.model_validate_json(existing.read_text("utf-8")).model_dump(mode="json")
    return build_run_lineage(source_job_dir).model_dump(mode="json")
