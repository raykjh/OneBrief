import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from onebrief.budget_guard import (
    BudgetExceeded,
    BudgetGuardError,
    BudgetStore,
    CallStatus,
    IntegrityError,
    RunStatus,
)
from onebrief.schemas import BudgetEnvelope, BudgetStatus, StageEstimate


def _estimate(recommended: float = 0.10) -> BudgetEnvelope:
    return BudgetEnvelope(
        price_card_version="google-agent-platform-global-standard-search-2026-08-12",
        price_source_url="https://example.test/pricing",
        endpoint="global-standard",
        estimated_source_tokens=100,
        estimated_contract_tokens=100,
        stages=[
            StageEstimate(
                stage="draft",
                model="gemini-3.5-flash",
                input_tokens_per_call=100,
                output_tokens_per_call=100,
                minimum_calls=1,
                recommended_calls=1,
                maximum_calls=1,
                minimum_cost_usd=0.001,
                recommended_cost_usd=0.002,
                maximum_cost_usd=0.003,
                estimated_minutes_per_call=1,
            )
        ],
        minimum_cost_usd=0.001,
        recommended_cost_usd=recommended,
        maximum_cost_usd=0.20,
        recommended_approval_usd=recommended,
        budget_limit_usd=1.0,
        status=BudgetStatus.WITHIN_BUDGET,
        estimated_minutes_minimum=1,
        estimated_minutes_recommended=1,
        estimated_minutes_maximum=1,
        notes=[],
    )


def test_approval_is_immutable_and_settlement_stays_below_cap(tmp_path: Path) -> None:
    store = BudgetStore(tmp_path)
    store.approve(_estimate(), 0.10)
    with pytest.raises(BudgetGuardError):
        store.approve(_estimate(), 0.20)
    call = store.reserve_call(
        stage="draft",
        model="gemini-3.5-flash",
        input_token_cap=1000,
        output_token_cap=1000,
    )
    ledger = store.settle_call(call.call_id, input_tokens=900, output_tokens=700)
    assert ledger.status == RunStatus.RUNNING
    assert ledger.actual_usd_micros < ledger.approval.approved_usd_micros
    assert ledger.entries[0].status == CallStatus.SETTLED


def test_failed_run_closes_ledger_and_releases_reservations(tmp_path: Path) -> None:
    store = BudgetStore(tmp_path)
    store.approve(_estimate(), 0.10)
    store.reserve_call(
        stage="draft",
        model="gemini-3.5-flash",
        input_token_cap=1000,
        output_token_cap=1000,
    )

    ledger = store.fail("repository precondition failed")

    assert ledger.status == RunStatus.FAILED
    assert ledger.reserved_usd_micros == 0
    assert ledger.entries[0].status == CallStatus.RELEASED
    assert ledger.entries[0].reason == "repository precondition failed"



def test_call_is_denied_before_reservation_can_exceed_budget(tmp_path: Path) -> None:
    store = BudgetStore(tmp_path)
    store.approve(_estimate(0.001), 0.001)
    with pytest.raises(BudgetExceeded, match="blocked before provider invocation"):
        store.reserve_call(
            stage="huge",
            model="gemini-3.5-flash",
            input_token_cap=100_000,
            output_token_cap=100_000,
        )
    ledger = store.read()
    assert ledger.status == RunStatus.NEEDS_BUDGET
    assert ledger.actual_usd_micros == 0
    assert ledger.reserved_usd_micros == 0
    assert ledger.entries[-1].status == CallStatus.DENIED


def test_tampered_ledger_is_rejected(tmp_path: Path) -> None:
    store = BudgetStore(tmp_path)
    store.approve(_estimate(), 0.10)
    raw = json.loads(store.ledger_path.read_text(encoding="utf-8"))
    raw["status"] = "complete"
    store.ledger_path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(IntegrityError):
        store.read()


def test_concurrent_reservations_cannot_double_spend(tmp_path: Path) -> None:
    store = BudgetStore(tmp_path)
    store.approve(_estimate(0.025), 0.025)

    def reserve() -> str:
        try:
            return store.reserve_call(
                stage="parallel",
                model="gemini-3.5-flash",
                input_token_cap=1000,
                output_token_cap=2000,
            ).status.value
        except BudgetExceeded:
            return "blocked"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = sorted(pool.map(lambda _: reserve(), range(2)))
    assert outcomes == ["blocked", "reserved"]
    ledger = store.read()
    assert ledger.actual_usd_micros + ledger.reserved_usd_micros <= ledger.approval.approved_usd_micros
