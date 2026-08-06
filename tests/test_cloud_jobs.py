import hashlib
import json
import shutil
from pathlib import Path

import pytest

from onebrief.cloud_jobs import (
    execute_cloud_run_job,
    parse_gcs_job_uri,
    run_cloud_worker,
)
from onebrief.execution_schemas import AnalysisPackage, DraftArtifact, VerificationReport
from onebrief.jobs import JobRecord, JobStatus, JobStore, create_job
from onebrief.producer import estimate_budget
from onebrief.schemas import IntakeRequest, InternalSource, RequirementsAnalysis, SourcePriority

from team_plan_support import minimal_team_plan


class FakeGateway:
    def __init__(self):
        self.outputs = [
            AnalysisPackage(
                objective="Create a grounded guide.",
                findings=[
                    {
                        "finding_id": "F01",
                        "source_name": "policy.md",
                        "evidence": "Manager approval is required.",
                        "implication": "State the approval rule.",
                    }
                ],
                recommended_structure=["Policy", "Procedure"],
                constraints=["Use only supplied evidence."],
                risks=[],
            ),
            DraftArtifact(
                title="Remote Work Guide",
                body_markdown=(
                    "# Remote Work Guide\n\nManager approval is required before remote work begins. "
                    "[F01]\n\nEmployees must follow the established internal approval process and "
                    "wait for the manager's decision. This guide adds no rule beyond the supplied "
                    "policy and keeps the decision with the authorized manager. [F01]"
                ),
                cited_finding_ids=["F01"],
                drafting_decisions=["Used only the supplied policy."],
            ),
            VerificationReport(
                verdict="PASS",
                criterion_checks=[
                    {
                        "criterion": "Every rule cites supplied evidence.",
                        "passed": True,
                        "evidence": "The approval rule cites F01.",
                    }
                ],
                blocking_issues=[],
                revision_instructions=[],
                missing_information=[],
            ),
        ]
        self.calls: list[str] = []

    def generate_json(self, *, stage: str, schema: type, **kwargs: object):
        self.calls.append(stage)
        if stage == "team_planning":
            request = json.loads(str(kwargs["contents"]))
            return minimal_team_plan(request["project_id"])
        value = self.outputs.pop(0)
        assert isinstance(value, schema)
        return value


def _create_job(tmp_path: Path) -> Path:
    raw = b"Remote work requires manager approval."
    source = InternalSource(
        name="policy.md",
        priority=SourcePriority.MANDATORY,
        requirement_keys=["remote_policy"],
        content=raw.decode(),
        size_bytes=len(raw),
        sha256=hashlib.sha256(raw).hexdigest(),
    )
    intake = IntakeRequest(goal="Create a grounded remote-work guide.")
    requirements = RequirementsAnalysis(
        supported=True,
        support_reason="The required policy was supplied.",
        normalized_goal="Create a grounded remote-work guide.",
        deliverables=["Remote-work guide"],
        mandatory_information=[],
        optional_information=[],
        acceptance_criteria=["Every rule cites supplied evidence."],
        assumptions=[],
        consolidated_questions=[],
        ready_for_estimate=True,
    )
    estimate = estimate_budget(
        intake.model_copy(update={"internal_sources": [source]}),
        requirements,
    )
    return create_job(
        jobs_dir=tmp_path / "jobs",
        intake=intake,
        requirements=requirements,
        sources=[source],
        estimate=estimate,
        approved_usd=estimate.recommended_approval_usd,
    )


class LocalCloudRepository:
    def __init__(self, remote_job: Path):
        self.remote_job = remote_job
        self.claimed = False
        self.completed: JobRecord | None = None
        self.failure: str | None = None

    def acquire_claim(self) -> None:
        if self.claimed:
            raise RuntimeError("already claimed")
        self.claimed = True

    def download_job(self, destination: Path) -> Path:
        shutil.copytree(self.remote_job, destination)
        return destination

    def upload_outputs(self, job_dir: Path) -> None:
        for name in ("job.json", "run", "work", "packages"):
            source = job_dir / name
            target = self.remote_job / name
            if source.is_dir():
                shutil.copytree(source, target, dirs_exist_ok=True)
            elif source.exists():
                shutil.copy2(source, target)

    def write_completion(self, record: JobRecord) -> None:
        self.completed = record

    def write_failure(self, message: str) -> None:
        self.failure = message


def test_cloud_worker_round_trip_publishes_remote_result(tmp_path: Path) -> None:
    local_job = _create_job(tmp_path / "source")
    remote_job = tmp_path / "remote" / local_job.name
    remote_job.parent.mkdir()
    shutil.copytree(local_job, remote_job)
    repository = LocalCloudRepository(remote_job)
    gateway = FakeGateway()

    record = run_cloud_worker(
        "gs://onebrief-test/jobs/example",
        repository=repository,
        gateway=gateway,
    )

    assert record.status == JobStatus.COMPLETE
    assert repository.claimed
    assert repository.completed == record
    assert repository.failure is None
    assert JobStore(remote_job).read().status == JobStatus.COMPLETE
    assert (remote_job / record.result_package / "package_manifest.json").exists()
    assert gateway.calls == [
        "team_planning",
        "evidence_analysis",
        "long_form_draft",
        "independent_verification_r0",
    ]


def test_execute_cloud_run_job_passes_only_the_job_uri_override() -> None:
    captured = {}

    class OperationValue:
        name = "operations/example"

    class Operation:
        operation = OperationValue()

    class Client:
        def run_job(self, *, request):
            captured["request"] = request
            return Operation()

    receipt = execute_cloud_run_job(
        "gs://onebrief-test/jobs/123",
        project="onebrief-project",
        region="asia-northeast3",
        cloud_run_job="onebrief-worker",
        client=Client(),
    )

    request = captured["request"]
    assert request.name == "projects/onebrief-project/locations/asia-northeast3/jobs/onebrief-worker"
    environment = request.overrides.container_overrides[0].env
    assert [(item.name, item.value) for item in environment] == [
        ("ONEBRIEF_JOB_URI", "gs://onebrief-test/jobs/123")
    ]
    assert receipt.operation_name == "operations/example"


@pytest.mark.parametrize(
    "uri",
    ["https://bucket/jobs/1", "gs://bucket", "gs:///jobs/1", "gs://bucket/jobs/../other"],
)
def test_parse_gcs_job_uri_rejects_unsafe_values(uri: str) -> None:
    with pytest.raises(ValueError):
        parse_gcs_job_uri(uri)
