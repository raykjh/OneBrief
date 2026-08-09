from onebrief.web_runtime_evidence import _wrapper


def test_mobile_observer_forces_the_exact_css_viewport_and_checks_clipping() -> None:
    document = _wrapper(toggle=True, viewport_width=375)

    assert "width:375px" in document
    assert "max-width:375px" in document
    assert "getBoundingClientRect" in document
    assert "r.right > frame.contentWindow.innerWidth+2" in document
    assert "current.startsWith('ko')" in document
