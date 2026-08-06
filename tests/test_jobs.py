import hashlib
import json
from pathlib import Path

import pytest

from onebrief.execution_schemas import (
    AnalysisPackage,
    DraftArtifact,
    VerificationReport,
)
from onebrief.jobs import JobStatus, JobStore, create_job, run_job, start_background_job
from onebrief.producer import estimate_budget
from onebrief.schemas import IntakeRequest, InternalSource, RequirementsAnalysis, SourcePriority

from team_plan_support import minimal_team_plan


class FakeGateway:
    def __init__(self, outputs: list[object]):
        self.outputs = outputs
        self.calls: list[str] = []

    def generate_json(self, *, stage: str, schema: type, **kwargs: object):
        self.calls.append(stage)
        if stage == "team_planning":
            request = json.loads(str(kwargs["contents"]))
            return minimal_team_plan(request["project_id"])
        value = self.outputs.pop(0)
        assert isinstance(value, schema)
        return value


def _source() -> InternalSource:
    raw = b"Remote work requires manager approval."
    return InternalSource(
        name="policy.md",
        priority=SourcePriority.MANDATORY,
        requirement_keys=["remote_policy"],
        content=raw.decode(),
        size_bytes=len(raw),
        sha256=hashlib.sha256(raw).hexdigest(),
    )


def _requirements() -> RequirementsAnalysis:
    return RequirementsAnalysis(
        supported=True,
        support_reason="The supplied policy supports the requested guide.",
        normalized_goal="Create a grounded remote-work guide.",
        deliverables=["Remote-work guide"],
        mandatory_information=[],
        optional_information=[],
        acceptance_criteria=["Every rule cites supplied evidence."],
        assumptions=[],
        consolidated_questions=[],
        ready_for_estimate=True,
    )


def _gateway() -> FakeGateway:
    return FakeGateway(
        [
            AnalysisPackage(
                objective="Create a grounded remote-work guide.",
                findings=[
                    {
                        "finding_id": "F01",
                        "source_name": "policy.md",
                        "evidence": "Manager approval is required.",
                        "implication": "The guide must describe the approval requirement.",
                    }
                ],
                recommended_structure=["Policy", "Procedure"],
                constraints=["Use only supplied evidence."],
                risks=[],
            ),
            DraftArtifact(
                title="Remote Work Guide",
                body_markdown="# Remote Work Guide\n\nManager approval is required. [F01]",
                cited_finding_ids=["F01"],
                drafting_decisions=["Used the mandatory policy."],
            ),
            VerificationReport(
                verdict="PASS",
                criterion_checks=[
                    {
                        "criterion": "Every rule cites supplied evidence.",
                        "passed": True,
                        "evidence": "The only rule cites F01.",
                    }
                ],
                blocking_issues=[],
                revision_instructions=[],
                missing_information=[],
            ),
        ]
    )


def _create(tmp_path: Path) -> Path:
    source = _source()
    intake = IntakeRequest(goal="Create a remote-work guide.")
    estimate = estimate_budget(
        intake.model_copy(update={"internal_sources": [source]}),
        _requirements(),
    )
    return create_job(
        jobs_dir=tmp_path / "jobs",
        intake=intake,
        requirements=_requirements(),
        sources=[source],
        estimate=estimate,
        approved_usd=estimate.recommended_approval_usd,
    )


def test_job_runs_to_immutable_result_package(tmp_path: Path) -> None:
    job_dir = _create(tmp_path)
    queued = JobStore(job_dir).read()
    assert queued.status == JobStatus.QUEUED
    assert (job_dir / "inputs" / "sources.json").exists()
    assert (job_dir / "inputs" / "input_manifest.json").exists()
    assert (job_dir / "run" / "approval.json").exists()

    gateway = _gateway()
    completed = run_job(job_dir, gateway=gateway)

    assert completed.status == JobStatus.COMPLETE
    assert gateway.calls == [
        "team_planning",
        "evidence_analysis",
        "long_form_draft",
        "independent_verification_r0",
    ]
    assert completed.result_package is not None
    package = job_dir / completed.result_package
    assert (package / "artifacts" / "final.md").exists()
    assert (package / "artifacts" / "final_verification.json").exists()
    assert (package / "audit" / "cost_ledger.json").exists()
    assert (package / "evidence" / "source_manifest.json").exists()
    assert not (package / "inputs" / "sources.json").exists()
    team_plans = list((package / "artifacts" / "workspace" / "projects").glob(
        "*/02_plan_and_teams/team_plan.json"
    ))
    assert len(team_plans) == 1
    manifest_path = package / "package_manifest.json"
    assert hashlib.sha256(manifest_path.read_bytes()).hexdigest() == completed.result_manifest_sha256
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    paths = {item["path"] for item in manifest["files"]}
    assert "artifacts/final.md" in paths
    assert "audit/cost_ledger.json" in paths


def test_completed_job_cannot_be_claimed_twice(tmp_path: Path) -> None:
    job_dir = _create(tmp_path)
    run_job(job_dir, gateway=_gateway())
    with pytest.raises(RuntimeError, match="cannot be claimed"):
        run_job(job_dir, gateway=_gateway())




def test_changed_input_is_blocked_before_any_agent_call(tmp_path: Path) -> None:
    job_dir = _create(tmp_path)
    (job_dir / "inputs" / "intake.json").write_text("{}\n", encoding="utf-8")
    gateway = _gateway()

    failed = run_job(job_dir, gateway=gateway)

    assert failed.status == JobStatus.FAILED
    assert "changed after approval" in failed.message
    assert gateway.calls == []


def test_background_start_returns_without_running_pipeline(tmp_path: Path, monkeypatch) -> None:
    job_dir = _create(tmp_path)
    captured: dict[str, object] = {}

    class Process:
        pid = 4321

    def fake_popen(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return Process()

    monkeypatch.setattr("onebrief.jobs.subprocess.Popen", fake_popen)
    started = start_background_job(job_dir)

    assert started.status == JobStatus.QUEUED
    assert started.worker_pid == 4321
    assert captured["command"][-2:] == ["job-worker", str(job_dir.resolve())]
    assert (job_dir / "logs" / "worker.stdout.log").exists()
    assert (job_dir / "logs" / "worker.stderr.log").exists()
