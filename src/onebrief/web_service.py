"""Minimal same-origin web service for OneBrief's guarded workflow."""

from __future__ import annotations

import asyncio
import hashlib
import json
import mimetypes
import os
import shutil
import tempfile
import threading
from datetime import UTC, datetime
from importlib.resources import files as package_files
from pathlib import Path
from typing import Annotated, Protocol
from uuid import uuid4

from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from google.api_core.exceptions import NotFound, PreconditionFailed
from google.cloud import storage
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from onebrief.cloud_jobs import GCSJobStore, CloudExecutionReceipt, submit_cloud_job
from onebrief.jobs import create_job
from onebrief.producer import estimate_budget
from onebrief.runner import inspect_requirements
from onebrief.schemas import (
    BudgetEnvelope,
    IntakeRequest,
    InternalSource,
    RequirementsAnalysis,
    SourcePriority,
)
from onebrief.source_loader import ALLOWED_SUFFIXES, MAX_FILE_BYTES

MAX_UPLOAD_FILES = 10
MAX_TOTAL_UPLOAD_BYTES = 1_000_000


def _now() -> str:
    return datetime.now(UTC).isoformat()


class WebSession(BaseModel):
    session_id: str
    created_at: str
    intake: IntakeRequest
    requirements: RequirementsAnalysis
    budget: BudgetEnvelope | None = None


class ExecutionLink(BaseModel):
    session_id: str
    job_uri: str
    operation_name: str
    created_at: str


class RunApproval(BaseModel):
    approved_usd: float = Field(gt=0)


class WebSessionStore(Protocol):
    def create(self, session: WebSession) -> None: ...

    def read(self, session_id: str) -> WebSession: ...

    def claim_run(self, session_id: str) -> None: ...

    def release_run(self, session_id: str) -> None: ...

    def save_execution(self, link: ExecutionLink) -> None: ...

    def read_execution(self, session_id: str) -> ExecutionLink: ...


class InMemoryWebSessionStore:
    """Local development store. Cloud deployment uses the GCS implementation."""

    def __init__(self) -> None:
        self.sessions: dict[str, WebSession] = {}
        self.claims: set[str] = set()
        self.executions: dict[str, ExecutionLink] = {}
        self.lock = threading.Lock()

    def create(self, session: WebSession) -> None:
        with self.lock:
            if session.session_id in self.sessions:
                raise RuntimeError("session already exists")
            self.sessions[session.session_id] = session

    def read(self, session_id: str) -> WebSession:
        try:
            return self.sessions[session_id]
        except KeyError as exc:
            raise FileNotFoundError("session not found") from exc

    def claim_run(self, session_id: str) -> None:
        with self.lock:
            if session_id in self.claims:
                raise RuntimeError("this session was already submitted")
            self.claims.add(session_id)

    def release_run(self, session_id: str) -> None:
        with self.lock:
            self.claims.discard(session_id)

    def save_execution(self, link: ExecutionLink) -> None:
        with self.lock:
            self.executions[link.session_id] = link

    def read_execution(self, session_id: str) -> ExecutionLink:
        try:
            return self.executions[session_id]
        except KeyError as exc:
            raise FileNotFoundError("execution not found") from exc


class GCSWebSessionStore:
    """Private durable UI state with generation guards for one-shot execution."""

    def __init__(self, bucket_name: str, *, client: storage.Client | None = None):
        self.client = client or storage.Client()
        self.bucket = self.client.bucket(bucket_name)

    def _blob(self, session_id: str, name: str):
        return self.bucket.blob(f"ui-sessions/{session_id}/{name}")

    def create(self, session: WebSession) -> None:
        self._blob(session.session_id, "session.json").upload_from_string(
            session.model_dump_json(indent=2),
            content_type="application/json",
            if_generation_match=0,
        )

    def read(self, session_id: str) -> WebSession:
        try:
            payload = self._blob(session_id, "session.json").download_as_text(encoding="utf-8")
        except NotFound as exc:
            raise FileNotFoundError("session not found") from exc
        return WebSession.model_validate_json(payload)

    def claim_run(self, session_id: str) -> None:
        try:
            self._blob(session_id, "run-claim.json").upload_from_string(
                json.dumps({"claimed_at": _now()}),
                content_type="application/json",
                if_generation_match=0,
            )
        except PreconditionFailed as exc:
            raise RuntimeError("this session was already submitted") from exc

    def release_run(self, session_id: str) -> None:
        blob = self._blob(session_id, "run-claim.json")
        try:
            blob.reload()
            blob.delete(if_generation_match=int(blob.generation))
        except NotFound:
            pass

    def save_execution(self, link: ExecutionLink) -> None:
        self._blob(link.session_id, "execution.json").upload_from_string(
            link.model_dump_json(indent=2),
            content_type="application/json",
            if_generation_match=0,
        )

    def read_execution(self, session_id: str) -> ExecutionLink:
        try:
            payload = self._blob(session_id, "execution.json").download_as_text(encoding="utf-8")
        except NotFound as exc:
            raise FileNotFoundError("execution not found") from exc
        return ExecutionLink.model_validate_json(payload)


_store: WebSessionStore | None = None


def get_session_store() -> WebSessionStore:
    global _store
    if _store is None:
        bucket = os.environ.get("ONEBRIEF_BUCKET")
        _store = GCSWebSessionStore(bucket) if bucket else InMemoryWebSessionStore()
    return _store


def _public_session(session: WebSession) -> dict[str, object]:
    return {
        "session_id": session.session_id,
        "requirements": session.requirements.model_dump(mode="json"),
        "budget": session.budget.model_dump(mode="json") if session.budget else None,
        "preflight_notice": (
            "Requirements inspection is one bounded Gemini call made before execution approval; "
            "the displayed execution estimate starts after that inspection."
        ),
    }


async def _sources_from_uploads(uploads: list[UploadFile]) -> list[InternalSource]:
    if len(uploads) > MAX_UPLOAD_FILES:
        raise HTTPException(422, "Invalid or unsupported upload.")
    sources: list[InternalSource] = []
    total = 0
    names: set[str] = set()
    for index, upload in enumerate(uploads, start=1):
        name = Path(upload.filename or "").name
        if not name or name in names:
            raise HTTPException(422, "Invalid or unsupported upload.")
        names.add(name)
        suffix = Path(name).suffix.lower()
        if suffix not in ALLOWED_SUFFIXES:
            raise HTTPException(422, "Invalid or unsupported upload.")
        raw = await upload.read(MAX_FILE_BYTES + 1)
        if len(raw) > MAX_FILE_BYTES:
            raise HTTPException(422, "Invalid or unsupported upload.")
        total += len(raw)
        if total > MAX_TOTAL_UPLOAD_BYTES:
            raise HTTPException(422, "Invalid or unsupported upload.")
        try:
            content = raw.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise HTTPException(422, "Invalid or unsupported upload.")
        if not content.strip():
            raise HTTPException(422, "Invalid or unsupported upload.")
        sources.append(
            InternalSource(
                name=name,
                priority=SourcePriority.MANDATORY,
                requirement_keys=[f"source_{index:02d}"],
                summary=f"User-uploaded authoritative source: {name}",
                content=content,
                media_type=upload.content_type or mimetypes.guess_type(name)[0] or "text/plain",
                size_bytes=len(raw),
                sha256=hashlib.sha256(raw).hexdigest(),
            )
        )
    return sources


def _approval_ceiling(session: WebSession) -> float:
    if session.budget is None:
        return 0.0
    values = [
        session.budget.maximum_cost_usd,
        float(os.environ.get("ONEBRIEF_WEB_MAX_APPROVAL_USD", "2.00")),
    ]
    if session.intake.budget_limit_usd is not None:
        values.append(session.intake.budget_limit_usd)
    return min(values)


def validate_approval(session: WebSession, approved_usd: float) -> None:
    if session.budget is None or not session.requirements.ready_for_estimate:
        raise ValueError("The approval is outside the permitted budget range.")
    minimum = session.budget.minimum_cost_usd
    ceiling = _approval_ceiling(session)
    if approved_usd + 1e-9 < minimum:
        raise ValueError("The approval is outside the permitted budget range.")
    if approved_usd - 1e-9 > ceiling:
        raise ValueError("The approval is outside the permitted budget range.")


def _service_config() -> tuple[str, str, str, str]:
    bucket = os.environ.get("ONEBRIEF_BUCKET", "")
    project = os.environ.get("GOOGLE_CLOUD_PROJECT", "")
    region = os.environ.get("ONEBRIEF_REGION", "asia-northeast3")
    job_name = os.environ.get("ONEBRIEF_CLOUD_RUN_JOB", "onebrief-worker")
    if not bucket or not project:
        raise RuntimeError("Cloud execution is not configured for this service.")
    return bucket, project, region, job_name


app = FastAPI(title="OneBrief", version="0.1.0", docs_url=None, redoc_url=None)


@app.get("/", response_class=HTMLResponse)
def home() -> HTMLResponse:
    page = package_files("onebrief").joinpath("web/index.html").read_text(encoding="utf-8")
    return HTMLResponse(page)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "onebrief-web"}


@app.post("/api/inspect")
async def inspect(
    goal: Annotated[str, Form(min_length=3, max_length=8000)],
    desired_output: Annotated[str | None, Form(max_length=2000)] = None,
    budget_limit_usd: Annotated[float, Form(gt=0)] = 0.50,
    public_research_allowed: Annotated[bool, Form()] = False,
    max_revision_rounds: Annotated[int, Form(ge=0, le=2)] = 1,
    uploads: Annotated[list[UploadFile] | None, File()] = None,
    store: WebSessionStore = Depends(get_session_store),
) -> dict[str, object]:
    sources = await _sources_from_uploads(uploads or [])
    intake = IntakeRequest(
        goal=goal,
        desired_output=desired_output or None,
        internal_sources=sources,
        public_research_allowed=public_research_allowed,
        budget_limit_usd=budget_limit_usd,
        max_revision_rounds=max_revision_rounds,
    )
    try:
        requirements = await inspect_requirements(intake)
        budget = estimate_budget(intake, requirements) if requirements.ready_for_estimate else None
        session = WebSession(
            session_id=str(uuid4()),
            created_at=_now(),
            intake=intake,
            requirements=requirements,
            budget=budget,
        )
        store.create(session)
        return _public_session(session)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(502, f"Service operation failed: {type(exc).__name__}") from exc


@app.post("/api/sessions/{session_id}/run")
async def run_session(
    session_id: str,
    approval: RunApproval,
    store: WebSessionStore = Depends(get_session_store),
) -> dict[str, object]:
    try:
        session = store.read(session_id)
        validate_approval(session, approval.approved_usd)
        store.claim_run(session_id)
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(409, str(exc)) from exc

    try:
        bucket, project, region, job_name = _service_config()
        with tempfile.TemporaryDirectory(prefix="onebrief_web_job_") as temp:
            job_dir = create_job(
                jobs_dir=Path(temp) / "jobs",
                intake=session.intake,
                requirements=session.requirements,
                sources=session.intake.internal_sources,
                estimate=session.budget,
                approved_usd=approval.approved_usd,
            )
            receipt: CloudExecutionReceipt = await asyncio.to_thread(
                submit_cloud_job,
                job_dir,
                bucket=bucket,
                project=project,
                region=region,
                cloud_run_job=job_name,
            )
        link = ExecutionLink(
            session_id=session_id,
            job_uri=receipt.job_uri,
            operation_name=receipt.operation_name,
            created_at=_now(),
        )
        store.save_execution(link)
        return {
            "session_id": session_id,
            "status": "queued",
            "message": "The Cloud Run background job was queued within the approved budget.",
        }
    except Exception as exc:
        store.release_run(session_id)
        raise HTTPException(502, f"Service operation failed: {type(exc).__name__}") from exc


@app.get("/api/sessions/{session_id}/status")
async def session_status(
    session_id: str,
    store: WebSessionStore = Depends(get_session_store),
) -> dict[str, object]:
    try:
        link = store.read_execution(session_id)
        record = await asyncio.to_thread(GCSJobStore(link.job_uri).read_job)
        return record.model_dump(mode="json")
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, f"Service operation failed: {type(exc).__name__}") from exc


@app.get("/api/sessions/{session_id}/result")
async def session_result(
    session_id: str,
    store: WebSessionStore = Depends(get_session_store),
) -> FileResponse:
    try:
        link = store.read_execution(session_id)
        temp = Path(tempfile.mkdtemp(prefix="onebrief_result_"))
        result_dir = temp / "result"
        await asyncio.to_thread(GCSJobStore(link.job_uri).download_result, result_dir)
        archive = Path(shutil.make_archive(str(temp / "onebrief-result"), "zip", result_dir))
        return FileResponse(
            archive,
            media_type="application/zip",
            filename=f"onebrief-{session_id[:8]}-result.zip",
            background=BackgroundTask(shutil.rmtree, temp, ignore_errors=True),
        )
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, f"Service operation failed: {type(exc).__name__}") from exc
