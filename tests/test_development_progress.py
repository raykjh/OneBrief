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


def test_semantic_visual_failure_outranks_structural_runtime_evidence() -> None:
    semantic = "independent Unity semantic visual observation failed: mobile UI is clipped"
    evidence = "Unity runtime evidence validation failed: missing scenario"

    assert development_failure_quality(semantic) > development_failure_quality(evidence)
