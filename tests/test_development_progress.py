from onebrief.development_progress import development_failure_quality


def test_unity_playmode_failure_outranks_static_contract_failure() -> None:
    static_failure = (
        "development verification failed: Unity visual test contract must "
        "perform real UI interaction"
    )
    playmode_failure = (
        "development verification failed: unity_playmode_visual_tests "
        "UNITY TEST FAILURES SettingsButton Expected: not null But was: null"
    )

    assert development_failure_quality(playmode_failure) > development_failure_quality(
        static_failure
    )


def test_runtime_evidence_failure_outranks_playmode_failure() -> None:
    playmode_failure = "development verification failed: unity_playmode_visual_tests"
    evidence_failure = "Unity runtime evidence validation failed: missing scenario"

    assert development_failure_quality(evidence_failure) > development_failure_quality(
        playmode_failure
    )


def test_missing_requested_locale_evidence_outranks_static_contract_failure() -> None:
    static_failure = (
        "development verification failed: Unity visual test contract: "
        "selected dropdown must be visible"
    )
    locale_failure = "Unity visual evidence did not exercise requested locale(s): es"

    assert development_failure_quality(locale_failure) > development_failure_quality(
        static_failure
    )


def test_png_viewport_mismatch_outranks_a_prior_playmode_failure() -> None:
    playmode_failure = (
        "development verification failed: unity_playmode_visual_tests "
        "UNITY TEST FAILURES WaitForEndOfFrame is not evoked in batchmode"
    )
    png_mismatch = "Unity visual scenario login_desktop viewport width does not match its PNG"

    assert development_failure_quality(png_mismatch) > development_failure_quality(
        playmode_failure
    )


def test_distinct_visual_scenario_failure_outranks_static_contract_failure() -> None:
    static_failure = (
        "development verification failed: Unity visual test contract must "
        "observe product text"
    )
    scenario_failure = (
        "Unity visual evidence requires a distinct rendered scenario for "
        "every requested real UI surface: login"
    )

    assert development_failure_quality(scenario_failure) > development_failure_quality(
        static_failure
    )


def test_viewport_contract_remains_a_static_failure() -> None:
    viewport_contract = (
        "development verification failed: Unity visual test contract: responsive "
        "Unity visual evidence must define and capture both a measured mobile viewport "
        "and a desktop viewport"
    )
    scenario_failure = (
        "Unity visual evidence requires a distinct rendered scenario for settings"
    )

    assert development_failure_quality(scenario_failure) > development_failure_quality(
        viewport_contract
    )


def test_semantic_visual_failure_outranks_structural_runtime_evidence() -> None:
    semantic = "independent Unity semantic visual observation failed: mobile UI is clipped"
    evidence = "Unity runtime evidence validation failed: missing scenario"

    assert development_failure_quality(semantic) > development_failure_quality(evidence)


def test_rejection_audit_suffixes_do_not_make_newer_runtime_evidence_worse() -> None:
    primary = (
        "Unity visual evidence reused an identical screenshot for lobby_desktop "
        "and lobby_mobile"
    )
    with_audit = (
        primary
        + " | Rejected repair delta changed Assets/Tests/PlayMode/Flow.cs"
        + " | Regression guard from the rejected attempt: older login failure"
        + " | Repair control: do not repeat the same executable content"
    )

    assert development_failure_quality(with_audit) == development_failure_quality(primary)
