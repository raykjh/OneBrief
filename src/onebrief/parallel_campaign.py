"""Immutable three-lane test campaigns with one accountable integration lane."""

from __future__ import annotations

import hashlib
import json
import os
import re
import statistics
import subprocess
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from uuid import uuid4

from filelock import FileLock
from pydantic import BaseModel, Field, model_validator


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _write_atomic(path: Path, payload: BaseModel | dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = payload.model_dump(mode="json") if isinstance(payload, BaseModel) else payload
    temporary = path.with_suffix(path.suffix + f".{uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(body, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True,
        encoding="utf-8", errors="replace", shell=False, check=False,
    )
    if completed.returncode:
        raise RuntimeError((completed.stdout + completed.stderr).strip()[:4000])
    return completed.stdout.strip()


class LaneKind(StrEnum):
    EXISTING_SOFTWARE = "existing_software"
    GREENFIELD_SOFTWARE = "greenfield_software"
    STRUCTURED_ARTIFACT = "structured_artifact"


class LaneStatus(StrEnum):
    COMPLETE = "complete"
    FAILED = "failed"
    BLOCKED = "blocked"


class SafeApplyOutcome(StrEnum):
    NOT_APPLICABLE = "not_applicable"
    NOT_ATTEMPTED = "not_attempted"
    APPLIED_AND_REVERIFIED = "applied_and_reverified"
    ROLLED_BACK = "rolled_back"
    FAILED = "failed"


class CampaignBaseline(BaseModel):
    commit_sha: str = Field(pattern=r"^[a-f0-9]{40}$")
    tree_sha: str = Field(pattern=r"^[a-f0-9]{40}$")
    source_ref: str = Field(min_length=1, max_length=200)
    image_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    frozen_at: str


class LaneDefinition(BaseModel):
    lane_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{1,40}$")
    kind: LaneKind
    case_path: str = Field(min_length=1, max_length=500)
    output_root: str = Field(min_length=1, max_length=500)
    max_budget_usd: float = Field(gt=0, le=100)
    project_id: str | None = Field(default=None, max_length=100)

    @model_validator(mode="after")
    def validate_paths(self) -> "LaneDefinition":
        for label, raw in (("case_path", self.case_path), ("output_root", self.output_root)):
            path = Path(raw)
            if path.is_absolute() or ".." in path.parts:
                raise ValueError(f"{label} must be a repository-relative path without '..'")
        return self


class CampaignDefinition(BaseModel):
    schema_version: str = "onebrief-parallel-campaign-definition-v1"
    campaign_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{2,60}$")
    baseline_ref: str = Field(min_length=1, max_length=200)
    image_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    total_budget_usd: float = Field(gt=0, le=240)
    lanes: list[LaneDefinition]

    @model_validator(mode="after")
    def validate_campaign(self) -> "CampaignDefinition":
        if not 2 <= len(self.lanes) <= 8:
            raise ValueError("a parallel campaign requires between two and eight test lanes")
        lane_ids = [item.lane_id for item in self.lanes]
        roots = [Path(item.output_root).as_posix().casefold() for item in self.lanes]
        if len(set(lane_ids)) != len(lane_ids):
            raise ValueError("test lane ids must be unique")
        if len(set(roots)) != len(roots):
            raise ValueError("test lane output roots must be unique")
        if sum(item.max_budget_usd for item in self.lanes) > self.total_budget_usd:
            raise ValueError("lane budgets exceed the campaign budget")
        return self


class LaneSpec(BaseModel):
    schema_version: str = "onebrief-parallel-lane-spec-v1"
    campaign_id: str
    lane_id: str
    kind: LaneKind
    case_path: str
    case_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    goal_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    contract_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    output_root: str
    max_budget_usd: float = Field(gt=0)
    project_id: str | None = None
    baseline: CampaignBaseline
    allows_common_code_changes: bool = False
    allows_safe_apply: bool = False


class IntegrationLaneSpec(BaseModel):
    schema_version: str = "onebrief-integration-lane-spec-v1"
    campaign_id: str
    lane_id: str = "integration"
    required_lane_ids: list[str]
    baseline: CampaignBaseline
    allows_common_code_changes: bool = True
    allows_safe_apply: bool = True
    rules: list[str]


class CampaignManifest(BaseModel):
    schema_version: str = "onebrief-parallel-campaign-v1"
    campaign_id: str
    created_at: str
    total_budget_usd: float
    baseline: CampaignBaseline
    lanes: list[LaneSpec]
    integration_lane: IntegrationLaneSpec


class FailureObservation(BaseModel):
    error_class: str = Field(min_length=1, max_length=100)
    summary: str = Field(min_length=1, max_length=4000)
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    stage: str | None = Field(default=None, max_length=100)
    automatically_recovered: bool = False

    @classmethod
    def from_error(
        cls, *, error_class: str, summary: str, stage: str | None = None,
        automatically_recovered: bool = False,
    ) -> "FailureObservation":
        normalized = re.sub(r"\b[0-9a-f]{8}-[0-9a-f-]{27,}\b", "<uuid>", summary.casefold())
        normalized = re.sub(r"\b\d+(?:\.\d+)?\b", "<n>", normalized)
        normalized = " ".join(normalized.split())
        identity = f"{error_class.casefold()}|{stage or ''}|{normalized}"
        return cls(
            error_class=error_class,
            summary=summary,
            fingerprint=_sha256_bytes(identity.encode("utf-8")),
            stage=stage,
            automatically_recovered=automatically_recovered,
        )


class LaneEvidence(BaseModel):
    schema_version: str = "onebrief-parallel-lane-evidence-v1"
    evidence_id: str = Field(default_factory=lambda: str(uuid4()))
    campaign_id: str
    lane_id: str
    baseline_commit_sha: str = Field(pattern=r"^[a-f0-9]{40}$")
    case_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    status: LaneStatus
    verified_complete: bool = False
    job_uri: str | None = None
    result_manifest_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    actual_cost_usd: float = Field(ge=0)
    elapsed_seconds: float = Field(ge=0)
    model_calls: int = Field(ge=0)
    denied_calls: int = Field(ge=0)
    user_questions: int = Field(default=0, ge=0)
    user_approvals: int = Field(default=0, ge=0)
    repeated_user_requests: int = Field(default=0, ge=0)
    recovery_attempts: int = Field(default=0, ge=0)
    recovery_successes: int = Field(default=0, ge=0)
    failures: list[FailureObservation] = Field(default_factory=list)
    common_fix_candidates: list[str] = Field(default_factory=list)
    project_specific_findings: list[str] = Field(default_factory=list)
    safe_apply_outcome: SafeApplyOutcome = SafeApplyOutcome.NOT_APPLICABLE
    observed_source_commit_sha: str = Field(pattern=r"^[a-f0-9]{40}$")
    tracked_source_mutation_detected: bool = False
    recorded_at: str = Field(default_factory=_utc_now)

    @model_validator(mode="after")
    def validate_completion(self) -> "LaneEvidence":
        if self.verified_complete and self.status != LaneStatus.COMPLETE:
            raise ValueError("verified completion requires complete lane status")
        return self


class FailureCluster(BaseModel):
    fingerprint: str
    lane_ids: list[str]
    occurrences: int = Field(ge=1)
    summaries: list[str]
    eligible_for_common_fix: bool


class CampaignAggregate(BaseModel):
    lane_count: int = Field(ge=2, le=8)
    verified_complete_count: int = Field(ge=0)
    completion_rate: float = Field(ge=0, le=1)
    total_actual_cost_usd: float = Field(ge=0)
    median_elapsed_seconds: float = Field(ge=0)
    p95_elapsed_seconds: float = Field(ge=0)
    total_user_interventions: int = Field(ge=0)
    automatic_recovery_rate: float = Field(ge=0, le=1)


class IntegrationBatch(BaseModel):
    schema_version: str = "onebrief-integration-batch-v1"
    campaign_id: str
    baseline: CampaignBaseline
    created_at: str
    evidence_ids: list[str]
    common_failure_clusters: list[FailureCluster]
    lane_specific_failure_clusters: list[FailureCluster]
    aggregate: CampaignAggregate
    common_fix_authority: str = "integration_lane_only"
    regression_lane_ids: list[str]
    promotion_blockers: list[str]


class RevalidationResult(BaseModel):
    lane_id: str
    passed: bool
    evidence_id: str
    safe_apply_outcome: SafeApplyOutcome


class RevalidationReceipt(BaseModel):
    schema_version: str = "onebrief-campaign-revalidation-v1"
    campaign_id: str
    candidate_commit_sha: str = Field(pattern=r"^[a-f0-9]{40}$")
    candidate_image_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    results: list[RevalidationResult]
    created_at: str = Field(default_factory=_utc_now)
    promotion_allowed: bool = False


class ParallelCampaignStore:
    def __init__(self, root: Path):
        self.root = root.resolve()

    def create(self, *, repo_root: Path, definition: CampaignDefinition) -> CampaignManifest:
        if self.root.exists() and any(self.root.iterdir()):
            raise FileExistsError(f"campaign directory already exists: {self.root}")
        repo_root = repo_root.resolve()
        commit = _git(repo_root, "rev-parse", f"{definition.baseline_ref}^{{commit}}")
        tree = _git(repo_root, "rev-parse", f"{commit}^{{tree}}")
        baseline = CampaignBaseline(
            commit_sha=commit,
            tree_sha=tree,
            source_ref=definition.baseline_ref,
            image_digest=definition.image_digest,
            frozen_at=_utc_now(),
        )
        specs: list[LaneSpec] = []
        for lane in definition.lanes:
            case_path = (repo_root / lane.case_path).resolve()
            if not case_path.is_relative_to(repo_root) or not case_path.is_file():
                raise FileNotFoundError(f"lane case is outside the repository: {lane.case_path}")
            raw = case_path.read_bytes()
            case = json.loads(raw.decode("utf-8"))
            goal = str(case.get("goal", "")).strip()
            if not goal:
                raise ValueError(f"lane case has no goal: {lane.case_path}")
            contract = {
                key: case.get(key)
                for key in ("goal", "acceptance_criteria", "verification_commands", "execution_rules")
                if key in case
            }
            specs.append(LaneSpec(
                campaign_id=definition.campaign_id,
                lane_id=lane.lane_id,
                kind=lane.kind,
                case_path=lane.case_path,
                case_sha256=_sha256_bytes(raw),
                goal_sha256=_sha256_bytes(goal.encode("utf-8")),
                contract_sha256=_sha256_bytes(json.dumps(
                    contract, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                ).encode("utf-8")),
                output_root=lane.output_root,
                max_budget_usd=lane.max_budget_usd,
                project_id=lane.project_id,
                baseline=baseline,
            ))
        integration = IntegrationLaneSpec(
            campaign_id=definition.campaign_id,
            required_lane_ids=[item.lane_id for item in specs],
            baseline=baseline,
            rules=[
                "Test lanes are evidence-only and must not modify shared source code.",
                "Only failures observed in at least two lanes are automatically eligible for a common fix.",
                "Project-specific behavior belongs in a ToolPack or project knowledge pack.",
                "A new baseline requires all three lanes to pass revalidation.",
                "Safe apply to the same source project is serialized and owned by integration.",
            ],
        )
        manifest = CampaignManifest(
            campaign_id=definition.campaign_id,
            created_at=_utc_now(),
            total_budget_usd=definition.total_budget_usd,
            baseline=baseline,
            lanes=specs,
            integration_lane=integration,
        )
        _write_atomic(self.root / "campaign.json", manifest)
        for spec in specs:
            _write_atomic(self.root / "lanes" / spec.lane_id / "assignment.json", spec)
            (self.root / "lanes" / spec.lane_id / "evidence").mkdir(parents=True)
        _write_atomic(self.root / "integration" / "assignment.json", integration)
        return manifest

    def manifest(self) -> CampaignManifest:
        return CampaignManifest.model_validate_json(
            (self.root / "campaign.json").read_text(encoding="utf-8")
        )

    def record_evidence(self, evidence: LaneEvidence) -> Path:
        manifest = self.manifest()
        specs = {item.lane_id: item for item in manifest.lanes}
        if evidence.campaign_id != manifest.campaign_id or evidence.lane_id not in specs:
            raise ValueError("evidence does not belong to this campaign lane")
        spec = specs[evidence.lane_id]
        if evidence.baseline_commit_sha != manifest.baseline.commit_sha:
            raise ValueError("lane evidence used a different baseline commit")
        if evidence.observed_source_commit_sha != manifest.baseline.commit_sha:
            raise ValueError("lane source revision drifted from the frozen baseline")
        if evidence.case_sha256 != spec.case_sha256:
            raise ValueError("lane case changed after campaign creation")
        if evidence.tracked_source_mutation_detected:
            raise PermissionError("test lanes may record evidence but cannot mutate shared source")
        if evidence.actual_cost_usd > spec.max_budget_usd:
            raise PermissionError(
                f"lane cost {evidence.actual_cost_usd:.6f} exceeds approved cap "
                f"{spec.max_budget_usd:.6f}"
            )
        target = self.root / "lanes" / evidence.lane_id / "evidence" / f"{evidence.evidence_id}.json"
        lock = FileLock(str(self.root / "lanes" / evidence.lane_id / ".evidence.lock"))
        with lock:
            if target.exists():
                raise FileExistsError(f"evidence id already exists: {evidence.evidence_id}")
            _write_atomic(target, evidence)
        return target

    def _latest_evidence(self) -> list[LaneEvidence]:
        manifest = self.manifest()
        selected: list[LaneEvidence] = []
        for lane in manifest.lanes:
            candidates = list((self.root / "lanes" / lane.lane_id / "evidence").glob("*.json"))
            if not candidates:
                raise RuntimeError(f"lane has no evidence: {lane.lane_id}")
            parsed = [LaneEvidence.model_validate_json(path.read_text("utf-8")) for path in candidates]
            selected.append(max(parsed, key=lambda item: item.recorded_at))
        return selected

    def integrate(self) -> IntegrationBatch:
        manifest = self.manifest()
        evidence = self._latest_evidence()
        grouped: dict[str, list[tuple[str, FailureObservation]]] = {}
        for item in evidence:
            for failure in item.failures:
                grouped.setdefault(failure.fingerprint, []).append((item.lane_id, failure))
        clusters: list[FailureCluster] = []
        for fingerprint, observations in sorted(grouped.items()):
            lanes = sorted({lane_id for lane_id, _ in observations})
            clusters.append(FailureCluster(
                fingerprint=fingerprint,
                lane_ids=lanes,
                occurrences=len(observations),
                summaries=list(dict.fromkeys(item.summary for _, item in observations)),
                eligible_for_common_fix=len(lanes) >= 2,
            ))
        elapsed = sorted(item.elapsed_seconds for item in evidence)
        p95_index = max(0, min(len(elapsed) - 1, round(0.95 * (len(elapsed) - 1))))
        recovery_attempts = sum(item.recovery_attempts for item in evidence)
        aggregate = CampaignAggregate(
            lane_count=len(evidence),
            verified_complete_count=sum(item.verified_complete for item in evidence),
            completion_rate=sum(item.verified_complete for item in evidence) / len(evidence),
            total_actual_cost_usd=round(sum(item.actual_cost_usd for item in evidence), 6),
            median_elapsed_seconds=round(statistics.median(elapsed), 3),
            p95_elapsed_seconds=round(elapsed[p95_index], 3),
            total_user_interventions=sum(
                item.user_questions + item.user_approvals + item.repeated_user_requests
                for item in evidence
            ),
            automatic_recovery_rate=(
                sum(item.recovery_successes for item in evidence) / recovery_attempts
                if recovery_attempts else 0.0
            ),
        )
        blockers = [
            f"{item.lane_id} is not verified complete"
            for item in evidence if not item.verified_complete
        ]
        batch = IntegrationBatch(
            campaign_id=manifest.campaign_id,
            baseline=manifest.baseline,
            created_at=_utc_now(),
            evidence_ids=[item.evidence_id for item in evidence],
            common_failure_clusters=[item for item in clusters if item.eligible_for_common_fix],
            lane_specific_failure_clusters=[item for item in clusters if not item.eligible_for_common_fix],
            aggregate=aggregate,
            regression_lane_ids=[item.lane_id for item in manifest.lanes],
            promotion_blockers=blockers,
        )
        _write_atomic(self.root / "integration" / "triage.json", batch)
        return batch

    def record_revalidation(self, receipt: RevalidationReceipt) -> RevalidationReceipt:
        manifest = self.manifest()
        expected = {item.lane_id for item in manifest.lanes}
        observed = {item.lane_id for item in receipt.results}
        if receipt.campaign_id != manifest.campaign_id or observed != expected:
            raise ValueError("revalidation must include each campaign lane exactly once")
        if len(receipt.results) != len(manifest.lanes):
            raise ValueError("revalidation requires one result for every campaign lane")
        promoted = receipt.model_copy(update={
            "promotion_allowed": all(item.passed for item in receipt.results)
        })
        _write_atomic(self.root / "integration" / "revalidation.json", promoted)
        return promoted
