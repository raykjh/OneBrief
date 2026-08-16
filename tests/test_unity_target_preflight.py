from onebrief.unity_evidence_plan import (
    UnityEvidenceJourneyPlan,
    candidate_unity_object_names,
    candidate_unity_scene_names,
    render_unity_evidence_journey,
    validate_unity_journey_targets,
)


def _plan(scene_name: str, target: str) -> UnityEvidenceJourneyPlan:
    return UnityEvidenceJourneyPlan.model_validate({
        "summary": "Exercise one exact committed control.",
        "test_directory": "Assets/Tests/PlayMode",
        "steps": [
            {"action": "load_scene", "scene_name": scene_name},
            {"action": "set_slider_value", "target": target, "number_value": 0.5},
            {"action": "assert_active", "target": target},
            {"action": "capture", "scenario_id": "settings_control"},
        ],
    })


def _catalog() -> dict[str, object]:
    return {
        "schema_version": "onebrief-unity-scene-catalog-v1",
        "source_revision": "a" * 40,
        "scenes": [{
            "scene_name": "LobbyScene_All",
            "object_names": ["LobbyCanvas", "SettingsPanel"],
            "control_object_names": [
                "SfxSlider", "BgmSlider", "LanguageDropdown", "CloseButton",
            ],
        }],
    }


def test_preflight_rejects_guessed_control_and_returns_exact_candidates() -> None:
    issues = validate_unity_journey_targets(
        _plan("LobbyScene_All", "SettingsPanel/SfxVolumeSlider"),
        _catalog(),
    )

    assert issues
    assert "SfxVolumeSlider" in issues[0]
    assert "SfxSlider" in issues[0]


def test_preflight_accepts_exact_control_from_extended_control_catalog() -> None:
    assert not validate_unity_journey_targets(
        _plan("LobbyScene_All", "SettingsPanel/SfxSlider"),
        _catalog(),
    )


def test_preflight_accepts_new_scene_only_when_product_change_creates_it() -> None:
    candidate = {
        "changes": [{
            "path": "Assets/KhalinosClient/Scenes/KhalinosLobby.unity",
            "base_sha256": None,
            "content": "%YAML 1.1",
        }]
    }
    assert not validate_unity_journey_targets(
        _plan("KhalinosLobby", "KhalinosSfxSlider"),
        _catalog(),
        candidate_unity_scene_names(candidate),
    )


def test_preflight_rejects_new_scene_name_without_product_backing() -> None:
    issues = validate_unity_journey_targets(
        _plan("LoginScene_New", "StartButton"),
        _catalog(),
        set(),
    )

    assert issues
    assert "no committed scene or new product change creates it" in issues[0]


def test_preflight_accepts_literal_runtime_object_created_by_product_change() -> None:
    candidate = {
        "changes": [{
            "path": "Assets/KhalinosClient/NewLoginPresentation.cs",
            "base_sha256": None,
            "content": 'var panel = new GameObject("LoginPanel");',
        }]
    }

    assert not validate_unity_journey_targets(
        _plan("LobbyScene_All", "LoginPanel"),
        _catalog(),
        set(),
        candidate_unity_object_names(candidate),
    )


def test_candidate_runtime_objects_ignore_test_harness_literals() -> None:
    candidate = {
        "changes": [{
            "path": "Assets/Tests/PlayMode/FakeUi.cs",
            "base_sha256": None,
            "content": 'var panel = new GameObject("InventedPanel");',
        }]
    }

    assert candidate_unity_object_names(candidate) == set()


def test_trusted_journey_compacts_long_hierarchy_paths_in_receipt_trace() -> None:
    rendered = render_unity_evidence_journey(_plan(
        "LobbyScene_All",
        "Canvas/SafeArea/CenterPanel/SettingsPanel/AudioPanel/SfxSlider",
    ))

    assert 'trace.Add("set_slider:SfxSlider:0.5")' in rendered.playmode_test_source
    assert "set_slider:Canvas/SafeArea" not in rendered.playmode_test_source
