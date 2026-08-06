from types import SimpleNamespace
from fastapi.testclient import TestClient
import pytest

from onebrief.schemas import IntakeRequest, RequirementsAnalysis
from onebrief.web_service import (
    ExecutionLink, InMemoryWebSessionStore, WebSession, app, get_session_store,
    validate_approval,
)


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
    assert 'form.delete("uploads")' in response.text
    assert 'this.disabled=true;q("#status").textContent=""' in response.text
    assert "/api/sessions/" in response.text and "/graph" in response.text
    assert "에이전트 실행 흐름" in response.text


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

def test_graph_endpoint_returns_selected_team_and_node_history(monkeypatch) -> None:
    store = InMemoryWebSessionStore()
    store.save_execution(ExecutionLink(
        session_id="session-1",
        job_uri="gs://test/jobs/job-1",
        operation_name="operations/1",
        created_at="2026-08-06T00:00:00+00:00",
    ))

    class FakeRepository:
        def read_job(self):
            return SimpleNamespace(
                job_id="job-1",
                status=SimpleNamespace(value="running"),
                current_stage="evidence_analysis",
                message="Analysis is running.",
            )

        def read_json(self, relative):
            if str(relative).endswith("execution_graph.json"):
                return {"nodes": [{
                    "node_id": "evidence_analysis",
                    "stage": "evidence_analysis",
                    "agent_type": "analyst",
                    "owner_instance_id": "analyst-01",
                    "depends_on": [],
                    "activation_reason": "Evidence must be grounded.",
                }]}
            return {"nodes": {"evidence_analysis": {
                "status": "running", "attempt": 1, "message": "Reading sources.",
                "output_paths": [],
            }}}

    monkeypatch.setattr("onebrief.web_service.GCSJobStore", lambda _uri: FakeRepository())
    app.dependency_overrides[get_session_store] = lambda: store
    try:
        response = TestClient(app).get("/api/sessions/session-1/graph")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    assert payload["nodes"][0]["agent_type"] == "analyst"
    assert payload["nodes"][0]["status"] == "running"
    assert payload["nodes"][0]["attempt"] == 1


def test_graph_endpoint_reports_team_planning_before_artifacts_exist(monkeypatch) -> None:
    store = InMemoryWebSessionStore()
    store.save_execution(ExecutionLink(
        session_id="session-2",
        job_uri="gs://test/jobs/job-2",
        operation_name="operations/2",
        created_at="2026-08-06T00:00:00+00:00",
    ))

    class FakeRepository:
        def read_job(self):
            return SimpleNamespace(
                job_id="job-2", status=SimpleNamespace(value="queued"),
                current_stage="queued", message="Queued.",
            )

        def read_json(self, _relative):
            raise FileNotFoundError("not ready")

    monkeypatch.setattr("onebrief.web_service.GCSJobStore", lambda _uri: FakeRepository())
    app.dependency_overrides[get_session_store] = lambda: store
    try:
        response = TestClient(app).get("/api/sessions/session-2/graph")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["nodes"] == []
    assert response.json()["job_status"] == "queued"
