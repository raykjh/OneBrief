"""Deterministic L0 handoff projected from durable OneBrief job state."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field

from onebrief.budget_guard import CostLedger, micros_to_dollars
from onebrief.governance import build_decision_request, canonical_digest
from onebrief.schemas import IntakeRequest, RequirementsAnalysis


class L0ResumeCapsule(BaseModel):
    schema_version: Literal["onebrief-l0-resume-capsule-v1"] = "onebrief-l0-resume-capsule-v1"
    capsule_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    goal: str
    current_position: str
    next_action: str
    completion_criteria: list[str]
    prohibitions: list[str]
    unresolved_conflicts: list[str]
    base_commit: str | None = Field(default=None, pattern=r"^[a-f0-9]{40}$")
    authorization_status: str
    approved_budget_usd: float = Field(ge=0)
    spent_budget_usd: float = Field(ge=0)
    remaining_budget_usd: float = Field(ge=0)


def _json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def _next_action(status: str) -> str:
    return {
        "complete": "Review the verified result and explicitly approve any external application.",
        "partial": "Resolve the evidence gap before claiming completion.",
        "needs_information": "Provide the requested result-changing information.",
        "needs_budget": "Approve additional budget or reduce scope.",
        "needs_authorization": "Approve a refreshed authority envelope or remove the blocked action.",
        "failed": "Inspect the classified failure and start only an approved bounded recovery.",
    }.get(status, "Resume the current approved stage from its durable checkpoint.")


def build_resume_capsule(job_dir: Path, *, status: str, stage: str, message: str) -> L0ResumeCapsule:
    job_dir = job_dir.resolve()
    intake = IntakeRequest.model_validate_json((job_dir / "inputs" / "intake.json").read_text("utf-8"))
    requirements = RequirementsAnalysis.model_validate_json(
        (job_dir / "inputs" / "requirements.json").read_text("utf-8")
    )
    ledger = CostLedger.model_validate_json((job_dir / "run" / "cost_ledger.json").read_text("utf-8"))
    provenance = _json(job_dir / "work" / "project_snapshot" / "restore_evidence.json")
    approved = ledger.approval.approved_usd_micros
    spent = ledger.actual_usd_micros
    decision = build_decision_request(status=status, stage=stage, message=message)
    conflicts = [message[:1200]] if decision and decision.reason.value == "conflict_detected" else []
    payload: dict[str, object] = {
        "goal": intake.goal,
        "current_position": f"{status}:{stage}",
        "next_action": _next_action(status),
        "completion_criteria": list(requirements.acceptance_criteria),
        "prohibitions": [
            "Do not exceed the immutable approved budget.",
            "Do not expand ToolPack or source-write authority autonomously.",
            "Do not claim completion without independent evidence.",
            "Do not apply results to the source without explicit approval and unchanged revision.",
        ],
        "unresolved_conflicts": conflicts,
        "base_commit": provenance.get("source_head_sha"),
        "authorization_status": "requires_user_decision" if decision else "within_existing_authority",
        "approved_budget_usd": micros_to_dollars(approved),
        "spent_budget_usd": micros_to_dollars(spent),
        "remaining_budget_usd": micros_to_dollars(max(0, approved - spent - ledger.reserved_usd_micros)),
    }
    return L0ResumeCapsule(capsule_digest=canonical_digest(payload), **payload)


def persist_resume_capsule(
    job_dir: Path, *, status: str, stage: str, message: str,
) -> L0ResumeCapsule:
    capsule = build_resume_capsule(job_dir, status=status, stage=stage, message=message)
    target = job_dir / "work" / "resume_capsule_l0.json"
    temp = target.with_suffix(f".json.{uuid4().hex}.tmp")
    temp.write_text(capsule.model_dump_json(indent=2) + "\n", encoding="utf-8")
    os.replace(temp, target)
    return capsule
