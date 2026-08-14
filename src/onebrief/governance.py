"""Small governance projections over OneBrief's existing authoritative stores."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, model_validator


def canonical_digest(payload: BaseModel | dict[str, object]) -> str:
    value = payload.model_dump(mode="json", exclude_none=True) if isinstance(payload, BaseModel) else payload
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")).hexdigest()


def parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


class RiskClass(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    PROTECTED = "protected"


class StopReason(StrEnum):
    MISSING_INFORMATION = "missing_information"
    ADDITIONAL_BUDGET_REQUIRED = "additional_budget_required"
    AUTHORITY_EXPANSION_REQUIRED = "authority_expansion_required"
    STALE_REVISION = "stale_revision"
    CONFLICT_DETECTED = "conflict_detected"
    EVIDENCE_UNAVAILABLE = "evidence_unavailable"
    EXTERNAL_SIDE_EFFECT_APPROVAL = "external_side_effect_approval"


class AuthorizationEnvelope(BaseModel):
    schema_version: Literal["onebrief-authorization-envelope-v1"] = "onebrief-authorization-envelope-v1"
    actor_id: str = Field(min_length=1, max_length=120)
    executor_id: str = Field(min_length=1, max_length=120)
    action: Literal["execute_completion_contract"] = "execute_completion_contract"
    completion_contract_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    toolpack_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    base_source_revision: str | None = Field(default=None, pattern=r"^[a-f0-9]{40}$")
    allowed_read_prefixes: list[str] = Field(default_factory=list, max_length=20)
    allowed_write_prefixes: list[str] = Field(default_factory=list, max_length=20)
    prohibited_actions: list[str] = Field(default_factory=list, max_length=20)
    risk_ceiling: RiskClass
    maximum_budget_usd: float = Field(ge=0)
    issued_at: str
    expires_at: str

    @model_validator(mode="after")
    def validate_window(self) -> "AuthorizationEnvelope":
        if parse_time(self.expires_at) <= parse_time(self.issued_at):
            raise ValueError("authorization expiry must be later than issue time")
        return self


class AuthorizationReceipt(BaseModel):
    schema_version: Literal["onebrief-authorization-receipt-v1"] = "onebrief-authorization-receipt-v1"
    receipt_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    session_id: str
    action_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    actor_id: str
    base_source_revision: str | None = Field(default=None, pattern=r"^[a-f0-9]{40}$")
    affected_scope: list[str]
    consumed_at: str
    result: Literal["accepted"] = "accepted"


class DecisionRequest(BaseModel):
    schema_version: Literal["onebrief-decision-request-v1"] = "onebrief-decision-request-v1"
    request_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    reason: StopReason
    action_required: str = Field(min_length=1, max_length=1200)
    affected_scope: list[str] = Field(default_factory=list, max_length=30)
    authorization_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    base_source_revision: str | None = Field(default=None, pattern=r"^[a-f0-9]{40}$")
    additional_budget_usd: float | None = Field(default=None, ge=0)
    expires_at: str | None = None
    safe_alternatives: list[str] = Field(default_factory=list, max_length=10)
    autonomous_retry_allowed: Literal[False] = False


def build_decision_request(
    *,
    status: str,
    stage: str,
    message: str,
    authorization_sha256: str | None = None,
    base_source_revision: str | None = None,
    additional_budget_usd: float | None = None,
    expires_at: str | None = None,
) -> DecisionRequest | None:
    """Map existing terminal gates to one deterministic user decision projection."""
    lowered = f"{stage} {message}".casefold()
    if status == "needs_information":
        reason = StopReason.MISSING_INFORMATION
        action = "Provide the missing result-changing information, or reduce the requested scope."
        alternatives = ["provide the requested information", "reduce scope", "stop the task"]
    elif status == "needs_budget":
        reason = StopReason.ADDITIONAL_BUDGET_REQUIRED
        action = "Approve additional budget or reduce the remaining scope."
        alternatives = ["approve the displayed additional amount", "reduce scope", "stop the task"]
    elif status == "needs_authorization":
        reason = StopReason.AUTHORITY_EXPANSION_REQUIRED
        action = "Review and approve an amended capability boundary before execution continues."
        alternatives = ["approve the amended boundary", "remove the blocked action", "stop the task"]
    elif "stale" in lowered or "source drift" in lowered or "changed after" in lowered:
        reason = StopReason.STALE_REVISION
        action = "Refresh the project inspection and approve a plan bound to the current source revision."
        alternatives = ["refresh inspection", "preserve current changes and stop"]
    elif "conflict" in lowered:
        reason = StopReason.CONFLICT_DETECTED
        action = "Choose how to resolve the conflicting changes; OneBrief will not merge them automatically."
        alternatives = ["keep current source", "review a new proposal", "stop the task"]
    elif status == "partial" or "unverifiable" in lowered or "evidence" in lowered:
        reason = StopReason.EVIDENCE_UNAVAILABLE
        action = "Provide or enable independent evidence, or accept that completion cannot be proven."
        alternatives = ["enable the required verifier", "change the completion criterion", "stop the task"]
    else:
        return None
    payload: dict[str, object] = {
        "reason": reason.value,
        "action_required": action,
        "affected_scope": [stage],
        "authorization_sha256": authorization_sha256,
        "base_source_revision": base_source_revision,
        "additional_budget_usd": additional_budget_usd,
        "expires_at": expires_at,
        "safe_alternatives": alternatives,
    }
    return DecisionRequest(request_id=canonical_digest(payload), **payload)


def external_apply_decision(
    *, authorization_sha256: str, base_source_revision: str, project_id: str,
) -> DecisionRequest:
    payload = {
        "reason": StopReason.EXTERNAL_SIDE_EFFECT_APPROVAL.value,
        "action_required": "Approve backup, source modification, local revalidation, and automatic rollback on failure.",
        "affected_scope": [f"project:{project_id}"],
        "authorization_sha256": authorization_sha256,
        "base_source_revision": base_source_revision,
        "safe_alternatives": ["apply the verified package", "download for review", "leave the source unchanged"],
    }
    return DecisionRequest(request_id=canonical_digest(payload), **payload)
