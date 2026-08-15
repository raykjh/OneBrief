"""Durable, user-facing completion status derived from contracts and evidence."""

from __future__ import annotations

import os
import re
from enum import StrEnum
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, Field

from onebrief.execution_schemas import VerificationReport, Verdict
from onebrief.handoff_protocol import EvidenceBinding, EvidenceKind, EvidenceStatus
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
    evidence_bindings: list[EvidenceBinding] = Field(default_factory=list, max_length=32)


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


class EvidenceDimensionState(BaseModel):
    kind: EvidenceKind
    status: CompletionStatus = CompletionStatus.PENDING
    bindings: list[EvidenceBinding] = Field(default_factory=list, max_length=128)
    failure_reasons: list[str] = Field(default_factory=list)


class CompletionLedger(BaseModel):
    """The product-level answer to: what remains before this work is done?"""

    target_state: str
    pass_condition: str
    criteria: list[CompletionCriterionState]
    evidence_dimensions: list[EvidenceDimensionState] = Field(default_factory=list)
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
    dimensions = {
        kind: EvidenceDimensionState(kind=kind)
        for kind in (EvidenceKind.COMPILE, EvidenceKind.BEHAVIOR, EvidenceKind.VISUAL)
    }
    ordered_reports = sorted(reports, key=lambda item: item[0])
    for round_number, report in ordered_reports:
        failed_ids = {
            check.criterion_id
            for check in report.criterion_checks
            if not check.passed and check.criterion_id
        }
        for check in report.criterion_checks:
            index = _match_index(contract, check.criterion_id, check.criterion)
            for binding in check.evidence_bindings:
                dimension = dimensions.get(binding.kind)
                if dimension is None:
                    continue
                if all(
                    existing.binding_id != binding.binding_id
                    for existing in dimension.bindings
                ):
                    dimension.bindings.append(binding)
                if binding.status in {EvidenceStatus.FAILED, EvidenceStatus.MISSING}:
                    dimension.status = CompletionStatus.REVISE
                    dimension.failure_reasons = list(dict.fromkeys([
                        *dimension.failure_reasons,
                        binding.summary,
                    ]))
                elif dimension.status == CompletionStatus.PENDING:
                    dimension.status = CompletionStatus.PASSED
            if index is None:
                continue
            state = states[index]
            state.attempts.append(CriterionAttempt(
                round_number=round_number,
                passed=check.passed,
                evidence=check.evidence,
                verdict=report.verdict,
                evidence_bindings=check.evidence_bindings,
            ))
            if check.passed and check.criterion_id not in failed_ids:
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
        evidence_dimensions=list(dimensions.values()),
        required_total=len(required),
        required_passed=len(passed),
        complete=bool(required) and len(passed) == len(required) and latest == Verdict.PASS,
        latest_verdict=latest,
        revision_rounds=max((number for number, _ in ordered_reports), default=0),
    )


def settle_consistent_verification(
    contract: CompletionContract, report: VerificationReport
) -> VerificationReport:
    """Resolve a verifier's contradictory REVISE only when every check passes.

    Deterministic and observation gates append their own failed checks, so an
    all-pass report is the one safe case where free-form blocker text cannot
    justify another costly maker round.
    """
    required_ids = {
        criterion.criterion_id
        for criterion in contract.quality_criteria
        if criterion.required
    }
    passed_ids = {
        check.criterion_id
        for check in report.criterion_checks
        if check.passed and check.criterion_id
    }
    if (
        required_ids
        and required_ids.issubset(passed_ids)
        and report.criterion_checks
        and all(check.passed for check in report.criterion_checks)
        and not report.missing_information
    ):
        return report.model_copy(update={
            "verdict": Verdict.PASS,
            "blocking_issues": [],
            "revision_instructions": [],
        })
    return report


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
