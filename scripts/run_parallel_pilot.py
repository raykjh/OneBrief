"""Submit and observe the three frozen OneBrief pilot lanes in parallel."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from onebrief.cloud_jobs import GCSJobStore  # noqa: E402
from onebrief.agent_platform_client import (  # noqa: E402
    dispatch_approved_job_via_agent_platform,
)
from onebrief.evaluation import ExecutionEvaluation  # noqa: E402
from onebrief.jobs import JobStatus  # noqa: E402
from onebrief.parallel_campaign import (  # noqa: E402
    FailureObservation,
    LaneEvidence,
    LaneStatus,
    ParallelCampaignStore,
    SafeApplyOutcome,
)

PROJECT = "onebrief-agent-20260805"
REGION = "asia-northeast3"
BUCKET = "onebrief-agent-20260805-jobs"
WORKER = "onebrief-worker"
AGENT_ENGINE = (
    "projects/1077683695702/locations/asia-northeast3/"
    "reasoningEngines/665354068385857536"
)
TERMINAL = {
    JobStatus.COMPLETE,
    JobStatus.PARTIAL,
    JobStatus.NEEDS_INFORMATION,
    JobStatus.NEEDS_BUDGET,
    JobStatus.NEEDS_AUTHORIZATION,
    JobStatus.FAILED,
}
RESERVED_INTAKE_USD = 0.20


def _require_ok(response, step: str) -> dict[str, object]:
    if response.status_code >= 400:
        raise RuntimeError(f"{step} failed ({response.status_code}): {response.text}")
    return response.json()


def _lane_inputs(lane_id: str) -> tuple[str, str, str | None, list[Path], str]:
    if lane_id == "exchange-existing":
        return (
            "existing_project",
            "production-ready bilingual web application",
            "exchange-release-onebrief",
            [],
            "Use the committed repository, tests, documentation, and first-party data rules as "
            "authoritative. Preserve existing behavior. Use only official or permitted free APIs "
            "and never mock data. Do not trade, deploy, push, or access credentials.",
        )
    if lane_id == "public-data-greenfield":
        return (
            "web_app",
            "self-contained responsive web application with tests and run instructions",
            None,
            [],
            "Choose official Korean public weather, air-quality, and disaster-alert sources that "
            "need no paid account when possible. Use standard accessible responsive web patterns. "
            "Missing credentials must produce a documented degraded mode, not mock data.",
        )
    if lane_id == "operations-workbook":
        return (
            "spreadsheet",
            "one verified .xlsx workbook and a concise usage note",
            None,
            [
                ROOT / "benchmark_inputs" / "operations_requests.csv",
                ROOT / "benchmark_inputs" / "operations_rules.md",
            ],
            "The uploaded CSV and rules are complete and authoritative. Use 2026-08-10 as the "
            "documented as-of date. Apply only supplied categorical rules; do not invent scores.",
        )
    raise KeyError(f"unknown pilot lane: {lane_id}")


def _configure_lane(campaign_root: Path, lane_id: str, budget_cap: float) -> Path:
    runtime = campaign_root / "runtime" / lane_id
    runtime.mkdir(parents=True, exist_ok=True)
    os.environ.update(
        {
            "PYTHONUTF8": "1",
            "GOOGLE_GENAI_USE_VERTEXAI": "TRUE",
            "GOOGLE_CLOUD_PROJECT": PROJECT,
            "GOOGLE_CLOUD_LOCATION": "global",
            "ONEBRIEF_AGENT_ENGINE_RESOURCE": AGENT_ENGINE,
            "ONEBRIEF_LOCAL_JOBS_ROOT": str(runtime / "local-jobs"),
            "ONEBRIEF_JOB_BUCKET": BUCKET,
            "ONEBRIEF_REGION": REGION,
            "ONEBRIEF_CLOUD_RUN_JOB": WORKER,
            "ONEBRIEF_WEB_MAX_APPROVAL_USD": f"{budget_cap - RESERVED_INTAKE_USD:.2f}",
        }
    )
    os.environ["ONEBRIEF_PROJECTS_ROOT"] = str(
        ROOT / "benchmarks" / "exchange-release-candidate" / "registry"
        if lane_id == "exchange-existing"
        else runtime / "registry"
    )
    return runtime


def submit_lane(campaign_root: Path, lane_id: str) -> dict[str, object]:
    store = ParallelCampaignStore(campaign_root)
    manifest = store.manifest()
    spec = next(item for item in manifest.lanes if item.lane_id == lane_id)
    runtime = _configure_lane(campaign_root, lane_id, spec.max_budget_usd)
    case = json.loads((ROOT / spec.case_path).read_text(encoding="utf-8"))
    output_target, desired_output, project_id, uploads, supplement = _lane_inputs(lane_id)

    from fastapi.testclient import TestClient
    from onebrief.web_service import app, get_session_store

    files = [
        (
            "uploads",
            (
                path.name,
                path.read_bytes(),
                "text/csv" if path.suffix == ".csv" else "text/markdown",
            ),
        )
        for path in uploads
    ]
    client = TestClient(app)
    data: dict[str, str] = {
        "goal": case["goal"],
        "output_target": output_target,
        "desired_output": desired_output,
        "budget_limit_usd": f"{spec.max_budget_usd - RESERVED_INTAKE_USD:.2f}",
    }
    if project_id:
        data["existing_project_id"] = project_id
    payload = _require_ok(client.post("/api/inspect", data=data, files=files), "inspection")

    for _ in range(6):
        if payload.get("sixsense_pending"):
            payload = _require_ok(
                client.post(
                    f"/api/sessions/{payload['session_id']}/sixsense",
                    json={"choices": [], "use_recommended": True},
                ),
                "SixSense defaults",
            )
            continue
        if payload["requirements"]["ready_for_estimate"]:
            break
        payload = _require_ok(
            client.post(
                f"/api/sessions/{payload['session_id']}/reinspect",
                data={"answers": supplement},
            ),
            "mandatory information supplement",
        )
    if payload.get("sixsense_pending") or not payload["requirements"]["ready_for_estimate"]:
        diagnostic = runtime / "requirements-diagnostic.json"
        diagnostic.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        questions = payload.get("requirements", {}).get("consolidated_questions", [])
        raise RuntimeError(
            "lane did not reach an estimable completion contract: "
            + "; ".join(str(item) for item in questions)
        )
    budget = payload.get("budget")
    preparation = payload.get("preparation")
    if not budget or not preparation or not preparation["ready_for_authorization"]:
        raise RuntimeError(f"lane authorization is blocked: {preparation}")
    execution_cap = spec.max_budget_usd - RESERVED_INTAKE_USD
    minimum = float(budget["minimum_cost_usd"])
    recommended = float(budget["recommended_approval_usd"])
    if minimum > execution_cap:
        raise RuntimeError(
            f"minimum executable budget {minimum:.6f} exceeds lane cap {execution_cap:.6f}"
        )
    approval = min(recommended, execution_cap)
    submitted = _require_ok(
        client.post(
            f"/api/sessions/{payload['session_id']}/run",
            json={
                "approved_usd": approval,
                "authorization_sha256": preparation["authorization_sha256"],
                "toolpack_sha256": preparation["permission_manifest"].get("toolpack_sha256"),
            },
        ),
        "Agent Platform dispatch",
    )
    link = get_session_store().read_execution(payload["session_id"])
    receipt = {
        "schema_version": "onebrief-parallel-lane-submission-v1",
        "campaign_id": manifest.campaign_id,
        "lane_id": lane_id,
        "baseline_commit_sha": manifest.baseline.commit_sha,
        "baseline_image_digest": manifest.baseline.image_digest,
        "case_sha256": spec.case_sha256,
        "approved_execution_usd": approval,
        "reserved_intake_usd": RESERVED_INTAKE_USD,
        "lane_total_cap_usd": spec.max_budget_usd,
        "session_id": payload["session_id"],
        "dispatch": submitted,
        "execution": link.model_dump(mode="json"),
    }
    target = runtime / "submission.json"
    target.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return receipt


def dispatch_uploaded(campaign_root: Path, lane_id: str, job_uri: str) -> dict[str, object]:
    """Retry only Agent Platform routing for an already approved immutable job."""
    store = ParallelCampaignStore(campaign_root)
    manifest = store.manifest()
    spec = next(item for item in manifest.lanes if item.lane_id == lane_id)
    remote = GCSJobStore(job_uri)
    record = remote.read_job()
    if record.status != JobStatus.QUEUED or record.attempts != 0:
        raise RuntimeError(
            f"only an unclaimed queued job can be redispatched, not {record.status.value} "
            f"attempt {record.attempts}"
        )
    approval = remote.read_json("run/approval.json")
    approved_usd = int(approval["approved_usd_micros"]) / 1_000_000
    if approved_usd > spec.max_budget_usd - RESERVED_INTAKE_USD:
        raise PermissionError("uploaded job approval exceeds the frozen lane budget")
    dispatch = dispatch_approved_job_via_agent_platform(
        resource_name=AGENT_ENGINE,
        job_uri=job_uri,
        user_id=f"onebrief-pilot-retry-{lane_id}",
        project=PROJECT,
        location=REGION,
    )
    receipt = {
        "schema_version": "onebrief-parallel-lane-submission-v1",
        "campaign_id": manifest.campaign_id,
        "lane_id": lane_id,
        "baseline_commit_sha": manifest.baseline.commit_sha,
        "baseline_image_digest": manifest.baseline.image_digest,
        "case_sha256": spec.case_sha256,
        "approved_execution_usd": approved_usd,
        "reserved_intake_usd": RESERVED_INTAKE_USD,
        "lane_total_cap_usd": spec.max_budget_usd,
        "session_id": f"retry-{lane_id}",
        "dispatch": {"status": "queued", "event_count": dispatch.event_count},
        "execution": {
            "session_id": f"retry-{lane_id}",
            "job_uri": job_uri,
            "operation_name": dispatch.cloud_run_operation,
            "agent_engine_resource": dispatch.agent_engine_resource,
            "agent_engine_session": dispatch.agent_engine_session,
        },
    }
    runtime = campaign_root / "runtime" / lane_id
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / "submission.json").write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return receipt


def resume_diagnostic(campaign_root: Path, lane_id: str) -> dict[str, object]:
    """Resume a persisted stage-one session without another Gemini inspection."""
    store = ParallelCampaignStore(campaign_root)
    manifest = store.manifest()
    spec = next(item for item in manifest.lanes if item.lane_id == lane_id)
    runtime = _configure_lane(campaign_root, lane_id, spec.max_budget_usd)
    diagnostic_path = runtime / "requirements-diagnostic.json"
    diagnostic = json.loads(diagnostic_path.read_text(encoding="utf-8"))

    from fastapi.testclient import TestClient
    from onebrief.preparation import build_preparation_plan
    from onebrief.producer import estimate_budget
    from onebrief.requirements_gate import apply_requirements_gate
    from onebrief.web_service import app, get_session_store

    session_store = get_session_store()
    previous = session_store.read(str(diagnostic["session_id"]))
    requirements = apply_requirements_gate(
        previous.intake,
        previous.requirements,
        previous.intake.internal_sources,
    )
    if not requirements.ready_for_estimate:
        raise RuntimeError(
            "updated deterministic gate still requires information: "
            + "; ".join(requirements.consolidated_questions)
        )
    budget = estimate_budget(previous.intake, requirements)
    preparation = build_preparation_plan(previous.intake, requirements, budget)
    if preparation is None or not preparation.ready_for_authorization:
        raise RuntimeError("the resumed session is not ready for authorization")
    resumed = previous.model_copy(update={
        "session_id": str(uuid4()),
        "requirements": requirements,
        "budget": budget,
        "preparation": preparation,
        "parent_session_id": previous.session_id,
        "amendment_kind": "deterministic_gate_recovery",
        "amendment_reason": "Removed a score-conversion request forbidden by authoritative rules.",
    })
    session_store.create(resumed)
    execution_cap = spec.max_budget_usd - RESERVED_INTAKE_USD
    if budget.minimum_cost_usd > execution_cap:
        raise RuntimeError("resumed minimum budget exceeds the frozen lane cap")
    approval = min(budget.recommended_approval_usd, execution_cap)
    response = TestClient(app).post(
        f"/api/sessions/{resumed.session_id}/run",
        json={
            "approved_usd": approval,
            "authorization_sha256": preparation.authorization_sha256,
            "toolpack_sha256": preparation.permission_manifest.toolpack_sha256,
        },
    )
    submitted = _require_ok(response, "resumed Agent Platform dispatch")
    link = session_store.read_execution(resumed.session_id)
    receipt = {
        "schema_version": "onebrief-parallel-lane-submission-v1",
        "campaign_id": manifest.campaign_id,
        "lane_id": lane_id,
        "baseline_commit_sha": manifest.baseline.commit_sha,
        "baseline_image_digest": manifest.baseline.image_digest,
        "case_sha256": spec.case_sha256,
        "approved_execution_usd": approval,
        "reserved_intake_usd": RESERVED_INTAKE_USD,
        "lane_total_cap_usd": spec.max_budget_usd,
        "session_id": resumed.session_id,
        "parent_session_id": previous.session_id,
        "dispatch": submitted,
        "execution": link.model_dump(mode="json"),
    }
    (runtime / "submission.json").write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return receipt


def submit_all(campaign_root: Path) -> None:
    manifest = ParallelCampaignStore(campaign_root).manifest()
    processes: list[tuple[str, subprocess.Popen[str], object]] = []
    for lane in manifest.lanes:
        runtime = campaign_root / "runtime" / lane.lane_id
        runtime.mkdir(parents=True, exist_ok=True)
        log = (runtime / "submission.log").open("w", encoding="utf-8")
        process = subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "submit",
                str(campaign_root),
                lane.lane_id,
            ],
            cwd=ROOT,
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
            env={**os.environ, "PYTHONUTF8": "1"},
        )
        processes.append((lane.lane_id, process, log))
    failed: list[str] = []
    for lane_id, process, log in processes:
        code = process.wait()
        log.close()
        print(f"{lane_id}: submission exit {code}", flush=True)
        if code:
            failed.append(lane_id)
    if failed:
        raise RuntimeError(f"parallel submission failed: {', '.join(failed)}")


def _existing_job_uris(campaign_root: Path) -> set[str]:
    uris: set[str] = set()
    for path in (campaign_root / "lanes").glob("*/evidence/*.json"):
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("job_uri"):
            uris.add(str(payload["job_uri"]))
    return uris


def monitor(campaign_root: Path, timeout_seconds: int) -> None:
    store = ParallelCampaignStore(campaign_root)
    manifest = store.manifest()
    submissions: dict[str, dict[str, object]] = {}
    for lane in manifest.lanes:
        path = campaign_root / "runtime" / lane.lane_id / "submission.json"
        submissions[lane.lane_id] = json.loads(path.read_text(encoding="utf-8"))
    deadline = time.monotonic() + timeout_seconds
    recorded = _existing_job_uris(campaign_root)
    pending = set(submissions)
    while pending and time.monotonic() < deadline:
        for lane_id in sorted(list(pending)):
            submission = submissions[lane_id]
            job_uri = str(submission["execution"]["job_uri"])
            remote = GCSJobStore(job_uri)
            record = remote.read_job()
            print(f"{lane_id}: {record.status.value} / {record.current_stage}", flush=True)
            if record.status not in TERMINAL:
                continue
            evaluation = ExecutionEvaluation.model_validate(
                remote.read_json("work/evaluation_metrics.json")
            )
            completion = record.status == JobStatus.COMPLETE and evaluation.completion_proven
            failures = [] if completion else [
                FailureObservation.from_error(
                    error_class=record.status.value,
                    stage=record.current_stage,
                    summary=record.message or "lane did not prove completion",
                )
            ]
            if job_uri not in recorded:
                spec = next(item for item in manifest.lanes if item.lane_id == lane_id)
                store.record_evidence(
                    LaneEvidence(
                        campaign_id=manifest.campaign_id,
                        lane_id=lane_id,
                        baseline_commit_sha=manifest.baseline.commit_sha,
                        case_sha256=spec.case_sha256,
                        status=LaneStatus.COMPLETE if completion else LaneStatus.FAILED,
                        verified_complete=completion,
                        job_uri=job_uri,
                        result_manifest_sha256=record.result_manifest_sha256,
                        actual_cost_usd=evaluation.actual_cost_usd,
                        elapsed_seconds=evaluation.elapsed_seconds,
                        model_calls=evaluation.model_calls,
                        denied_calls=evaluation.denied_calls,
                        user_questions=evaluation.user_supplement_count,
                        failures=failures,
                        safe_apply_outcome=SafeApplyOutcome.NOT_APPLICABLE,
                        observed_source_commit_sha=manifest.baseline.commit_sha,
                    )
                )
                recorded.add(job_uri)
            pending.remove(lane_id)
        if pending:
            time.sleep(20)
    if pending:
        raise TimeoutError(f"lanes still running after timeout: {', '.join(sorted(pending))}")
    print(store.integrate().model_dump_json(indent=2), flush=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    submit = subparsers.add_parser("submit")
    submit.add_argument("campaign_root", type=Path)
    submit.add_argument("lane_id", nargs="?")
    observe = subparsers.add_parser("monitor")
    observe.add_argument("campaign_root", type=Path)
    observe.add_argument("--timeout", type=int, default=3600)
    redispatch = subparsers.add_parser("dispatch-uploaded")
    redispatch.add_argument("campaign_root", type=Path)
    redispatch.add_argument("lane_id")
    redispatch.add_argument("job_uri")
    resume = subparsers.add_parser("resume-diagnostic")
    resume.add_argument("campaign_root", type=Path)
    resume.add_argument("lane_id")
    args = parser.parse_args()
    campaign_root = args.campaign_root.resolve()
    if args.command == "submit":
        if args.lane_id:
            print(json.dumps(submit_lane(campaign_root, args.lane_id), ensure_ascii=False, indent=2))
        else:
            submit_all(campaign_root)
    elif args.command == "monitor":
        monitor(campaign_root, args.timeout)
    elif args.command == "dispatch-uploaded":
        print(json.dumps(
            dispatch_uploaded(campaign_root, args.lane_id, args.job_uri),
            ensure_ascii=False,
            indent=2,
        ))
    else:
        print(json.dumps(
            resume_diagnostic(campaign_root, args.lane_id),
            ensure_ascii=False,
            indent=2,
        ))


if __name__ == "__main__":
    main()
