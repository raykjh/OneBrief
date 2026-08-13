import hashlib
import json
import shutil
from pathlib import Path

import pytest
from google.api_core.exceptions import NotFound

from onebrief.cloud_jobs import (
    GCSJobStore,
    RuntimeCapabilityHandoff,
    execute_cloud_run_job,
    parse_gcs_job_uri,
    run_local_capability_worker,
    run_cloud_worker,
    verify_runtime_handoff,
)
from onebrief.dynamic_role_agents import GovernanceDecision
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
            GovernanceDecision(
                verdict="PASS",
                rationale="Verified result satisfies the contract.",
            ),        ]
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
        self.handoffs: list[RuntimeCapabilityHandoff] = []
        self.upload_count = 0

    def acquire_claim(self) -> None:
        if self.claimed:
            raise RuntimeError("already claimed")
        self.claimed = True

    def download_job(self, destination: Path) -> Path:
        shutil.copytree(self.remote_job, destination)
        return destination

    def upload_outputs(self, job_dir: Path) -> None:
        self.upload_count += 1
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

    def write_runtime_handoff(self, handoff: RuntimeCapabilityHandoff) -> None:
        self.handoffs.append(handoff)

    def read_runtime_handoff(self) -> RuntimeCapabilityHandoff:
        if not self.handoffs:
            raise FileNotFoundError("no runtime handoff")
        return self.handoffs[-1]


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
        "final_approval",
    ]


def test_managed_worker_hands_edge_only_runtime_to_approved_local_runner(
    tmp_path: Path, monkeypatch,
) -> None:
    local_job = _create_job(tmp_path / "source")
    remote_job = tmp_path / "remote" / local_job.name
    remote_job.parent.mkdir()
    shutil.copytree(local_job, remote_job)
    repository = LocalCloudRepository(remote_job)
    gateway = FakeGateway()
    handoff = RuntimeCapabilityHandoff(
        handoff_id="a" * 64,
        job_uri="gs://onebrief-test/jobs/example",
        job_id=JobStore(remote_job).read().job_id,
        project_id="julpae",
        source_head_sha="b" * 40,
        required_adapters=["unity_compile", "unity_playmode_visual_tests"],
        input_manifest_sha256="c" * 64,
        approved_budget_usd_micros=1_000_000,
        created_at="2026-08-13T00:00:00+00:00",
    )
    monkeypatch.setenv("CLOUD_RUN_EXECUTION", "managed-execution")
    monkeypatch.setattr(
        "onebrief.cloud_jobs.required_local_runtime_adapters",
        lambda _job: ["unity_compile", "unity_playmode_visual_tests"],
    )
    monkeypatch.setattr(
        "onebrief.cloud_jobs.build_runtime_handoff", lambda *_args, **_kwargs: handoff
    )

    record = run_cloud_worker(
        "gs://onebrief-test/jobs/example",
        repository=repository,
        gateway=gateway,
    )

    assert record.status == JobStatus.QUEUED
    assert not repository.claimed
    assert repository.handoffs == [handoff]
    assert repository.upload_count == 0
    assert gateway.calls == []


def test_runtime_handoff_verification_is_digest_and_scope_bound(
    tmp_path: Path, monkeypatch,
) -> None:
    expected = RuntimeCapabilityHandoff(
        handoff_id="a" * 64,
        job_uri="gs://onebrief-test/jobs/example",
        job_id="job-1",
        project_id="julpae",
        source_head_sha="b" * 40,
        required_adapters=["unity_compile"],
        input_manifest_sha256="c" * 64,
        approved_budget_usd_micros=30_000_000,
        created_at="2026-08-13T00:00:00+00:00",
    )
    monkeypatch.setattr("onebrief.cloud_jobs.build_runtime_handoff", lambda *_a, **_k: expected)
    monkeypatch.setattr(
        "onebrief.cloud_jobs.approved_edge_runtime_adapters",
        lambda _job: ["unity_compile"],
    )
    verify_runtime_handoff(
        tmp_path, job_uri=expected.job_uri, handoff=expected.model_copy(
            update={"created_at": "2026-08-13T00:01:00+00:00"}
        ),
    )
    with pytest.raises(PermissionError, match="immutable approved job"):
        verify_runtime_handoff(
            tmp_path,
            job_uri=expected.job_uri,
            handoff=expected.model_copy(update={"approved_budget_usd_micros": 31_000_000}),
        )


def test_local_capability_worker_rejects_handoff_before_claim(
    tmp_path: Path, monkeypatch,
) -> None:
    local_job = _create_job(tmp_path / "source")
    remote_job = tmp_path / "remote" / local_job.name
    remote_job.parent.mkdir()
    shutil.copytree(local_job, remote_job)
    repository = LocalCloudRepository(remote_job)
    repository.handoffs.append(RuntimeCapabilityHandoff(
        handoff_id="a" * 64,
        job_uri="gs://onebrief-test/jobs/example",
        job_id=JobStore(remote_job).read().job_id,
        project_id="julpae",
        source_head_sha="b" * 40,
        required_adapters=["unity_compile"],
        input_manifest_sha256="c" * 64,
        approved_budget_usd_micros=1_000_000,
        created_at="2026-08-13T00:00:00+00:00",
    ))
    monkeypatch.setattr(
        "onebrief.cloud_jobs.verify_runtime_handoff",
        lambda *_a, **_k: (_ for _ in ()).throw(PermissionError("tampered")),
    )

    with pytest.raises(PermissionError, match="tampered"):
        run_local_capability_worker(
            "gs://onebrief-test/jobs/example", repository=repository
        )

    assert not repository.claimed
    assert repository.failure == "PermissionError: tampered"


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

def test_upload_outputs_publishes_terminal_job_record_last(tmp_path: Path, monkeypatch) -> None:
    class Client:
        def bucket(self, _name):
            return object()

    job_dir = tmp_path / "job"
    (job_dir / "work").mkdir(parents=True)
    (job_dir / "run").mkdir()
    (job_dir / "job.json").write_text("{}", encoding="utf-8")
    (job_dir / "work" / "execution_graph_state.json").write_text("{}", encoding="utf-8")
    (job_dir / "run" / "cost_ledger.json").write_text("{}", encoding="utf-8")
    snapshot = job_dir / "work" / "project_snapshot"
    (snapshot / "repository" / "Assets").mkdir(parents=True)
    (snapshot / "registry" / "project").mkdir(parents=True)
    (snapshot / "repository" / "Assets" / "large.asset").write_text("ephemeral")
    (snapshot / "registry" / "project" / "toolpack.json").write_text("ephemeral")
    (snapshot / "restore_evidence.json").write_text("{}", encoding="utf-8")
    uploaded = []
    repository = GCSJobStore("gs://onebrief-test/jobs/ordered", client=Client())
    monkeypatch.setattr(
        repository,
        "_replace_or_create",
        lambda _source, relative: uploaded.append(relative.as_posix()),
    )

    repository.upload_outputs(job_dir)

    assert uploaded[-1] == "job.json"
    assert "work/execution_graph_state.json" in uploaded[:-1]
    assert "work/project_snapshot/restore_evidence.json" in uploaded[:-1]
    assert not any("project_snapshot/repository" in item for item in uploaded)
    assert not any("project_snapshot/registry" in item for item in uploaded)


def test_budget_amendment_downloads_only_allowlisted_cloud_artifacts(tmp_path: Path) -> None:
    objects = {
        "jobs/prior/work/analysis.json": b'{"objective":"grounded"}',
        "jobs/prior/work/public_research.md": b"# Sources\n",
        "jobs/prior/work/secret.txt": b"must not copy",
    }

    class Blob:
        def __init__(self, name):
            self.name = name

        def download_to_filename(self, filename):
            if self.name not in objects:
                raise NotFound("missing")
            Path(filename).write_bytes(objects[self.name])

    class Bucket:
        def blob(self, name):
            return Blob(name)

    class Client:
        def bucket(self, _name):
            return Bucket()

    work = tmp_path / "new-job" / "work"
    copied = GCSJobStore(
        "gs://onebrief-test/jobs/prior", client=Client()
    ).download_reusable_artifacts(work)

    assert copied == ["public_research.md", "analysis.json"]
    assert (work / "analysis.json").is_file()
    assert (work / "public_research.md").is_file()
    assert not (work / "secret.txt").exists()
    manifest = json.loads((work / "reuse_manifest.json").read_text(encoding="utf-8"))
    assert {item["target"] for item in manifest["artifacts"]} == {
        "analysis.json", "public_research.md"
    }


def test_budget_amendment_restores_only_latest_narrative_candidate_as_round_zero(
    tmp_path: Path,
) -> None:
    objects = {
        "jobs/prior/work/draft_r0.json": b'{"title":"old"}',
        "jobs/prior/work/draft_r4.json": b'{"title":"latest"}',
    }

    class Blob:
        def __init__(self, name):
            self.name = name

        def download_to_filename(self, filename):
            if self.name not in objects:
                raise NotFound("missing")
            Path(filename).write_bytes(objects[self.name])

    class Client:
        def bucket(self, _name):
            return type("Bucket", (), {"blob": lambda _self, name: Blob(name)})()

    work = tmp_path / "work"
    copied = GCSJobStore(
        "gs://onebrief-test/jobs/prior", client=Client()
    ).download_reusable_artifacts(work)

    assert copied == ["draft_r0.json"]
    assert json.loads((work / "draft_r0.json").read_text("utf-8"))["title"] == "latest"
    assert not (work / "draft_r4.json").exists()
    manifest = json.loads((work / "reuse_manifest.json").read_text("utf-8"))
    assert manifest["artifacts"] == [{
        "source": "draft_r4.json",
        "target": "draft_r0.json",
        "sha256": hashlib.sha256(objects["jobs/prior/work/draft_r4.json"]).hexdigest(),
        "selection": "most_progressed_narrative_candidate",
    }]


def test_missing_newer_code_candidate_does_not_delete_base_candidate(tmp_path: Path) -> None:
    objects = {
        "jobs/prior/work/code_change_set.json": b'{"summary":"base"}',
    }

    class Blob:
        def __init__(self, name):
            self.name = name

        def download_to_filename(self, filename):
            if self.name not in objects:
                raise NotFound("missing")
            Path(filename).write_bytes(objects[self.name])

    class Client:
        def bucket(self, _name):
            return type("Bucket", (), {"blob": lambda _self, name: Blob(name)})()

    work = tmp_path / "work"
    copied = GCSJobStore(
        "gs://onebrief-test/jobs/prior", client=Client()
    ).download_reusable_artifacts(work)

    assert "code_change_set.json" in copied
    assert json.loads((work / "code_change_set.json").read_text("utf-8"))["summary"] == "base"


def test_latest_numbered_revision_overwrites_base_candidate(tmp_path: Path) -> None:
    objects = {
        "jobs/prior/work/code_change_set.json": b'{"summary":"base"}',
        "jobs/prior/work/code_change_set_r1.json": b'{"summary":"revision"}',
    }

    class Blob:
        def __init__(self, name):
            self.name = name

        def download_to_filename(self, filename):
            if self.name not in objects:
                raise NotFound("missing")
            Path(filename).write_bytes(objects[self.name])

    class Client:
        def bucket(self, _name):
            return type("Bucket", (), {"blob": lambda _self, name: Blob(name)})()

    work = tmp_path / "work"
    GCSJobStore("gs://onebrief-test/jobs/prior", client=Client()).download_reusable_artifacts(work)
    assert json.loads((work / "code_change_set.json").read_text("utf-8"))["summary"] == "revision"


def test_latest_development_failure_becomes_canonical_resume_feedback(tmp_path: Path) -> None:
    objects = {
        "jobs/prior/work/development_verification_failure_r0.txt": b"lint failed",
        "jobs/prior/work/development_verification_failure_r1.txt": b"language control failed",
        "jobs/prior/work/development_verification_failure.txt": b"english copy leaked",
    }

    class Blob:
        def __init__(self, name):
            self.name = name

        def download_to_filename(self, filename):
            if self.name not in objects:
                raise NotFound("missing")
            Path(filename).write_bytes(objects[self.name])

    class Client:
        def bucket(self, _name):
            return type("Bucket", (), {"blob": lambda _self, name: Blob(name)})()

    work = tmp_path / "work"
    copied = GCSJobStore(
        "gs://onebrief-test/jobs/prior", client=Client()
    ).download_reusable_artifacts(work)

    assert "development_verification_failure.txt" in copied
    assert (work / "development_verification_failure.txt").read_text("utf-8") == (
        "english copy leaked"
    )


def test_resume_derives_most_progressed_candidate_from_older_run(tmp_path: Path) -> None:
    objects = {
        "jobs/prior/work/code_change_set_r0.json": b'{"summary":"lint"}',
        "jobs/prior/work/development_verification_failure_r0.txt": b"development verification failed: lint",
        "jobs/prior/work/code_change_set_r1.json": b'{"summary":"working-language-control"}',
        "jobs/prior/work/development_verification_failure_r1.txt": b"web observation failed: cjk leaked | labels identical",
        "jobs/prior/work/code_change_set_r2.json": b'{"summary":"regressed"}',
        "jobs/prior/work/development_verification_failure_r2.txt": b"web observation failed: no control | text same | lang same | one state",
    }

    class Blob:
        def __init__(self, name):
            self.name = name

        def download_to_filename(self, filename):
            if self.name not in objects:
                raise NotFound("missing")
            Path(filename).write_bytes(objects[self.name])

    class Client:
        def bucket(self, _name):
            return type("Bucket", (), {"blob": lambda _self, name: Blob(name)})()

    work = tmp_path / "work"
    GCSJobStore("gs://onebrief-test/jobs/prior", client=Client()).download_reusable_artifacts(work)

    assert json.loads((work / "code_change_set.json").read_text("utf-8"))["summary"] == (
        "working-language-control"
    )
    assert (work / "development_verification_failure.txt").read_text("utf-8") == (
        "web observation failed: cjk leaked | labels identical"
    )


def test_resume_replaces_stale_explicit_best_with_playmode_checkpoint(
    tmp_path: Path,
) -> None:
    objects = {
        "jobs/prior/work/development_best_candidate.json": b'{"summary":"static"}',
        "jobs/prior/work/development_best_failure.txt": (
            b"development verification failed: Unity visual test contract rejected"
        ),
        "jobs/prior/work/code_change_set_r3.json": b'{"summary":"playmode"}',
        "jobs/prior/work/development_verification_failure_r3.txt": (
            b"development verification failed: unity_playmode_visual_tests "
            b"UNITY TEST FAILURES SettingsButton was null"
        ),
    }

    class Blob:
        def __init__(self, name):
            self.name = name

        def download_to_filename(self, filename):
            if self.name not in objects:
                raise NotFound("missing")
            Path(filename).write_bytes(objects[self.name])

    class Client:
        def bucket(self, _name):
            return type("Bucket", (), {"blob": lambda _self, name: Blob(name)})()

    work = tmp_path / "work"
    GCSJobStore("gs://onebrief-test/jobs/prior", client=Client()).download_reusable_artifacts(work)

    assert json.loads((work / "development_best_candidate.json").read_text("utf-8"))[
        "summary"
    ] == "playmode"
    assert json.loads((work / "code_change_set.json").read_text("utf-8"))[
        "summary"
    ] == "playmode"
