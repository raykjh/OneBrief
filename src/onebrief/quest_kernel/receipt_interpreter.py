"""Interpret raw execution receipts without granting new authority."""

from __future__ import annotations

from onebrief.execution_schemas import PipelineStatus, Verdict
from onebrief.handoff_protocol import FailureCode, FailureOwner
from onebrief.quest_kernel.models import (
    QuestTransition,
    QuestTransitionDecision,
    RawQuestReceipt,
    TransitionOwner,
)
from onebrief.schemas import ExecutionPhase


def _decision(
    raw: RawQuestReceipt,
    transition: QuestTransition,
    owner: TransitionOwner,
    rationale: str,
    *,
    next_phase: ExecutionPhase | None = None,
    model_repair_allowed: bool = False,
) -> QuestTransitionDecision:
    return QuestTransitionDecision(
        transition=transition,
        failure_owner=owner,
        next_phase=next_phase,
        model_repair_allowed=model_repair_allowed,
        raw_receipt_sha256=raw.sha256,
        rationale=rationale,
    )


def _authorization_text(raw: RawQuestReceipt) -> str:
    values = []
    if raw.checkpoint is not None:
        values.append(raw.checkpoint.message)
    if raw.verification is not None:
        values.extend(raw.verification.blocking_issues)
        values.extend(raw.verification.missing_information)
    if raw.repair_contract is not None:
        values.append(raw.repair_contract.rationale)
        values.append(raw.repair_contract.hypothesis.suspected_cause)
    return " ".join(values).casefold()


def interpret_raw_receipt(raw: RawQuestReceipt) -> QuestTransitionDecision:
    """Return the narrowest transition supported by authoritative raw receipts.

    Structured phase and repair receipts take precedence over diagnostic prose.
    Ambiguity stops the project instead of assigning speculative edit authority.
    """

    checkpoint = raw.checkpoint
    phase = raw.phase_decision
    repair = raw.repair_contract

    if (
        checkpoint is not None
        and checkpoint.status in {
            PipelineStatus.NEEDS_AUTHORIZATION,
            PipelineStatus.NEEDS_INFORMATION,
            PipelineStatus.NEEDS_BUDGET,
        }
    ) or (phase is not None and phase.failure_code == FailureCode.AUTHORITY_REQUIRED):
        return _decision(
            raw,
            QuestTransition.NEEDS_AUTHORIZATION,
            TransitionOwner.AUTHORIZATION,
            "The authoritative checkpoint requires a human-owned amendment.",
        )

    if repair is not None and not repair.execution_allowed:
        authorization = "author" in _authorization_text(raw) or "permission" in _authorization_text(raw)
        if authorization:
            return _decision(
                raw,
                QuestTransition.NEEDS_AUTHORIZATION,
                TransitionOwner.AUTHORIZATION,
                "The no-progress contract reached an authorization boundary.",
            )
        return _decision(
            raw,
            QuestTransition.STRUCTURAL_STOP,
            TransitionOwner.ORCHESTRATOR,
            "The convergence contract blocks another non-learning repair strategy.",
        )

    if (
        checkpoint is not None
        and checkpoint.status == PipelineStatus.COMPLETE
        and checkpoint.final_verdict == Verdict.PASS
        and raw.verification is not None
        and raw.verification.verdict == Verdict.PASS
        and raw.completion_ledger_complete
    ):
        return _decision(
            raw,
            QuestTransition.PASS,
            TransitionOwner.NONE,
            "Execution, independent verification, and the completion ledger all PASS.",
        )

    if phase is not None:
        if not phase.model_repair_allowed or phase.next_phase is None:
            return _decision(
                raw,
                QuestTransition.STRUCTURAL_STOP,
                TransitionOwner.ORCHESTRATOR,
                "The structured phase receipt does not authorize another maker mutation.",
            )
        if (
            phase.failure_owner == FailureOwner.PRODUCT
            and phase.next_phase == ExecutionPhase.PRODUCT_IMPLEMENTATION
        ):
            return _decision(
                raw,
                QuestTransition.REPAIR_PRODUCT,
                TransitionOwner.PRODUCT,
                phase.rationale,
                next_phase=ExecutionPhase.PRODUCT_IMPLEMENTATION,
                model_repair_allowed=True,
            )
        if (
            phase.failure_owner == FailureOwner.EVIDENCE
            and phase.next_phase == ExecutionPhase.EVIDENCE_CONSTRUCTION
        ):
            return _decision(
                raw,
                QuestTransition.REPAIR_EVIDENCE,
                TransitionOwner.EVIDENCE,
                phase.rationale,
                next_phase=ExecutionPhase.EVIDENCE_CONSTRUCTION,
                model_repair_allowed=True,
            )

    return _decision(
        raw,
        QuestTransition.STRUCTURAL_STOP,
        TransitionOwner.ORCHESTRATOR,
        "The raw receipts do not support a safe product, evidence, or success transition.",
    )
