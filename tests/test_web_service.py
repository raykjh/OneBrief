from fastapi.testclient import TestClient
import pytest

from onebrief.schemas import IntakeRequest, RequirementsAnalysis
from onebrief.web_service import InMemoryWebSessionStore, WebSession, app, get_session_store, validate_approval


def requirements(ready: bool) -> RequirementsAnalysis:
    missing = [] if ready else [{
        "key": "policy",
        "request": "Provide the authoritative policy.",
        "reason": "The agent cannot invent company rules.",
        "acceptable_evidence": ["Policy document"],
    }]
    return RequirementsAnalysis(
        supported=True,
        support_reason="The task is supported.",
        normalized_goal="Create a grounded policy summary.",
        deliverables=["Policy summary"],
        mandatory_information=missing,
        optional_information=[],
        acceptance_criteria=["Every claim is grounded."],
        assumptions=[],
        consolidated_questions=[] if ready else ["Please upload the authoritative policy."],
        ready_for_estimate=ready,
    )


def test_home_serves_the_real_workflow() -> None:
    response = TestClient(app).get("/")
    assert response.status_code == 200
    assert "OneBrief" in response.text
    assert "/api/inspect" in response.text
    assert "Math.min(x.recommended_approval_usd,ceiling)" in response.text


@pytest.mark.parametrize("ready", [False, True])
def test_inspect_returns_questions_or_budget(monkeypatch, ready: bool) -> None:
    store = InMemoryWebSessionStore()

    async def fake_inspect(_intake):
        return requirements(ready)

    monkeypatch.setattr("onebrief.web_service.inspect_requirements", fake_inspect)
    app.dependency_overrides[get_session_store] = lambda: store
    try:
        response = TestClient(app).post(
            "/api/inspect",
            data={"goal": "Create a grounded policy summary.", "budget_limit_usd": "0.5"},
            files={"uploads": ("policy.md", b"Manager approval is required.", "text/markdown")},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    assert (payload["budget"] is not None) is ready
    assert payload["requirements"]["ready_for_estimate"] is ready
    assert store.read(payload["session_id"]).intake.internal_sources[0].name == "policy.md"


def test_approval_cannot_exceed_the_user_limit(monkeypatch) -> None:
    monkeypatch.setenv("ONEBRIEF_WEB_MAX_APPROVAL_USD", "0.10")
    session = WebSession(
        session_id="test",
        created_at="2026-08-05T00:00:00+00:00",
        intake=IntakeRequest(goal="Create a summary.", budget_limit_usd=0.05),
        requirements=requirements(True),
        budget={
            "price_card_version": "test", "price_source_url": "https://example.test",
            "endpoint": "global", "estimated_source_tokens": 0, "estimated_contract_tokens": 100,
            "stages": [], "minimum_cost_usd": 0.01, "recommended_cost_usd": 0.03,
            "maximum_cost_usd": 0.20, "recommended_approval_usd": 0.03,
            "budget_limit_usd": 0.05, "status": "within_budget",
            "estimated_minutes_minimum": 1, "estimated_minutes_recommended": 2,
            "estimated_minutes_maximum": 3, "notes": [],
        },
    )
    validate_approval(session, 0.05)
    with pytest.raises(ValueError):
        validate_approval(session, 0.0501)
