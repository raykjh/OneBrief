"""Two-phase software execution contracts and budget routing.

Product implementation and evidence construction are deliberately separate
modification authorities.  Trusted verification decides which authority owns
the next repair; a maker cannot spend the other phase's wallet or edit its
paths merely because the global approval still has money left.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Mapping

from pydantic import BaseModel, Field, model_validator

from onebrief.convergence_policy import (
    FailureLayer,
    classify_failure_code,
    classify_failure_layer,
    failure_owner_for,
)
from onebrief.handoff_protocol import FailureCode, FailureOwner
from onebrief.schemas import ExecutionPhase, PhaseBudgetEstimate

PHASE_STATE_KEY = "onebrief:execution_phase"
PHASE_DECISION_STATE_KEY = "onebrief:phase_decision"
MAKER_PHASE_BINDING_STATE_KEY = "onebrief_maker_model_binding"


class PhaseBudgetAllocation(BaseModel):
    phase: ExecutionPhase
    approved_usd_micros: int = Field(ge=0)
    max_ai_repair_calls: int = Field(ge=0, le=24)
    max_deterministic_attempts: int = Field(ge=0, le=100)
    editable_scope: list[str] = Field(default_factory=list, max_length=24)


class PhaseBudgetPolicy(BaseModel):
    schema_version: str = "onebrief-phase-budget-policy-v1"
    approval_id: str
    budget_estimate_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    allocations: list[PhaseBudgetAllocation] = Field(min_length=1, max_length=8)
    policy_sha256: str = ""

    @model_validator(mode="after")
    def phases_are_unique(self) -> "PhaseBudgetPolicy":
        phases = [item.phase for item in self.allocations]
        if len(phases) != len(set(phases)):
            raise ValueError("phase budget policy contains duplicate phases")
        return self


class EvidenceSpecification(BaseModel):
    schema_version: str = "onebrief-evidence-specification-v1"
    completion_contract_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    criterion_ids: list[str] = Field(default_factory=list, max_length=64)
    immutable_evidence_inputs: list[str] = Field(default_factory=list, max_length=128)
    product_edit_scope: list[str] = Field(default_factory=list, max_length=24)
    evidence_edit_scope: list[str] = Field(default_factory=list, max_length=24)
    frozen_before_implementation: bool = True
    specification_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class PhaseDecision(BaseModel):
    schema_version: str = "onebrief-phase-decision-v1"
    round_number: int = Field(ge=0)
    failure_code: FailureCode = FailureCode.UNKNOWN
    failure_layer: FailureLayer
    failure_owner: FailureOwner
    next_phase: ExecutionPhase | None
    model_repair_allowed: bool
    rationale: str


class DeterministicAttempt(BaseModel):
    attempt_id: str
    phase: ExecutionPhase
    purpose: str = Field(min_length=3, max_length=300)
    created_at: str


class PhaseAttemptLedger(BaseModel):
    schema_version: str = "onebrief-phase-attempt-ledger-v1"
    policy_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    revision: int = Field(default=0, ge=0)
    attempts: list[DeterministicAttempt] = Field(default_factory=list, max_length=200)
    integrity_sha256: str = ""


PRODUCT_SCOPE = ["product source; excludes tests, evidence, screenshots, and reports"]
EVIDENCE_SCOPE = ["tests and executable evidence harness; excludes product behavior"]


def active_execution_phase(
    state: Mapping[str, object],
    *,
    default: ExecutionPhase = ExecutionPhase.PRODUCT_IMPLEMENTATION,
) -> ExecutionPhase:
    """Resolve the exact repair authority from durable session state.

    The deterministic verifier's typed phase decision is authoritative.  The
    ADK model binding is a digest-like acknowledgement of the same decision and
    is used as the second source when an event boundary has not yet materialized
    the direct phase key.  This keeps an LLM proposal from choosing its own
    product-versus-evidence authority merely by editing a path on the other
    surface.
    """

    decision = state.get(PHASE_DECISION_STATE_KEY)
    if isinstance(decision, Mapping):
        next_phase = str(decision.get("next_phase", "")).strip()
        if next_phase:
            return ExecutionPhase(next_phase)
    binding = state.get(MAKER_PHASE_BINDING_STATE_KEY)
    if isinstance(binding, Mapping):
        bound_phase = str(binding.get("execution_phase", "")).strip()
        if bound_phase:
            return ExecutionPhase(bound_phase)
    direct_phase = str(state.get(PHASE_STATE_KEY, default.value)).strip()
    return ExecutionPhase(direct_phase or default.value)


def canonical_sha256(payload: object) -> str:
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def phase_attempt_ledger_sha256(ledger: PhaseAttemptLedger) -> str:
    return canonical_sha256(
        ledger.model_dump(mode="json", exclude={"integrity_sha256"})
    )


def deterministic_attempt(
    *, attempt_id: str, phase: ExecutionPhase, purpose: str
) -> DeterministicAttempt:
    return DeterministicAttempt(
        attempt_id=attempt_id,
        phase=phase,
        purpose=purpose,
        created_at=datetime.now(UTC).isoformat(),
    )


def phase_policy_sha256(policy: PhaseBudgetPolicy) -> str:
    return canonical_sha256(
        policy.model_dump(mode="json", exclude={"policy_sha256"})
    )


def allocate_phase_policy(
    *,
    approval_id: str,
    budget_estimate_sha256: str,
    approved_usd_micros: int,
    estimates: list[PhaseBudgetEstimate],
) -> PhaseBudgetPolicy | None:
    """Fill phase wallets through minimum, recommended, then maximum tiers.

    Proportional scaling from recommended weights underfunded phases with a
    small routine budget but a legitimate expensive escalation.  In
    particular, an approval above the recommended total could still leave the
    evidence wallet below its declared maximum while unrelated phases received
    surplus.  Tiered filling preserves every estimate's floor first and then
    distributes only the remaining headroom toward its next declared tier.
    """

    if not estimates:
        return None
    minimums = [max(0, round(item.minimum_cost_usd * 1_000_000)) for item in estimates]
    recommended = [
        max(minimum, round(item.recommended_cost_usd * 1_000_000))
        for minimum, item in zip(minimums, estimates, strict=True)
    ]
    maximums = [
        max(target, round(item.maximum_cost_usd * 1_000_000))
        for target, item in zip(recommended, estimates, strict=True)
    ]
    if not any(maximums):
        return None

    assigned_caps = [0 for _ in estimates]

    def fill_toward(targets: list[int], available: int) -> int:
        gaps = [max(0, target - current) for current, target in zip(
            assigned_caps, targets, strict=True
        )]
        total_gap = sum(gaps)
        if available <= 0 or total_gap <= 0:
            return available
        distributed = min(available, total_gap)
        base = [distributed * gap // total_gap for gap in gaps]
        remainder = distributed - sum(base)
        order = sorted(
            range(len(gaps)),
            key=lambda index: (
                (distributed * gaps[index]) % total_gap,
                gaps[index],
                -index,
            ),
            reverse=True,
        )
        for index in order[:remainder]:
            if gaps[index]:
                base[index] += 1
        for index, amount in enumerate(base):
            assigned_caps[index] += amount
        return available - distributed

    remaining = approved_usd_micros
    remaining = fill_toward(minimums, remaining)
    remaining = fill_toward(recommended, remaining)
    remaining = fill_toward(maximums, remaining)
    implicit_reserve = 0
    if remaining:
        reserve_index = next(
            (
                index for index, estimate in enumerate(estimates)
                if estimate.phase == ExecutionPhase.RESERVE
            ),
            None,
        )
        if reserve_index is None:
            # Approval above every phase's declared maximum is contingency,
            # not extra final-verification money. Keep it non-editing so a
            # policy-approved repair escalation can borrow it without starving
            # another phase or expanding authority.
            implicit_reserve = remaining
        else:
            assigned_caps[reserve_index] += remaining

    allocations: list[PhaseBudgetAllocation] = []
    for estimate, cap in zip(estimates, assigned_caps, strict=True):
        allocations.append(PhaseBudgetAllocation(
            phase=estimate.phase,
            approved_usd_micros=cap,
            max_ai_repair_calls=estimate.max_ai_repair_calls,
            max_deterministic_attempts=estimate.max_deterministic_attempts,
            editable_scope=list(estimate.editable_scope),
        ))
    if implicit_reserve:
        allocations.append(PhaseBudgetAllocation(
            phase=ExecutionPhase.RESERVE,
            approved_usd_micros=implicit_reserve,
            max_ai_repair_calls=0,
            max_deterministic_attempts=0,
            editable_scope=[],
        ))
    policy = PhaseBudgetPolicy(
        approval_id=approval_id,
        budget_estimate_sha256=budget_estimate_sha256,
        allocations=allocations,
    )
    return policy.model_copy(update={"policy_sha256": phase_policy_sha256(policy)})


def phase_for_stage(stage: str) -> ExecutionPhase:
    normalized = stage.casefold().strip()
    for phase in ExecutionPhase:
        prefix = phase.value + "::"
        if normalized.startswith(prefix):
            return phase
    if normalized.startswith("long_form_draft") or normalized.startswith("revision"):
        return ExecutionPhase.PRODUCT_IMPLEMENTATION
    if (
        normalized.startswith("independent_verification")
        or normalized.startswith("final_approval")
        or normalized.startswith("policy_guard")
    ):
        return ExecutionPhase.FINAL_VERIFICATION
    return ExecutionPhase.SHARED_CONTEXT


def phase_stage(phase: ExecutionPhase, stage: str) -> str:
    if stage.casefold().startswith(phase.value + "::"):
        return stage
    return f"{phase.value}::{stage}"


def is_ai_repair_stage(stage: str) -> bool:
    normalized = stage.casefold()
    # A compact retry repairs the structured transport of the *same* agent
    # attempt.  It must still pass the phase and total-dollar wallets, but it
    # is not a new semantic repair round.  Counting it here can strand the
    # independent verifier even when no product/evidence repair is allowed.
    if normalized.endswith("_compact_retry"):
        return False
    return (
        "repair" in normalized
        or "retry" in normalized
        or "reasoning_escalation" in normalized
    )


def is_authority_preserving_ai_repair_reservation(stage: str) -> bool:
    """Recognize a repair and its structured-output transport retry.

    A compact retry is not another semantic repair turn, but it remains inside
    the same approved phase and authority. It may therefore use non-editing
    reserve without consuming another ``max_ai_repair_calls`` slot.
    """

    normalized = stage.casefold()
    return is_ai_repair_stage(stage) or (
        normalized.endswith("_compact_retry")
        and any(marker in normalized for marker in ("repair", "reasoning_escalation"))
    )


def is_evidence_path(path: str) -> bool:
    normalized = path.replace("\\", "/").strip("/").casefold()
    parts = {part for part in PurePosixPath(normalized).parts if part}
    filename = PurePosixPath(normalized).name
    stem = filename.rsplit(".", 1)[0]
    return bool(
        parts.intersection({
            "test", "tests", "testing", "evidence", "screenshots",
            "observations", "independent_observations", "reports", "coverage",
        })
        or filename.endswith((".png", ".jpg", ".jpeg", ".log"))
        or stem.endswith(("test", "tests", "spec"))
        or filename.startswith(("test_", "spec_"))
        or ".test." in filename
        or ".spec." in filename
        or "runtime-evidence" in stem
        or "runtime_evidence" in stem
    )


def is_project_history_path(path: str) -> bool:
    """Return generated/manual project history that can never repair runtime behavior."""

    normalized = path.replace("\\", "/").strip("/").casefold()
    parts = {part for part in PurePosixPath(normalized).parts if part}
    return bool(parts.intersection({
        "_patch_notes", "patch_notes", "patch-notes", "changelogs", "change_logs",
    }))


def path_allowed_for_phase(path: str, phase: ExecutionPhase) -> bool:
    if is_project_history_path(path):
        return False
    if phase == ExecutionPhase.PRODUCT_IMPLEMENTATION:
        return not is_evidence_path(path)
    if phase == ExecutionPhase.EVIDENCE_CONSTRUCTION:
        return is_evidence_path(path)
    return False


def classify_failure_owner(
    *, context: str, failure_text: str, affected_paths: list[str] | None = None
) -> FailureOwner:
    layer = classify_failure_layer(context, failure_text)
    code = classify_failure_code(context, failure_text, layer)
    typed_owner = failure_owner_for(code, layer)
    paths = affected_paths or []
    evidence_targeted = bool(paths) and all(is_evidence_path(path) for path in paths)
    normalized = failure_text.casefold()
    if any(marker in normalized for marker in (
        "generated playmode test does not interact",
        "update the playmode test journey",
        "playmode test journey to include steps that interact",
        "update the declarative runtime journey",
        "generic playmode pass is insufficient",
        "active shipped ui object is unavailable",
    )):
        return FailureOwner.EVIDENCE
    # Build/runtime codes normally indicate a shipped-product defect, but the
    # compiler can name a generated test or trusted evidence harness as the
    # only failing surface. The exact failed delta is a stronger ownership
    # signal than the generic build code; keep that repair in the evidence
    # wallet so a test compiler error cannot open product-write authority.
    if layer in {
        FailureLayer.BUILD,
        FailureLayer.RUNTIME,
        FailureLayer.SOURCE_BINDING,
    } and evidence_targeted:
        return FailureOwner.EVIDENCE
    if code != FailureCode.UNKNOWN or layer in {
        FailureLayer.EVIDENCE_RUNTIME,
        FailureLayer.EVIDENCE_TOPOLOGY,
        FailureLayer.EVIDENCE_INTEGRITY,
        FailureLayer.SEMANTIC_PRODUCT,
        FailureLayer.AUTHORITY,
        FailureLayer.PROVIDER,
        FailureLayer.STRUCTURED_OUTPUT,
    }:
        return typed_owner
    # Layer classification is authoritative when runtime evidence has already
    # identified a shipped product defect.  Audit suffixes from the evidence
    # phase must not pull that defect back into the test-harness wallet.
    if layer == FailureLayer.SEMANTIC_PRODUCT:
        return FailureOwner.PRODUCT
    if layer in {FailureLayer.EVIDENCE_TOPOLOGY, FailureLayer.EVIDENCE_INTEGRITY}:
        return FailureOwner.EVIDENCE
    if any(marker in normalized for marker in (
        "evidence harness", "evidence topology", "unity visual test contract",
        "missing playmode test",
        "executed onebrief.visual playmode test",
        "add a discoverable unity playmode test", "testassemblies",
        "runtime-evidence.json", "screenshot capture is missing",
        "generated playmode test does not interact",
        "update the playmode test journey",
        "playmode test journey to include steps that interact",
        "update the declarative runtime journey",
        "generic playmode pass is insufficient",
        "active shipped ui object is unavailable",
    )):
        return FailureOwner.EVIDENCE
    if any(marker in normalized for marker in (
        "unity license", "license activation", "executable was not found",
        "no such file or directory", "worker unavailable", "provider unavailable",
        "connection reset", "deadline exceeded", "service unavailable",
    )):
        return FailureOwner.ENVIRONMENT
    if layer == FailureLayer.AUTHORITY:
        return FailureOwner.CONTRACT
    if layer in {FailureLayer.PROVIDER, FailureLayer.STRUCTURED_OUTPUT}:
        return FailureOwner.ENVIRONMENT
    if layer in {FailureLayer.BUILD, FailureLayer.RUNTIME, FailureLayer.SOURCE_BINDING}:
        return FailureOwner.EVIDENCE if evidence_targeted else FailureOwner.PRODUCT
    return FailureOwner.PRODUCT


def decide_repair_phase(
    *,
    context: str,
    failure_text: str,
    round_number: int,
    affected_paths: list[str] | None = None,
) -> PhaseDecision:
    layer = classify_failure_layer(context, failure_text)
    code = classify_failure_code(context, failure_text, layer)
    owner = classify_failure_owner(
        context=context, failure_text=failure_text, affected_paths=affected_paths
    )
    if owner == FailureOwner.PRODUCT:
        phase = ExecutionPhase.PRODUCT_IMPLEMENTATION
        rationale = "Trusted verification identified a product-source defect."
        allowed = True
    elif owner == FailureOwner.EVIDENCE:
        phase = ExecutionPhase.EVIDENCE_CONSTRUCTION
        rationale = "Trusted verification identified an evidence-harness defect."
        allowed = True
    elif owner == FailureOwner.ENVIRONMENT:
        phase = None
        rationale = "Provider or protocol failure requires deterministic recovery, not a model edit."
        allowed = False
    else:
        phase = None
        rationale = "The failure changes authority or contract and requires a decision."
        allowed = False
    return PhaseDecision(
        round_number=round_number,
        failure_code=code,
        failure_layer=layer,
        failure_owner=owner,
        next_phase=phase,
        model_repair_allowed=allowed,
        rationale=rationale,
    )


def build_evidence_specification(
    *, completion_contract: object, criterion_ids: list[str], immutable_inputs: list[str]
) -> EvidenceSpecification:
    contract_hash = canonical_sha256(completion_contract)
    payload = {
        "completion_contract_sha256": contract_hash,
        "criterion_ids": criterion_ids,
        "immutable_evidence_inputs": immutable_inputs,
        "product_edit_scope": PRODUCT_SCOPE,
        "evidence_edit_scope": EVIDENCE_SCOPE,
        "frozen_before_implementation": True,
    }
    return EvidenceSpecification(
        **payload,
        specification_sha256=canonical_sha256(payload),
    )
