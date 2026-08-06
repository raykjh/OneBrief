"""Project closure, reusable-pack candidacy, and guarded promotion."""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _atomic_json(path: Path, value: BaseModel | dict | list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(value, BaseModel):
        payload = value.model_dump(mode="json")
    elif isinstance(value, list):
        payload = [item.model_dump(mode="json") if isinstance(item, BaseModel) else item for item in value]
    else:
        payload = value
    temporary = path.with_suffix(path.suffix + f".{uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


class PackKind(StrEnum):
    KNOWLEDGE = "knowledge"
    TOOL = "tool"
    TEMPLATE = "template"
    RULE = "rule"
    WORKFLOW = "workflow"


class ReviewState(StrEnum):
    PENDING = "pending"
    PASSED = "passed"
    FAILED = "failed"


class CandidateState(StrEnum):
    CANDIDATE = "candidate"
    BLOCKED = "blocked"
    PROMOTED = "promoted"
    RETIRED = "retired"


class ClosureFile(BaseModel):
    path: str
    size_bytes: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class ReuseCandidate(BaseModel):
    schema_version: str = "onebrief-pack-candidate-v1"
    candidate_id: str
    project_id: str
    kind: PackKind
    display_name: str
    source_files: list[ClosureFile]
    activation_conditions: list[str]
    capability_summary: str
    safety_boundary: list[str]
    validation_evidence: list[str]
    successful_project_uses: list[str] = Field(default_factory=list)
    privacy_review: ReviewState = ReviewState.PENDING
    provenance_review: ReviewState = ReviewState.PENDING
    independent_review: ReviewState = ReviewState.PENDING
    state: CandidateState = CandidateState.CANDIDATE

    @model_validator(mode="after")
    def validate_candidate(self) -> "ReuseCandidate":
        if not self.source_files:
            raise ValueError("a reusable-pack candidate requires hashed source files")
        if not self.activation_conditions or not self.safety_boundary:
            raise ValueError("a reusable-pack candidate requires activation and safety boundaries")
        return self


class ProjectClosureReport(BaseModel):
    schema_version: str = "onebrief-project-closure-v1"
    project_id: str
    terminal_status: str
    closed_at: str
    retained_files: list[ClosureFile]
    reusable_candidates: list[str]
    ephemeral_disposition: dict[str, str]
    closure_checks: dict[str, bool]


class PackRelease(BaseModel):
    schema_version: str = "onebrief-pack-release-v1"
    pack_id: str
    version: str
    kind: PackKind
    display_name: str
    released_at: str
    candidate_id: str
    source_files: list[ClosureFile]
    activation_conditions: list[str]
    safety_boundary: list[str]
    successful_project_uses: list[str]


SECRET_PATTERNS = (
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"(?i)(?:api[_-]?key|access[_-]?token|client[_-]?secret)\s*[:=]\s*['\"]?\S+"),
)


def _bounded_text_is_secret_free(paths: list[Path]) -> bool:
    for path in paths:
        if path.stat().st_size > 1_000_000:
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if any(pattern.search(content) for pattern in SECRET_PATTERNS):
            return False
    return True


class PackRegistry:
    """Versioned registry; promotion is deliberately stricter than closure."""

    def __init__(self, root: Path):
        self.root = root.resolve()

    def register_candidate(self, candidate: ReuseCandidate) -> Path:
        path = self.root / "candidates" / candidate.candidate_id / "candidate.json"
        if path.exists():
            existing = ReuseCandidate.model_validate_json(path.read_text(encoding="utf-8"))
            if existing != candidate:
                raise FileExistsError(f"candidate identity already exists: {candidate.candidate_id}")
            return path
        _atomic_json(path, candidate)
        return path

    def promote(self, candidate_id: str, *, version: str) -> PackRelease:
        candidate_path = self.root / "candidates" / candidate_id / "candidate.json"
        candidate = ReuseCandidate.model_validate_json(candidate_path.read_text(encoding="utf-8"))
        reviews = (
            candidate.privacy_review,
            candidate.provenance_review,
            candidate.independent_review,
        )
        if any(review != ReviewState.PASSED for review in reviews):
            raise ValueError("all privacy, provenance, and independent reviews must pass")
        if len(set(candidate.successful_project_uses)) < 2:
            raise ValueError("promotion requires successful use in at least two projects")
        if candidate.state != CandidateState.CANDIDATE:
            raise ValueError(f"candidate cannot be promoted while {candidate.state.value}")
        pack_id = f"{candidate.kind.value}-{candidate.display_name.casefold().replace(' ', '-')}"
        release = PackRelease(
            pack_id=pack_id,
            version=version,
            kind=candidate.kind,
            display_name=candidate.display_name,
            released_at=_now(),
            candidate_id=candidate.candidate_id,
            source_files=candidate.source_files,
            activation_conditions=candidate.activation_conditions,
            safety_boundary=candidate.safety_boundary,
            successful_project_uses=candidate.successful_project_uses,
        )
        release_path = self.root / "releases" / pack_id / version / "release.json"
        if release_path.exists():
            raise FileExistsError(f"immutable pack release already exists: {pack_id}@{version}")
        _atomic_json(release_path, release)
        _atomic_json(
            candidate_path,
            candidate.model_copy(update={"state": CandidateState.PROMOTED}),
        )
        self._write_index()
        return release

    def _write_index(self) -> None:
        releases = []
        for path in sorted((self.root / "releases").glob("*/*/release.json")):
            release = PackRelease.model_validate_json(path.read_text(encoding="utf-8"))
            releases.append({
                "pack_id": release.pack_id,
                "version": release.version,
                "kind": release.kind.value,
                "path": path.relative_to(self.root).as_posix(),
            })
        _atomic_json(
            self.root / "index.json",
            {"schema_version": "onebrief-pack-registry-v1", "releases": releases},
        )


class ProjectClosureManager:
    def __init__(self, registry_root: Path):
        self.registry = PackRegistry(registry_root)

    def close(
        self,
        *,
        project_id: str,
        project_dir: Path,
        work_dir: Path,
        terminal_status: str,
    ) -> ProjectClosureReport:
        if terminal_status not in {
            "complete", "partial", "needs_information", "needs_budget", "failed"
        }:
            raise ValueError("only a terminal project can be closed")
        closure_dir = project_dir / "09_closure"
        if (closure_dir / "closure_report.json").exists():
            return ProjectClosureReport.model_validate_json(
                (closure_dir / "closure_report.json").read_text(encoding="utf-8")
            )

        retained_paths = [
            path for path in sorted(work_dir.rglob("*"))
            if path.is_file() and ".tmp" not in path.name and "09_closure" not in path.parts
        ]
        retained = [
            ClosureFile(
                path=path.relative_to(work_dir).as_posix(),
                size_bytes=path.stat().st_size,
                sha256=_sha256(path),
            )
            for path in retained_paths
        ]
        candidates: list[ReuseCandidate] = []
        toolpack_manifest = work_dir / "toolpacks" / "toolpack_execution.json"
        if terminal_status == "complete" and toolpack_manifest.is_file():
            payload = json.loads(toolpack_manifest.read_text(encoding="utf-8"))
            for run in payload.get("runs", []):
                if run.get("status") != "passed":
                    continue
                pack_id = str(run["toolpack_id"])
                source_paths = [toolpack_manifest]
                for item in run.get("evidence", []):
                    evidence = (
                        work_dir / "toolpacks" / pack_id / "evidence" / item["source_path"]
                    )
                    if evidence.is_file():
                        source_paths.append(evidence)
                source_files = [
                    ClosureFile(
                        path=path.relative_to(work_dir).as_posix(),
                        size_bytes=path.stat().st_size,
                        sha256=_sha256(path),
                    )
                    for path in source_paths
                ]
                privacy = (
                    ReviewState.PASSED
                    if _bounded_text_is_secret_free(source_paths)
                    else ReviewState.FAILED
                )
                candidate = ReuseCandidate(
                    candidate_id=f"{project_id}-{pack_id}-toolpack",
                    project_id=project_id,
                    kind=PackKind.TOOL,
                    display_name=pack_id,
                    source_files=source_files,
                    activation_conditions=[
                        f"A goal explicitly selects the approved {pack_id} capability."
                    ],
                    capability_summary=(
                        f"Deterministic {pack_id} execution and hashed evidence packaging."
                    ),
                    safety_boundary=list(run.get("safety_boundary", [])),
                    validation_evidence=[
                        f"{item['command_id']}: exit={item['exit_code']}"
                        for item in run.get("commands", [])
                    ],
                    successful_project_uses=[project_id],
                    privacy_review=privacy,
                    provenance_review=ReviewState.PENDING,
                    independent_review=ReviewState.PENDING,
                    state=(
                        CandidateState.CANDIDATE
                        if privacy == ReviewState.PASSED else CandidateState.BLOCKED
                    ),
                )
                self.registry.register_candidate(candidate)
                _atomic_json(
                    closure_dir / "candidates" / f"{candidate.candidate_id}.json",
                    candidate,
                )
                candidates.append(candidate)

        report = ProjectClosureReport(
            project_id=project_id,
            terminal_status=terminal_status,
            closed_at=_now(),
            retained_files=retained,
            reusable_candidates=[item.candidate_id for item in candidates],
            ephemeral_disposition={
                "provider_credentials": "never retained in project closure",
                "temporary_files": "excluded from the immutable retained manifest",
                "project_private_memory": "retained only inside the closed project workspace",
                "reusable_material": "copied only as a reviewed candidate, never auto-promoted",
            },
            closure_checks={
                "terminal_status_confirmed": True,
                "retained_files_hashed": all(item.sha256 for item in retained),
                "automatic_promotion_prevented": True,
                "candidate_privacy_scan_passed": all(
                    item.privacy_review == ReviewState.PASSED for item in candidates
                ),
            },
        )
        _atomic_json(closure_dir / "retained_manifest.json", retained)
        _atomic_json(closure_dir / "closure_report.json", report)
        return report

