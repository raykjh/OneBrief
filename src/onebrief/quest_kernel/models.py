"""Typed inputs and decisions at the KHALINOS Quest transition boundary."""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from onebrief.execution_schemas import ExecutionCheckpoint, VerificationReport
from onebrief.phase_execution import PhaseDecision
from onebrief.convergence_policy import RepairContract
from onebrief.schemas import ExecutionPhase


def canonical_sha256(value: object) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json", exclude_none=True)
    return hashlib.sha256(json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


class QuestTransition(StrEnum):
    PASS = "pass"
    REPAIR_PRODUCT = "repair_product"
    REPAIR_EVIDENCE = "repair_evidence"
    NEEDS_AUTHORIZATION = "needs_authorization"
    STRUCTURAL_STOP = "structural_stop"
    COMPLETE = "complete"


class TransitionOwner(StrEnum):
    NONE = "none"
    PRODUCT = "product"
    EVIDENCE = "evidence"
    AUTHORIZATION = "authorization"
    ORCHESTRATOR = "orchestrator"
    UNKNOWN = "unknown"


class RawQuestReceipt(BaseModel):
    """Unmodified receipts emitted by execution and independent verification."""

    schema_version: Literal["khalinos-raw-quest-receipt-v1"] = (
        "khalinos-raw-quest-receipt-v1"
    )
    checkpoint: ExecutionCheckpoint | None = None
    verification: VerificationReport | None = None
    phase_decision: PhaseDecision | None = None
    repair_contract: RepairContract | None = None
    completion_ledger_complete: bool = False
    artifacts: dict[str, Any] = Field(default_factory=dict, max_length=32)

    @model_validator(mode="after")
    def contains_authoritative_input(self) -> "RawQuestReceipt":
        if not any((
            self.checkpoint,
            self.verification,
            self.phase_decision,
            self.repair_contract,
        )):
            raise ValueError("raw Quest receipt contains no authoritative execution input")
        return self

    @property
    def sha256(self) -> str:
        return canonical_sha256(self)


class QuestTransitionDecision(BaseModel):
    schema_version: Literal["khalinos-quest-transition-decision-v1"] = (
        "khalinos-quest-transition-decision-v1"
    )
    transition: QuestTransition
    failure_owner: TransitionOwner
    next_phase: ExecutionPhase | None = None
    model_repair_allowed: bool = False
    raw_receipt_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    rationale: str = Field(min_length=3, max_length=1200)

    @model_validator(mode="after")
    def transition_matches_authority(self) -> "QuestTransitionDecision":
        repairs = {
            QuestTransition.REPAIR_PRODUCT: (
                TransitionOwner.PRODUCT,
                ExecutionPhase.PRODUCT_IMPLEMENTATION,
            ),
            QuestTransition.REPAIR_EVIDENCE: (
                TransitionOwner.EVIDENCE,
                ExecutionPhase.EVIDENCE_CONSTRUCTION,
            ),
        }
        expected = repairs.get(self.transition)
        if expected is not None:
            if (self.failure_owner, self.next_phase) != expected:
                raise ValueError("repair transition does not match its owning phase")
            if not self.model_repair_allowed:
                raise ValueError("repair transition must explicitly permit a model repair")
        elif self.next_phase is not None or self.model_repair_allowed:
            raise ValueError("terminal transition cannot retain maker authority")
        if self.transition in {QuestTransition.PASS, QuestTransition.COMPLETE}:
            if self.failure_owner != TransitionOwner.NONE:
                raise ValueError("successful transition cannot retain a failure owner")
        if self.transition == QuestTransition.NEEDS_AUTHORIZATION:
            if self.failure_owner != TransitionOwner.AUTHORIZATION:
                raise ValueError("authorization transition requires authorization ownership")
        return self
