from onebrief.guarded_gemini import BudgetedGeminiClient


def test_only_transient_capacity_errors_are_retried(monkeypatch) -> None:
    client = object.__new__(BudgetedGeminiClient)
    monkeypatch.setattr("onebrief.guarded_gemini.time.sleep", lambda _seconds: None)
    calls = 0

    def transient_then_pass():
        nonlocal calls
        calls += 1
        if calls < 3:
            raise RuntimeError("429 RESOURCE_EXHAUSTED")
        return "ok"

    assert client._invoke_with_transient_retry(transient_then_pass) == "ok"
    assert calls == 3

    permanent_calls = 0

    def permanent():
        nonlocal permanent_calls
        permanent_calls += 1
        raise ValueError("invalid schema")

    try:
        client._invoke_with_transient_retry(permanent)
    except ValueError:
        pass
    else:
        raise AssertionError("permanent errors must not be retried")
    assert permanent_calls == 1
