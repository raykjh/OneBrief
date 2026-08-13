"""Tamper-evident, atomic budget approval and per-call reservation ledger."""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import UTC, datetime
from decimal import Decimal, ROUND_CEILING
from enum import StrEnum
from pathlib import Path
from typing import Any
from uuid import uuid4

from filelock import FileLock
from pydantic import BaseModel, Field, model_validator

from onebrief.producer import PRICE_CARD_VERSION, PRICES
from onebrief.phase_execution import (
    PhaseAttemptLedger,
    PhaseBudgetPolicy,
    allocate_phase_policy,
    deterministic_attempt,
    is_ai_repair_stage,
    phase_attempt_ledger_sha256,
    phase_for_stage,
    phase_policy_sha256,
)
from onebrief.schemas import BudgetEnvelope, ExecutionPhase

MICROS_PER_DOLLAR = 1_000_000
RESERVATION_SAFETY_FACTOR = Decimal("1.05")


class BudgetGuardError(RuntimeError):
    pass


class BudgetExceeded(BudgetGuardError):
    pass


class IntegrityError(BudgetGuardError):
    pass


class RunStatus(StrEnum):
    APPROVED = "approved"
    RUNNING = "running"
    NEEDS_BUDGET = "needs_budget"
    COMPLETE = "complete"
    FAILED = "failed"


class CallStatus(StrEnum):
    RESERVED = "reserved"
    SETTLED = "settled"
    RELEASED = "released"
    DENIED = "denied"


class ApprovalRecord(BaseModel):
    approval_id: str
    approved_at: str
    approved_usd_micros: int = Field(gt=0)
    recommended_usd_micros: int = Field(gt=0)
    minimum_usd_micros: int = Field(gt=0)
    budget_estimate_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    price_card_version: str
    endpoint: str
    phase_policy_sha256: str | None = Field(
        default=None, pattern=r"^[a-f0-9]{64}$"
    )


class CostEntry(BaseModel):
    call_id: str
    stage: str
    model: str
    status: CallStatus
    input_token_cap: int = Field(ge=0)
    output_token_cap: int = Field(ge=0)
    reserved_usd_micros: int = Field(ge=0)
    fixed_cost_cap_micros: int = Field(default=0, ge=0)
    actual_usd_micros: int = Field(ge=0)
    actual_input_tokens: int = Field(ge=0)
    actual_output_tokens: int = Field(ge=0)
    phase_reserve_borrowed_usd_micros: int = Field(default=0, ge=0)
    created_at: str
    settled_at: str | None = None
    reason: str | None = None


class CostLedger(BaseModel):
    run_id: str
    revision: int = Field(ge=0)
    status: RunStatus
    approval: ApprovalRecord
    actual_usd_micros: int = Field(ge=0)
    reserved_usd_micros: int = Field(ge=0)
    entries: list[CostEntry]
    updated_at: str
    integrity_sha256: str = ""

    @model_validator(mode="after")
    def verify_totals(self) -> "CostLedger":
        reserved = sum(
            entry.reserved_usd_micros
            for entry in self.entries
            if entry.status == CallStatus.RESERVED
        )
        actual = sum(
            entry.actual_usd_micros
            for entry in self.entries
            if entry.status == CallStatus.SETTLED
        )
        if reserved != self.reserved_usd_micros or actual != self.actual_usd_micros:
            raise ValueError("ledger totals do not match entries")
        if reserved + actual > self.approval.approved_usd_micros:
            raise ValueError("ledger exceeds approved budget")
        return self


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def dollars_to_micros(amount: float) -> int:
    return int(
        (Decimal(str(amount)) * MICROS_PER_DOLLAR).to_integral_value(rounding=ROUND_CEILING)
    )


def micros_to_dollars(amount: int) -> float:
    return float(Decimal(amount) / MICROS_PER_DOLLAR)


def estimate_hash(estimate: BudgetEnvelope) -> str:
    canonical = json.dumps(
        estimate.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def quote_call_micros(
    model: str,
    input_tokens: int,
    output_tokens: int,
    fixed_cost_usd: float = 0.0,
) -> int:
    if model not in PRICES:
        raise ValueError(f"model has no approved price card: {model}")
    if input_tokens < 0 or output_tokens <= 0:
        raise ValueError("token caps must be non-negative and output cap must be positive")
    price = PRICES[model]
    # A $X / 1M-token rate equals X microdollars per token.
    raw = (
        Decimal(input_tokens) * Decimal(str(price.input_per_million))
        + Decimal(output_tokens) * Decimal(str(price.output_per_million))
    )
    protected = raw * RESERVATION_SAFETY_FACTOR
    return int(protected.to_integral_value(rounding=ROUND_CEILING)) + dollars_to_micros(fixed_cost_usd)


def actual_call_micros(
    model: str,
    input_tokens: int,
    output_tokens: int,
    fixed_cost_usd: float = 0.0,
) -> int:
    price = PRICES[model]
    raw = (
        Decimal(input_tokens) * Decimal(str(price.input_per_million))
        + Decimal(output_tokens) * Decimal(str(price.output_per_million))
    )
    return int(raw.to_integral_value(rounding=ROUND_CEILING)) + dollars_to_micros(fixed_cost_usd)


class BudgetStore:
    """Single-run file store protected against concurrent writers."""

    def __init__(self, run_dir: Path):
        self.run_dir = run_dir
        self.approval_path = run_dir / "approval.json"
        self.ledger_path = run_dir / "cost_ledger.json"
        self.phase_policy_path = run_dir / "phase_budget_policy.json"
        self.phase_attempts_path = run_dir / "phase_attempts.json"
        self.lock = FileLock(str(run_dir / ".budget.lock"), timeout=10)

    def _integrity(self, ledger: CostLedger) -> str:
        payload = ledger.model_dump(mode="json", exclude={"integrity_sha256"})
        for entry in payload["entries"]:
            if not entry.get("fixed_cost_cap_micros"):
                entry.pop("fixed_cost_cap_micros", None)
            if not entry.get("phase_reserve_borrowed_usd_micros"):
                entry.pop("phase_reserve_borrowed_usd_micros", None)
        if not payload["approval"].get("phase_policy_sha256"):
            payload["approval"].pop("phase_policy_sha256", None)
        canonical = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _atomic_write(self, path: Path, payload: dict[str, Any]) -> None:
        temp = path.with_suffix(path.suffix + f".{uuid4().hex}.tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temp, path)

    def _save_unlocked(self, ledger: CostLedger) -> None:
        ledger.revision += 1
        ledger.updated_at = utc_now()
        ledger.integrity_sha256 = self._integrity(ledger)
        self._atomic_write(self.ledger_path, ledger.model_dump(mode="json"))

    def _load_unlocked(self) -> CostLedger:
        ledger = CostLedger.model_validate_json(self.ledger_path.read_text(encoding="utf-8"))
        if ledger.integrity_sha256 != self._integrity(ledger):
            raise IntegrityError("cost ledger integrity check failed")
        approval = ApprovalRecord.model_validate_json(self.approval_path.read_text(encoding="utf-8"))
        if approval != ledger.approval:
            raise IntegrityError("approval record and ledger disagree")
        self._load_phase_policy_unlocked(approval)
        return ledger

    def _load_phase_policy_unlocked(
        self, approval: ApprovalRecord
    ) -> PhaseBudgetPolicy | None:
        expected = approval.phase_policy_sha256
        if expected is None:
            return None
        if not self.phase_policy_path.is_file():
            raise IntegrityError("approved phase budget policy is missing")
        policy = PhaseBudgetPolicy.model_validate_json(
            self.phase_policy_path.read_text(encoding="utf-8")
        )
        if policy.approval_id != approval.approval_id:
            raise IntegrityError("phase budget policy belongs to another approval")
        actual = phase_policy_sha256(policy)
        if policy.policy_sha256 != actual or expected != actual:
            raise IntegrityError("phase budget policy integrity check failed")
        if policy.budget_estimate_sha256 != approval.budget_estimate_sha256:
            raise IntegrityError("phase budget policy and estimate disagree")
        if sum(item.approved_usd_micros for item in policy.allocations) != (
            approval.approved_usd_micros
        ):
            raise IntegrityError("phase wallets do not sum to the total approval")
        return policy

    def approve(self, estimate: BudgetEnvelope, approved_usd: float) -> CostLedger:
        approved = dollars_to_micros(approved_usd)
        minimum = dollars_to_micros(estimate.minimum_cost_usd)
        recommended = dollars_to_micros(estimate.recommended_cost_usd)
        if approved < minimum:
            raise BudgetExceeded(
                f"approval ${micros_to_dollars(approved):.6f} is below minimum "
                f"${micros_to_dollars(minimum):.6f}"
            )
        if estimate.price_card_version != PRICE_CARD_VERSION:
            raise IntegrityError("budget estimate uses an outdated price card")
        self.run_dir.mkdir(parents=True, exist_ok=True)
        with self.lock:
            if self.approval_path.exists() or self.ledger_path.exists():
                raise BudgetGuardError("run already has an immutable approval")
            approval_id = str(uuid4())
            budget_estimate_sha256 = estimate_hash(estimate)
            phase_policy = allocate_phase_policy(
                approval_id=approval_id,
                budget_estimate_sha256=budget_estimate_sha256,
                approved_usd_micros=approved,
                estimates=estimate.phase_budgets,
            )
            approval = ApprovalRecord(
                approval_id=approval_id,
                approved_at=utc_now(),
                approved_usd_micros=approved,
                recommended_usd_micros=recommended,
                minimum_usd_micros=minimum,
                budget_estimate_sha256=budget_estimate_sha256,
                price_card_version=estimate.price_card_version,
                endpoint=estimate.endpoint,
                phase_policy_sha256=(
                    phase_policy.policy_sha256 if phase_policy is not None else None
                ),
            )
            ledger = CostLedger(
                run_id=str(uuid4()),
                revision=0,
                status=RunStatus.APPROVED,
                approval=approval,
                actual_usd_micros=0,
                reserved_usd_micros=0,
                entries=[],
                updated_at=utc_now(),
            )
            self._atomic_write(self.approval_path, approval.model_dump(mode="json"))
            if phase_policy is not None:
                self._atomic_write(
                    self.phase_policy_path, phase_policy.model_dump(mode="json")
                )
            self._save_unlocked(ledger)
            return ledger

    def read(self) -> CostLedger:
        with self.lock:
            return self._load_unlocked()

    def reserve_call(
        self,
        *,
        stage: str,
        model: str,
        input_token_cap: int,
        output_token_cap: int,
        fixed_cost_usd: float = 0.0,
    ) -> CostEntry:
        reserve = quote_call_micros(
            model, input_token_cap, output_token_cap, fixed_cost_usd
        )
        with self.lock:
            ledger = self._load_unlocked()
            if ledger.status not in {RunStatus.APPROVED, RunStatus.RUNNING}:
                raise BudgetGuardError(f"run cannot start a call while {ledger.status.value}")
            projected = ledger.actual_usd_micros + ledger.reserved_usd_micros + reserve
            now = utc_now()
            denial_reason: str | None = None
            phase_policy = self._load_phase_policy_unlocked(ledger.approval)
            phase = phase_for_stage(stage)
            phase_reserve_borrowed = 0
            if phase_policy is not None:
                allocation = next(
                    (item for item in phase_policy.allocations if item.phase == phase),
                    None,
                )
                if allocation is None:
                    denial_reason = f"stage has no approved phase wallet: {phase.value}"
                else:
                    phase_entries = [
                        entry for entry in ledger.entries
                        if phase_for_stage(entry.stage) == phase
                    ]
                    phase_spent = sum(
                        entry.actual_usd_micros
                        for entry in phase_entries
                        if entry.status == CallStatus.SETTLED
                    ) + sum(
                        entry.reserved_usd_micros
                        for entry in phase_entries
                        if entry.status == CallStatus.RESERVED
                    )
                    if phase_spent + reserve > allocation.approved_usd_micros:
                        shortfall = phase_spent + reserve - allocation.approved_usd_micros
                        reserve_allocation = next(
                            (
                                item for item in phase_policy.allocations
                                if item.phase == ExecutionPhase.RESERVE
                            ),
                            None,
                        )
                        reserve_entries = [
                            entry for entry in ledger.entries
                            if phase_for_stage(entry.stage) == ExecutionPhase.RESERVE
                        ]
                        reserve_spent = sum(
                            entry.actual_usd_micros
                            for entry in reserve_entries
                            if entry.status == CallStatus.SETTLED
                        ) + sum(
                            entry.reserved_usd_micros
                            for entry in reserve_entries
                            if entry.status == CallStatus.RESERVED
                        )
                        already_borrowed = sum(
                            entry.phase_reserve_borrowed_usd_micros
                            for entry in ledger.entries
                            if entry.status in {CallStatus.RESERVED, CallStatus.SETTLED}
                        )
                        reserve_available = max(
                            0,
                            (reserve_allocation.approved_usd_micros if reserve_allocation else 0)
                            - reserve_spent
                            - already_borrowed,
                        )
                        # The owner approved the elevated model rung and the
                        # total hard cap up front. Only that policy-qualified
                        # reasoning escalation may borrow unused reserve; an
                        # ordinary phase overrun still stops for a new decision.
                        approved_escalation = bool(
                            re.search(r"_reasoning_escalation_r\d+", stage)
                        )
                        if (
                            phase != ExecutionPhase.RESERVE
                            and approved_escalation
                            and shortfall <= reserve_available
                        ):
                            phase_reserve_borrowed = shortfall
                        else:
                            denial_reason = (
                                f"{phase.value} wallet would be exceeded; "
                                f"phase remaining ${micros_to_dollars(allocation.approved_usd_micros - phase_spent):.6f}"
                            )
                    repair_calls = sum(
                        1 for entry in phase_entries
                        if entry.status in {CallStatus.RESERVED, CallStatus.SETTLED}
                        and is_ai_repair_stage(entry.stage)
                    )
                    if (
                        denial_reason is None
                        and is_ai_repair_stage(stage)
                        and repair_calls >= allocation.max_ai_repair_calls
                    ):
                        denial_reason = (
                            f"{phase.value} AI repair-call limit is exhausted "
                            f"({allocation.max_ai_repair_calls})"
                        )
            if denial_reason is None and projected > ledger.approval.approved_usd_micros:
                denial_reason = "worst-case reservation would exceed approved budget"
            if denial_reason is not None:
                denied = CostEntry(
                    call_id=str(uuid4()),
                    stage=stage,
                    model=model,
                    status=CallStatus.DENIED,
                    input_token_cap=input_token_cap,
                    output_token_cap=output_token_cap,
                    reserved_usd_micros=0,
                    fixed_cost_cap_micros=dollars_to_micros(fixed_cost_usd),
                    actual_usd_micros=0,
                    actual_input_tokens=0,
                    actual_output_tokens=0,
                    created_at=now,
                    settled_at=now,
                    reason=denial_reason,
                )
                ledger.entries.append(denied)
                ledger.status = RunStatus.NEEDS_BUDGET
                self._save_unlocked(ledger)
                raise BudgetExceeded(
                    f"call blocked before provider invocation: {denial_reason}; "
                    f"needs ${micros_to_dollars(reserve):.6f}, "
                    f"remaining ${micros_to_dollars(ledger.approval.approved_usd_micros - ledger.actual_usd_micros - ledger.reserved_usd_micros):.6f}"
                )
            entry = CostEntry(
                call_id=str(uuid4()),
                stage=stage,
                model=model,
                status=CallStatus.RESERVED,
                input_token_cap=input_token_cap,
                output_token_cap=output_token_cap,
                reserved_usd_micros=reserve,
                actual_usd_micros=0,
                fixed_cost_cap_micros=dollars_to_micros(fixed_cost_usd),
                actual_input_tokens=0,
                actual_output_tokens=0,
                phase_reserve_borrowed_usd_micros=phase_reserve_borrowed,
                created_at=now,
            )
            ledger.entries.append(entry)
            ledger.reserved_usd_micros += reserve
            ledger.status = RunStatus.RUNNING
            self._save_unlocked(ledger)
            return entry

    def phase_summary(self) -> dict[str, dict[str, int]]:
        """Return auditable phase caps and current spend without mutating state."""

        with self.lock:
            ledger = self._load_unlocked()
            policy = self._load_phase_policy_unlocked(ledger.approval)
            if policy is None:
                return {}
            summary: dict[str, dict[str, int]] = {}
            for allocation in policy.allocations:
                entries = [
                    entry for entry in ledger.entries
                    if phase_for_stage(entry.stage) == allocation.phase
                ]
                summary[allocation.phase.value] = {
                    "approved_usd_micros": allocation.approved_usd_micros,
                    "actual_usd_micros": sum(
                        entry.actual_usd_micros
                        for entry in entries if entry.status == CallStatus.SETTLED
                    ),
                    "reserved_usd_micros": sum(
                        entry.reserved_usd_micros
                        for entry in entries if entry.status == CallStatus.RESERVED
                    ),
                    "ai_repair_calls": sum(
                        1 for entry in entries
                        if entry.status in {CallStatus.RESERVED, CallStatus.SETTLED}
                        and is_ai_repair_stage(entry.stage)
                    ),
                    "max_ai_repair_calls": allocation.max_ai_repair_calls,
                    "borrowed_from_reserve_usd_micros": sum(
                        entry.phase_reserve_borrowed_usd_micros
                        for entry in entries
                        if entry.status in {CallStatus.RESERVED, CallStatus.SETTLED}
                    ),
                }
            borrowed_total = sum(
                entry.phase_reserve_borrowed_usd_micros
                for entry in ledger.entries
                if entry.status in {CallStatus.RESERVED, CallStatus.SETTLED}
            )
            if ExecutionPhase.RESERVE.value in summary:
                summary[ExecutionPhase.RESERVE.value][
                    "lent_to_approved_escalations_usd_micros"
                ] = borrowed_total
            return summary

    def reserve_deterministic_attempt(
        self, *, phase: ExecutionPhase, purpose: str
    ) -> None:
        """Consume one bounded non-model tool attempt for an execution phase."""

        # Direct unit-level convergence runs do not create an approved budget
        # ledger. Production execution always does; keep the lower-level loop
        # independently testable without inventing authority.
        if not self.ledger_path.is_file():
            return
        with self.lock:
            ledger = self._load_unlocked()
            policy = self._load_phase_policy_unlocked(ledger.approval)
            if policy is None:
                return
            allocation = next(
                (item for item in policy.allocations if item.phase == phase), None
            )
            if allocation is None:
                raise BudgetGuardError(
                    f"deterministic attempt has no approved phase: {phase.value}"
                )
            if self.phase_attempts_path.is_file():
                attempts = PhaseAttemptLedger.model_validate_json(
                    self.phase_attempts_path.read_text(encoding="utf-8")
                )
                if attempts.integrity_sha256 != phase_attempt_ledger_sha256(attempts):
                    raise IntegrityError("phase attempt ledger integrity check failed")
                if attempts.policy_sha256 != policy.policy_sha256:
                    raise IntegrityError("phase attempt ledger and policy disagree")
            else:
                attempts = PhaseAttemptLedger(policy_sha256=policy.policy_sha256)
            used = sum(item.phase == phase for item in attempts.attempts)
            if used >= allocation.max_deterministic_attempts:
                raise BudgetExceeded(
                    f"{phase.value} deterministic-attempt limit is exhausted "
                    f"({allocation.max_deterministic_attempts})"
                )
            attempts.attempts.append(deterministic_attempt(
                attempt_id=str(uuid4()), phase=phase, purpose=purpose
            ))
            attempts.revision += 1
            attempts.integrity_sha256 = phase_attempt_ledger_sha256(attempts)
            self._atomic_write(
                self.phase_attempts_path, attempts.model_dump(mode="json")
            )

    def settle_call(
        self,
        call_id: str,
        *,
        input_tokens: int,
        output_tokens: int,
        fixed_cost_usd: float = 0.0,
    ) -> CostLedger:
        with self.lock:
            ledger = self._load_unlocked()
            entry = next((item for item in ledger.entries if item.call_id == call_id), None)
            if entry is None or entry.status != CallStatus.RESERVED:
                raise BudgetGuardError("call reservation is missing or already closed")
            if input_tokens > entry.input_token_cap or output_tokens > entry.output_token_cap:
                ledger.status = RunStatus.FAILED
                self._save_unlocked(ledger)
                raise IntegrityError("provider usage exceeded the reserved token caps")
            actual_fixed = dollars_to_micros(fixed_cost_usd)
            if actual_fixed > entry.fixed_cost_cap_micros:
                ledger.status = RunStatus.FAILED
                self._save_unlocked(ledger)
                raise IntegrityError("actual fixed cost exceeded the reserved cap")
            actual = actual_call_micros(
                entry.model, input_tokens, output_tokens, fixed_cost_usd
            )
            if actual > entry.reserved_usd_micros:
                ledger.status = RunStatus.FAILED
                self._save_unlocked(ledger)
                raise IntegrityError("actual model cost exceeded protected reservation")
            entry.status = CallStatus.SETTLED
            entry.actual_input_tokens = input_tokens
            entry.actual_output_tokens = output_tokens
            entry.actual_usd_micros = actual
            entry.settled_at = utc_now()
            ledger.reserved_usd_micros -= entry.reserved_usd_micros
            ledger.actual_usd_micros += actual
            self._save_unlocked(ledger)
            return ledger

    def release_call(self, call_id: str, reason: str) -> CostLedger:
        with self.lock:
            ledger = self._load_unlocked()
            entry = next((item for item in ledger.entries if item.call_id == call_id), None)
            if entry is None or entry.status != CallStatus.RESERVED:
                raise BudgetGuardError("call reservation is missing or already closed")
            ledger.reserved_usd_micros -= entry.reserved_usd_micros
            entry.status = CallStatus.RELEASED
            entry.reason = reason[:500]
            entry.settled_at = utc_now()
            self._save_unlocked(ledger)
            return ledger

    def complete(self) -> CostLedger:
        with self.lock:
            ledger = self._load_unlocked()
            if ledger.reserved_usd_micros:
                raise BudgetGuardError("cannot complete while calls are reserved")
            if ledger.status == RunStatus.NEEDS_BUDGET:
                raise BudgetGuardError("cannot complete a run waiting for more budget")
            ledger.status = RunStatus.COMPLETE
            self._save_unlocked(ledger)
            return ledger

    def fail(self, reason: str = "execution failed") -> CostLedger:
        """Close a failed run and release any unfinished call reservations."""
        with self.lock:
            ledger = self._load_unlocked()
            now = utc_now()
            for entry in ledger.entries:
                if entry.status == CallStatus.RESERVED:
                    ledger.reserved_usd_micros -= entry.reserved_usd_micros
                    entry.status = CallStatus.RELEASED
                    entry.reason = reason[:500]
                    entry.settled_at = now
            ledger.status = RunStatus.FAILED
            self._save_unlocked(ledger)
            return ledger

