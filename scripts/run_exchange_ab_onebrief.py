"""Submit the fixed Exchange A/B goal through OneBrief's real stage-one flow."""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

BENCHMARK = ROOT / "benchmark_cases" / "exchange_release_candidate.json"
RUN_ROOT = ROOT / "benchmarks" / "exchange-release-candidate"
REGISTRY = RUN_ROOT / "registry"


def _require_ok(response, step: str) -> dict[str, object]:
    if response.status_code >= 400:
        raise RuntimeError(f"{step} failed ({response.status_code}): {response.text}")
    return response.json()


def main() -> None:
    case = json.loads(BENCHMARK.read_text(encoding="utf-8"))
    os.environ.update({
        "ONEBRIEF_PROJECTS_ROOT": str(REGISTRY),
        "ONEBRIEF_LOCAL_JOBS_ROOT": str(RUN_ROOT / "local-jobs"),
        "ONEBRIEF_JOB_BUCKET": "onebrief-agent-20260805-jobs",
        "GOOGLE_CLOUD_PROJECT": "onebrief-agent-20260805",
        "ONEBRIEF_REGION": "asia-northeast3",
        "ONEBRIEF_CLOUD_RUN_JOB": "onebrief-worker",
        "ONEBRIEF_WEB_MAX_APPROVAL_USD": "10.00",
    })
    resource = os.environ.get("ONEBRIEF_AGENT_ENGINE_RESOURCE")
    if not resource:
        raise RuntimeError("ONEBRIEF_AGENT_ENGINE_RESOURCE is required")

    from fastapi.testclient import TestClient
    from onebrief.web_service import app, get_session_store

    client = TestClient(app)
    payload = _require_ok(client.post(
        "/api/inspect",
        data={
            "goal": case["goal"],
            "output_target": "existing_project",
            "existing_project_id": "exchange-release-onebrief",
            "desired_output": case.get("desired_output", "production-ready web application"),
            "budget_limit_usd": "10.00",
        },
    ), "stage-one inspection")

    if payload.get("sixsense_pending"):
        payload = _require_ok(client.post(
            f"/api/sessions/{payload['session_id']}/sixsense",
            json={"choices": [], "use_recommended": True},
        ), "SixSense recommended defaults")

    if not payload["requirements"]["ready_for_estimate"]:
        answer = (
            "Use the repository's committed code, tests, docs, and first-party data rules as "
            "authoritative. Use only official or free public APIs, never mock data. Apply the "
            "acceptance criteria and verification commands in the submitted goal. Preserve existing "
            "features; no automated trading, credentials, personalized advice, deployment, or push."
        )
        payload = _require_ok(client.post(
            f"/api/sessions/{payload['session_id']}/reinspect",
            data={"answers": answer},
        ), "mandatory-information supplement")
        if payload.get("sixsense_pending"):
            payload = _require_ok(client.post(
                f"/api/sessions/{payload['session_id']}/sixsense",
                json={"choices": [], "use_recommended": True},
            ), "post-supplement SixSense")

    if not payload.get("budget") or not payload.get("preparation"):
        raise RuntimeError("OneBrief did not reach a budgeted authorization plan")
    preparation = payload["preparation"]
    if not preparation["ready_for_authorization"]:
        raise RuntimeError(f"authorization is blocked: {preparation['blockers']}")
    approval = min(float(payload["budget"]["recommended_approval_usd"]), 10.0)
    toolpack_sha = preparation["permission_manifest"].get("toolpack_sha256")
    run_payload = {
        "approved_usd": approval,
        "authorization_sha256": preparation["authorization_sha256"],
        "toolpack_sha256": toolpack_sha,
    }
    submitted = _require_ok(client.post(
        f"/api/sessions/{payload['session_id']}/run",
        json=run_payload,
    ), "Agent Platform execution dispatch")
    link = get_session_store().read_execution(payload["session_id"])
    receipt = {
        "schema_version": "onebrief-ab-submission-v1",
        "variant": "onebrief_convergence",
        "submitted_at": datetime.now(UTC).isoformat(),
        "case": case,
        "session_id": payload["session_id"],
        "approved_usd": approval,
        "budget": payload["budget"],
        "preparation": preparation,
        "dispatch": submitted,
        "execution": link.model_dump(mode="json"),
    }
    output = RUN_ROOT / "onebrief-submission.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        previous = json.loads(output.read_text(encoding="utf-8"))
        previous_session = str(previous.get("session_id", "unknown"))
        history = RUN_ROOT / "attempt-history"
        history.mkdir(parents=True, exist_ok=True)
        output.replace(history / f"onebrief-submission-{previous_session}.json")
    output.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(receipt, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
