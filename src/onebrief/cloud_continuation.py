"""Budget-preserving continuation of a failed Cloud OneBrief work order."""

from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from onebrief.agent_platform_client import (
    AgentPlatformDispatchReceipt,
    dispatch_approved_job_via_agent_platform,
)
from onebrief.budget_guard import BudgetStore, micros_to_dollars
from onebrief.cloud_jobs import GCSJobStore, upload_cloud_job
from onebrief.jobs import JobStatus, JobStore, create_job
from onebrief.schemas import BudgetEnvelope, IntakeRequest, InternalSource, RequirementsAnalysis
from onebrief.team_planning import TeamPlan


@dataclass(frozen=True)
class CloudContinuationReceipt:
    source_job_uri: str
    child_job_uri: str
    source_actual_usd: float
    child_approved_usd: float
    aggregate_approval_ceiling_usd: float
    reused_artifacts: tuple[str, ...]
    dispatch: AgentPlatformDispatchReceipt


def _read_models(job_dir: Path) -> tuple[
    IntakeRequest,
    RequirementsAnalysis,
    list[InternalSource],
    BudgetEnvelope,
]:
    inputs = job_dir / "inputs"
    intake = IntakeRequest.model_validate_json((inputs / "intake.json").read_text("utf-8"))
    requirements = RequirementsAnalysis.model_validate_json(
        (inputs / "requirements.json").read_text("utf-8")
    )
    sources = [
        InternalSource.model_validate(item)
        for item in json.loads((inputs / "sources.json").read_text("utf-8"))
    ]
    estimate = BudgetEnvelope.model_validate_json(
        (inputs / "budget_estimate.json").read_text("utf-8")
    )
    return intake, requirements, sources, estimate


def create_budget_preserving_continuation(
    *,
    source_job_dir: Path,
    source_job_uri: str,
    jobs_dir: Path,
    reusable_source: GCSJobStore,
    reusable_team_plan: TeamPlan | None = None,
) -> tuple[Path, float, float, tuple[str, ...]]:
    """Create a child whose spend cannot exceed its parent's unused approval.

    The immutable inputs and estimate are copied into a newly approved work order.
    Only the fixed cloud reuse allowlist is restored; checkpoints, ledgers, result
    packages, and arbitrary files are never inherited.
    """

    source_record = JobStore(source_job_dir).read()
    if source_record.status not in {
        JobStatus.FAILED,
        JobStatus.PARTIAL,
        JobStatus.NEEDS_BUDGET,
    }:
        raise RuntimeError(
            f"cloud continuation requires a terminal resumable source, not {source_record.status.value}"
        )
    source_ledger = BudgetStore(source_job_dir / "run").read()
    remaining_micros = (
        source_ledger.approval.approved_usd_micros
        - source_ledger.actual_usd_micros
        - source_ledger.reserved_usd_micros
    )
    if remaining_micros <= 0:
        raise RuntimeError("source approval has no unused budget")

    source_actual_usd = micros_to_dollars(source_ledger.actual_usd_micros)
    child_approved_usd = micros_to_dollars(remaining_micros)
    intake, requirements, sources, estimate = _read_models(source_job_dir)
    if child_approved_usd < estimate.minimum_cost_usd:
        raise RuntimeError(
            "unused source approval is below the immutable minimum estimate; "
            "a new user approval is required"
        )

    child_dir = create_job(
        jobs_dir=jobs_dir,
        intake=intake,
        requirements=requirements,
        sources=sources,
        estimate=estimate,
        approved_usd=child_approved_usd,
        benchmark_variant=source_record.benchmark_variant,
    )
    reused_items = reusable_source.download_reusable_artifacts(child_dir / "work")
    if reusable_team_plan is not None:
        child_id = JobStore(child_dir).read().job_id
        plan = reusable_team_plan.model_copy(update={"project_id": child_id})
        plan_path = (
            child_dir / "work" / "workspace" / "projects" / child_id
            / "02_plan_and_teams" / "team_plan.json"
        )
        plan_path.parent.mkdir(parents=True, exist_ok=True)
        # TeamAssembler protects project records by comparing their canonical
        # representation byte-for-byte. Seed the resumed plan using that same
        # stable JSON form so a reusable plan is not mistaken for an overwrite.
        plan_path.write_text(
            json.dumps(
                plan.model_dump(mode="json"),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            "utf-8",
        )
        reused_items.append("team_plan.json")
    reused = tuple(dict.fromkeys(reused_items))
    manifest = {
        "schema_version": "onebrief-cloud-continuation-v1",
        "source_job_uri": source_job_uri,
        "source_job_id": source_record.job_id,
        "source_run_id": source_record.run_id,
        "source_actual_usd": source_actual_usd,
        "child_approved_usd": child_approved_usd,
        "aggregate_approval_ceiling_usd": micros_to_dollars(
            source_ledger.approval.approved_usd_micros
        ),
        "reused_artifacts": list(reused),
        "policy": "actual parent spend plus child approval cannot exceed the original approval",
    }
    (child_dir / "work" / "continuation_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return child_dir, source_actual_usd, child_approved_usd, reused


def _find_team_plan(job_dir: Path) -> TeamPlan | None:
    candidates = sorted((job_dir / "work" / "workspace" / "projects").glob(
        "*/02_plan_and_teams/team_plan.json"
    )) if (job_dir / "work" / "workspace" / "projects").is_dir() else []
    if not candidates:
        return None
    return TeamPlan.model_validate_json(candidates[-1].read_text("utf-8"))


def _has_code_candidate(job_dir: Path) -> bool:
    work = job_dir / "work"
    return any((work / name).is_file() for name in (
        "code_change_set.json",
        "code_change_set_retry_r1.json",
        *(f"code_change_set_r{index}.json" for index in range(1, 7)),
    ))


def continue_cloud_job_via_agent_platform(
    *,
    source_job_uri: str,
    jobs_dir: Path,
    bucket: str,
    agent_engine_resource: str,
    project: str,
    location: str,
    user_id: str,
    storage_client: Any | None = None,
    agent_platform_client: Any | None = None,
) -> CloudContinuationReceipt:
    source_store = GCSJobStore(source_job_uri, client=storage_client)
    with tempfile.TemporaryDirectory(prefix="onebrief_cloud_resume_") as temp:
        temp_root = Path(temp)
        source_job_dir = source_store.download_job(temp_root / "source")
        reusable_plan = _find_team_plan(source_job_dir)
        reusable_artifact_store = source_store
        has_code_candidate = _has_code_candidate(source_job_dir)
        ancestor_uri = source_job_uri
        visited = {ancestor_uri}
        depth = 0
        cursor = source_job_dir
        while (reusable_plan is None or not has_code_candidate) and depth < 8:
            lineage_path = cursor / "work" / "continuation_manifest.json"
            if not lineage_path.is_file():
                break
            lineage = json.loads(lineage_path.read_text("utf-8"))
            ancestor_uri = str(lineage.get("source_job_uri", ""))
            if not ancestor_uri or ancestor_uri in visited:
                break
            visited.add(ancestor_uri)
            depth += 1
            ancestor_store = GCSJobStore(ancestor_uri, client=storage_client)
            cursor = ancestor_store.download_job(
                temp_root / f"ancestor-{depth}"
            )
            reusable_plan = reusable_plan or _find_team_plan(cursor)
            if not has_code_candidate and _has_code_candidate(cursor):
                reusable_artifact_store = ancestor_store
                has_code_candidate = True
        child_dir, actual, approved, reused = create_budget_preserving_continuation(
            source_job_dir=source_job_dir,
            source_job_uri=source_job_uri,
            jobs_dir=jobs_dir,
            reusable_source=reusable_artifact_store,
            reusable_team_plan=reusable_plan,
        )
    child_uri = upload_cloud_job(child_dir, bucket=bucket, client=storage_client)
    dispatch = dispatch_approved_job_via_agent_platform(
        resource_name=agent_engine_resource,
        job_uri=child_uri,
        user_id=user_id,
        project=project,
        location=location,
        client=agent_platform_client,
    )
    ceiling = actual + approved
    return CloudContinuationReceipt(
        source_job_uri=source_job_uri,
        child_job_uri=child_uri,
        source_actual_usd=actual,
        child_approved_usd=approved,
        aggregate_approval_ceiling_usd=ceiling,
        reused_artifacts=reused,
        dispatch=dispatch,
    )
