from onebrief.unity_evidence_plan import (
    UnityEvidenceJourneyPlan,
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


def test_preflight_leaves_genuinely_new_scene_to_runtime_verification() -> None:
    assert not validate_unity_journey_targets(
        _plan("KhalinosLobby", "KhalinosSfxSlider"),
        _catalog(),
    )
