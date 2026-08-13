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

from onebrief.cloud_jobs import (
    GCSJobStore,
    CloudExecutionReceipt,
    submit_cloud_job,
    upload_cloud_job,
)
from onebrief.capability_packs import capability_pack_refs_require_approved_host
from onebrief.agent_platform_client import dispatch_approved_job_via_agent_platform
from onebrief.automatic_resume import (
    can_attempt_automatic_resume,
    can_attempt_bounded_repair_resume,
    can_attempt_structural_resume,
    create_bounded_repair_resume,
    create_structural_resume,
    revalidate_failed_development,
    seed_automatic_resume,
)
from onebrief.completion_ledger import build_completion_ledger
from onebrief.project_bootstrap import (
    FolderRegistrationRequest,
    choose_project_folder,
    draft_project_folder,
    register_project_folder,
)
from onebrief.preparation import PreparationPlan, build_preparation_plan
from onebrief.governance import (
    AuthorizationReceipt,
    DecisionRequest,
    build_decision_request,
    canonical_digest,
    external_apply_decision,
    parse_time,
)
from onebrief.jobs import JobStore, create_job, run_job
from onebrief.milestones import MilestonePlan
from onebrief.producer import estimate_budget
from onebrief.project_catalog import ProjectCatalog, RegisteredProject
from onebrief.project_import import (
    ExternalProjectImporter,
    MANIFEST_NAME,
    MAX_MANIFEST_BYTES,
)
from onebrief.project_snapshot import restore_project_snapshot
from onebrief.toolpack_lifecycle import ProjectToolPackLifecycle, ToolPackApprovalRequest
from onebrief.project_continuity import ProjectContinuationContext, ProjectContinuityStore
from onebrief.result_delivery import ExchangePreviewManager
from onebrief.safe_apply import SafeApplyReceipt, apply_verified_project_result
from onebrief.request_reuse import (
    find_reuse_candidate,
    request_fingerprint,
    seed_reusable_artifacts,
)
from onebrief.runner import inspect_requirements, reinspect_requirements
from onebrief.toolpacks import attach_toolpack_descriptors, route_toolpack_candidates
from onebrief.schemas import (
    BudgetEnvelope,
    IntakeRequest,
    InformationRequirement,
    InternalSource,
    OutputTarget,
    RequirementsAnalysis,
    SourcePriority,
    ToolPackId,
)
from onebrief.source_loader import ALLOWED_SUFFIXES, MAX_FILE_BYTES

MAX_UPLOAD_FILES = 10
MAX_TOTAL_UPLOAD_BYTES = 3_000_000


def _now() -> str:
    return datetime.now(UTC).isoformat()


class WebSession(BaseModel):
    session_id: str
    created_at: str
    intake: IntakeRequest
    requirements: RequirementsAnalysis
    budget: BudgetEnvelope | None = None
    request_fingerprint: str = Field(default="0" * 64, pattern=r"^[a-f0-9]{64}$")
    selected_project: RegisteredProject | None = None
    continuation_context: ProjectContinuationContext | None = None
    reuse_source_job_uri: str | None = None
    previous_attempt: dict[str, object] | None = None
    preparation: PreparationPlan | None = None
    parent_session_id: str | None = None
    amendment_kind: str | None = None
    amendment_reason: str | None = Field(default=None, max_length=4000)
    sixsense_confirmed: bool = False


class SixSenseChoice(BaseModel):
    question_id: str = Field(pattern=r"^S0[2-6]$")
    option_id: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{0,31}$")
    custom_text: str | None = Field(default=None, max_length=1000)


class SixSenseConfirmation(BaseModel):
    choices: list[SixSenseChoice] = Field(default_factory=list, max_length=5)
    use_recommended: bool = False


class ExecutionLink(BaseModel):
    session_id: str
    job_uri: str
    operation_name: str
    created_at: str
    agent_engine_resource: str | None = None
    agent_engine_session: str | None = None
    authorization_receipt: AuthorizationReceipt | None = None


class RunApproval(BaseModel):
    approved_usd: float = Field(gt=0)
    authorization_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    toolpack_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    actor_id: str = Field(default="local_user", min_length=1, max_length=120)


class ApplyApproval(BaseModel):
    decision_request_id: str = Field(pattern=r"^[a-f0-9]{64}$")


def _apply_decision_request(session: WebSession, record) -> DecisionRequest | None:
    preparation = session.preparation
    if (
        preparation is None
        or not record.result_manifest_sha256
        or not preparation.authorization_envelope.base_source_revision
    ):
        return None
    action_digest = canonical_digest({
        "run_authorization_sha256": preparation.authorization_sha256,
        "result_manifest_sha256": record.result_manifest_sha256,
        "base_source_revision": preparation.authorization_envelope.base_source_revision,
        "project_id": session.intake.existing_project_id,
    })
    return external_apply_decision(
        authorization_sha256=action_digest,
        base_source_revision=preparation.authorization_envelope.base_source_revision,
        project_id=str(session.intake.existing_project_id),
    )


def _authorization_receipt(
    session: WebSession, approval: RunApproval, *, consumed_at: str,
) -> AuthorizationReceipt | None:
    if session.preparation is None or approval.authorization_sha256 is None:
        return None
    envelope = session.preparation.authorization_envelope
    payload = {
        "session_id": session.session_id,
        "action_digest": approval.authorization_sha256,
        "actor_id": approval.actor_id,
        "base_source_revision": envelope.base_source_revision,
        "affected_scope": [
            *envelope.allowed_read_prefixes,
            *envelope.allowed_write_prefixes,
        ] or ["result_package"],
    }
    return AuthorizationReceipt(
        receipt_id=canonical_digest(payload),
        consumed_at=consumed_at,
        **payload,
    )


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


class LocalWebSessionStore(InMemoryWebSessionStore):
    """Restart-safe local UI state; keeps the InMemory API used by local execution."""

    def __init__(self, root: Path):
        super().__init__()
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, session_id: str, name: str) -> Path:
        if not session_id or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-" for char in session_id):
            raise FileNotFoundError("invalid session id")
        return self.root / session_id / name

    @staticmethod
    def _atomic(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + f".{uuid4().hex}.tmp")
        temporary.write_text(content, encoding="utf-8")
        os.replace(temporary, path)

    def create(self, session: WebSession) -> None:
        with self.lock:
            path = self._path(session.session_id, "session.json")
            if path.exists():
                raise RuntimeError("session already exists")
            self._atomic(path, session.model_dump_json(indent=2) + "\n")
            self.sessions[session.session_id] = session

    def read(self, session_id: str) -> WebSession:
        if session_id in self.sessions:
            return self.sessions[session_id]
        path = self._path(session_id, "session.json")
        try:
            session = WebSession.model_validate_json(path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise FileNotFoundError("session not found") from exc
        self.sessions[session_id] = session
        return session

    def claim_run(self, session_id: str) -> None:
        with self.lock:
            path = self._path(session_id, "run-claim.json")
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                with path.open("x", encoding="utf-8") as stream:
                    stream.write(json.dumps({"claimed_at": _now()}))
            except FileExistsError as exc:
                raise RuntimeError("this session was already submitted") from exc
            self.claims.add(session_id)

    def release_run(self, session_id: str) -> None:
        with self.lock:
            self._path(session_id, "run-claim.json").unlink(missing_ok=True)
            self.claims.discard(session_id)

    def save_execution(self, link: ExecutionLink) -> None:
        with self.lock:
            self._atomic(
                self._path(link.session_id, "execution.json"),
                link.model_dump_json(indent=2) + "\n",
            )
            self.executions[link.session_id] = link

    def read_execution(self, session_id: str) -> ExecutionLink:
        if session_id in self.executions:
            return self.executions[session_id]
        path = self._path(session_id, "execution.json")
        try:
            link = ExecutionLink.model_validate_json(path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise FileNotFoundError("execution not found") from exc
        self.executions[session_id] = link
        return link


def _existing_bounded_resume(
    store: InMemoryWebSessionStore, source_job: Path,
) -> tuple[WebSession, ExecutionLink, JobRecord, dict[str, object]] | None:
    """Resolve an already-created bounded child without dispatching it again."""

    claim_path = source_job / ".bounded-repair-resume-claim.json"
    try:
        claim = json.loads(claim_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return None
    child_job_id = str(claim.get("child_job_id", ""))
    if claim.get("status") != "created" or not child_job_id:
        return None
    child_job = (source_job.parent / child_job_id).resolve()
    if child_job.parent != source_job.parent.resolve() or not child_job.is_dir():
        return None
    try:
        child_record = JobStore(child_job).read()
    except (OSError, ValueError):
        return None
    if child_record.job_id != child_job_id:
        return None

    links = list(store.executions.values())
    if isinstance(store, LocalWebSessionStore):
        for path in store.root.glob("*/execution.json"):
            try:
                link = ExecutionLink.model_validate_json(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if link.session_id not in {item.session_id for item in links}:
                links.append(link)
    for existing_link in links:
        if existing_link.operation_name != "local":
            continue
        try:
            if Path(existing_link.job_uri).resolve() != child_job:
                continue
            child_session = store.read(existing_link.session_id)
        except (FileNotFoundError, OSError, ValueError):
            continue
        manifest_path = child_job / "work" / "continuation_manifest.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError):
            manifest = {}
        return child_session, existing_link, child_record, manifest
    return None


def _bind_bounded_resume_session(source_job: Path, child_session_id: str) -> None:
    """Bind the one-shot continuation claim to its durable UI session."""

    claim_path = source_job / ".bounded-repair-resume-claim.json"
    try:
        claim = json.loads(claim_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return
    if claim.get("status") != "created" or not claim.get("child_job_id"):
        return
    claim["child_session_id"] = child_session_id
    LocalWebSessionStore._atomic(
        claim_path, json.dumps(claim, ensure_ascii=False, indent=2) + "\n"
    )

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
_local_tasks: set[asyncio.Task[object]] = set()


class LocalJobRepository:
    """Read-only status adapter for a development job kept on this PC."""

    def __init__(self, job_dir: Path):
        self.job_dir = job_dir.resolve()

    def read_job(self):
        return JobStore(self.job_dir).read()

    def read_json(self, relative: str):
        path = (self.job_dir / relative).resolve()
        if not path.is_relative_to(self.job_dir) or not path.is_file():
            raise FileNotFoundError(relative)
        return json.loads(path.read_text(encoding="utf-8"))



def _local_jobs_root() -> Path:
    return Path(os.environ.get(
        "ONEBRIEF_LOCAL_JOBS_ROOT",
        str(Path(tempfile.gettempdir()) / "onebrief-local-jobs"),
    )).resolve()


def _local_apply_backups_root() -> Path:
    configured = os.environ.get("ONEBRIEF_APPLY_BACKUPS_ROOT")
    if configured:
        return Path(configured).resolve()
    local = os.environ.get("LOCALAPPDATA")
    base = Path(local) if local else Path(tempfile.gettempdir())
    return (base / "OneBrief" / "apply-backups").resolve()


def get_session_store() -> WebSessionStore:
    global _store
    if _store is None:
        bucket = os.environ.get("ONEBRIEF_BUCKET")
        _store = GCSWebSessionStore(bucket) if bucket else LocalWebSessionStore(
            Path(tempfile.gettempdir()) / "onebrief-web-sessions"
        )
    return _store


def _merge_canonical_goal(goal: str, supplement: str) -> str:
    """Promote confirmed answers into the single goal used by future continuations."""
    addition = supplement.strip()
    if not addition:
        return goal.strip()
    marker = "[\ucd94\uac00 \ud655\uc815\u00b7\ubcf4\uc644\uc0ac\ud56d]"
    merged = f"{goal.strip()}\n\n{marker}\n{addition}"
    if len(merged) > 8000:
        raise HTTPException(
            422,
            "\ubaa9\ud45c\uc640 \ubcf4\uc644\uc0ac\ud56d\uc744 \ud569\uce5c \ub0b4\uc6a9\uc774 8,000\uc790\ub97c \ub118\uc2b5\ub2c8\ub2e4. \ud575\uc2ec\ub9cc \ub0a8\uaca8 \uc904\uc5ec \uc8fc\uc138\uc694.",
        )
    return merged


def _public_session(session: WebSession) -> dict[str, object]:
    sixsense = session.requirements.sixsense
    sixsense_pending = bool(
        not session.sixsense_confirmed and sixsense and sixsense.questions
    )
    return {
        "session_id": session.session_id,
        "canonical_goal": session.intake.goal,
        "requirements": session.requirements.model_dump(mode="json"),
        "selected_project": session.selected_project.model_dump(mode="json") if session.selected_project else None,
        "continuation": session.continuation_context.model_dump(mode="json") if session.continuation_context else None,
        "previous_attempt": session.previous_attempt,
        "parent_session_id": session.parent_session_id,
        "amendment_kind": session.amendment_kind,
        "amendment_reason": session.amendment_reason,
        "sixsense": sixsense.model_dump(mode="json") if sixsense else None,
        "sixsense_pending": sixsense_pending,
        "sixsense_confirmed": session.sixsense_confirmed,
        "preparation": (
            session.preparation.model_dump(mode="json") if session.preparation else None
        ),
        "budget": (
            session.budget.model_dump(mode="json")
            if session.budget and not sixsense_pending
            else None
        ),
        "approval_range": (
            {"minimum": session.budget.minimum_cost_usd, "maximum": _approval_ceiling(session)}
            if session.budget and not sixsense_pending
            else None
        ),
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
    return min(
        session.budget.maximum_cost_usd,
        float(os.environ.get("ONEBRIEF_WEB_MAX_APPROVAL_USD", "10.00")),
    )


def validate_approval(session: WebSession, approved_usd: float) -> None:
    if session.budget is None or not session.requirements.ready_for_estimate:
        raise ValueError("아직 실행 예산을 승인할 수 있는 상태가 아닙니다.")
    minimum = session.budget.minimum_cost_usd
    ceiling = _approval_ceiling(session)
    if approved_usd + 1e-9 < minimum:
        raise ValueError(f"승인액은 최소 예상 금액 ${minimum:.4f} 이상이어야 합니다.")
    if approved_usd - 1e-9 > ceiling:
        raise ValueError(f"승인액은 현재 허용 상한 ${ceiling:.4f} 이하여야 합니다.")


def _service_config() -> tuple[str, str, str, str]:
    bucket = os.environ.get("ONEBRIEF_JOB_BUCKET") or os.environ.get("ONEBRIEF_BUCKET", "")
    project = os.environ.get("GOOGLE_CLOUD_PROJECT", "")
    region = os.environ.get("ONEBRIEF_REGION", "asia-northeast3")
    job_name = os.environ.get("ONEBRIEF_CLOUD_RUN_JOB", "onebrief-worker")
    if not bucket or not project:
        raise RuntimeError("Cloud execution is not configured for this service.")
    return bucket, project, region, job_name


def _local_project_cloud_configured() -> bool:
    bucket = os.environ.get("ONEBRIEF_JOB_BUCKET") or os.environ.get("ONEBRIEF_BUCKET")
    return bool(bucket and os.environ.get("GOOGLE_CLOUD_PROJECT"))


def _project_requires_approved_host(project: RegisteredProject | None) -> bool:
    """Keep host-bound adapters on the machine whose runtime was qualified."""

    if project is None:
        return False
    if "unity" in project.project_type.casefold():
        return True
    if project.origin != "imported":
        return False
    try:
        generated = ProjectToolPackLifecycle(project.project_id).state().generated
    except (FileNotFoundError, OSError, ValueError):
        # An imported project with an unreadable binding is safer on its
        # approved host; normal authorization checks will still reject stale
        # or missing ToolPack state before execution.
        return True
    return bool(
        generated
        and capability_pack_refs_require_approved_host(generated.capability_packs)
    )


app = FastAPI(title="OneBrief", version="0.1.0", docs_url=None, redoc_url=None)


@app.get("/", response_class=HTMLResponse)
def home() -> HTMLResponse:
    page = package_files("onebrief").joinpath("web/index.html").read_text(encoding="utf-8")
    return HTMLResponse(page)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "onebrief-web"}


@app.get("/api/sessions/{session_id}")
def read_session(
    session_id: str,
    store: WebSessionStore = Depends(get_session_store),
) -> dict[str, object]:
    try:
        return _public_session(store.read(session_id))
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.post("/api/projects/import")
async def import_external_project(
    manifest: Annotated[UploadFile, File()],
) -> dict[str, object]:
    if manifest.filename != MANIFEST_NAME:
        raise HTTPException(
            422,
            f"\uc548\ub0b4\ud30c\uc77c \uc774\ub984\uc740 {MANIFEST_NAME}\uc774\uc5b4\uc57c \ud569\ub2c8\ub2e4.",
        )
    payload = await manifest.read(MAX_MANIFEST_BYTES + 1)
    try:
        result = ExternalProjectImporter().import_bytes(payload)
        project = ProjectCatalog().get(result.record.manifest.project_id)
    except (OSError, ValueError, KeyError) as exc:
        raise HTTPException(422, str(exc)) from exc
    return {
        "project": project.model_dump(mode="json"),
        "inventory": result.record.inventory.model_dump(mode="json"),
        "toolpack_preparation": result.record.toolpack_preparation.model_dump(mode="json"),
        "registry_path": result.registry_path,
    }



@app.post("/api/projects/pick-folder")
def pick_external_project_folder() -> dict[str, object]:
    if os.name != "nt":
        raise HTTPException(404, "Local folder selection is unavailable in this deployment.")
    try:
        root = choose_project_folder()
        if root is None:
            return {"cancelled": True}
        draft = draft_project_folder(root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"cancelled": False, "draft": draft.model_dump(mode="json")}


@app.post("/api/projects/register-folder")
def register_external_project_folder(
    request: FolderRegistrationRequest,
) -> dict[str, object]:
    if os.name != "nt":
        raise HTTPException(404, "Local folder registration is unavailable in this deployment.")
    try:
        result = register_project_folder(request)
        project = ProjectCatalog().get(result.record.manifest.project_id)
    except (OSError, ValueError, KeyError) as exc:
        raise HTTPException(422, str(exc)) from exc
    return {
        "project": project.model_dump(mode="json"),
        "inventory": result.record.inventory.model_dump(mode="json"),
        "toolpack_preparation": result.record.toolpack_preparation.model_dump(mode="json"),
        "registry_path": result.registry_path,
    }

def _toolpack_project(project_id: str) -> RegisteredProject:
    try:
        project = ProjectCatalog().get(project_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    if project.origin != "imported":
        raise HTTPException(409, "Built-in ToolPacks are managed by the application release.")
    return project


@app.get("/api/projects/{project_id}/toolpack")
def project_toolpack_state(project_id: str) -> dict[str, object]:
    _toolpack_project(project_id)
    try:
        state = ProjectToolPackLifecycle(project_id).state()
    except (OSError, ValueError) as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"state": state.model_dump(mode="json")}


@app.post("/api/projects/{project_id}/toolpack/generate")
def generate_project_toolpack(project_id: str) -> dict[str, object]:
    _toolpack_project(project_id)
    try:
        state = ProjectToolPackLifecycle(project_id).generate_and_qualify()
        project = ProjectCatalog().get(project_id)
    except (OSError, ValueError) as exc:
        raise HTTPException(422, str(exc)) from exc
    return {
        "state": state.model_dump(mode="json"),
        "project": project.model_dump(mode="json"),
    }


@app.post("/api/projects/{project_id}/toolpack/approve")
def approve_project_toolpack(
    project_id: str,
    request: ToolPackApprovalRequest,
) -> dict[str, object]:
    _toolpack_project(project_id)
    try:
        state = ProjectToolPackLifecycle(project_id).approve(request.toolpack_sha256)
        project = ProjectCatalog().get(project_id)
    except (OSError, ValueError) as exc:
        raise HTTPException(422, str(exc)) from exc
    return {
        "state": state.model_dump(mode="json"),
        "project": project.model_dump(mode="json"),
    }

@app.get("/api/projects")
def projects(q: str = "") -> dict[str, object]:
    items: list[dict[str, object]] = []
    for project in ProjectCatalog().list(q[:200]):
        payload = project.model_dump(mode="json")
        try:
            state = ProjectContinuityStore(project, _local_jobs_root()).load_or_bootstrap()
            payload["continuation"] = state.model_dump(mode="json")
        except Exception:
            payload["continuation"] = None
        items.append(payload)
    return {"projects": items}

@app.post("/api/inspect")
async def inspect(
    goal: Annotated[str, Form(min_length=3, max_length=8000)],
    output_target: Annotated[OutputTarget, Form()] = OutputTarget.AUTO,
    desired_output: Annotated[str | None, Form(max_length=2000)] = None,
    budget_limit_usd: Annotated[float | None, Form(gt=0)] = None,
    public_research_disabled: Annotated[bool, Form()] = False,
    max_revision_rounds: Annotated[int, Form(ge=0, le=6)] = 6,
    existing_project_id: Annotated[str | None, Form(max_length=64)] = None,
    uploads: Annotated[list[UploadFile] | None, File()] = None,
    store: WebSessionStore = Depends(get_session_store),
) -> dict[str, object]:
    sources = await _sources_from_uploads(uploads or [])
    selected_project = None
    continuation_context = None
    toolpack_state = None
    if output_target == OutputTarget.EXISTING_PROJECT:
        if not existing_project_id:
            raise HTTPException(422, "기존 프로젝트 개선을 선택하면 대상 프로젝트를 골라야 합니다.")
        try:
            selected_project = ProjectCatalog().get(existing_project_id)
        except KeyError as exc:
            raise HTTPException(422, str(exc)) from exc
        if selected_project.origin == "imported":
            lifecycle = ProjectToolPackLifecycle(existing_project_id)
            toolpack_state = lifecycle.state()
            if (
                toolpack_state.status in {"needs_generation", "validation_failed"}
                or any("HEAD changed" in item for item in toolpack_state.execution_blockers)
            ):
                toolpack_state = lifecycle.generate_and_qualify()
            selected_project = ProjectCatalog().get(existing_project_id)
        continuation_context = ProjectContinuityStore(
            selected_project, _local_jobs_root()
        ).context().for_request(goal)
        sources = [*sources, continuation_context.as_internal_source()]
    elif existing_project_id:
        raise HTTPException(422, "기존 프로젝트가 아닌 작업에는 프로젝트를 지정할 수 없습니다.")
    intake = IntakeRequest(
        goal=goal,
        output_target=output_target,
        existing_project_id=existing_project_id or None,
        desired_output=desired_output or None,
        internal_sources=sources,
        public_research_allowed=not public_research_disabled,
        budget_limit_usd=budget_limit_usd,
        # Revision depth is an internal safety ceiling. The user approves money and
        # permissions, not an arbitrary retry count.
        max_revision_rounds=6,
    )
    intake = route_toolpack_candidates(intake)
    intake = attach_toolpack_descriptors(intake)
    try:
        fingerprint = request_fingerprint(intake)
        candidate = (
            find_reuse_candidate(_local_jobs_root(), intake, selected_project)
            if isinstance(store, InMemoryWebSessionStore)
            else None
        )
        if candidate is not None:
            requirements = RequirementsAnalysis.model_validate_json(
                (candidate.job_dir / "inputs" / "requirements.json").read_text(encoding="utf-8")
            )
        else:
            requirements = await inspect_requirements(intake)
        sixsense_pending = bool(requirements.sixsense and requirements.sixsense.questions)
        budget = (
            estimate_budget(intake, requirements)
            if requirements.ready_for_estimate and not sixsense_pending
            else None
        )
        preparation = build_preparation_plan(intake, requirements, budget, toolpack_state)
        session = WebSession(
            session_id=str(uuid4()),
            created_at=_now(),
            intake=intake,
            requirements=requirements,
            budget=budget,
            request_fingerprint=fingerprint,
            selected_project=selected_project,
            continuation_context=continuation_context,
            reuse_source_job_uri=str(candidate.job_dir) if candidate else None,
            previous_attempt=candidate.public_summary() if candidate else None,
            preparation=preparation,
            sixsense_confirmed=not sixsense_pending,
        )
        store.create(session)
        return _public_session(session)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(502, f"Service operation failed: {type(exc).__name__}") from exc


@app.post("/api/sessions/{session_id}/sixsense")
async def confirm_sixsense(
    session_id: str,
    confirmation: SixSenseConfirmation,
    store: WebSessionStore = Depends(get_session_store),
) -> dict[str, object]:
    """Apply a pre-generated rapid choice sequence with one final model reinspection."""
    try:
        previous = store.read(session_id)
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    plan = previous.requirements.sixsense
    if previous.sixsense_confirmed or plan is None or not plan.questions:
        raise HTTPException(409, "SixSense 확인이 필요한 세션이 아닙니다.")

    supplied = {choice.question_id: choice for choice in confirmation.choices}
    unknown = set(supplied) - {question.question_id for question in plan.questions}
    if unknown:
        raise HTTPException(422, "현재 SixSense 질문과 일치하지 않는 답변이 있습니다.")
    decision_lines = [f"표준 방향: {plan.standard_profile}"]
    for question in plan.questions:
        choice = supplied.get(question.question_id)
        custom = (choice.custom_text or "").strip() if choice else ""
        if custom:
            if not question.allow_custom:
                raise HTTPException(422, f"{question.question_id}은 직접 입력을 허용하지 않습니다.")
            decision = custom
        else:
            option_id = choice.option_id if choice else None
            if confirmation.use_recommended or option_id is None:
                option = next(item for item in question.options if item.recommended)
            else:
                option = next(
                    (item for item in question.options if item.option_id == option_id),
                    None,
                )
                if option is None:
                    raise HTTPException(422, f"{question.question_id}의 선택지가 유효하지 않습니다.")
            decision = option.decision
        decision_lines.append(f"{question.question_id} {question.dimension}: {decision}")
    answer_text = "\n".join(decision_lines)
    answer_source = InternalSource(
        name=f"sixsense-decisions-{uuid4().hex[:8]}.md",
        priority=SourcePriority.MANDATORY,
        requirement_keys=(
            [item.key for item in previous.requirements.mandatory_information]
            or ["sixsense_preferences"]
        ),
        summary="User-confirmed rapid SixSense decisions and accepted working defaults.",
        content=f"# SixSense 확정사항\n\n{answer_text}\n",
        media_type="text/markdown",
    )
    augmented = previous.intake.model_copy(
        update={
            "goal": _merge_canonical_goal(previous.intake.goal, answer_text),
            "internal_sources": [*previous.intake.internal_sources, answer_source],
        }
    )
    try:
        requirements = await reinspect_requirements(
            augmented,
            previous.requirements,
            sixsense_completed=True,
        )
        budget = estimate_budget(augmented, requirements) if requirements.ready_for_estimate else None
        toolpack_state = None
        if previous.selected_project and previous.selected_project.origin == "imported":
            toolpack_state = ProjectToolPackLifecycle(previous.selected_project.project_id).state()
        preparation = build_preparation_plan(augmented, requirements, budget, toolpack_state)
        session = WebSession(
            session_id=str(uuid4()),
            created_at=_now(),
            intake=augmented,
            requirements=requirements,
            budget=budget,
            request_fingerprint=request_fingerprint(augmented),
            selected_project=previous.selected_project,
            continuation_context=previous.continuation_context,
            preparation=preparation,
            parent_session_id=previous.parent_session_id or previous.session_id,
            sixsense_confirmed=True,
        )
        store.create(session)
        return _public_session(session)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(502, f"Service operation failed: {type(exc).__name__}") from exc

@app.post("/api/sessions/{session_id}/reinspect")
async def reinspect_session(
    session_id: str,
    answers: Annotated[str, Form(max_length=8000)] = "",
    store: WebSessionStore = Depends(get_session_store),
) -> dict[str, object]:
    try:
        previous = store.read(session_id)
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    answer_text = answers.strip()
    if not answer_text:
        raise HTTPException(422, "부족한 정보에 대한 답변을 입력해 주세요.")
    requirement_keys = [item.key for item in previous.requirements.mandatory_information]
    answer_source = InternalSource(
        name=f"user-supplement-{uuid4().hex[:8]}.md",
        priority=SourcePriority.MANDATORY,
        requirement_keys=requirement_keys or ["user_supplement"],
        summary="User-provided answers to the consolidated requirements questions.",
        content=(
            "# 요구사항 보완 답변\n\n"
            f"{answer_text}\n"
        ),
        media_type="text/markdown",
    )
    augmented = previous.intake.model_copy(
        update={
            "goal": _merge_canonical_goal(previous.intake.goal, answer_text),
            "internal_sources": [*previous.intake.internal_sources, answer_source],
        }
    )
    try:
        requirements = await reinspect_requirements(augmented, previous.requirements)
        budget = estimate_budget(augmented, requirements) if requirements.ready_for_estimate else None
        toolpack_state = None
        if previous.selected_project and previous.selected_project.origin == "imported":
            toolpack_state = ProjectToolPackLifecycle(
                previous.selected_project.project_id
            ).state()
        preparation = build_preparation_plan(
            augmented,
            requirements,
            budget,
            toolpack_state,
            amendment_reason=previous.amendment_reason,
        )
        session = WebSession(
            session_id=str(uuid4()),
            created_at=_now(),
            intake=augmented,
            requirements=requirements,
            budget=budget,
            request_fingerprint=request_fingerprint(augmented),
            selected_project=previous.selected_project,
            continuation_context=previous.continuation_context,
            reuse_source_job_uri=None,
            previous_attempt=None,
            preparation=preparation,
            parent_session_id=previous.parent_session_id or previous.session_id,
            amendment_kind=previous.amendment_kind,
            amendment_reason=previous.amendment_reason,
            sixsense_confirmed=previous.sixsense_confirmed,
        )
        store.create(session)
        return _public_session(session)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(502, f"Service operation failed: {type(exc).__name__}") from exc


AMENDABLE_JOB_STATUSES = {
    "needs_information",
    "needs_budget",
    "needs_authorization",
}


@app.post("/api/sessions/{session_id}/amend")
async def amend_session(
    session_id: str,
    store: WebSessionStore = Depends(get_session_store),
) -> dict[str, object]:
    """Return a stopped run to stage one without discarding its lineage."""
    try:
        previous = store.read(session_id)
        link = store.read_execution(session_id)
        repository = (
            LocalJobRepository(Path(link.job_uri))
            if link.operation_name == "local" else GCSJobStore(link.job_uri)
        )
        record = await asyncio.to_thread(repository.read_job)
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, f"Service operation failed: {type(exc).__name__}") from exc

    status = record.status.value
    bounded_failed = bool(
        status == "failed"
        and link.operation_name == "local"
        and find_reuse_candidate(
            _local_jobs_root(), previous.intake, previous.selected_project
        ) is not None
    )
    if status not in AMENDABLE_JOB_STATUSES and not bounded_failed:
        raise HTTPException(409, "This run does not require a stage-one amendment.")

    effective_status = "needs_budget" if bounded_failed else status

    reason = (record.message or record.current_stage or status).strip()[:4000]
    requirements = previous.requirements
    budget = previous.budget
    if effective_status in {"needs_information", "needs_authorization"}:
        key = "runtime_information" if effective_status == "needs_information" else "runtime_authorization"
        request = (
            "Provide the authoritative information identified during verification."
            if status == "needs_information"
            else (
                "Confirm whether the goal should avoid the blocked capability or explicitly "
                "requires OneBrief to propose a different permission boundary."
            )
        )
        runtime_requirement = InformationRequirement(
            key=key,
            request=request,
            reason=reason[:500],
            acceptable_evidence=["A direct user decision or authoritative source"],
        )
        retained = [item for item in requirements.mandatory_information if item.key != key]
        requirements = requirements.model_copy(update={
            "mandatory_information": [*retained, runtime_requirement],
            "consolidated_questions": [request, reason],
            "ready_for_estimate": False,
        })
        budget = None

    selected_project = previous.selected_project
    toolpack_state = None
    if previous.intake.existing_project_id:
        try:
            selected_project = ProjectCatalog().get(previous.intake.existing_project_id)
            if selected_project.origin == "imported":
                toolpack_state = ProjectToolPackLifecycle(
                    selected_project.project_id
                ).state()
        except (KeyError, OSError, ValueError) as exc:
            raise HTTPException(409, str(exc)) from exc

    preparation = build_preparation_plan(
        previous.intake,
        requirements,
        budget,
        toolpack_state,
        amendment_reason=reason,
    )
    reusable_artifacts: list[str] = []
    if effective_status in {"needs_budget", "needs_authorization"} and link.operation_name == "local":
        candidate = find_reuse_candidate(
            _local_jobs_root(), previous.intake, selected_project
        )
        if candidate is not None:
            reusable_artifacts = list(candidate.reusable_artifacts)
    amended = WebSession(
        session_id=str(uuid4()),
        created_at=_now(),
        intake=previous.intake,
        requirements=requirements,
        budget=budget,
        request_fingerprint=previous.request_fingerprint,
        selected_project=selected_project,
        continuation_context=previous.continuation_context,
        reuse_source_job_uri=(
            link.job_uri if effective_status in {"needs_budget", "needs_authorization"} else None
        ),
        previous_attempt={
            "job_id": record.job_id,
            "status": effective_status,
            "reusable_artifacts": reusable_artifacts,
            "message": (
                "The previous run was preserved. OneBrief will reuse only artifacts "
                "that remain compatible with the amended approval."
            ),
        },
        preparation=preparation,
        parent_session_id=session_id,
        amendment_kind=effective_status,
        amendment_reason=reason,
    )
    store.create(amended)
    return _public_session(amended)



@app.post("/api/sessions/{session_id}/run")
async def run_session(
    session_id: str,
    approval: RunApproval,
    store: WebSessionStore = Depends(get_session_store),
) -> dict[str, object]:
    try:
        session = store.read(session_id)
        if session.preparation is not None:
            if not session.preparation.ready_for_authorization:
                raise ValueError(
                    "Stage 1 is blocked: " + " ".join(session.preparation.blockers)
                )
            if approval.authorization_sha256 != session.preparation.authorization_sha256:
                raise ValueError("The completion, permission, or budget plan changed before approval.")
            envelope = session.preparation.authorization_envelope
            if approval.actor_id != envelope.actor_id:
                raise ValueError("The approval actor does not match the prepared authorization.")
            if datetime.now(UTC) >= parse_time(envelope.expires_at):
                raise ValueError("The prepared authorization expired; inspect and approve a fresh plan.")
            if approval.approved_usd - 1e-9 > envelope.maximum_budget_usd:
                raise ValueError("The approval exceeds the budget bound into the authorization digest.")
        validate_approval(session, approval.approved_usd)
        selected_project = None
        if session.intake.existing_project_id:
            selected_project = ProjectCatalog().get(session.intake.existing_project_id)
            if selected_project.origin == "imported" and session.preparation is not None:
                expected_toolpack = session.preparation.permission_manifest.toolpack_sha256
                if not expected_toolpack or approval.toolpack_sha256 != expected_toolpack:
                    raise ValueError("The exact generated ToolPack permissions were not approved.")
                lifecycle = ProjectToolPackLifecycle(selected_project.project_id)
                state = lifecycle.state()
                expected_revision = session.preparation.authorization_envelope.base_source_revision
                if (
                    expected_revision
                    and (state.generated is None or state.generated.repository_head_sha != expected_revision)
                ):
                    raise ValueError("The project source revision changed after authorization was prepared.")
                if state.status == "validated":
                    lifecycle.approve(expected_toolpack)
                elif (
                    state.status != "approved"
                    or state.generated is None
                    or state.generated.sha256 != expected_toolpack
                ):
                    raise ValueError("The ToolPack changed or is no longer qualified for execution.")
                selected_project = ProjectCatalog().get(session.intake.existing_project_id)
            if not selected_project.ready_for_isolated_edit:
                raise ValueError(
                    "선택한 프로젝트에 커밋되지 않은 변경이 있습니다. 현재 변경을 보존한 채 "
                    "안전하게 격리 실행할 수 있도록 먼저 정리해야 합니다."
                )
        store.claim_run(session_id)
    except (FileNotFoundError, KeyError) as exc:
        raise HTTPException(404, str(exc)) from exc
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(409, str(exc)) from exc

    try:
        authorization_receipt = _authorization_receipt(session, approval, consumed_at=_now())
        development_job = any(
            item in session.intake.toolpack_ids
            for item in (ToolPackId.EXCHANGE_DEVELOPMENT, ToolPackId.PROJECT_DEVELOPMENT)
        )
        snapshot_development = ToolPackId.PROJECT_DEVELOPMENT in session.intake.toolpack_ids
        cloud_snapshot_allowed = bool(
            snapshot_development
            and _local_project_cloud_configured()
            and not _project_requires_approved_host(selected_project)
        )
        if development_job and not (
            cloud_snapshot_allowed
        ):
            if isinstance(store, GCSWebSessionStore):
                raise RuntimeError(
                    "Imported-project development is local-only because the approved repository is on this PC."
                )
            local_root = _local_jobs_root()
            local_root.mkdir(parents=True, exist_ok=True)
            preferred_reuse = (
                Path(session.reuse_source_job_uri)
                if session.reuse_source_job_uri
                else None
            )
            reuse_candidate = find_reuse_candidate(
                local_root,
                session.intake,
                selected_project,
                preferred_job_dir=preferred_reuse,
            )
            job_dir = create_job(
                jobs_dir=local_root,
                intake=session.intake,
                requirements=session.requirements,
                sources=session.intake.internal_sources,
                estimate=session.budget,
                approved_usd=approval.approved_usd,
                embed_project_snapshot=False,
            )
            if reuse_candidate is not None:
                seed_reusable_artifacts(reuse_candidate, job_dir)
            link = ExecutionLink(
                session_id=session_id,
                job_uri=str(job_dir),
                operation_name="local",
                created_at=_now(),
                authorization_receipt=authorization_receipt,
            )
            store.save_execution(link)
            task = asyncio.create_task(asyncio.to_thread(run_job, job_dir))
            _local_tasks.add(task)
            task.add_done_callback(_local_tasks.discard)
            return {
                "session_id": session_id,
                "status": "queued",
                "message": "승인된 한도 안에서 로컬 개발 작업을 시작했습니다.",
            }
        if development_job and not isinstance(store, InMemoryWebSessionStore):
            raise RuntimeError(
                "Cloud project snapshots must be created by the local OneBrief app that can read the approved repository."
            )
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
            if (
                session.amendment_kind == "needs_budget"
                and session.reuse_source_job_uri
                and session.reuse_source_job_uri.startswith("gs://")
            ):
                await asyncio.to_thread(
                    GCSJobStore(session.reuse_source_job_uri).download_reusable_artifacts,
                    job_dir / "work",
                )
            agent_engine_resource = os.environ.get("ONEBRIEF_AGENT_ENGINE_RESOURCE")
            if agent_engine_resource:
                job_uri = await asyncio.to_thread(
                    upload_cloud_job, job_dir, bucket=bucket
                )
                dispatch = await asyncio.to_thread(
                    dispatch_approved_job_via_agent_platform,
                    resource_name=agent_engine_resource,
                    job_uri=job_uri,
                    user_id=f"onebrief-web-{session_id}",
                    project=project,
                    location=region,
                )
                receipt = CloudExecutionReceipt(
                    job_uri=job_uri,
                    cloud_run_job=(
                        f"projects/{project}/locations/{region}/jobs/{job_name}"
                    ),
                    operation_name=dispatch.cloud_run_operation,
                )
            else:
                dispatch = None
                receipt = await asyncio.to_thread(
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
            agent_engine_resource=(
                dispatch.agent_engine_resource if dispatch is not None else None
            ),
            agent_engine_session=(
                dispatch.agent_engine_session if dispatch is not None else None
            ),
            authorization_receipt=authorization_receipt,
        )
        store.save_execution(link)
        return {
            "session_id": session_id,
            "status": "queued",
            "message": (
                "The Agent Platform project owner queued the approved Cloud Run job."
                if dispatch is not None
                else "The Cloud Run background job was queued within the approved budget."
            ),
        }
    except (PermissionError, ValueError) as exc:
        store.release_run(session_id)
        raise HTTPException(422, str(exc)) from exc
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
        repository = (
            LocalJobRepository(Path(link.job_uri))
            if link.operation_name == "local" else GCSJobStore(link.job_uri)
        )
        record = await asyncio.to_thread(repository.read_job)
        payload = record.model_dump(mode="json")
        payload["execution_trace"] = {
            "job_uri": link.job_uri,
            "cloud_run_operation": link.operation_name,
            "agent_engine_resource": link.agent_engine_resource,
            "agent_dispatch_id": link.agent_engine_session,
            "authorization_receipt": (
                link.authorization_receipt.model_dump(mode="json")
                if link.authorization_receipt else None
            ),
        }
        try:
            payload["evaluation"] = await asyncio.to_thread(
                repository.read_json, "work/evaluation_metrics.json"
            )
        except FileNotFoundError:
            payload["evaluation"] = None
        try:
            payload["lineage"] = await asyncio.to_thread(
                repository.read_json, "work/lineage_summary.json"
            )
        except FileNotFoundError:
            payload["lineage"] = None
        try:
            payload["resume_capsule"] = await asyncio.to_thread(
                repository.read_json, "work/resume_capsule_l0.json"
            )
        except FileNotFoundError:
            payload["resume_capsule"] = None
        try:
            payload["convergence_ledger"] = await asyncio.to_thread(
                repository.read_json, "work/convergence_ledger.json"
            )
        except FileNotFoundError:
            payload["convergence_ledger"] = None
        try:
            payload["repair_contract"] = await asyncio.to_thread(
                repository.read_json, "work/repair_contract.json"
            )
        except FileNotFoundError:
            payload["repair_contract"] = None
        repair_contract_payload = payload["repair_contract"]
        progress_gate_allows_resume = bool(
            repair_contract_payload is None
            or (
                isinstance(repair_contract_payload, dict)
                and repair_contract_payload.get("execution_allowed", False)
            )
        )
        try:
            session = store.read(session_id)
            try:
                completion = await asyncio.to_thread(
                    repository.read_json, "work/completion_ledger.json"
                )
                completion_proven = bool(completion.get("complete"))
            except FileNotFoundError:
                completion_proven = False
            payload["can_apply"] = bool(
                isinstance(store, InMemoryWebSessionStore)
                and session.intake.output_target == OutputTarget.EXISTING_PROJECT
                and session.intake.existing_project_id
                and record.status.value == "complete"
                and record.result_package
                and completion_proven
            )
            apply_decision = (
                _apply_decision_request(session, record) if payload["can_apply"] else None
            )
            payload["apply_decision_request"] = (
                apply_decision.model_dump(mode="json") if apply_decision else None
            )
            payload["project_id"] = session.intake.existing_project_id
            preparation = session.preparation
            decision = build_decision_request(
                status=record.status.value,
                stage=record.current_stage,
                message=record.message,
                authorization_sha256=(preparation.authorization_sha256 if preparation else None),
                base_source_revision=(
                    preparation.authorization_envelope.base_source_revision
                    if preparation else None
                ),
                expires_at=(preparation.authorization_envelope.expires_at if preparation else None),
            )
            payload["decision_request"] = (
                decision.model_dump(mode="json") if decision else None
            )
            if (
                record.status.value in {"failed", "partial", "needs_information"}
                and session.budget is not None
                and session.intake.existing_project_id
            ):
                if link.operation_name == "local":
                    local_job = Path(link.job_uri).resolve()
                    bounded_resume = can_attempt_bounded_repair_resume(local_job)
                    payload["auto_resume_available"] = (
                        bounded_resume
                        or (
                            progress_gate_allows_resume
                            and (
                            can_attempt_automatic_resume(local_job)
                            or can_attempt_structural_resume(local_job)
                            )
                        )
                    )
                else:
                    # Cloud candidates are downloaded and deterministically
                    # revalidated by the local coordinator when Resume is
                    # requested.  Do not hide that recovery path merely
                    # because the preserved job is in GCS.
                    payload["auto_resume_available"] = bool(
                        progress_gate_allows_resume and link.job_uri.startswith("gs://")
                    )
            else:
                payload["auto_resume_available"] = False
        except FileNotFoundError:
            payload["can_apply"] = False
            payload["auto_resume_available"] = False
        return payload
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, f"Service operation failed: {type(exc).__name__}") from exc


@app.post("/api/sessions/{session_id}/resume")
async def automatically_resume_session(
    session_id: str,
    store: WebSessionStore = Depends(get_session_store),
) -> dict[str, object]:
    """Revalidate preserved work and continue under only the unused original budget."""
    if not isinstance(store, InMemoryWebSessionStore):
        raise HTTPException(409, "Automatic trusted revalidation requires the local coordinator.")
    child_session_id = str(uuid4())
    try:
        previous = store.read(session_id)
        link = store.read_execution(session_id)
        if previous.budget is None:
            raise RuntimeError("this task has no resumable budget approval")
        project_id = previous.intake.existing_project_id
        if not project_id:
            raise RuntimeError("automatic resume requires an existing project")
        project = ProjectCatalog().get(project_id)
        if link.operation_name == "local":
            source_job = Path(link.job_uri).resolve()
            source_context = None
        elif link.job_uri.startswith("gs://"):
            source_context = tempfile.TemporaryDirectory(prefix="onebrief_cloud_resume_")
            source_job = Path(source_context.name) / "job"
            await asyncio.to_thread(GCSJobStore(link.job_uri).download_job, source_job)
            # Rebuild the ephemeral registry at its current local path. The
            # downloaded registry deliberately points at the terminated Cloud
            # container and must never be trusted as a live execution root.
            restored_workspace = source_job / "work" / "project_snapshot"
            await asyncio.to_thread(shutil.rmtree, restored_workspace, True)
            await asyncio.to_thread(restore_project_snapshot, source_job, project_id)
        else:
            raise RuntimeError("this task has no resumable execution store")
        provenance_path = source_job / "work" / "project_snapshot" / "restore_evidence.json"
        provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
        if provenance.get("source_head_sha") != project.head_sha:
            raise RuntimeError("the source project changed after the failed run")
        expected_toolpack = (
            previous.preparation.permission_manifest.toolpack_sha256
            if previous.preparation is not None else None
        )
        state = ProjectToolPackLifecycle(project_id).state()
        if (
            not expected_toolpack
            or state.status != "approved"
            or state.generated is None
            or state.generated.sha256 != expected_toolpack
        ):
            raise RuntimeError("the approved ToolPack changed after the failed run")
        if link.operation_name == "local":
            existing = _existing_bounded_resume(store, source_job)
            if existing is not None:
                child_session, _child_link, child_record, manifest = existing
                return {
                    "session_id": child_session.session_id,
                    "status": child_record.status.value,
                    "source_job_id": str(manifest.get("source_job_id", "")),
                    "cumulative_actual_usd": float(
                        manifest.get("cumulative_actual_usd", 0.0)
                    ),
                    "remaining_approved_usd": float(
                        manifest.get("child_approved_usd", 0.0)
                    ),
                    "maker_reused": True,
                    "idempotent_reuse": True,
                    "message": "The existing bounded continuation was returned without redispatch.",
                }
        if link.operation_name == "local" and can_attempt_structural_resume(source_job):
            child_job, structural = await asyncio.to_thread(
                create_structural_resume, source_job, _local_jobs_root()
            )
            child = previous.model_copy(update={
                "session_id": child_session_id,
                "created_at": _now(),
                "selected_project": project,
                "reuse_source_job_uri": link.job_uri,
                "previous_attempt": {
                    "job_id": structural.source_job_id,
                    "status": "failed",
                    "reusable_artifacts": [],
                    "message": (
                        "A deterministic team-plan defect was repaired. No maker output existed; "
                        "the workflow restarts under only the unused original approval."
                    ),
                },
                "parent_session_id": session_id,
                "amendment_kind": "structural_automatic_resume",
                "amendment_reason": "A missing mandatory stage owner was restored from the agent registry.",
            })
            execution = ExecutionLink(
                session_id=child_session_id,
                job_uri=str(child_job),
                operation_name="local",
                created_at=_now(),
                authorization_receipt=link.authorization_receipt,
            )
            store.create(child)
            store.claim_run(child_session_id)
            store.save_execution(execution)
            task = asyncio.create_task(asyncio.to_thread(run_job, child_job))
            _local_tasks.add(task)
            task.add_done_callback(_local_tasks.discard)
            return {
                "session_id": child_session_id,
                "status": "queued",
                "source_job_id": structural.source_job_id,
                "source_actual_usd": structural.source_actual_usd,
                "remaining_approved_usd": structural.remaining_approved_usd,
                "maker_reused": False,
                "message": "The structural plan was repaired and resumed within the original cap.",
            }
        if link.operation_name == "local" and can_attempt_bounded_repair_resume(source_job):
            child_job, repair = await asyncio.to_thread(
                create_bounded_repair_resume, source_job, _local_jobs_root()
            )
            child = previous.model_copy(update={
                "session_id": child_session_id,
                "created_at": _now(),
                "selected_project": project,
                "reuse_source_job_uri": link.job_uri,
                "previous_attempt": {
                    "job_id": repair.source_job_id,
                    "status": "failed",
                    "reusable_artifacts": repair.reused_artifacts,
                    "message": (
                        "The rejected candidate and exact failure evidence were retained. "
                        "Completed planning and analysis are not billed again."
                    ),
                },
                "parent_session_id": session_id,
                "amendment_kind": "bounded_repair_resume",
                "amendment_reason": (
                    "The same maker resumes from the best rejected candidate under only unused approval."
                ),
            })
            execution = ExecutionLink(
                session_id=child_session_id,
                job_uri=str(child_job),
                operation_name="local",
                created_at=_now(),
                authorization_receipt=link.authorization_receipt,
            )
            store.create(child)
            store.claim_run(child_session_id)
            store.save_execution(execution)
            await asyncio.to_thread(
                _bind_bounded_resume_session, source_job, child_session_id
            )
            task = asyncio.create_task(asyncio.to_thread(run_job, child_job))
            _local_tasks.add(task)
            task.add_done_callback(_local_tasks.discard)
            return {
                "session_id": child_session_id,
                "status": "queued",
                "source_job_id": repair.source_job_id,
                "source_actual_usd": repair.source_actual_usd,
                "cumulative_actual_usd": repair.cumulative_actual_usd,
                "remaining_approved_usd": repair.remaining_approved_usd,
                "maker_reused": True,
                "message": "The rejected candidate resumed within the original aggregate cap.",
            }
        plan = await asyncio.to_thread(
            revalidate_failed_development, source_job, project_id
        )
        source_status = JobStore(source_job).read().status.value
        if plan.remaining_approved_usd < previous.budget.minimum_cost_usd:
            raise RuntimeError(
                "the unused original approval is below the minimum resumable budget"
            )
        child_context = None
        if link.operation_name == "local":
            child_jobs_root = _local_jobs_root()
        else:
            child_context = tempfile.TemporaryDirectory(prefix="onebrief_cloud_resume_child_")
            child_jobs_root = Path(child_context.name) / "jobs"
        child_job = create_job(
            jobs_dir=child_jobs_root,
            intake=previous.intake,
            requirements=previous.requirements,
            sources=previous.intake.internal_sources,
            estimate=previous.budget,
            approved_usd=plan.remaining_approved_usd,
            # Trusted revalidation already bound the exact approved HEAD and
            # per-file hashes. Re-embedding a full Unity source snapshot here
            # is redundant and can fail on large binary font/assets that are
            # intentionally outside the model context.
            embed_project_snapshot=False,
        )
        seed_automatic_resume(source_job, child_job, plan)
        child = previous.model_copy(update={
            "session_id": child_session_id,
            "created_at": _now(),
            "selected_project": project,
            "reuse_source_job_uri": link.job_uri,
            "previous_attempt": {
                "job_id": plan.source_job_id,
                "status": source_status,
                "reusable_artifacts": ["code_change_set.json", "development/"],
                "message": (
                    "Trusted adapters revalidated the preserved result. "
                    "The task resumed without calling the maker again."
                ),
            },
            "parent_session_id": session_id,
            "amendment_kind": "automatic_resume",
            "amendment_reason": (
                "A newer trusted verifier accepted the preserved candidate; "
                "only the unused original approval was transferred."
            ),
        })
        if link.operation_name == "local":
            execution = ExecutionLink(
                session_id=child_session_id,
                job_uri=str(child_job),
                operation_name="local",
                created_at=_now(),
            )
        else:
            bucket, cloud_project, region, job_name = _service_config()
            receipt = await asyncio.to_thread(
                submit_cloud_job,
                child_job,
                bucket=bucket,
                project=cloud_project,
                region=region,
                cloud_run_job=job_name,
            )
            execution = ExecutionLink(
                session_id=child_session_id,
                job_uri=receipt.job_uri,
                operation_name=receipt.operation_name,
                created_at=_now(),
            )
        store.create(child)
        store.claim_run(child_session_id)
        store.save_execution(execution)
        if link.operation_name == "local":
            task = asyncio.create_task(asyncio.to_thread(run_job, child_job))
            _local_tasks.add(task)
            task.add_done_callback(_local_tasks.discard)
        if child_context is not None:
            child_context.cleanup()
        if source_context is not None:
            source_context.cleanup()
        return {
            "session_id": child_session_id,
            "status": "queued",
            "source_job_id": plan.source_job_id,
            "source_actual_usd": plan.source_actual_usd,
            "remaining_approved_usd": plan.remaining_approved_usd,
            "maker_reused": True,
            "message": "Trusted revalidation passed; the preserved maker result resumed.",
        }
    except (FileNotFoundError, KeyError) as exc:
        raise HTTPException(404, str(exc)) from exc
    except (OSError, PermissionError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(409, str(exc)) from exc


@app.get("/api/sessions/{session_id}/criteria")
async def session_criteria(
    session_id: str,
    store: WebSessionStore = Depends(get_session_store),
) -> dict[str, object]:
    """Return the definition-of-done ledger, not merely agent activity."""
    try:
        link = store.read_execution(session_id)
        repository = (
            LocalJobRepository(Path(link.job_uri))
            if link.operation_name == "local" else GCSJobStore(link.job_uri)
        )
        record = await asyncio.to_thread(repository.read_job)
        try:
            ledger = await asyncio.to_thread(repository.read_json, "work/completion_ledger.json")
        except FileNotFoundError:
            requirements_payload = await asyncio.to_thread(
                repository.read_json, "inputs/requirements.json"
            )
            requirements = RequirementsAnalysis.model_validate(requirements_payload)
            ledger = build_completion_ledger(
                requirements.completion_contract, []
            ).model_dump(mode="json")
        ledger["session_id"] = session_id
        ledger["job_status"] = record.status.value
        ledger["current_stage"] = record.current_stage
        return ledger
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, f"Service operation failed: {type(exc).__name__}") from exc


@app.get("/api/sessions/{session_id}/milestones")
async def session_milestones(
    session_id: str,
    store: WebSessionStore = Depends(get_session_store),
) -> dict[str, object]:
    """Return user-visible vertical slices and their durable checkpoint state."""
    try:
        link = store.read_execution(session_id)
        repository = (
            LocalJobRepository(Path(link.job_uri))
            if link.operation_name == "local" else GCSJobStore(link.job_uri)
        )
        record = await asyncio.to_thread(repository.read_job)
        try:
            plan = await asyncio.to_thread(
                repository.read_json, "work/milestone_state/milestone_plan.json"
            )
        except FileNotFoundError:
            return {
                "session_id": session_id,
                "job_status": record.status.value,
                "milestones": [],
                "message": "Milestone planning is not required or is still being prepared.",
            }
        milestones = []
        for milestone in plan.get("milestones", []):
            milestone_id = str(milestone.get("milestone_id", ""))
            try:
                pointer = await asyncio.to_thread(
                    repository.read_json,
                    f"work/milestone_state/current/{milestone_id}.json",
                )
            except FileNotFoundError:
                pointer = {"state": "pending", "checkpoint_id": None}
            contract = milestone.get("contract", {})
            milestones.append({
                "milestone_id": milestone_id,
                "title": milestone.get("title", milestone_id),
                "outcome": milestone.get("outcome", ""),
                "kind": milestone.get("kind", "implementation"),
                "dependencies": milestone.get("dependencies", []),
                "verification_scope": contract.get("verification_scope", "targeted"),
                "primary_criterion_ids": contract.get("primary_criterion_ids", []),
                "state": pointer.get("state", "pending"),
                "checkpoint_id": pointer.get("checkpoint_id"),
            })
        return {
            "session_id": session_id,
            "job_status": record.status.value,
            "plan_sha256": MilestonePlan.model_validate(plan).sha256,
            "minimum_cost_usd": plan.get("minimum_cost_usd"),
            "maximum_cost_usd": plan.get("maximum_cost_usd"),
            "milestones": milestones,
            "message": record.message,
        }
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, f"Service operation failed: {type(exc).__name__}") from exc


@app.get("/api/sessions/{session_id}/graph")
async def session_graph(
    session_id: str,
    store: WebSessionStore = Depends(get_session_store),
) -> dict[str, object]:
    """Return the selected team DAG and its durable node execution history."""
    try:
        link = store.read_execution(session_id)
        repository = (
            LocalJobRepository(Path(link.job_uri))
            if link.operation_name == "local" else GCSJobStore(link.job_uri)
        )
        record = await asyncio.to_thread(repository.read_job)
        base = f"work/workspace/projects/{record.job_id}/02_plan_and_teams"
        try:
            graph = await asyncio.to_thread(repository.read_json, f"{base}/execution_graph.json")
            state = await asyncio.to_thread(repository.read_json, "work/execution_graph_state.json")
        except FileNotFoundError:
            return {
                "session_id": session_id,
                "job_status": record.status.value,
                "current_stage": record.current_stage,
                "nodes": [],
                "message": "The project owner is still selecting the team and execution order.",
            }
        state_nodes = state.get("nodes", {})
        nodes = []
        for node in graph.get("nodes", []):
            node_id = str(node.get("node_id", ""))
            run = state_nodes.get(node_id, {}) if isinstance(state_nodes, dict) else {}
            nodes.append({
                "node_id": node_id,
                "stage": node.get("stage", node_id),
                "agent_type": node.get("agent_type", ""),
                "owner_instance_id": node.get("owner_instance_id", ""),
                "depends_on": node.get("depends_on", []),
                "activation_reason": node.get("activation_reason", ""),
                "status": run.get("status", "pending"),
                "attempt": run.get("attempt", 0),
                "message": run.get("message", ""),
                "output_paths": run.get("output_paths", []),
                "parallel_group": node.get("parallel_group"),
            })
        return {
            "session_id": session_id,
            "job_status": record.status.value,
            "current_stage": record.current_stage,
            "nodes": nodes,
            "message": record.message,
        }
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, f"Service operation failed: {type(exc).__name__}") from exc


@app.post("/api/projects/{project_id}/latest-result/preview")
async def preview_latest_project_result(project_id: str) -> dict[str, object]:
    try:
        project = ProjectCatalog().get(project_id)
        state = ProjectContinuityStore(project, _local_jobs_root()).load_or_bootstrap()
        if not state.last_run_id or not state.last_result_package:
            raise RuntimeError("completed project result is unavailable")
        receipt = await asyncio.to_thread(
            ExchangePreviewManager(project, _local_jobs_root()).start,
            state.last_run_id,
        )
        return receipt.model_dump(mode="json")
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except (FileNotFoundError, RuntimeError, PermissionError) as exc:
        raise HTTPException(409, str(exc)) from exc

@app.get("/api/projects/{project_id}/latest-result")
async def latest_project_result(project_id: str) -> FileResponse:
    try:
        project = ProjectCatalog().get(project_id)
        state = ProjectContinuityStore(project, _local_jobs_root()).load_or_bootstrap()
        if not state.last_run_id or not state.last_result_package:
            raise RuntimeError("completed project result is unavailable")
        jobs_root = _local_jobs_root()
        job_dir = (jobs_root / state.last_run_id).resolve()
        if not job_dir.is_relative_to(jobs_root) or not job_dir.is_dir():
            raise RuntimeError("completed project result is unavailable")
        record = await asyncio.to_thread(JobStore(job_dir).read)
        package = (job_dir / (record.result_package or "")).resolve()
        if not package.is_relative_to(job_dir) or not package.exists():
            raise RuntimeError("completed project result is unavailable")
        if package.is_dir():
            temp = Path(tempfile.mkdtemp(prefix="onebrief_latest_result_"))
            archive = await asyncio.to_thread(
                shutil.make_archive, str(temp / "onebrief-result"), "zip", package
            )
            return FileResponse(
                Path(archive),
                media_type="application/zip",
                filename=f"onebrief-{project_id}-latest-result.zip",
                background=BackgroundTask(shutil.rmtree, temp, ignore_errors=True),
            )
        return FileResponse(
            package,
            media_type="application/zip",
            filename=f"onebrief-{project_id}-latest-result.zip",
        )
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc

@app.get("/api/sessions/{session_id}/result")
async def session_result(
    session_id: str,
    store: WebSessionStore = Depends(get_session_store),
) -> FileResponse:
    try:
        session = store.read(session_id)
        link = store.read_execution(session_id)
        if link.operation_name == "local":
            job_dir = Path(link.job_uri)
            record = await asyncio.to_thread(JobStore(job_dir).read)
            if session.intake.output_target == OutputTarget.SPREADSHEET:
                workbook = job_dir / "work" / "result.xlsx"
                if workbook.is_file():
                    return FileResponse(
                        workbook,
                        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        filename="OneBrief 결과.xlsx",
                    )
            if not record.result_package:
                raise RuntimeError("result package is not ready")
            package = (job_dir / record.result_package).resolve()
            if not package.is_relative_to(job_dir.resolve()) or not package.exists():
                raise RuntimeError("result package is unavailable")
            if package.is_dir():
                temp = Path(tempfile.mkdtemp(prefix="onebrief_local_result_"))
                archive = await asyncio.to_thread(
                    shutil.make_archive,
                    str(temp / "onebrief-result"),
                    "zip",
                    package,
                )
                return FileResponse(
                    Path(archive),
                    media_type="application/zip",
                    filename=f"onebrief-{session_id[:8]}-result.zip",
                    background=BackgroundTask(shutil.rmtree, temp, ignore_errors=True),
                )
            return FileResponse(
                package,
                media_type="application/zip",
                filename=f"onebrief-{session_id[:8]}-result.zip",
            )
        temp = Path(tempfile.mkdtemp(prefix="onebrief_result_"))
        result_dir = temp / "result"
        await asyncio.to_thread(GCSJobStore(link.job_uri).download_result, result_dir)
        if session.intake.output_target == OutputTarget.SPREADSHEET:
            workbook = result_dir / "artifacts" / "result.xlsx"
            if workbook.is_file():
                return FileResponse(
                    workbook,
                    media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    filename="OneBrief 결과.xlsx",
                    background=BackgroundTask(shutil.rmtree, temp, ignore_errors=True),
                )
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


@app.post("/api/sessions/{session_id}/apply")
async def apply_session_result(
    session_id: str,
    approval: ApplyApproval | None = None,
    store: WebSessionStore = Depends(get_session_store),
) -> dict[str, object]:
    """Apply one verified Cloud/local result to its unchanged local project."""
    if not isinstance(store, InMemoryWebSessionStore):
        raise HTTPException(409, "안전 적용은 원본 프로젝트에 접근할 수 있는 로컬 OneBrief에서만 가능합니다.")
    try:
        session = store.read(session_id)
        project_id = session.intake.existing_project_id
        if session.intake.output_target != OutputTarget.EXISTING_PROJECT or not project_id:
            raise RuntimeError("이 작업은 기존 프로젝트 개선 결과가 아닙니다.")
        link = store.read_execution(session_id)
        expected_apply = None
        if session.preparation is not None:
            repository = (
                LocalJobRepository(Path(link.job_uri))
                if link.operation_name == "local" else GCSJobStore(link.job_uri)
            )
            record_for_approval = await asyncio.to_thread(repository.read_job)
            expected_apply = _apply_decision_request(session, record_for_approval)
        if expected_apply is not None and (
            approval is None or approval.decision_request_id != expected_apply.request_id
        ):
            raise RuntimeError(
                "The external apply approval is missing, stale, or bound to a different result."
            )
        with tempfile.TemporaryDirectory(prefix="onebrief_apply_result_") as temp_name:
            if link.operation_name == "local":
                job_dir = Path(link.job_uri).resolve()
                record = await asyncio.to_thread(JobStore(job_dir).read)
                if record.status.value != "complete" or not record.result_package:
                    raise RuntimeError("적용할 완료 결과가 아직 없습니다.")
                result_root = (job_dir / record.result_package).resolve()
                if not result_root.is_relative_to(job_dir) or not result_root.is_dir():
                    raise RuntimeError("결과 패키지를 찾을 수 없습니다.")
            else:
                result_root = Path(temp_name) / "result"
                await asyncio.to_thread(GCSJobStore(link.job_uri).download_result, result_root)
            receipt: SafeApplyReceipt = await asyncio.to_thread(
                apply_verified_project_result,
                project_id,
                result_root,
                _local_apply_backups_root(),
            )
        return receipt.model_dump(mode="json")
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except (KeyError, PermissionError, RuntimeError, ValueError) as exc:
        raise HTTPException(409, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, f"Service operation failed: {type(exc).__name__}") from exc
