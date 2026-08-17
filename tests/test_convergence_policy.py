from onebrief.convergence_policy import (
    CURRENT_CONVERGENCE_POLICY_REVISION,
    ConvergenceLedger,
    ConvergencePolicy,
    FailureLayer,
    LEGACY_CONVERGENCE_POLICY_REVISION,
    ProgressKind,
    new_convergence_ledger,
    repair_contract_blocks_resume,
)
from onebrief.handoff_protocol import FailureCode, FailureOwner


def test_new_convergence_epoch_is_distinct_from_legacy_stored_ledgers() -> None:
    legacy = ConvergenceLedger.model_validate({
        "schema_version": "onebrief-convergence-ledger-v1",
        "observations": [],
        "repair_contracts": [],
    })
    current = new_convergence_ledger()

    assert legacy.policy_revision == LEGACY_CONVERGENCE_POLICY_REVISION
    assert current.policy_revision == CURRENT_CONVERGENCE_POLICY_REVISION
    assert legacy.policy_revision != current.policy_revision


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


def test_stopped_repair_contract_allows_only_verifier_revalidation() -> None:
    policy = ConvergencePolicy()
    ledger = ConvergenceLedger()
    for attempt, strategy in enumerate(("strategy-a", "strategy-b"), start=1):
        observation = policy.observe(
            context="development_verification",
            failure_text="the same trusted runtime boundary failed",
            attempt_number=attempt,
            strategy_fingerprint=strategy,
        )
        ledger = policy.record(
            ledger, observation, policy.issue_contract(ledger, observation)
        )
    third = policy.observe(
        context="development_verification",
        failure_text="the same trusted runtime boundary failed",
        attempt_number=3,
        strategy_fingerprint="strategy-c",
    )
    stopped = policy.issue_contract(ledger, third)

    assert stopped.execution_allowed is False
    assert repair_contract_blocks_resume(
        stopped, verifier_only_revalidation=False
    )
    assert not repair_contract_blocks_resume(
        stopped, verifier_only_revalidation=True
    )


def test_missing_unity_playmode_harness_is_evidence_topology_not_product_runtime() -> None:
    policy = ConvergencePolicy()
    observation = policy.observe(
        context="development_verification",
        failure_text=(
            "development verification failed: Unity visual test contract: add a discoverable "
            "Unity PlayMode test whose namespace/full name begins with OneBrief.Visual | "
            "Unity visual test contract: add a Unity test .asmdef with optionalUnityReferences "
            "containing TestAssemblies"
        ),
        attempt_number=1,
    )
    contract = policy.issue_contract(ConvergenceLedger(), observation)

    assert observation.layer == FailureLayer.EVIDENCE_TOPOLOGY
    assert set(observation.symptom_keys) == {
        "artifact:missing_playmode_test",
        "artifact:missing_test_asmdef",
    }
    assert contract.execution_allowed is True
    assert contract.permitted_paths == []
    assert contract.verification_ladder[0] == "proof_topology_scan"


def test_missing_requested_unity_locale_is_evidence_topology() -> None:
    policy = ConvergencePolicy()
    observation = policy.observe(
        context="development_verification",
        failure_text="Unity visual evidence did not exercise requested locale(s): es",
        attempt_number=1,
    )

    assert observation.layer == FailureLayer.EVIDENCE_TOPOLOGY
    assert observation.symptom_keys == ["artifact:evidence_topology"]


def test_batchmode_system_framebuffer_readback_is_evidence_topology() -> None:
    observation = ConvergencePolicy().observe(
        context="development_verification",
        failure_text=(
            "UNITY TEST FAILURES OneBrief.Visual.Flow: Unhandled log message: "
            "'[Error] ReadPixels was called to read pixels from system frame buffer, "
            "while not inside drawing frame.'"
        ),
        attempt_number=1,
    )

    assert observation.layer == FailureLayer.EVIDENCE_TOPOLOGY


def test_missing_authentication_precondition_is_evidence_topology_not_product() -> None:
    observation = ConvergencePolicy().observe(
        context="development_verification",
        failure_text=(
            "Unity visual test contract: protected destination evidence must establish an "
            "approved authenticated/test state or exercise the complete authentication UI; "
            "a precondition-free click test must not drive product navigation repairs"
        ),
        attempt_number=1,
        affected_paths=["Assets/Tests/PlayMode/LoginFlowTest.cs"],
    )

    assert observation.layer == FailureLayer.EVIDENCE_TOPOLOGY
    assert observation.owner == FailureOwner.EVIDENCE
    assert observation.symptom_keys == ["artifact:auth_precondition"]

    contract = ConvergencePolicy().issue_contract(ConvergenceLedger(), observation)
    assert "existing test account" in contract.hypothesis.cheapest_probe
    assert "shipped UI/controller" in contract.hypothesis.cheapest_probe
    assert "product navigation" in contract.hypothesis.repair_boundary


def test_unchanged_requested_unity_locale_is_a_semantic_product_failure() -> None:
    observation = ConvergencePolicy().observe(
        context="development_verification",
        failure_text="Unity visual scenario locale_es did not visibly change any text",
        attempt_number=1,
    )

    assert observation.layer == FailureLayer.SEMANTIC_PRODUCT
    assert observation.symptom_keys == ["artifact:wrong_language"]


def test_detached_unity_component_opens_a_new_unrestricted_binding_probe() -> None:
    policy = ConvergencePolicy()
    observation = policy.observe(
        context="development_verification",
        failure_text=(
            "changed Unity UI MonoBehaviour SettingsBinder is not reachable from any committed "
            ".unity/.prefab script GUID, runtime initialization entrypoint, or other production "
            "source reference; repair or attach the active component instead of editing a detached source file"
        ),
        attempt_number=1,
        affected_paths=["Assets/UI/SettingsBinder.cs"],
    )
    contract = policy.issue_contract(ConvergenceLedger(), observation)

    assert observation.layer == FailureLayer.SEMANTIC_PRODUCT
    assert observation.symptom_keys == ["artifact:inactive_binding"]
    assert contract.execution_allowed is True
    assert contract.permitted_paths == []
    assert "active component" in contract.hypothesis.cheapest_probe


def test_mixed_unity_contract_routes_detached_product_failure_before_evidence_repairs() -> None:
    policy = ConvergencePolicy()
    observation = policy.observe(
        context="development_verification",
        failure_text=(
            "Unity visual test contract: changed Unity UI MonoBehaviour SettingsBinder "
            "is not reachable from any committed .unity/.prefab script GUID, runtime "
            "initialization entrypoint, or other production source reference; repair or "
            "attach the active component instead of editing a detached source file | "
            "Unity visual test contract: the OneBrief.Visual test must discover the real "
            "LanguageDropdown control from the loaded scene"
        ),
        attempt_number=1,
        affected_paths=["Assets/UI/SettingsBinder.cs"],
    )
    contract = policy.issue_contract(ConvergenceLedger(), observation)

    assert observation.layer == FailureLayer.SEMANTIC_PRODUCT
    assert observation.owner == FailureOwner.PRODUCT
    assert contract.verification_ladder[0] == "exact_source_promotion"
    assert "active component" in contract.hypothesis.cheapest_probe


def test_verification_only_candidate_routes_next_repair_to_product_owner() -> None:
    observation = ConvergencePolicy().observe(
        context="development_verification",
        failure_text=(
            "Unity visual test contract: the change set contains only verification code; "
            "add an actual production UI implementation under the approved project source "
            "before claiming UI modernization"
        ),
        attempt_number=2,
        affected_paths=["Assets/Tests/PlayMode/SettingsFlowTest.cs"],
    )

    assert observation.layer == FailureLayer.SEMANTIC_PRODUCT
    assert observation.owner == FailureOwner.PRODUCT


def test_missing_requested_unity_scene_routes_to_product_owner() -> None:
    observation = ConvergencePolicy().observe(
        context="development_verification",
        failure_text=(
            "unity_playmode_visual_tests: Scene 'TitleScene' couldn't be loaded because "
            "it has not been added to the active build profile or shared scene list"
        ),
        attempt_number=1,
        affected_paths=["Assets/Tests/PlayMode/OneBriefGeneratedJourneyTest.cs"],
    )

    assert observation.layer == FailureLayer.SEMANTIC_PRODUCT
    assert observation.owner == FailureOwner.PRODUCT


def test_inert_unity_component_routes_to_product_owner() -> None:
    observation = ConvergencePolicy().observe(
        context="development_verification",
        failure_text=(
            "Unity visual test contract: new Unity UI MonoBehaviour NavigationManager "
            "is not attached to a changed scene/prefab and has no runtime initialization "
            "entrypoint; an inert source file does not implement the UI"
        ),
        attempt_number=1,
        affected_paths=["Assets/Scripts/UI/NavigationManager.cs"],
    )

    assert observation.layer == FailureLayer.SEMANTIC_PRODUCT
    assert observation.owner == FailureOwner.PRODUCT


def test_missing_self_declared_unity_topology_path_routes_to_product_owner() -> None:
    observation = ConvergencePolicy().observe(
        context="development_verification",
        failure_text=(
            "Unity visual test contract: new Unity production source declares missing "
            "scene/prefab path Assets/Scenes/TitleScreen.unity; a maker-authored topology "
            "mapping is not evidence that the product region exists"
        ),
        attempt_number=1,
    )

    assert observation.layer == FailureLayer.SEMANTIC_PRODUCT
    assert observation.owner == FailureOwner.PRODUCT


def test_missing_tangible_topology_wins_over_missing_evidence_harness() -> None:
    observation = ConvergencePolicy().observe(
        context="development_verification",
        failure_text=(
            "Unity visual test contract: New-client construction preflight: the whole-product "
            "topology contains no actual production scene or prefab. A region enum is not a "
            "runtime product region. | Unity visual test contract: add a discoverable Unity "
            "PlayMode test | Unity visual test contract: add a Unity test .asmdef"
        ),
        attempt_number=1,
    )

    assert observation.layer == FailureLayer.SEMANTIC_PRODUCT
    assert observation.owner == FailureOwner.PRODUCT


def test_locale_observer_measurement_order_is_evidence_topology() -> None:
    observation = ConvergencePolicy().observe(
        context="development_verification",
        failure_text=(
            "Unity visual test contract: locale changed_visible_text_count must compare "
            "before/after visible text snapshots while the localized target surface remains active"
        ),
        attempt_number=1,
    )

    assert observation.layer == FailureLayer.EVIDENCE_TOPOLOGY
    assert observation.symptom_keys == ["artifact:evidence_topology"]


def test_invalid_unity_evidence_manifest_is_evidence_topology() -> None:
    observation = ConvergencePolicy().observe(
        context="development_verification",
        failure_text=(
            "Unity visual evidence manifest is invalid: scenarios Field required"
        ),
        attempt_number=1,
        affected_paths=["Assets/Tests/PlayMode/LoginVisualTest.cs"],
    )

    assert observation.layer == FailureLayer.EVIDENCE_TOPOLOGY
    assert observation.symptom_keys == ["artifact:invalid_evidence_manifest"]


def test_evidence_topology_second_variant_requires_generalization_and_third_stops() -> None:
    policy = ConvergencePolicy()
    ledger = ConvergenceLedger()
    failures = [
        (
            "Unity visual test contract: the OneBrief.Visual test must write "
            "onebrief-evidence/runtime-evidence.json | Unity visual test contract: "
            "add a Unity test .asmdef with optionalUnityReferences containing TestAssemblies"
        ),
        (
            "Unity visual test contract: runtime-evidence.json must use schema_version "
            "onebrief-unity-visual-evidence-v1 | Unity visual test contract: a camera "
            "RenderTexture does not capture ScreenSpaceOverlay UI | Unity visual test contract: "
            "add a Unity test .asmdef with optionalUnityReferences containing TestAssemblies"
        ),
        (
            "Unity visual test contract: each general UI evidence scenario must contain "
            "scenario_id, observed_state, interaction, assertion_count, viewport_width, "
            "viewport_height, and screenshot_path | Unity visual test contract: add a Unity "
            "test .asmdef with optionalUnityReferences containing TestAssemblies"
        ),
    ]
    contracts = []
    for index, failure in enumerate(failures, 1):
        observation = policy.observe(
            context="development_verification",
            failure_text=failure,
            attempt_number=index,
            affected_paths=["Assets/Tests/PlayMode/VisualFlowTest.cs"],
            strategy_fingerprint=f"strategy-{index}",
        )
        contract = policy.issue_contract(ledger, observation)
        ledger = policy.record(ledger, observation, contract)
        contracts.append(contract)

    assert contracts[0].execution_allowed is True
    assert contracts[1].execution_allowed is False
    assert contracts[1].generalization_required is True
    assert contracts[1].case_patch_allowed is False
    assert contracts[2].execution_allowed is False
    assert contracts[2].variant_count == 3
    assert contracts[2].case_patch_allowed is False
    assert contracts[-1].progress_kind == ProgressKind.NO_PROGRESS
    assert contracts[-1].permitted_paths == []


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


def test_exact_observation_replay_is_idempotent_not_a_new_failure() -> None:
    policy = ConvergencePolicy()
    ledger = ConvergenceLedger()
    observation = policy.observe(
        context="development_verification",
        failure_text="runtime evidence viewport does not match its PNG",
        attempt_number=1,
        affected_paths=["Assets/Tests/PlayMode/VisualFlowTest.cs"],
        strategy_fingerprint="measured-png",
    )
    first = policy.issue_contract(ledger, observation)
    ledger = policy.record(ledger, observation, first)

    replay = policy.issue_contract(ledger, observation)
    replayed_ledger = policy.record(ledger, observation, replay)

    assert replay.contract_id == first.contract_id
    assert replay.execution_allowed is True
    assert replayed_ledger == ledger


def test_restart_replay_is_idempotent_when_strategy_metadata_is_absent() -> None:
    policy = ConvergencePolicy()
    failure = (
        "development verification failed: unity_playmode_visual_tests (exit_code=2) | "
        "UNITY TEST FAILURES LanguageDropdown not found in LobbyScene"
    )
    first_observation = policy.observe(
        context="development_verification",
        failure_text=failure,
        attempt_number=1,
        affected_paths=["Assets/Tests/PlayMode/OneBriefVisualTest.cs"],
        strategy_fingerprint="candidate-a",
    )
    first_contract = policy.issue_contract(ConvergenceLedger(), first_observation)
    ledger = policy.record(ConvergenceLedger(), first_observation, first_contract)

    resumed_observation = policy.observe(
        context="development_verification",
        failure_text=failure,
        attempt_number=1,
    )
    resumed_contract = policy.issue_contract(ledger, resumed_observation)

    assert resumed_observation.observation_id == first_observation.observation_id
    assert resumed_contract.contract_id == first_contract.contract_id
    assert resumed_contract.execution_allowed is True


def test_all_static_unity_visual_contract_failures_are_evidence_topology() -> None:
    observation = ConvergencePolicy().observe(
        context="development_verification",
        failure_text=(
            "Unity visual test contract: runtime evidence must record actual captured "
            "PNG texture dimensions"
        ),
        attempt_number=1,
    )

    assert observation.layer == FailureLayer.EVIDENCE_TOPOLOGY


def test_second_variant_requires_generalization_before_execution() -> None:
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
    assert contract.execution_allowed is False
    assert contract.generalization_required is True
    assert contract.case_patch_allowed is False
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


def test_screenshot_materialization_failure_is_a_typed_v2_observation() -> None:
    observation = ConvergencePolicy().observe(
        context="development_verification",
        failure_text="Unity visual scenario login_to_lobby screenshot is unavailable",
        attempt_number=4,
        affected_paths=["Assets/Tests/PlayMode/LoginVisualTest.cs"],
    )

    assert observation.schema_version == "onebrief-failure-observation-v2"
    assert observation.code == FailureCode.UNITY_SCREENSHOT_NOT_MATERIALIZED
    assert observation.owner == FailureOwner.EVIDENCE
    assert observation.layer == FailureLayer.EVIDENCE_RUNTIME
