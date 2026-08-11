"""Cloud Storage transport and Cloud Run Job execution for OneBrief jobs."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

from google.api_core.exceptions import NotFound, PreconditionFailed
from google.cloud import run_v2, storage
from pydantic import BaseModel

from onebrief.jobs import JobRecord, JobStatus, JobStore, run_job, verify_input_snapshot
from onebrief.development_progress import development_failure_quality


REUSABLE_WORK_ARTIFACTS = (
    "project_architecture.json",
    "public_research.json",
    "public_research.md",
    "analysis.json",
    *(f"draft_r{index}.json" for index in range(13)),
    "code_change_set.json",
    "code_change_set_retry_r1.json",
    "code_change_set_r1.json",
    "code_change_set_r2.json",
    "code_change_set_r3.json",
    "code_change_set_r4.json",
    "code_change_set_r5.json",
    "code_change_set_r6.json",
    "development_verification_failure_r0.txt",
    "development_verification_failure_r1.txt",
    "development_verification_failure_r2.txt",
    "development_verification_failure.txt",
    "development_best_candidate.json",
    "development_best_failure.txt",
)


@dataclass(frozen=True)
class GCSJobUri:
    bucket: str
    prefix: str

    @property
    def uri(self) -> str:
        return f"gs://{self.bucket}/{self.prefix}"


class CloudExecutionReceipt(BaseModel):
    job_uri: str
    cloud_run_job: str
    operation_name: str
    asynchronous: bool = True


class CloudJobRepository(Protocol):
    def acquire_claim(self) -> None: ...

    def download_job(self, destination: Path) -> Path: ...

    def upload_outputs(self, job_dir: Path) -> None: ...

    def write_completion(self, record: JobRecord) -> None: ...

    def write_failure(self, message: str) -> None: ...


def parse_gcs_job_uri(uri: str) -> GCSJobUri:
    if not uri.startswith("gs://"):
        raise ValueError("cloud job URI must start with gs://")
    remainder = uri[5:]
    if not remainder or remainder.startswith("/") or "//" in remainder:
        raise ValueError("cloud job URI contains an empty bucket or path segment")
    remainder = remainder.rstrip("/")
    bucket, separator, prefix = remainder.partition("/")
    if not bucket or not separator or not prefix:
        raise ValueError("cloud job URI must include a bucket and object prefix")
    pure = PurePosixPath(prefix)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise ValueError("cloud job URI contains an unsafe object prefix")
    return GCSJobUri(bucket=bucket, prefix=pure.as_posix())


def _safe_relative(blob_name: str, prefix: str) -> Path:
    base = f"{prefix.rstrip('/')}/"
    if not blob_name.startswith(base):
        raise ValueError("object is outside the job prefix")
    relative = PurePosixPath(blob_name[len(base) :])
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise ValueError(f"unsafe object name: {blob_name}")
    return Path(*relative.parts)


def _local_job_files(job_dir: Path) -> list[Path]:
    return [
        path
        for path in sorted(job_dir.rglob("*"))
        if path.is_file()
        and path.name not in {".job.lock", ".budget.lock"}
        and ".tmp" not in path.name
    ]


class GCSJobStore:
    """One job prefix in GCS with generation-guarded writes."""

    def __init__(
        self,
        job_uri: str | GCSJobUri,
        *,
        client: storage.Client | None = None,
    ):
        self.location = parse_gcs_job_uri(job_uri) if isinstance(job_uri, str) else job_uri
        self.client = client or storage.Client()
        self.bucket = self.client.bucket(self.location.bucket)

    def _name(self, relative: str | Path) -> str:
        value = relative.as_posix() if isinstance(relative, Path) else relative
        return f"{self.location.prefix}/{value.lstrip('/')}"

    def _upload_new(self, source: Path, relative: Path) -> None:
        self.bucket.blob(self._name(relative)).upload_from_filename(
            str(source),
            if_generation_match=0,
        )

    def upload_new_job(self, job_dir: Path) -> str:
        job_dir = job_dir.resolve()
        record = JobStore(job_dir).read()
        if record.status != JobStatus.QUEUED:
            raise RuntimeError(f"only queued jobs can be uploaded, not {record.status.value}")
        verify_input_snapshot(job_dir)
        for source in _local_job_files(job_dir):
            relative = source.relative_to(job_dir)
            if relative.parts[0] in {"logs", "packages"}:
                continue
            self._upload_new(source, relative)
        return self.location.uri

    def acquire_claim(self) -> None:
        payload = json.dumps(
            {
                "execution": os.environ.get("CLOUD_RUN_EXECUTION", "local-cloud-worker"),
                "task_index": os.environ.get("CLOUD_RUN_TASK_INDEX", "0"),
            },
            ensure_ascii=False,
        )
        try:
            self.bucket.blob(self._name("control/claim.json")).upload_from_string(
                payload,
                content_type="application/json",
                if_generation_match=0,
            )
        except PreconditionFailed as exc:
            raise RuntimeError("cloud job was already claimed by another execution") from exc

    def download_job(self, destination: Path) -> Path:
        destination.mkdir(parents=True, exist_ok=False)
        found = False
        for blob in self.client.list_blobs(
            self.location.bucket,
            prefix=f"{self.location.prefix}/",
        ):
            relative = _safe_relative(blob.name, self.location.prefix)
            if relative.parts[0] == "control":
                continue
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            blob.download_to_filename(str(target))
            found = True
        if not found or not (destination / "job.json").exists():
            raise FileNotFoundError(f"cloud job is incomplete: {self.location.uri}")
        return destination

    def _replace_or_create(self, source: Path, relative: Path) -> None:
        blob = self.bucket.blob(self._name(relative))
        try:
            blob.reload()
            generation = int(blob.generation)
            blob.upload_from_filename(str(source), if_generation_match=generation)
        except NotFound:
            blob.upload_from_filename(str(source), if_generation_match=0)

    def upload_outputs(self, job_dir: Path) -> None:
        allowed = {"job.json", "run", "work", "packages"}
        files = [
            source
            for source in _local_job_files(job_dir)
            if source.relative_to(job_dir).parts[0] in allowed
        ]
        # Publish the terminal job record last. Readers must never observe COMPLETE
        # before the graph, audit ledger, and immutable result package exist.
        files.sort(key=lambda source: source.relative_to(job_dir).as_posix() == "job.json")
        for source in files:
            self._replace_or_create(source, source.relative_to(job_dir))

    def write_completion(self, record: JobRecord) -> None:
        self.bucket.blob(self._name("control/completion.json")).upload_from_string(
            record.model_dump_json(indent=2),
            content_type="application/json",
            if_generation_match=0,
        )

    def write_failure(self, message: str) -> None:
        payload = json.dumps({"error": message[:2000]}, ensure_ascii=False, indent=2)
        try:
            self.bucket.blob(self._name("control/worker_failure.json")).upload_from_string(
                payload,
                content_type="application/json",
                if_generation_match=0,
            )
        except PreconditionFailed:
            pass

    def read_job(self) -> JobRecord:
        payload = self.bucket.blob(self._name("job.json")).download_as_text(encoding="utf-8")
        return JobRecord.model_validate_json(payload)

    def read_json(self, relative: str | Path) -> dict[str, Any]:
        try:
            payload = self.bucket.blob(self._name(relative)).download_as_text(encoding="utf-8")
        except NotFound as exc:
            raise FileNotFoundError(f"job artifact not found: {relative}") from exc
        value = json.loads(payload)
        if not isinstance(value, dict):
            raise ValueError(f"job artifact is not a JSON object: {relative}")
        return value

    def download_reusable_artifacts(self, destination_work: Path) -> list[str]:
        """Copy only the fixed resumable allowlist into a newly approved job."""
        destination_work = destination_work.resolve()
        destination_work.mkdir(parents=True, exist_ok=True)
        copied: list[dict[str, str]] = []
        downloaded: dict[str, bytes] = {}
        for name in REUSABLE_WORK_ARTIFACTS:
            source_name = name
            target_name = (
                "code_change_set.json"
                if name.startswith("code_change_set") or name == "development_best_candidate.json"
                else (
                    "development_verification_failure.txt"
                    if name.startswith("development_verification_failure")
                    or name == "development_best_failure.txt"
                    else name
                )
            )
            target = (destination_work / target_name).resolve()
            temporary = destination_work / f".reuse-{source_name}.tmp"
            if not target.is_relative_to(destination_work):
                raise ValueError("unsafe reusable artifact destination")
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                self.bucket.blob(self._name(f"work/{source_name}")).download_to_filename(
                    str(temporary)
                )
                downloaded[source_name] = temporary.read_bytes()
                os.replace(temporary, target)
            except NotFound:
                # Several versioned candidate names intentionally map to the
                # same canonical target. A missing newer candidate must not
                # erase an older allowlisted candidate already copied.
                temporary.unlink(missing_ok=True)
                continue
            digest = hashlib.sha256(target.read_bytes()).hexdigest()
            copied.append({"source": source_name, "target": target_name, "sha256": digest})
        # Narrative revisions are checkpoints from one prior attempt, not
        # prepaid future turns in the child. Restore only the most progressed
        # candidate as round zero so every later round is a newly billed repair
        # followed by fresh verification.
        draft_candidates = sorted(
            (
                int(name.removeprefix("draft_r").removesuffix(".json")),
                name,
            )
            for name in downloaded
            if name.startswith("draft_r")
            and name.endswith(".json")
            and name.removeprefix("draft_r").removesuffix(".json").isdigit()
        )
        if draft_candidates:
            _round, source_name = draft_candidates[-1]
            target = destination_work / "draft_r0.json"
            target.write_bytes(downloaded[source_name])
            for index in range(1, 13):
                (destination_work / f"draft_r{index}.json").unlink(missing_ok=True)
            copied = [
                item for item in copied
                if not str(item["target"]).startswith("draft_r")
            ]
            copied.append({
                "source": source_name,
                "target": "draft_r0.json",
                "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                "selection": "most_progressed_narrative_candidate",
            })
        # Older runs predate explicit best-candidate checkpoints.  Derive the
        # most progressed paired candidate deterministically instead of blindly
        # resuming from the final (possibly regressed) revision.
        if "development_best_candidate.json" not in downloaded:
            paired: list[tuple[tuple[int, int], str, str]] = []
            for index in range(7):
                candidate_name = f"code_change_set_r{index}.json"
                failure_name = f"development_verification_failure_r{index}.txt"
                if candidate_name in downloaded and failure_name in downloaded:
                    message = downloaded[failure_name].decode("utf-8", errors="replace")
                    paired.append((development_failure_quality(message), candidate_name, failure_name))
            if paired:
                _quality, candidate_name, failure_name = max(paired, key=lambda item: item[0])
                candidate_target = destination_work / "code_change_set.json"
                failure_target = destination_work / "development_verification_failure.txt"
                candidate_target.write_bytes(downloaded[candidate_name])
                failure_target.write_bytes(downloaded[failure_name])
                for source_name, target in (
                    (candidate_name, candidate_target), (failure_name, failure_target)
                ):
                    digest = hashlib.sha256(target.read_bytes()).hexdigest()
                    copied.append({
                        "source": source_name,
                        "target": target.name,
                        "sha256": digest,
                        "selection": "derived_best_progress",
                    })
        if copied:
            (destination_work / "reuse_manifest.json").write_text(
                json.dumps({
                    "schema_version": "onebrief-reuse-manifest-v1",
                    "source_job_uri": self.location.uri,
                    "reason": "identical approved request continuation within the original budget ceiling",
                    "artifacts": copied,
                }, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        return [item["target"] for item in copied]

    def download_result(self, destination: Path) -> Path:
        record = self.read_job()
        if not record.result_package:
            raise RuntimeError(f"cloud job has no result package while {record.status.value}")
        package_prefix = self._name(record.result_package).rstrip("/")
        destination = destination.resolve()
        if destination.exists() and any(destination.iterdir()):
            raise FileExistsError("result destination must be empty")
        destination.mkdir(parents=True, exist_ok=True)
        found = False
        for blob in self.client.list_blobs(
            self.location.bucket,
            prefix=f"{package_prefix}/",
        ):
            relative = _safe_relative(blob.name, package_prefix)
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            blob.download_to_filename(str(target))
            found = True
        if not found or not (destination / "package_manifest.json").exists():
            raise FileNotFoundError("remote result package is incomplete")
        return destination


def upload_cloud_job(
    job_dir: Path,
    *,
    bucket: str,
    client: storage.Client | None = None,
) -> str:
    record = JobStore(job_dir).read()
    location = GCSJobUri(bucket=bucket, prefix=f"jobs/{record.job_id}")
    return GCSJobStore(location, client=client).upload_new_job(job_dir)


def execute_cloud_run_job(
    job_uri: str,
    *,
    project: str,
    region: str,
    cloud_run_job: str,
    client: run_v2.JobsClient | None = None,
) -> CloudExecutionReceipt:
    parse_gcs_job_uri(job_uri)
    name = f"projects/{project}/locations/{region}/jobs/{cloud_run_job}"
    request = run_v2.RunJobRequest(
        name=name,
        overrides={
            "container_overrides": [
                {"env": [{"name": "ONEBRIEF_JOB_URI", "value": job_uri}]}
            ],
            "task_count": 1,
        },
    )
    operation = (client or run_v2.JobsClient()).run_job(request=request)
    operation_name = getattr(getattr(operation, "operation", None), "name", "")
    return CloudExecutionReceipt(
        job_uri=job_uri,
        cloud_run_job=name,
        operation_name=operation_name,
    )


def submit_cloud_job(
    job_dir: Path,
    *,
    bucket: str,
    project: str,
    region: str,
    cloud_run_job: str,
    storage_client: storage.Client | None = None,
    run_client: run_v2.JobsClient | None = None,
) -> CloudExecutionReceipt:
    job_uri = upload_cloud_job(job_dir, bucket=bucket, client=storage_client)
    return execute_cloud_run_job(
        job_uri,
        project=project,
        region=region,
        cloud_run_job=cloud_run_job,
        client=run_client,
    )


def run_cloud_worker(
    job_uri: str,
    *,
    repository: CloudJobRepository | None = None,
    gateway: object | None = None,
) -> JobRecord:
    """Download, execute, and publish one claimed cloud job using ephemeral disk."""
    remote = repository or GCSJobStore(job_uri)
    remote.acquire_claim()
    try:
        with tempfile.TemporaryDirectory(prefix="onebrief_cloud_") as temp:
            local_job = remote.download_job(Path(temp) / "job")
            record = run_job(local_job, gateway=gateway)
            remote.upload_outputs(local_job)
            remote.write_completion(record)
            return record
    except Exception as exc:
        remote.write_failure(f"{type(exc).__name__}: {exc}")
        raise


def copy_repository_job(source: Path, destination: Path) -> Path:
    """Test helper for repository implementations backed by a local directory."""
    shutil.copytree(source, destination)
    return destination
