"""Deterministic veto over proposed KHALINOS Quest transitions."""

from __future__ import annotations

from onebrief.execution_schemas import PipelineStatus, Verdict
from onebrief.quest_kernel.models import (
    QuestTransition,
    QuestTransitionDecision,
    RawQuestReceipt,
)


def validate_transition(
    raw: RawQuestReceipt,
    decision: QuestTransitionDecision,
) -> QuestTransitionDecision:
    """Reject a decision that is detached from, or stronger than, its Receipt."""

    if decision.raw_receipt_sha256 != raw.sha256:
        raise PermissionError("Quest transition is bound to a different raw receipt")
    if decision.transition in {QuestTransition.PASS, QuestTransition.COMPLETE}:
        checkpoint = raw.checkpoint
        verification = raw.verification
        if not (
            checkpoint is not None
            and checkpoint.status == PipelineStatus.COMPLETE
            and checkpoint.final_verdict == Verdict.PASS
            and verification is not None
            and verification.verdict == Verdict.PASS
            and raw.completion_ledger_complete
        ):
            raise PermissionError("Quest PASS requires execution, verification, and ledger proof")
    if decision.transition == QuestTransition.COMPLETE:
        raise PermissionError(
            "A single Quest receipt cannot complete the project without Project Canvas context"
        )
    if decision.transition in {
        QuestTransition.REPAIR_PRODUCT,
        QuestTransition.REPAIR_EVIDENCE,
    }:
        if raw.phase_decision is None:
            raise PermissionError("repair authority requires a structured phase decision")
        if raw.repair_contract is not None and not raw.repair_contract.execution_allowed:
            raise PermissionError("no-progress contract blocks another maker mutation")
    return decision
