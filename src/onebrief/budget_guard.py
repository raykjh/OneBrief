"""Tamper-evident, atomic budget approval and per-call reservation ledger."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime
from decimal import Decimal, ROUND_CEILING
from enum import StrEnum
from pathlib import Path
from typing import Any
from uuid import uuid4

from filelock import FileLock
from pydantic import BaseModel, Field, model_validator

from onebrief.producer import PRICE_CARD_VERSION, PRICES
from onebrief.schemas import BudgetEnvelope

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


class CostEntry(BaseModel):
    call_id: str
    stage: str
    model: str
    status: CallStatus
    input_token_cap: int = Field(ge=0)
    output_token_cap: int = Field(ge=0)
    reserved_usd_micros: int = Field(ge=0)
    actual_usd_micros: int = Field(ge=0)
    actual_input_tokens: int = Field(ge=0)
    actual_output_tokens: int = Field(ge=0)
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


def quote_call_micros(model: str, input_tokens: int, output_tokens: int) -> int:
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
    return int(protected.to_integral_value(rounding=ROUND_CEILING))


def actual_call_micros(model: str, input_tokens: int, output_tokens: int) -> int:
    price = PRICES[model]
    raw = (
        Decimal(input_tokens) * Decimal(str(price.input_per_million))
        + Decimal(output_tokens) * Decimal(str(price.output_per_million))
    )
    return int(raw.to_integral_value(rounding=ROUND_CEILING))


class BudgetStore:
    """Single-run file store protected against concurrent writers."""

    def __init__(self, run_dir: Path):
        self.run_dir = run_dir
        self.approval_path = run_dir / "approval.json"
        self.ledger_path = run_dir / "cost_ledger.json"
        self.lock = FileLock(str(run_dir / ".budget.lock"), timeout=10)

    def _integrity(self, ledger: CostLedger) -> str:
        payload = ledger.model_dump(mode="json", exclude={"integrity_sha256"})
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
        return ledger

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
            approval = ApprovalRecord(
                approval_id=str(uuid4()),
                approved_at=utc_now(),
                approved_usd_micros=approved,
                recommended_usd_micros=recommended,
                minimum_usd_micros=minimum,
                budget_estimate_sha256=estimate_hash(estimate),
                price_card_version=estimate.price_card_version,
                endpoint=estimate.endpoint,
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
    ) -> CostEntry:
        reserve = quote_call_micros(model, input_token_cap, output_token_cap)
        with self.lock:
            ledger = self._load_unlocked()
            if ledger.status not in {RunStatus.APPROVED, RunStatus.RUNNING}:
                raise BudgetGuardError(f"run cannot start a call while {ledger.status.value}")
            projected = ledger.actual_usd_micros + ledger.reserved_usd_micros + reserve
            now = utc_now()
            if projected > ledger.approval.approved_usd_micros:
                denied = CostEntry(
                    call_id=str(uuid4()),
                    stage=stage,
                    model=model,
                    status=CallStatus.DENIED,
                    input_token_cap=input_token_cap,
                    output_token_cap=output_token_cap,
                    reserved_usd_micros=0,
                    actual_usd_micros=0,
                    actual_input_tokens=0,
                    actual_output_tokens=0,
                    created_at=now,
                    settled_at=now,
                    reason="worst-case reservation would exceed approved budget",
                )
                ledger.entries.append(denied)
                ledger.status = RunStatus.NEEDS_BUDGET
                self._save_unlocked(ledger)
                raise BudgetExceeded(
                    f"call blocked before provider invocation: needs ${micros_to_dollars(reserve):.6f}, "
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
                actual_input_tokens=0,
                actual_output_tokens=0,
                created_at=now,
            )
            ledger.entries.append(entry)
            ledger.reserved_usd_micros += reserve
            ledger.status = RunStatus.RUNNING
            self._save_unlocked(ledger)
            return entry

    def settle_call(self, call_id: str, *, input_tokens: int, output_tokens: int) -> CostLedger:
        with self.lock:
            ledger = self._load_unlocked()
            entry = next((item for item in ledger.entries if item.call_id == call_id), None)
            if entry is None or entry.status != CallStatus.RESERVED:
                raise BudgetGuardError("call reservation is missing or already closed")
            if input_tokens > entry.input_token_cap or output_tokens > entry.output_token_cap:
                ledger.status = RunStatus.FAILED
                self._save_unlocked(ledger)
                raise IntegrityError("provider usage exceeded the reserved token caps")
            actual = actual_call_micros(entry.model, input_tokens, output_tokens)
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

