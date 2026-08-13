"""Digest-bound work handoffs shared by OneBrief execution roles.

Stage-specific payloads (analysis packages, code change sets, verification
reports, and repair contracts) remain authoritative.  This module provides a
small common envelope around those payloads so responsibility, scope,
criteria, evidence, and source revision cannot be reinterpreted as chat prose.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, Field, model_validator


class FailureOwner(StrEnum):
    PRODUCT = "product"
    EVIDENCE = "evidence"
    ENVIRONMENT = "environment"
    CONTRACT = "contract"


class FailureCode(StrEnum):
    UNKNOWN = "unknown"
    STRUCTURED_OUTPUT_INVALID = "structured_output_invalid"
    SOURCE_REVISION_STALE = "source_revision_stale"
    BUILD_FAILED = "build_failed"
    RUNTIME_FAILED = "runtime_failed"
    EVIDENCE_TOPOLOGY_INVALID = "evidence_topology_invalid"
    EVIDENCE_INTEGRITY_INVALID = "evidence_integrity_invalid"
    UNITY_SCREENSHOT_NOT_MATERIALIZED = "unity_screenshot_not_materialized"
    SEMANTIC_PRODUCT_DEFECT = "semantic_product_defect"
    AUTHORITY_REQUIRED = "authority_required"
    PROVIDER_UNAVAILABLE = "provider_unavailable"


class EvidenceKind(StrEnum):
    COMPILE = "compile"
    BEHAVIOR = "behavior"
    VISUAL = "visual"
    POLICY = "policy"
    OTHER = "other"


class EvidenceStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    MISSING = "missing"


class HandoffReceiptStatus(StrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    NEEDS_DECISION = "needs_decision"


class ArtifactReference(BaseModel):
    artifact_type: str = Field(min_length=1, max_length=80)
    path: str = Field(min_length=1, max_length=500)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class EvidenceBinding(BaseModel):
    """Bind one proof observation to exactly one completion concern."""

    schema_version: str = "onebrief-evidence-binding-v1"
    binding_id: str = Field(pattern=r"^EB-[a-f0-9]{16}$")
    criterion_id: str | None = Field(default=None, pattern=r"^Q[0-9]{2}$")
    kind: EvidenceKind
    status: EvidenceStatus
    summary: str = Field(min_length=1, max_length=1000)
    artifact: ArtifactReference | None = None
    command_id: str | None = Field(default=None, max_length=120)
    observation_id: str | None = Field(default=None, pattern=r"^FO-[a-f0-9]{16}$")


class WorkHandoffEnvelopeV1(BaseModel):
    schema_version: str = "onebrief-work-handoff-v1"
    handoff_id: str = Field(pattern=r"^WH-[a-f0-9]{16}$")
    project_id: str = Field(min_length=1, max_length=120)
    milestone_id: str = Field(pattern=r"^M[0-9]{2}$")
    round_number: int = Field(ge=0, le=100)
    sender_agent_id: str = Field(min_length=1, max_length=120)
    recipient_agent_id: str = Field(min_length=1, max_length=120)
    stage: str = Field(min_length=1, max_length=120)
    goal_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    completion_contract_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_revision: str = Field(min_length=7, max_length=128)
    owned_criterion_ids: list[str] = Field(default_factory=list, max_length=32)
    preserve_passed_criterion_ids: list[str] = Field(default_factory=list, max_length=32)
    failure_observation_id: str | None = Field(
        default=None, pattern=r"^FO-[a-f0-9]{16}$"
    )
    permitted_paths: list[str] = Field(default_factory=list, max_length=64)
    forbidden_path_patterns: list[str] = Field(default_factory=list, max_length=32)
    input_artifacts: list[ArtifactReference] = Field(default_factory=list, max_length=64)
    evidence_bindings: list[EvidenceBinding] = Field(default_factory=list, max_length=64)
    expected_output_schema: str = Field(min_length=1, max_length=160)
    required_evidence: list[str] = Field(default_factory=list, max_length=32)
    created_at: str
    handoff_digest: str = Field(pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def validate_authority_boundary(self) -> "WorkHandoffEnvelopeV1":
        if self.sender_agent_id == self.recipient_agent_id:
            raise ValueError("work handoff sender and recipient must be distinct")
        owned = set(self.owned_criterion_ids)
        preserved = set(self.preserve_passed_criterion_ids)
        if owned.intersection(preserved):
            raise ValueError("owned criteria cannot also be frozen as previously passed")
        referenced = {
            item.criterion_id for item in self.evidence_bindings if item.criterion_id
        }
        if not referenced.issubset(owned | preserved):
            raise ValueError("evidence binding references a criterion outside the handoff")
        if len(self.owned_criterion_ids) != len(owned):
            raise ValueError("owned criterion IDs must be unique")
        if len(self.preserve_passed_criterion_ids) != len(preserved):
            raise ValueError("preserved criterion IDs must be unique")
        return self


class HandoffReceipt(BaseModel):
    schema_version: str = "onebrief-handoff-receipt-v1"
    handoff_id: str = Field(pattern=r"^WH-[a-f0-9]{16}$")
    recipient_agent_id: str = Field(min_length=1, max_length=120)
    status: HandoffReceiptStatus
    understood_criterion_ids: list[str] = Field(default_factory=list, max_length=32)
    understood_permitted_paths: list[str] = Field(default_factory=list, max_length=64)
    handoff_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    rationale: str = Field(min_length=1, max_length=1000)
    created_at: str
    receipt_digest: str = Field(pattern=r"^[a-f0-9]{64}$")


def canonical_sha256(payload: object) -> str:
    def jsonable(value: object) -> object:
        if isinstance(value, BaseModel):
            return jsonable(value.model_dump(mode="json"))
        if isinstance(value, dict):
            return {str(key): jsonable(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [jsonable(item) for item in value]
        if isinstance(value, StrEnum):
            return value.value
        return value

    payload = jsonable(payload)
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _without(payload: dict[str, object], *keys: str) -> dict[str, object]:
    return {key: value for key, value in payload.items() if key not in keys}


def create_evidence_binding(
    *,
    criterion_id: str | None,
    kind: EvidenceKind,
    status: EvidenceStatus,
    summary: str,
    artifact: ArtifactReference | None = None,
    command_id: str | None = None,
    observation_id: str | None = None,
) -> EvidenceBinding:
    summary = str(summary)[:1000]
    payload = {
        "criterion_id": criterion_id,
        "kind": kind.value,
        "status": status.value,
        "summary": summary,
        "artifact": artifact.model_dump(mode="json") if artifact else None,
        "command_id": command_id,
        "observation_id": observation_id,
    }
    return EvidenceBinding(
        binding_id="EB-" + canonical_sha256(payload)[:16],
        **payload,
    )


def create_work_handoff(**values: object) -> WorkHandoffEnvelopeV1:
    payload = {
        **values,
        "created_at": str(values.get("created_at") or datetime.now(UTC).isoformat()),
        "handoff_id": "WH-" + "0" * 16,
        "handoff_digest": "0" * 64,
    }
    normalized = WorkHandoffEnvelopeV1.model_validate(payload).model_dump(mode="json")
    normalized["handoff_id"] = "WH-" + canonical_sha256(
        _without(normalized, "handoff_id", "handoff_digest")
    )[:16]
    normalized["handoff_digest"] = canonical_sha256(
        _without(normalized, "handoff_digest")
    )
    return WorkHandoffEnvelopeV1.model_validate(normalized)


def verify_work_handoff(envelope: WorkHandoffEnvelopeV1) -> None:
    payload = envelope.model_dump(mode="json")
    expected = canonical_sha256(_without(payload, "handoff_digest"))
    if envelope.handoff_digest != expected:
        raise PermissionError("work handoff digest does not match its approved content")
    identity_payload = _without(payload, "handoff_id", "handoff_digest")
    if envelope.handoff_id != "WH-" + canonical_sha256(identity_payload)[:16]:
        raise PermissionError("work handoff ID does not match its approved content")


def accept_work_handoff(
    envelope: WorkHandoffEnvelopeV1,
    *,
    recipient_agent_id: str,
    rationale: str = "The recipient accepted the exact digest-bound scope.",
) -> HandoffReceipt:
    verify_work_handoff(envelope)
    if recipient_agent_id != envelope.recipient_agent_id:
        raise PermissionError("work handoff was offered to a different recipient")
    payload = {
        "handoff_id": envelope.handoff_id,
        "recipient_agent_id": recipient_agent_id,
        "status": HandoffReceiptStatus.ACCEPTED.value,
        "understood_criterion_ids": list(envelope.owned_criterion_ids),
        "understood_permitted_paths": list(envelope.permitted_paths),
        "handoff_digest": envelope.handoff_digest,
        "rationale": rationale,
        "created_at": datetime.now(UTC).isoformat(),
        "receipt_digest": "0" * 64,
    }
    normalized = HandoffReceipt.model_validate(payload).model_dump(mode="json")
    normalized["receipt_digest"] = canonical_sha256(
        _without(normalized, "receipt_digest")
    )
    return HandoffReceipt.model_validate(normalized)


def verify_handoff_receipt(
    envelope: WorkHandoffEnvelopeV1, receipt: HandoffReceipt
) -> None:
    verify_work_handoff(envelope)
    if receipt.handoff_id != envelope.handoff_id:
        raise PermissionError("handoff receipt belongs to a different handoff")
    if receipt.recipient_agent_id != envelope.recipient_agent_id:
        raise PermissionError("handoff receipt belongs to a different recipient")
    if receipt.handoff_digest != envelope.handoff_digest:
        raise PermissionError("handoff receipt acknowledges different content")
    expected = canonical_sha256(
        _without(receipt.model_dump(mode="json"), "receipt_digest")
    )
    if receipt.receipt_digest != expected:
        raise PermissionError("handoff receipt digest is invalid")
