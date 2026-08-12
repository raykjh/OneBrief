from onebrief.convergence_policy import (
    ConvergenceLedger,
    ConvergencePolicy,
    FailureLayer,
    ProgressKind,
)


def test_source_binding_failure_uses_deterministic_probe_without_model_escalation() -> None:
    policy = ConvergencePolicy()
    observation = policy.observe(
        context="development_candidate_promotion",
        failure_text=(
            "edit anchors could not rediscover one approved source range: "
            "Assets/UI/Settings.cs"
        ),
        attempt_number=1,
        affected_paths=["Assets/UI/Settings.cs"],
        strategy_fingerprint="strategy-a",
    )
    contract = policy.issue_contract(ConvergenceLedger(), observation)

    assert observation.layer == FailureLayer.SOURCE_BINDING
    assert contract.execution_allowed is True
    assert contract.hypothesis.requires_model_reasoning is False
    assert contract.verification_ladder[0] == "schema_and_selector_probe"


def test_same_failure_and_same_strategy_is_blocked_as_no_progress() -> None:
    policy = ConvergencePolicy()
    ledger = ConvergenceLedger()
    first = policy.observe(
        context="development_verification",
        failure_text="independent Unity semantic visual observation failed: mobile lobby overlap",
        attempt_number=1,
        affected_paths=["Assets/UI/Lobby.cs"],
        strategy_fingerprint="same-strategy",
    )
    first_contract = policy.issue_contract(ledger, first)
    ledger = policy.record(ledger, first, first_contract)
    repeated = policy.observe(
        context="development_verification",
        failure_text="independent Unity semantic visual observation failed: mobile lobby overlap",
        attempt_number=2,
        affected_paths=["Assets/UI/Lobby.cs"],
        strategy_fingerprint="same-strategy",
    )
    repeated_contract = policy.issue_contract(ledger, repeated)

    assert repeated_contract.progress_kind == ProgressKind.NO_PROGRESS
    assert repeated_contract.execution_allowed is False
    assert repeated_contract.escalation_required is True


def test_one_materially_different_hypothesis_is_allowed() -> None:
    policy = ConvergencePolicy()
    ledger = ConvergenceLedger()
    first = policy.observe(
        context="development_verification",
        failure_text="runtime test failed: settings navigation",
        attempt_number=1,
        affected_paths=["Assets/UI/Router.cs"],
        strategy_fingerprint="strategy-a",
    )
    ledger = policy.record(ledger, first, policy.issue_contract(ledger, first))
    second = policy.observe(
        context="development_verification",
        failure_text="runtime test failed: settings navigation",
        attempt_number=2,
        affected_paths=["Assets/UI/Router.cs"],
        strategy_fingerprint="strategy-b",
    )
    contract = policy.issue_contract(ledger, second)

    assert contract.progress_kind == ProgressKind.NEW_HYPOTHESIS
    assert contract.execution_allowed is True
    assert contract.occurrence == 2


def test_third_same_causal_boundary_stops_even_with_new_strategy() -> None:
    policy = ConvergencePolicy()
    ledger = ConvergenceLedger()
    for attempt, strategy in ((1, "a"), (2, "b")):
        observation = policy.observe(
            context="development_verification",
            failure_text="compile error: MissingType",
            attempt_number=attempt,
            affected_paths=["Assets/Product.cs"],
            strategy_fingerprint=strategy,
        )
        ledger = policy.record(
            ledger, observation, policy.issue_contract(ledger, observation)
        )
    third = policy.observe(
        context="development_verification",
        failure_text="compile error: MissingType",
        attempt_number=3,
        affected_paths=["Assets/Product.cs"],
        strategy_fingerprint="c",
    )
    contract = policy.issue_contract(ledger, third)

    assert contract.execution_allowed is False
    assert contract.progress_kind == ProgressKind.NO_PROGRESS


def test_authority_failure_never_authorizes_autonomous_repair() -> None:
    policy = ConvergencePolicy()
    observation = policy.observe(
        context="safe_apply",
        failure_text="PermissionError: path is outside the approved authority scope",
        attempt_number=1,
    )
    contract = policy.issue_contract(ConvergenceLedger(), observation)

    assert observation.layer == FailureLayer.AUTHORITY
    assert contract.execution_allowed is False
    assert contract.verification_ladder == ["authority_digest_check"]


def test_rephrased_visual_observation_cannot_reset_same_symptom_counter() -> None:
    policy = ConvergencePolicy()
    ledger = ConvergenceLedger()
    failures = [
        (
            "screenshots/lobby_mobile.png: UI elements are overlapping and unreadable; "
            "screenshots/settings_mobile.png: UI is clipped; "
            "screenshots/settings_mobile.png: Missing glyphs are visible."
        ),
        (
            "screenshots/lobby_mobile.png: Severe UI overlapping is present; "
            "screenshots/settings_mobile.png: controls are clipping at both sides; "
            "screenshots/settings_mobile.png: unsupported glyph tofu □ remains."
        ),
        (
            "screenshots/lobby_mobile.png: navigation labels overlap; "
            "screenshots/settings_mobile.png: the panel is clipped; "
            "screenshots/settings_mobile.png: Missing glyph remains."
        ),
    ]
    contracts = []
    for attempt, failure in enumerate(failures, start=1):
        observation = policy.observe(
            context="development_verification",
            failure_text=failure,
            attempt_number=attempt,
            affected_paths=[f"Assets/UI/Attempt{attempt}.cs"],
            strategy_fingerprint=f"strategy-{attempt}",
        )
        contract = policy.issue_contract(ledger, observation)
        contracts.append(contract)
        ledger = policy.record(ledger, observation, contract)

    assert contracts[0].occurrence == 1
    assert contracts[1].occurrence == 2
    assert contracts[2].occurrence == 3
    assert contracts[2].execution_allowed is False
    assert contracts[2].progress_kind == ProgressKind.NO_PROGRESS


def test_removed_visual_symptom_counts_as_real_progress() -> None:
    policy = ConvergencePolicy()
    ledger = ConvergenceLedger()
    first = policy.observe(
        context="development_verification",
        failure_text=(
            "screenshots/lobby_mobile.png: controls overlap; "
            "screenshots/settings_mobile.png: panel is clipped; "
            "screenshots/settings_mobile.png: missing glyph remains."
        ),
        attempt_number=1,
    )
    ledger = policy.record(ledger, first, policy.issue_contract(ledger, first))
    narrowed = policy.observe(
        context="development_verification",
        failure_text=(
            "screenshots/lobby_mobile.png: controls overlap; "
            "screenshots/settings_mobile.png: panel is clipped."
        ),
        attempt_number=2,
    )
    contract = policy.issue_contract(ledger, narrowed)

    assert contract.progress_kind == ProgressKind.CRITERION_ADVANCE
    assert contract.execution_allowed is True


def test_unity_layout_evidence_replaces_global_scaler_guess_with_topology_probe() -> None:
    policy = ConvergencePolicy()
    observation = policy.observe(
        context="development_verification",
        failure_text=(
            "independent Unity semantic visual observation failed: "
            "screenshots/lobby_mobile.png: controls overlap | "
            "Deterministic Unity layout diagnostics: "
            '{"risk_counts":{"stretched_width_positive_delta":2},'
            '"highest_priority_risks":["Lobby/Canvas/Panel"]}'
        ),
        attempt_number=1,
        affected_paths=["Assets/UI/Lobby.cs"],
    )

    contract = policy.issue_contract(ConvergenceLedger(), observation)

    assert "RectTransform topology risk" in contract.hypothesis.suspected_cause
    assert "nearest risky RectTransform ancestry" in contract.hypothesis.cheapest_probe
    assert contract.hypothesis.repair_boundary == (
        "One diagnosed production RectTransform ancestry and one visible symptom."
    )
