from onebrief.web_runtime_evidence import _language_state_issues, _wrapper


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
