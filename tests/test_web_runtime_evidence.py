import json

import pytest

from onebrief.web_runtime_evidence import (
    _language_state_issues,
    _wrapper,
    validate_preserved_language_states,
)


def test_mobile_observer_forces_the_exact_css_viewport_and_checks_clipping() -> None:
    document = _wrapper(toggle=True, viewport_width=375)

    assert "width:375px" in document
    assert "max-width:375px" in document
    assert "getBoundingClientRect" in document
    assert "r.right > frame.contentWindow.innerWidth+2" in document
    assert "querySelectorAll('select')" in document
    assert "dispatchEvent(new Event('change'" in document
    assert "for(const option of [...select.options]" in document
    assert "semanticText" in document
    assert 'src="about:blank"' in document
    assert "frame.src='/'" in document
    assert "location.href==='about:blank'" in document
    assert document.index("frame.addEventListener('load'") < document.index("frame.src='/'")


def test_language_observer_rejects_korean_fragments_in_japanese_content() -> None:
    states = [
        {"requested": "ko", "state": {"lang": "ko", "semanticText": "줄패 게임 소개"}},
        {"requested": "en", "state": {"lang": "en", "semanticText": "JULPAE game guide"}},
        {
            "requested": "ja",
            "state": {
                "lang": "ja",
                "semanticText": "48枚의 카드を使用してチーム을組みます",
            },
        },
    ]

    issues = _language_state_issues(states)

    assert "The Japanese rendered content contains a Korean-script fragment." in issues


def test_language_observer_accepts_distinct_matching_language_states() -> None:
    states = [
        {"requested": "ko", "state": {"lang": "ko", "semanticText": "줄패 게임 소개"}},
        {"requested": "en", "state": {"lang": "en", "semanticText": "JULPAE game guide"}},
        {"requested": "ja", "state": {"lang": "ja", "semanticText": "ジュルペ ゲーム紹介"}},
        {"requested": "zh-cn", "state": {"lang": "zh-CN", "semanticText": "朱牌 游戏介绍"}},
        {"requested": "es", "state": {"lang": "es", "semanticText": "Guía del juego JULPAE"}},
    ]

    assert _language_state_issues(states) == []


def _write_observation(root, spanish: str) -> None:
    root.mkdir()
    states = [
        {"requested": "en", "state": {"semanticText": "JULPAE game guide"}},
        {"requested": "es", "state": {"semanticText": spanish}},
    ]
    (root / "observation.json").write_text(
        json.dumps({"mobile": {"states": states}}), encoding="utf-8"
    )


def test_preservation_rejects_same_script_translation_drift(tmp_path) -> None:
    baseline, candidate = tmp_path / "baseline", tmp_path / "candidate"
    _write_observation(baseline, "JULPAE es un juego de cartas")
    _write_observation(candidate, "JULPAE is a card game")

    with pytest.raises(RuntimeError, match="visible locale copy changed: es"):
        validate_preserved_language_states(baseline, candidate)


def test_preservation_accepts_identical_locale_states(tmp_path) -> None:
    baseline, candidate = tmp_path / "baseline", tmp_path / "candidate"
    _write_observation(baseline, "JULPAE es un juego de cartas")
    _write_observation(candidate, "JULPAE es un juego de cartas")

    validate_preserved_language_states(baseline, candidate)
