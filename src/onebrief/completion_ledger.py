"""Durable, user-facing completion status derived from contracts and evidence."""

from __future__ import annotations

import os
import re
from enum import StrEnum
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, Field

from onebrief.execution_schemas import VerificationReport, Verdict
from onebrief.schemas import CompletionContract, EvaluationMode


class CompletionStatus(StrEnum):
    PENDING = "pending"
    PASSED = "passed"
    REVISE = "revise"
    NEEDS_INFORMATION = "needs_information"
    UNVERIFIABLE = "unverifiable"


class CriterionAttempt(BaseModel):
    round_number: int = Field(ge=0)
    passed: bool
    evidence: str
    verdict: Verdict


class CompletionCriterionState(BaseModel):
    criterion_id: str
    description: str
    evaluation_mode: EvaluationMode
    evidence_required: str
    required: bool
    status: CompletionStatus = CompletionStatus.PENDING
    attempts: list[CriterionAttempt] = Field(default_factory=list)
    failure_reasons: list[str] = Field(default_factory=list)
    revision_instructions: list[str] = Field(default_factory=list)


class CompletionLedger(BaseModel):
    """The product-level answer to: what remains before this work is done?"""

    target_state: str
    pass_condition: str
    criteria: list[CompletionCriterionState]
    required_total: int
    required_passed: int
    complete: bool
    latest_verdict: Verdict | None = None
    revision_rounds: int = 0


def _normalized(value: str) -> str:
    return re.sub(r"[^0-9a-z가-힣]+", "", value.casefold())


def _round_number(path: Path) -> int:
    match = re.search(r"verification_r(\d+)\.json$", path.name)
    return int(match.group(1)) if match else 0


def _match_index(contract: CompletionContract, criterion_id: str | None, label: str) -> int | None:
    if criterion_id:
        for index, criterion in enumerate(contract.quality_criteria):
            if criterion.criterion_id == criterion_id:
                return index
    normalized_label = _normalized(label)
    if not normalized_label:
        return None
    for index, criterion in enumerate(contract.quality_criteria):
        normalized_description = _normalized(criterion.description)
        if (
            normalized_label == normalized_description
            or normalized_label in normalized_description
            or normalized_description in normalized_label
        ):
            return index
    return None


def build_completion_ledger(
    contract: CompletionContract,
    reports: list[tuple[int, VerificationReport]],
) -> CompletionLedger:
    states = [
        CompletionCriterionState(
            criterion_id=item.criterion_id,
            description=item.description,
            evaluation_mode=item.evaluation_mode,
            evidence_required=item.evidence_required,
            required=item.required,
        )
        for item in contract.quality_criteria
    ]
    ordered_reports = sorted(reports, key=lambda item: item[0])
    for round_number, report in ordered_reports:
        unmatched = list(range(len(states)))
        for check in report.criterion_checks:
            index = _match_index(contract, check.criterion_id, check.criterion)
            if index is None and len(report.criterion_checks) == len(states) and unmatched:
                # Backward compatibility for old reports produced before criterion IDs.
                index = unmatched[0]
            if index is None:
                continue
            if index in unmatched:
                unmatched.remove(index)
            state = states[index]
            state.attempts.append(CriterionAttempt(
                round_number=round_number,
                passed=check.passed,
                evidence=check.evidence,
                verdict=report.verdict,
            ))
            if check.passed:
                state.status = CompletionStatus.PASSED
                state.failure_reasons = []
                state.revision_instructions = []
            else:
                state.status = {
                    Verdict.NEEDS_INFORMATION: CompletionStatus.NEEDS_INFORMATION,
                    Verdict.UNVERIFIABLE: CompletionStatus.UNVERIFIABLE,
                }.get(report.verdict, CompletionStatus.REVISE)
                state.failure_reasons = list(report.blocking_issues)
                state.revision_instructions = list(report.revision_instructions)

    required = [item for item in states if item.required]
    passed = [item for item in required if item.status == CompletionStatus.PASSED]
    latest = ordered_reports[-1][1].verdict if ordered_reports else None
    return CompletionLedger(
        target_state=contract.target_state,
        pass_condition=contract.pass_condition,
        criteria=states,
        required_total=len(required),
        required_passed=len(passed),
        complete=bool(required) and len(passed) == len(required) and latest == Verdict.PASS,
        latest_verdict=latest,
        revision_rounds=max((number for number, _ in ordered_reports), default=0),
    )


def refresh_completion_ledger(contract: CompletionContract, output_dir: Path) -> CompletionLedger:
    reports: list[tuple[int, VerificationReport]] = []
    for path in sorted(output_dir.glob("verification_r*.json"), key=_round_number):
        reports.append((_round_number(path), VerificationReport.model_validate_json(
            path.read_text(encoding="utf-8")
        )))
    final_path = output_dir / "final_verification.json"
    if final_path.exists():
        final = VerificationReport.model_validate_json(final_path.read_text(encoding="utf-8"))
        final_round = max((number for number, _ in reports), default=0)
        if not reports or reports[-1][1] != final:
            reports.append((final_round, final))
    ledger = build_completion_ledger(contract, reports)
    path = output_dir / "completion_ledger.json"
    temporary = path.with_suffix(f".json.{uuid4().hex}.tmp")
    temporary.write_text(ledger.model_dump_json(indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)
    return ledger
