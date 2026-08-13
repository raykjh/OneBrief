import json
from pathlib import Path

import pytest

from onebrief.budget_guard import BudgetExceeded, BudgetStore, IntegrityError
from onebrief.phase_execution import (
    FailureOwner,
    active_execution_phase,
    allocate_phase_policy,
    decide_repair_phase,
    is_evidence_path,
    path_allowed_for_phase,
    phase_for_stage,
)
from onebrief.handoff_protocol import FailureCode
from onebrief.schemas import (
    BudgetEnvelope,
    BudgetStatus,
    ExecutionPhase,
    PhaseBudgetEstimate,
    StageEstimate,
)


def _estimate(*, repair_limit: int = 2, deterministic_limit: int = 2) -> BudgetEnvelope:
    return BudgetEnvelope(
        price_card_version="google-agent-platform-global-standard-search-2026-08-12",
        price_source_url="https://example.test/pricing",
        endpoint="global-standard",
        estimated_source_tokens=100,
        estimated_contract_tokens=100,
        stages=[StageEstimate(
            stage="long_form_draft",
            model="gemini-3.5-flash",
            input_tokens_per_call=100,
            output_tokens_per_call=100,
            minimum_calls=1,
            recommended_calls=1,
            maximum_calls=2,
            minimum_cost_usd=0.001,
            recommended_cost_usd=0.01,
            maximum_cost_usd=0.02,
            estimated_minutes_per_call=1,
        )],
        minimum_cost_usd=0.001,
        recommended_cost_usd=0.1,
        maximum_cost_usd=0.2,
        recommended_approval_usd=0.1,
        budget_limit_usd=1.0,
        status=BudgetStatus.WITHIN_BUDGET,
        estimated_minutes_minimum=1,
        estimated_minutes_recommended=1,
        estimated_minutes_maximum=2,
        notes=[],
        phase_budgets=[
            PhaseBudgetEstimate(
                phase=ExecutionPhase.PRODUCT_IMPLEMENTATION,
                minimum_cost_usd=0.001,
                recommended_cost_usd=0.02,
                maximum_cost_usd=0.04,
                max_ai_repair_calls=repair_limit,
                max_deterministic_attempts=deterministic_limit,
                editable_scope=["product"],
            ),
            PhaseBudgetEstimate(
                phase=ExecutionPhase.EVIDENCE_CONSTRUCTION,
                minimum_cost_usd=0,
                recommended_cost_usd=0.03,
                maximum_cost_usd=0.06,
                max_ai_repair_calls=2,
                max_deterministic_attempts=2,
                editable_scope=["evidence"],
            ),
            PhaseBudgetEstimate(
                phase=ExecutionPhase.FINAL_VERIFICATION,
                minimum_cost_usd=0,
                recommended_cost_usd=0.03,
                maximum_cost_usd=0.06,
                max_deterministic_attempts=2,
            ),
            PhaseBudgetEstimate(
                phase=ExecutionPhase.SHARED_CONTEXT,
                minimum_cost_usd=0,
                recommended_cost_usd=0.02,
                maximum_cost_usd=0.04,
                max_deterministic_attempts=1,
            ),
        ],
    )


def test_active_execution_phase_prefers_typed_decision_over_stale_keys() -> None:
    state = {
        "onebrief:execution_phase": "evidence_construction",
        "onebrief_maker_model_binding": {
            "execution_phase": "evidence_construction",
        },
        "onebrief:phase_decision": {
            "next_phase": "product_implementation",
            "failure_owner": "product",
        },
    }

    assert active_execution_phase(state) == ExecutionPhase.PRODUCT_IMPLEMENTATION


def test_active_execution_phase_uses_bound_phase_before_direct_fallback() -> None:
    state = {
        "onebrief:execution_phase": "evidence_construction",
        "onebrief_maker_model_binding": {
            "execution_phase": "product_implementation",
        },
    }

    assert active_execution_phase(state) == ExecutionPhase.PRODUCT_IMPLEMENTATION


def test_phase_routing_and_path_authority_are_disjoint() -> None:
    assert phase_for_stage(
        "evidence_construction::repair::long_form_draft"
    ) == ExecutionPhase.EVIDENCE_CONSTRUCTION
    assert is_evidence_path("Assets/Tests/PlayMode/UiEvidenceTests.cs")
    assert not is_evidence_path("Assets/Scripts/LoginView.cs")
    assert path_allowed_for_phase(
        "Assets/Scripts/LoginView.cs", ExecutionPhase.PRODUCT_IMPLEMENTATION
    )
    assert not path_allowed_for_phase(
        "Assets/Tests/PlayMode/UiEvidenceTests.cs",
        ExecutionPhase.PRODUCT_IMPLEMENTATION,
    )
    assert path_allowed_for_phase(
        "Assets/Tests/PlayMode/UiEvidenceTests.cs",
        ExecutionPhase.EVIDENCE_CONSTRUCTION,
    )


def test_trusted_failure_selects_product_or_evidence_owner() -> None:
    evidence = decide_repair_phase(
        context="development_acceptance_verification",
        failure_text="Add a discoverable Unity PlayMode test and TestAssemblies asmdef.",
        round_number=1,
    )
    product = decide_repair_phase(
        context="development_acceptance_verification",
        failure_text="Independent Unity semantic visual observation failed: login text overlaps.",
        round_number=2,
        affected_paths=["Assets/Scripts/LoginView.cs"],
    )
    environment = decide_repair_phase(
        context="development_verification",
        failure_text="Unity license activation is unavailable.",
        round_number=3,
    )
    assert evidence.failure_owner == FailureOwner.EVIDENCE
    assert evidence.next_phase == ExecutionPhase.EVIDENCE_CONSTRUCTION
    assert product.failure_owner == FailureOwner.PRODUCT
    assert product.next_phase == ExecutionPhase.PRODUCT_IMPLEMENTATION
    assert environment.failure_owner == FailureOwner.ENVIRONMENT
    assert environment.next_phase is None
    assert not environment.model_repair_allowed


def test_batchmode_framebuffer_failure_cannot_open_product_edit_authority() -> None:
    decision = decide_repair_phase(
        context="development_verification",
        failure_text=(
            "UNITY TEST FAILURES OneBrief.Visual.Flow: ReadPixels was called to read "
            "pixels from system frame buffer, while not inside drawing frame."
        ),
        round_number=1,
    )

    assert decision.failure_owner == FailureOwner.EVIDENCE
    assert decision.failure_layer.value == "evidence_topology"
    assert decision.next_phase == ExecutionPhase.EVIDENCE_CONSTRUCTION
    assert path_allowed_for_phase(
        "Assets/Tests/PlayMode/OneBriefVisualTest.cs", decision.next_phase
    )
    assert not path_allowed_for_phase(
        "Assets/JULPAE/Scripts/Lobby/LobbyResponsiveLayout.cs", decision.next_phase
    )


def test_zero_discovered_visual_tests_selects_evidence_construction() -> None:
    decision = decide_repair_phase(
        context="development_verification",
        failure_text=(
            "Unity visual verification requires at least one executed "
            "OneBrief.Visual PlayMode test"
        ),
        round_number=1,
    )

    assert decision.failure_owner == FailureOwner.EVIDENCE
    assert decision.failure_layer.value == "evidence_topology"
    assert decision.next_phase == ExecutionPhase.EVIDENCE_CONSTRUCTION
    assert decision.model_repair_allowed is True


def test_invalid_unity_visual_manifest_stays_in_evidence_construction() -> None:
    decision = decide_repair_phase(
        context="development_verification",
        failure_text="Unity visual evidence manifest is invalid: scenarios Field required",
        round_number=3,
        affected_paths=["Assets/Tests/PlayMode/LoginVisualTest.cs"],
    )

    assert decision.failure_owner == FailureOwner.EVIDENCE
    assert decision.next_phase == ExecutionPhase.EVIDENCE_CONSTRUCTION
    assert decision.model_repair_allowed is True


def test_unavailable_unity_screenshot_is_typed_and_evidence_owned() -> None:
    decision = decide_repair_phase(
        context="development_verification",
        failure_text="Unity visual scenario login_to_lobby screenshot is unavailable",
        round_number=4,
        affected_paths=["Assets/Tests/PlayMode/OneBrief.Visual.LoginTest.cs"],
    )

    assert decision.failure_code == FailureCode.UNITY_SCREENSHOT_NOT_MATERIALIZED
    assert decision.failure_owner == FailureOwner.EVIDENCE
    assert decision.next_phase == ExecutionPhase.EVIDENCE_CONSTRUCTION


def test_frozen_julpae_m01_failure_routes_to_evidence() -> None:
    fixture = json.loads(
        (Path(__file__).parent / "fixtures" / "julpae_m01_screenshot_failure.json")
        .read_text(encoding="utf-8")
    )
    decision = decide_repair_phase(
        context="development_verification",
        failure_text=fixture["failure_text"],
        round_number=4,
    )

    assert decision.failure_code.value == fixture["expected_failure_code"]
    assert decision.failure_owner.value == fixture["expected_failure_owner"]
    assert decision.next_phase.value == fixture["expected_next_phase"]


def test_unity_visual_contract_remains_evidence_owned_with_mixed_candidate() -> None:
    decision = decide_repair_phase(
        context="development_acceptance_verification",
        failure_text=(
            "Unity visual test contract: each scenario must contain scenario_id, "
            "observed_state, interaction, assertion_count, viewport_width, "
            "viewport_height, and screenshot_path"
        ),
        round_number=4,
        affected_paths=[
            "Assets/JULPAE/Scripts/Localization/JulpaeLanguageSelectorGroup.cs",
            "Assets/Tests/PlayMode/OneBriefVisualTest.cs",
        ],
    )

    assert decision.failure_owner == FailureOwner.EVIDENCE
    assert decision.next_phase == ExecutionPhase.EVIDENCE_CONSTRUCTION


def test_real_scene_missing_control_stays_with_executed_evidence_probe() -> None:
    decision = decide_repair_phase(
        context="development_verification",
        failure_text=(
            "development verification failed: unity_playmode_visual_tests (exit_code=2) "
            "UNITY TEST FAILURES OneBrief.Visual.Flow: LanguageDropdown not found in "
            "LobbyScene. Expected: not null But was: null | Rejected repair delta changed "
            "path(s): Assets/Tests/PlayMode/OneBriefVisualTest.cs | Unity visual test "
            "contract: the test must interact with the real scene UI"
        ),
        round_number=5,
        affected_paths=["Assets/Tests/PlayMode/OneBriefVisualTest.cs"],
    )

    assert decision.failure_owner == FailureOwner.EVIDENCE
    assert decision.next_phase == ExecutionPhase.EVIDENCE_CONSTRUCTION
    assert decision.model_repair_allowed


def test_phase_repair_limit_blocks_before_provider_call(tmp_path: Path) -> None:
    store = BudgetStore(tmp_path)
    store.approve(_estimate(repair_limit=1), 0.1)
    first = store.reserve_call(
        stage="product_implementation::repair::long_form_draft",
        model="gemini-3.5-flash",
        input_token_cap=100,
        output_token_cap=100,
    )
    store.settle_call(first.call_id, input_tokens=50, output_tokens=50)

    with pytest.raises(BudgetExceeded, match="repair-call limit"):
        store.reserve_call(
            stage="product_implementation::repair::long_form_draft",
            model="gemini-3.5-flash",
            input_token_cap=100,
            output_token_cap=100,
        )


def test_compact_transport_retry_does_not_consume_semantic_repair_limit(
    tmp_path: Path,
) -> None:
    store = BudgetStore(tmp_path)
    store.approve(_estimate(repair_limit=0), 0.1)

    reservation = store.reserve_call(
        stage="final_verification::independent_verification_compact_retry",
        model="gemini-3.1-pro-preview",
        input_token_cap=100,
        output_token_cap=100,
    )

    assert reservation.status.value == "reserved"


def test_approved_reasoning_escalation_can_borrow_unused_reserve(tmp_path: Path) -> None:
    store = BudgetStore(tmp_path)
    estimate = _estimate(repair_limit=3).model_copy(update={
        "phase_budgets": [
            PhaseBudgetEstimate(
                phase=ExecutionPhase.PRODUCT_IMPLEMENTATION,
                minimum_cost_usd=0.001,
                recommended_cost_usd=0.001,
                maximum_cost_usd=0.001,
                max_ai_repair_calls=3,
                max_deterministic_attempts=2,
                editable_scope=["product"],
            ),
            PhaseBudgetEstimate(
                phase=ExecutionPhase.RESERVE,
                minimum_cost_usd=0,
                recommended_cost_usd=0,
                maximum_cost_usd=0,
                max_ai_repair_calls=0,
                max_deterministic_attempts=0,
            ),
        ]
    })
    store.approve(estimate, 0.10)

    call = store.reserve_call(
        stage="product_implementation::repair::long_form_draft_reasoning_escalation_r2",
        model="gemini-3.1-pro-preview",
        input_token_cap=1_000,
        output_token_cap=1_000,
    )

    assert call.phase_reserve_borrowed_usd_micros > 0
    summary = store.phase_summary()
    assert summary["product_implementation"]["borrowed_from_reserve_usd_micros"] > 0
    assert summary["reserve"]["lent_to_approved_escalations_usd_micros"] > 0


def test_repeated_escalations_borrow_only_incremental_reserve(tmp_path: Path) -> None:
    store = BudgetStore(tmp_path)
    estimate = _estimate(repair_limit=4).model_copy(update={
        "phase_budgets": [
            PhaseBudgetEstimate(
                phase=ExecutionPhase.PRODUCT_IMPLEMENTATION,
                minimum_cost_usd=0.001,
                recommended_cost_usd=0.001,
                maximum_cost_usd=0.001,
                max_ai_repair_calls=4,
                max_deterministic_attempts=2,
            ),
            PhaseBudgetEstimate(
                phase=ExecutionPhase.RESERVE,
                minimum_cost_usd=0,
                recommended_cost_usd=0,
                maximum_cost_usd=0,
            ),
        ]
    })
    store.approve(estimate, 0.10)
    first = store.reserve_call(
        stage="product_implementation::repair::long_form_draft_reasoning_escalation_r1",
        model="gemini-3.1-pro-preview",
        input_token_cap=1_000,
        output_token_cap=1_000,
    )
    store.settle_call(first.call_id, input_tokens=200, output_tokens=100)
    second = store.reserve_call(
        stage="product_implementation::repair::long_form_draft_reasoning_escalation_r2",
        model="gemini-3.1-pro-preview",
        input_token_cap=1_000,
        output_token_cap=1_000,
    )

    assert first.phase_reserve_borrowed_usd_micros > 0
    assert second.phase_reserve_borrowed_usd_micros > 0
    assert (
        first.phase_reserve_borrowed_usd_micros
        + second.phase_reserve_borrowed_usd_micros
        < first.reserved_usd_micros + second.reserved_usd_micros
    )


def test_non_escalated_phase_call_cannot_borrow_reserve(tmp_path: Path) -> None:
    store = BudgetStore(tmp_path)
    estimate = _estimate(repair_limit=3).model_copy(update={
        "phase_budgets": [
            PhaseBudgetEstimate(
                phase=ExecutionPhase.PRODUCT_IMPLEMENTATION,
                minimum_cost_usd=0.001,
                recommended_cost_usd=0.001,
                maximum_cost_usd=0.001,
                max_ai_repair_calls=3,
                max_deterministic_attempts=2,
            ),
            PhaseBudgetEstimate(
                phase=ExecutionPhase.RESERVE,
                minimum_cost_usd=0,
                recommended_cost_usd=0,
                maximum_cost_usd=0,
            ),
        ]
    })
    store.approve(estimate, 0.10)

    with pytest.raises(BudgetExceeded, match="wallet would be exceeded"):
        store.reserve_call(
            stage="product_implementation::long_form_draft",
            model="gemini-3.5-flash",
            input_token_cap=1_000,
            output_token_cap=1_000,
        )


def test_authority_preserving_evidence_repair_can_borrow_unused_reserve(
    tmp_path: Path,
) -> None:
    estimate = _estimate().model_copy(update={
        "phase_budgets": [
            PhaseBudgetEstimate(
                phase=ExecutionPhase.EVIDENCE_CONSTRUCTION,
                minimum_cost_usd=0.001,
                recommended_cost_usd=0.001,
                maximum_cost_usd=0.001,
                max_ai_repair_calls=2,
                max_deterministic_attempts=2,
            ),
            PhaseBudgetEstimate(
                phase=ExecutionPhase.RESERVE,
                minimum_cost_usd=0,
                recommended_cost_usd=0,
                maximum_cost_usd=0,
            ),
        ]
    })
    store = BudgetStore(tmp_path / "run")
    store.approve(estimate, 0.1)

    call = store.reserve_call(
        stage="evidence_construction::repair::long_form_draft",
        model="gemini-3.5-flash",
        input_token_cap=1_000,
        output_token_cap=1_000,
    )

    assert call.phase_reserve_borrowed_usd_micros > 0
    summary = store.phase_summary()
    assert summary["evidence_construction"]["borrowed_from_reserve_usd_micros"] > 0


def test_deterministic_attempts_have_a_separate_hard_limit(tmp_path: Path) -> None:
    store = BudgetStore(tmp_path)
    store.approve(_estimate(deterministic_limit=1), 0.1)
    store.reserve_deterministic_attempt(
        phase=ExecutionPhase.PRODUCT_IMPLEMENTATION,
        purpose="isolated build and tests",
    )
    with pytest.raises(BudgetExceeded, match="deterministic-attempt limit"):
        store.reserve_deterministic_attempt(
            phase=ExecutionPhase.PRODUCT_IMPLEMENTATION,
            purpose="repeat isolated build and tests",
        )


def test_phase_policy_is_bound_to_the_immutable_approval(tmp_path: Path) -> None:
    store = BudgetStore(tmp_path)
    store.approve(_estimate(), 0.1)
    raw = json.loads(store.phase_policy_path.read_text(encoding="utf-8"))
    raw["allocations"][0]["approved_usd_micros"] += 1
    store.phase_policy_path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(IntegrityError, match="phase budget policy integrity"):
        store.read()


def test_phase_policy_fills_asymmetric_escalation_headroom_before_surplus() -> None:
    estimates = [
        PhaseBudgetEstimate(
            phase=ExecutionPhase.PRODUCT_IMPLEMENTATION,
            minimum_cost_usd=0.10,
            recommended_cost_usd=0.50,
            maximum_cost_usd=0.60,
        ),
        PhaseBudgetEstimate(
            phase=ExecutionPhase.EVIDENCE_CONSTRUCTION,
            minimum_cost_usd=0.0,
            recommended_cost_usd=0.10,
            maximum_cost_usd=0.50,
        ),
        PhaseBudgetEstimate(
            phase=ExecutionPhase.FINAL_VERIFICATION,
            minimum_cost_usd=0.10,
            recommended_cost_usd=0.30,
            maximum_cost_usd=0.50,
        ),
    ]

    policy = allocate_phase_policy(
        approval_id="approval",
        budget_estimate_sha256="a" * 64,
        approved_usd_micros=1_250_000,
        estimates=estimates,
    )

    assert policy is not None
    caps = {item.phase: item.approved_usd_micros for item in policy.allocations}
    assert sum(caps.values()) == 1_250_000
    assert caps[ExecutionPhase.PRODUCT_IMPLEMENTATION] == 550_000
    assert caps[ExecutionPhase.EVIDENCE_CONSTRUCTION] == 300_000
    assert caps[ExecutionPhase.FINAL_VERIFICATION] == 400_000


def test_phase_policy_keeps_above_maximum_approval_as_borrowable_reserve() -> None:
    estimates = [
        PhaseBudgetEstimate(
            phase=ExecutionPhase.EVIDENCE_CONSTRUCTION,
            minimum_cost_usd=0.10,
            recommended_cost_usd=0.12,
            maximum_cost_usd=0.15,
            max_ai_repair_calls=2,
        ),
        PhaseBudgetEstimate(
            phase=ExecutionPhase.FINAL_VERIFICATION,
            minimum_cost_usd=0.10,
            recommended_cost_usd=0.12,
            maximum_cost_usd=0.15,
        ),
    ]

    policy = allocate_phase_policy(
        approval_id="approval",
        budget_estimate_sha256="b" * 64,
        approved_usd_micros=2_000_000,
        estimates=estimates,
    )

    assert policy is not None
    caps = {item.phase: item.approved_usd_micros for item in policy.allocations}
    assert caps[ExecutionPhase.EVIDENCE_CONSTRUCTION] == 150_000
    assert caps[ExecutionPhase.FINAL_VERIFICATION] == 150_000
    assert caps[ExecutionPhase.RESERVE] == 1_700_000
    assert sum(caps.values()) == 2_000_000
