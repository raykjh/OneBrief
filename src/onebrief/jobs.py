"""Durable jobs, detached execution, and immutable result packages."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any
from uuid import uuid4

from filelock import FileLock
from pydantic import BaseModel, Field

from onebrief.budget_guard import BudgetExceeded, BudgetStore, micros_to_dollars
from onebrief.execution_graph import compile_execution_graph, persist_execution_graph
from onebrief.execution_pipeline import ExecutionPipeline
from onebrief.execution_schemas import PipelineStatus
from onebrief.guarded_gemini import BudgetedGeminiClient
from onebrief.model_policy import (
    ModelPolicyGateway,
    evaluate_model_budget,
    persist_model_approval,
)
from onebrief.requirements_gate import require_ready_for_estimate
from onebrief.schemas import BudgetEnvelope, IntakeRequest, InternalSource, RequirementsAnalysis
from onebrief.source_loader import source_records
from onebrief.team_planning import TeamPlan, TeamPlanningCoordinator


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _atomic_json(path: Path, payload: BaseModel | dict[str, Any] | list[Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(payload, BaseModel):
        value = payload.model_dump(mode="json")
    else:
        value = payload
    temp = path.with_suffix(path.suffix + f".{uuid4().hex}.tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, path)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETE = "complete"
    PARTIAL = "partial"
    NEEDS_INFORMATION = "needs_information"
    NEEDS_BUDGET = "needs_budget"
    FAILED = "failed"


class JobRecord(BaseModel):
    job_id: str
    status: JobStatus
    created_at: str
    updated_at: str
    attempts: int = Field(ge=0)
    worker_pid: int | None = None
    current_stage: str = "queued"
    message: str = ""
    run_id: str
    result_package: str | None = None
    result_manifest_sha256: str | None = None


class PackageFile(BaseModel):
    path: str
    size_bytes: int = Field(ge=0)
    sha256: str


class ResultPackageManifest(BaseModel):
    package_version: str = "onebrief-result-v1"
    job_id: str
    status: JobStatus
    created_at: str
    files: list[PackageFile]


class InputSnapshotManifest(BaseModel):
    snapshot_version: str = "onebrief-input-v1"
    created_at: str
    files: list[PackageFile]


class JobStore:
    def __init__(self, job_dir: Path):
        self.job_dir = job_dir.resolve()
        self.record_path = self.job_dir / "job.json"
        self.lock = FileLock(str(self.job_dir / ".job.lock"), timeout=10)

    def _read_unlocked(self) -> JobRecord:
        return JobRecord.model_validate_json(self.record_path.read_text(encoding="utf-8"))

    def _save_unlocked(self, record: JobRecord) -> JobRecord:
        record.updated_at = _now()
        _atomic_json(self.record_path, record)
        return record

    def read(self) -> JobRecord:
        with self.lock:
            return self._read_unlocked()

    def claim(self, worker_pid: int) -> JobRecord:
        with self.lock:
            record = self._read_unlocked()
            if record.status != JobStatus.QUEUED:
                raise RuntimeError(f"job cannot be claimed while {record.status.value}")
            record.status = JobStatus.RUNNING
            record.current_stage = "starting"
            record.worker_pid = worker_pid
            record.attempts += 1
            record.message = "Background worker claimed the job."
            return self._save_unlocked(record)

    def note_spawned(self, worker_pid: int) -> JobRecord:
        with self.lock:
            record = self._read_unlocked()
            if record.status not in {JobStatus.QUEUED, JobStatus.RUNNING}:
                return record
            record.worker_pid = worker_pid
            return self._save_unlocked(record)

    def finish(
        self,
        status: JobStatus,
        *,
        stage: str,
        message: str,
        result_package: Path | None = None,
        manifest_sha256: str | None = None,
    ) -> JobRecord:
        with self.lock:
            record = self._read_unlocked()
            record.status = status
            record.current_stage = stage
            record.message = message[:1000]
            record.worker_pid = None
            if result_package is not None:
                record.result_package = result_package.relative_to(self.job_dir).as_posix()
                record.result_manifest_sha256 = manifest_sha256
            return self._save_unlocked(record)




def _snapshot_files(inputs_dir: Path) -> list[PackageFile]:
    return [
        PackageFile(
            path=path.relative_to(inputs_dir).as_posix(),
            size_bytes=path.stat().st_size,
            sha256=_sha256(path),
        )
        for path in sorted(inputs_dir.rglob("*"))
        if path.is_file() and path.name != "input_manifest.json"
    ]


def _write_input_snapshot(inputs_dir: Path) -> None:
    _atomic_json(
        inputs_dir / "input_manifest.json",
        InputSnapshotManifest(created_at=_now(), files=_snapshot_files(inputs_dir)),
    )


def verify_input_snapshot(job_dir: Path) -> None:
    inputs_dir = job_dir / "inputs"
    manifest = InputSnapshotManifest.model_validate_json(
        (inputs_dir / "input_manifest.json").read_text(encoding="utf-8")
    )
    expected = {item.path: item for item in manifest.files}
    observed = {item.path: item for item in _snapshot_files(inputs_dir)}
    if set(expected) != set(observed):
        raise RuntimeError("job input snapshot file set changed after approval")
    for name, recorded in expected.items():
        current = observed[name]
        if current.size_bytes != recorded.size_bytes or current.sha256 != recorded.sha256:
            raise RuntimeError(f"job input changed after approval: {name}")
def create_job(
    *,
    jobs_dir: Path,
    intake: IntakeRequest,
    requirements: RequirementsAnalysis,
    sources: list[InternalSource],
    estimate: BudgetEnvelope,
    approved_usd: float,
) -> Path:
    """Create an atomic, self-contained work order with immutable budget approval."""
    requirements = require_ready_for_estimate(
        intake.model_copy(update={"internal_sources": sources}), requirements, sources
    )
    if not sources and not intake.public_research_allowed:
        raise ValueError("job creation requires an authoritative source or approved public research")
    jobs_dir = jobs_dir.resolve()
    jobs_dir.mkdir(parents=True, exist_ok=True)
    job_id = str(uuid4())
    job_dir = jobs_dir / job_id
    staging = jobs_dir / f".{job_id}.{uuid4().hex}.tmp"
    staging.mkdir()
    try:
        inputs = staging / "inputs"
        inputs.mkdir()
        _atomic_json(inputs / "intake.json", intake)
        _atomic_json(inputs / "requirements.json", requirements)
        _atomic_json(inputs / "sources.json", [item.model_dump(mode="json") for item in sources])
        _atomic_json(
            inputs / "source_manifest.json",
            [item.model_dump(mode="json") for item in source_records(sources)],
        )
        _atomic_json(inputs / "budget_estimate.json", estimate)
        _write_input_snapshot(inputs)
        ledger = BudgetStore(staging / "run").approve(estimate, approved_usd)
        now = _now()
        record = JobRecord(
            job_id=job_id,
            status=JobStatus.QUEUED,
            created_at=now,
            updated_at=now,
            attempts=0,
            run_id=ledger.run_id,
            message="Inputs and budget approval were snapshotted; waiting for a worker.",
        )
        _atomic_json(staging / "job.json", record)
        os.replace(staging, job_dir)
        return job_dir
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _copy_if_present(source: Path, destination: Path) -> None:
    if source.exists() and source.is_file() and ".tmp" not in source.name:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)


def build_result_package(
    job_dir: Path,
    *,
    status: JobStatus,
    attempt: int,
) -> tuple[Path, str]:
    """Publish a versioned package only after every copied file is hashed."""
    packages_dir = job_dir / "packages"
    packages_dir.mkdir(parents=True, exist_ok=True)
    final_dir = packages_dir / f"result-v{attempt:03d}"
    temp_dir = packages_dir / f".result-v{attempt:03d}.{uuid4().hex}.tmp"
    temp_dir.mkdir()
    try:
        work_dir = job_dir / "work"
        if work_dir.exists():
            for source in sorted(work_dir.rglob("*")):
                if source.is_file() and ".tmp" not in source.name:
                    _copy_if_present(source, temp_dir / "artifacts" / source.relative_to(work_dir))
        for name in ("approval.json", "cost_ledger.json", "model_execution_policy.json"):
            _copy_if_present(job_dir / "run" / name, temp_dir / "audit" / name)
        _copy_if_present(
            job_dir / "inputs" / "source_manifest.json",
            temp_dir / "evidence" / "source_manifest.json",
        )
        files = [
            PackageFile(
                path=path.relative_to(temp_dir).as_posix(),
                size_bytes=path.stat().st_size,
                sha256=_sha256(path),
            )
            for path in sorted(temp_dir.rglob("*"))
            if path.is_file()
        ]
        manifest = ResultPackageManifest(
            job_id=JobStore(job_dir).read().job_id,
            status=status,
            created_at=_now(),
            files=files,
        )
        _atomic_json(temp_dir / "package_manifest.json", manifest)
        manifest_hash = _sha256(temp_dir / "package_manifest.json")
        if final_dir.exists():
            raise RuntimeError(f"immutable result package already exists: {final_dir.name}")
        os.replace(temp_dir, final_dir)
        return final_dir, manifest_hash
    except Exception:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise


def _pipeline_status(status: PipelineStatus) -> JobStatus:
    return {
        PipelineStatus.COMPLETE: JobStatus.COMPLETE,
        PipelineStatus.PARTIAL: JobStatus.PARTIAL,
        PipelineStatus.NEEDS_INFORMATION: JobStatus.NEEDS_INFORMATION,
        PipelineStatus.NEEDS_BUDGET: JobStatus.NEEDS_BUDGET,
        PipelineStatus.FAILED: JobStatus.FAILED,
        PipelineStatus.RUNNING: JobStatus.RUNNING,
    }[status]


def run_job(job_dir: Path, *, gateway: object | None = None) -> JobRecord:
    """Claim and execute one job. The persisted checkpoint makes model work resumable."""
    job_dir = job_dir.resolve()
    store = JobStore(job_dir)
    claimed = store.claim(os.getpid())
    try:
        verify_input_snapshot(job_dir)
        intake = IntakeRequest.model_validate_json(
            (job_dir / "inputs" / "intake.json").read_text(encoding="utf-8")
        )
        requirements = RequirementsAnalysis.model_validate_json(
            (job_dir / "inputs" / "requirements.json").read_text(encoding="utf-8")
        )
        sources_payload = json.loads((job_dir / "inputs" / "sources.json").read_text(encoding="utf-8"))
        sources = [InternalSource.model_validate(item) for item in sources_payload]
        run_dir = job_dir / "run"
        workspace_root = job_dir / "work" / "workspace"
        raw_gateway = gateway or BudgetedGeminiClient(run_dir)
        TeamPlanningCoordinator(raw_gateway, workspace_root).plan_and_deploy(
            project_id=claimed.job_id,
            intake=intake,
            requirements=requirements,
            sources=sources,
        )
        project_dir = workspace_root / "projects" / claimed.job_id
        plan = TeamPlan.model_validate_json(
            (project_dir / "02_plan_and_teams" / "team_plan.json").read_text(encoding="utf-8")
        )
        execution_graph = compile_execution_graph(plan, intake.toolpack_ids)
        persist_execution_graph(
            execution_graph,
            project_dir / "02_plan_and_teams" / "execution_graph.json",
        )
        estimate = BudgetEnvelope.model_validate_json(
            (job_dir / "inputs" / "budget_estimate.json").read_text(encoding="utf-8")
        )
        approved_usd = micros_to_dollars(BudgetStore(run_dir).read().approval.approved_usd_micros)
        model_decision, model_policy = evaluate_model_budget(
            estimate, plan, approved_usd=approved_usd
        )
        persist_model_approval(
            run_dir=run_dir,
            project_dir=project_dir,
            decision=model_decision,
            policy=model_policy,
        )
        policy_gateway = ModelPolicyGateway(raw_gateway, model_policy)
        pipeline = ExecutionPipeline(
            run_dir,
            gateway=policy_gateway,
            stage_models={stage: model.value for stage, model in model_policy.stage_models.items()},
            execution_graph=execution_graph,
        )
        checkpoint = pipeline.run(
            intake=intake,
            requirements=requirements,
            sources=sources,
            output_dir=job_dir / "work",
        )
        status = _pipeline_status(checkpoint.status)
        package, digest = build_result_package(job_dir, status=status, attempt=claimed.attempts)
        return store.finish(
            status,
            stage=checkpoint.current_stage,
            message=checkpoint.message,
            result_package=package,
            manifest_sha256=digest,
        )
    except BudgetExceeded as exc:
        package, digest = build_result_package(
            job_dir,
            status=JobStatus.NEEDS_BUDGET,
            attempt=claimed.attempts,
        )
        return store.finish(
            JobStatus.NEEDS_BUDGET,
            stage="budget_gate",
            message=str(exc),
            result_package=package,
            manifest_sha256=digest,
        )
    except Exception as exc:
        return store.finish(
            JobStatus.FAILED,
            stage="failed",
            message=f"{type(exc).__name__}: {exc}",
        )


def start_background_job(job_dir: Path) -> JobRecord:
    """Detach a worker process and return immediately to the caller."""
    job_dir = job_dir.resolve()
    store = JobStore(job_dir)
    current = store.read()
    if current.status != JobStatus.QUEUED:
        raise RuntimeError(f"job cannot start while {current.status.value}")
    log_dir = job_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    command = [sys.executable, "-m", "onebrief.cli", "job-worker", str(job_dir)]
    flags = 0
    popen_args: dict[str, Any] = {}
    if os.name == "nt":
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    else:
        popen_args["start_new_session"] = True
    with (log_dir / "worker.stdout.log").open("ab") as stdout, (
        log_dir / "worker.stderr.log"
    ).open("ab") as stderr:
        process = subprocess.Popen(
            command,
            cwd=job_dir,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            creationflags=flags,
            close_fds=True,
            **popen_args,
        )
    return store.note_spawned(process.pid)
