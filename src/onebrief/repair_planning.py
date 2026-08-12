"""Criterion-scoped repair plans for bounded convergence loops."""

from __future__ import annotations

import hashlib
import re
from enum import StrEnum

from pydantic import BaseModel, Field

from onebrief.execution_schemas import VerificationReport
from onebrief.schemas import CompletionContract


class RepairDisposition(StrEnum):
    BOUNDED_REPAIR = "bounded_repair"
    DECOMPOSE_SCOPE = "decompose_scope"
    ESCALATE = "escalate"


class RepairTask(BaseModel):
    """One failed completion criterion, isolated from already passing behavior."""

    task_id: str = Field(pattern=r"^R[0-9]{2}$")
    criterion_id: str | None = Field(default=None, pattern=r"^Q[0-9]{2}$")
    objective: str = Field(min_length=3, max_length=600)
    failure_evidence: list[str] = Field(min_length=1, max_length=8)
    revision_instructions: list[str] = Field(min_length=1, max_length=8)
    preserve_criterion_ids: list[str] = Field(default_factory=list, max_length=12)
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    occurrence: int = Field(ge=1)
    disposition: RepairDisposition


class RepairPlan(BaseModel):
    """Deterministic bridge from verifier evidence to the next maker turn."""

    schema_version: str = "onebrief-repair-plan-v1"
    round_number: int = Field(ge=0)
    tasks: list[RepairTask] = Field(min_length=1, max_length=12)
    passing_criterion_ids: list[str] = Field(default_factory=list, max_length=12)
    stop_after_this_round: bool = False
    rationale: str = Field(min_length=3, max_length=800)


def _normalize_failure(value: str) -> str:
    normalized = value.casefold()
    normalized = re.sub(r"\b[0-9a-f]{8}-[0-9a-f-]{27,}\b", "<uuid>", normalized)
    normalized = re.sub(r"[a-z]:\\[^\n\r:]+", "<path>", normalized)
    normalized = re.sub(r"\b\d+(?:\.\d+)?\b", "<n>", normalized)
    return " ".join(normalized.split())


def _fingerprint(criterion_id: str | None, evidence: list[str]) -> str:
    identity = f"{criterion_id or 'system'}|" + "|".join(
        _normalize_failure(item) for item in evidence
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def build_repair_plan(
    contract: CompletionContract,
    report: VerificationReport,
    *,
    round_number: int,
    prior_fingerprints: list[str] | None = None,
) -> RepairPlan | None:
    """Create the smallest next-turn plan from failed criterion evidence.

    A repeated failure is not treated as permission for more of the same. The
    second observation asks the maker to decompose the repair, and the third
    marks the plan for escalation after one final bounded attempt.
    """

    failed = [check for check in report.criterion_checks if not check.passed]
    if not failed:
        return None
    passing = [
        check.criterion_id
        for check in report.criterion_checks
        if check.passed and check.criterion_id
    ]
    known = list(prior_fingerprints or [])
    contract_by_id = {item.criterion_id: item for item in contract.quality_criteria}
    tasks: list[RepairTask] = []
    for index, check in enumerate(failed[:12], start=1):
        # A deterministic adapter can report several independent blockers.
        # When they have already been split into checks, reattaching the full
        # blocker list to every task defeats decomposition and makes the maker
        # repeatedly choose only one convenient item.
        evidence = list(dict.fromkeys(
            [check.evidence, *report.blocking_issues]
            if len(failed) == 1
            else [check.evidence]
        ))[:8]
        instructions = list(dict.fromkeys(report.revision_instructions))[:8] or [
            "Repair only the evidenced failure and preserve every passing criterion."
        ]
        # Shared blocker lists often mention several criteria. Bind repetition to
        # this criterion's own evidence so an unrelated failure cannot reset or
        # inflate its retry history.
        fingerprint = _fingerprint(check.criterion_id, [check.evidence])
        occurrence = known.count(fingerprint) + 1
        if occurrence >= 3:
            disposition = RepairDisposition.ESCALATE
        elif occurrence == 2:
            disposition = RepairDisposition.DECOMPOSE_SCOPE
            instructions = [
                "The same failure repeated. Split it into the smallest independently verifiable change before editing.",
                *instructions,
            ][:8]
        else:
            disposition = RepairDisposition.BOUNDED_REPAIR
        criterion = contract_by_id.get(check.criterion_id or "")
        objective = (
            criterion.description
            if criterion is not None
            else (check.criterion.strip() or "Repair the evidenced system failure.")
        )
        if len(objective) < 3:
            objective = f"Repair {objective} failure"
        tasks.append(RepairTask(
            task_id=f"R{index:02d}",
            criterion_id=check.criterion_id,
            objective=objective,
            failure_evidence=evidence,
            revision_instructions=instructions,
            preserve_criterion_ids=passing,
            fingerprint=fingerprint,
            occurrence=occurrence,
            disposition=disposition,
        ))
    stop = any(item.disposition == RepairDisposition.ESCALATE for item in tasks)
    return RepairPlan(
        round_number=round_number,
        tasks=tasks,
        passing_criterion_ids=passing,
        stop_after_this_round=stop,
        rationale=(
            "Repair only failed completion slices. Passing slices are frozen as regression constraints; "
            "repeated fingerprints trigger decomposition instead of blind retries."
        ),
    )
