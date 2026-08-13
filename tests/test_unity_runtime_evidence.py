import json
from pathlib import Path

import pytest

from onebrief.unity_runtime_evidence import (
    requested_ui_transition,
    validate_and_copy_unity_visual_evidence,
)


def png(width: int = 32, height: int = 32) -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n"
        + b"\x00\x00\x00\rIHDR"
        + width.to_bytes(4, "big")
        + height.to_bytes(4, "big")
        + b"\x08\x06\x00\x00\x00"
    )


def write_results(path: Path, *, visual: bool = True) -> None:
    name = "OneBrief.Visual.LanguageSwitch" if visual else "Project.LegacyTest"
    path.write_text(
        f'<test-run testcasecount="1" passed="1" failed="0">'
        f'<test-suite><test-case fullname="{name}" result="Passed" /></test-suite>'
        f'</test-run>',
        encoding="utf-8",
    )


def write_evidence(root: Path, scenarios: list[dict[str, object]]) -> None:
    evidence = root / "onebrief-evidence"
    (evidence / "captures").mkdir(parents=True)
    for scenario in scenarios:
        (evidence / str(scenario["screenshot_path"])).write_bytes(
            png(
                int(scenario.get("viewport_width", 32)),
                int(scenario.get("viewport_height", 32)),
            ) + str(scenario["scenario_id"]).encode("utf-8")
        )
    (evidence / "runtime-evidence.json").write_text(
        json.dumps({
            "schema_version": "onebrief-unity-visual-evidence-v1",
            "scenarios": scenarios,
        }),
        encoding="utf-8",
    )


def scenario(locale: str, *, missing: int = 0) -> dict[str, object]:
    return {
        "scenario_id": f"locale-{locale}",
        "expected_locale": locale,
        "observed_locale": locale,
        "changed_visible_text_count": 3,
        "missing_glyph_count": missing,
        "screenshot_path": f"captures/{locale}.png",
    }


def ui_scenario(
    state: str, *, width: int, height: int, interaction: str = "open real screen"
) -> dict[str, object]:
    return {
        "scenario_id": state,
        "observed_state": state,
        "interaction": interaction,
        "assertion_count": 2,
        "viewport_width": width,
        "viewport_height": height,
        "screenshot_path": f"captures/{state}-{width}x{height}.png",
    }


def test_valid_visual_evidence_is_copied_and_summarized(tmp_path: Path) -> None:
    results = tmp_path / "results.xml"
    write_results(results)
    write_evidence(tmp_path, [scenario("zh-Hans"), scenario("ja"), scenario("es")])

    summary = validate_and_copy_unity_visual_evidence(
        tmp_path,
        results,
        tmp_path / "packaged",
        "Add Chinese, Japanese, and Spanish multilingual UI.",
    )

    assert summary.test_count == 1
    assert summary.observed_locales == ["es", "ja", "zh-hans"]
    assert len(summary.screenshot_paths) == 3
    assert (tmp_path / "packaged" / "runtime-evidence.json").is_file()


def test_missing_glyphs_fail_even_when_playmode_test_claims_pass(tmp_path: Path) -> None:
    results = tmp_path / "results.xml"
    write_results(results)
    write_evidence(tmp_path, [scenario("zh-Hans", missing=4)])

    with pytest.raises(RuntimeError, match="missing glyph"):
        validate_and_copy_unity_visual_evidence(
            tmp_path, results, tmp_path / "packaged", "Chinese UI"
        )


def test_missing_manifest_instructs_runtime_test_repair_not_static_artifact(
    tmp_path: Path,
) -> None:
    results = tmp_path / "results.xml"
    write_results(results)

    with pytest.raises(RuntimeError, match="generated during the executed") as failure:
        validate_and_copy_unity_visual_evidence(
            tmp_path, results, tmp_path / "packaged", "Login UI"
        )

    assert "do not add a static onebrief-evidence file" in str(failure.value)


def test_every_explicitly_requested_locale_must_be_exercised(tmp_path: Path) -> None:
    results = tmp_path / "results.xml"
    write_results(results)
    write_evidence(tmp_path, [scenario("ja")])

    with pytest.raises(RuntimeError, match="requested locale"):
        validate_and_copy_unity_visual_evidence(
            tmp_path, results, tmp_path / "packaged", "Japanese and Spanish UI"
        )


def test_legacy_or_zero_coverage_test_cannot_masquerade_as_visual_proof(tmp_path: Path) -> None:
    results = tmp_path / "results.xml"
    write_results(results, visual=False)
    write_evidence(tmp_path, [scenario("ja")])

    with pytest.raises(RuntimeError, match="OneBrief.Visual"):
        validate_and_copy_unity_visual_evidence(
            tmp_path, results, tmp_path / "packaged", "Japanese UI"
        )


def test_identical_locale_screenshots_cannot_masquerade_as_visual_proof(tmp_path: Path) -> None:
    results = tmp_path / "results.xml"
    write_results(results)
    scenarios = [scenario("ja"), scenario("es")]
    write_evidence(tmp_path, scenarios)
    duplicate = (tmp_path / "onebrief-evidence" / str(scenarios[0]["screenshot_path"])).read_bytes()
    (tmp_path / "onebrief-evidence" / str(scenarios[1]["screenshot_path"])).write_bytes(duplicate)

    with pytest.raises(RuntimeError, match="identical screenshot"):
        validate_and_copy_unity_visual_evidence(
            tmp_path, results, tmp_path / "packaged", "Japanese and Spanish UI"
        )


def test_known_evidence_root_prefix_is_normalized_without_widening_path_access(tmp_path: Path) -> None:
    results = tmp_path / "results.xml"
    write_results(results)
    item = scenario("ja")
    item["screenshot_path"] = "onebrief-evidence/captures/ja.png"
    evidence = tmp_path / "onebrief-evidence"
    (evidence / "captures").mkdir(parents=True)
    (evidence / "captures" / "ja.png").write_bytes(png() + b"ja")
    (evidence / "runtime-evidence.json").write_text(
        json.dumps({"schema_version": "onebrief-unity-visual-evidence-v1", "scenarios": [item]}),
        encoding="utf-8",
    )

    summary = validate_and_copy_unity_visual_evidence(
        tmp_path, results, tmp_path / "packaged", "Japanese UI"
    )

    assert summary.screenshot_paths == ["screenshots/captures/ja.png"]


def test_general_unity_ui_evidence_does_not_require_fake_locale_fields(tmp_path: Path) -> None:
    results = tmp_path / "results.xml"
    write_results(results)
    scenarios = [
        ui_scenario("login-mobile", width=32, height=64),
        ui_scenario("lobby-desktop", width=64, height=32, interaction="login to lobby"),
        ui_scenario("settings-desktop", width=64, height=32, interaction="open settings and return to lobby"),
    ]
    write_evidence(tmp_path, scenarios)

    summary = validate_and_copy_unity_visual_evidence(
        tmp_path,
        results,
        tmp_path / "packaged",
        "Modernize login, lobby, and settings UI for mobile and desktop.",
    )

    assert summary.observed_locales == []
    assert summary.observed_states == ["lobby-desktop", "login-mobile", "settings-desktop"]


def test_general_unity_ui_evidence_requires_every_requested_surface(tmp_path: Path) -> None:
    results = tmp_path / "results.xml"
    write_results(results)
    write_evidence(tmp_path, [ui_scenario("login", width=32, height=32)])

    with pytest.raises(RuntimeError, match="lobby, settings"):
        validate_and_copy_unity_visual_evidence(
            tmp_path, results, tmp_path / "packaged", "Login, lobby, settings UI"
        )


def test_requested_ui_transition_preserves_repeated_return_destination() -> None:
    goal = (
        "로그인·로비·설정 UI를 현대화하고 "
        "로그인→로비→설정→로비의 핵심 화면 이동을 검증한다."
    )

    assert requested_ui_transition(goal) == ["login", "lobby", "settings", "lobby"]


def test_requested_ui_transition_does_not_join_repeated_audit_copies() -> None:
    copied_goal = "로그인→로비→설정→로비 이동을 검증한다."

    assert requested_ui_transition(copied_goal + "\n" + copied_goal) == [
        "login", "lobby", "settings", "lobby",
    ]


def test_ordered_ui_journey_requires_the_final_return_evidence(tmp_path: Path) -> None:
    results = tmp_path / "results.xml"
    write_results(results)
    write_evidence(tmp_path, [
        ui_scenario("login", width=64, height=32),
        ui_scenario("lobby", width=64, height=32, interaction="login click"),
        ui_scenario("settings", width=32, height=64, interaction="settings click"),
    ])

    with pytest.raises(RuntimeError, match="login -> lobby -> settings -> lobby"):
        validate_and_copy_unity_visual_evidence(
            tmp_path,
            results,
            tmp_path / "packaged",
            "로그인→로비→설정→로비 이동을 검증한다.",
        )


def test_ordered_ui_journey_accepts_distinct_final_return_evidence(tmp_path: Path) -> None:
    results = tmp_path / "results.xml"
    write_results(results)
    write_evidence(tmp_path, [
        ui_scenario("login", width=64, height=32),
        ui_scenario("lobby", width=64, height=32, interaction="login click"),
        ui_scenario("settings", width=32, height=64, interaction="settings click"),
        ui_scenario("lobby-returned", width=32, height=64, interaction="back click"),
    ])

    summary = validate_and_copy_unity_visual_evidence(
        tmp_path,
        results,
        tmp_path / "packaged",
        "로그인→로비→설정→로비 이동을 검증한다.",
    )

    assert summary.scenario_count == 4


def test_ordered_ui_journey_accepts_identical_screenshot_for_real_return_state(
    tmp_path: Path,
) -> None:
    results = tmp_path / "results.xml"
    write_results(results)
    scenarios = [
        ui_scenario("login", width=64, height=32),
        ui_scenario("lobby", width=32, height=64, interaction="login click"),
        ui_scenario("settings", width=32, height=64, interaction="settings click"),
        ui_scenario("lobby-returned", width=32, height=64, interaction="back click"),
    ]
    write_evidence(tmp_path, scenarios)
    lobby = tmp_path / "onebrief-evidence" / str(scenarios[1]["screenshot_path"])
    returned = tmp_path / "onebrief-evidence" / str(scenarios[3]["screenshot_path"])
    returned.write_bytes(lobby.read_bytes())

    summary = validate_and_copy_unity_visual_evidence(
        tmp_path,
        results,
        tmp_path / "packaged",
        "login -> lobby -> settings -> lobby",
    )

    assert summary.scenario_count == 4


def test_identical_screenshot_still_rejects_different_ui_states(tmp_path: Path) -> None:
    results = tmp_path / "results.xml"
    write_results(results)
    scenarios = [
        ui_scenario("login", width=64, height=32),
        ui_scenario("lobby", width=64, height=32, interaction="login click"),
    ]
    write_evidence(tmp_path, scenarios)
    login = tmp_path / "onebrief-evidence" / str(scenarios[0]["screenshot_path"])
    lobby = tmp_path / "onebrief-evidence" / str(scenarios[1]["screenshot_path"])
    lobby.write_bytes(login.read_bytes())

    with pytest.raises(RuntimeError, match="identical screenshot"):
        validate_and_copy_unity_visual_evidence(
            tmp_path, results, tmp_path / "packaged", "login -> lobby",
        )


def test_combined_final_state_names_do_not_replace_distinct_surface_screenshots(
    tmp_path: Path,
) -> None:
    results = tmp_path / "results.xml"
    write_results(results)
    write_evidence(tmp_path, [
        ui_scenario("LoginToLobbyToSettingsDesktop", width=64, height=32),
        ui_scenario("LoginToLobbyToSettingsMobile", width=32, height=64),
    ])

    with pytest.raises(RuntimeError, match="distinct rendered scenario"):
        validate_and_copy_unity_visual_evidence(
            tmp_path,
            results,
            tmp_path / "packaged",
            "Modernize Login, Lobby, and Settings UI for mobile and desktop.",
        )


def test_invalid_manifest_exposes_actionable_schema_failure(tmp_path: Path) -> None:
    results = tmp_path / "results.xml"
    write_results(results)
    evidence = tmp_path / "onebrief-evidence"
    evidence.mkdir()
    (evidence / "runtime-evidence.json").write_text(
        '{"status":"passed","screenshot_captured":true}', encoding="utf-8"
    )

    with pytest.raises(RuntimeError, match="scenarios"):
        validate_and_copy_unity_visual_evidence(
            tmp_path, results, tmp_path / "packaged", "Login UI"
        )
