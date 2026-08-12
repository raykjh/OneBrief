"""Trusted local revalidation and budget-preserving continuation of failed work."""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

from pydantic import BaseModel, Field

from onebrief.budget_guard import BudgetStore, micros_to_dollars
from onebrief.development_toolpack import DevelopmentRun
from onebrief.generic_development_toolpack import (
    ApprovedProjectDevelopmentToolPack,
    ProjectCodeChangeSet,
)
from onebrief.jobs import JobStatus, JobStore, create_job
from onebrief.schemas import BudgetEnvelope, IntakeRequest, InternalSource, RequirementsAnalysis

TRUSTED_REVALIDATION_VERSION = "web-observer-v7"

class AutomaticResumePlan(BaseModel):
    source_job_id: str
    project_id: str
    remaining_approved_usd: float = Field(gt=0)
    source_actual_usd: float = Field(ge=0)
    revalidation_dir: str
    revalidated_change_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class StructuralResumePlan(BaseModel):
    source_job_id: str
    remaining_approved_usd: float = Field(gt=0)
    source_actual_usd: float = Field(ge=0)
    failure_fingerprint: str


class BoundedRepairResumePlan(BaseModel):
    source_job_id: str
    remaining_approved_usd: float = Field(gt=0)
    source_actual_usd: float = Field(ge=0)
    cumulative_actual_usd: float = Field(ge=0)
    aggregate_approval_ceiling_usd: float = Field(gt=0)
    reused_artifacts: list[str]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _remaining_approval(job_dir: Path) -> tuple[float, float]:
    ledger = BudgetStore(job_dir / "run").read()
    approved = ledger.approval.approved_usd_micros
    remaining = approved - ledger.actual_usd_micros - ledger.reserved_usd_micros
    return micros_to_dollars(max(remaining, 0)), micros_to_dollars(ledger.actual_usd_micros)


def can_attempt_automatic_resume(job_dir: Path) -> bool:
    """Return true for a rejected development result that retains budget and work."""
    try:
        record = JobStore(job_dir).read()
        remaining, _ = _remaining_approval(job_dir)
    except (OSError, ValueError):
        return False
    if record.status not in {JobStatus.FAILED, JobStatus.PARTIAL} or remaining <= 0:
        return False
    message = record.message.casefold()
    validation_failure = any(token in message for token in (
        "web observation failed",
        "development verification failed",
        "development patch hygiene failed",
        "approved web observation",
        "independent observation capability was unavailable",
    ))
    return validation_failure and (job_dir / "work" / "code_change_set.json").is_file()


def can_attempt_structural_resume(job_dir: Path) -> bool:
    """Allow one budget-preserving retry for a deterministic pre-workflow plan defect."""

    try:
        record = JobStore(job_dir).read()
        remaining, _ = _remaining_approval(job_dir)
    except (OSError, ValueError):
        return False
    if record.status != JobStatus.FAILED or remaining <= 0:
        return False
    return "team plan is missing stage owner:" in record.message.casefold()


def create_structural_resume(source_job: Path, jobs_dir: Path) -> tuple[Path, StructuralResumePlan]:
    """Retry pre-execution orchestration under only the parent's unused approval."""

    source_job = source_job.resolve()
    if not can_attempt_structural_resume(source_job):
        raise RuntimeError("the failed job is not eligible for structural automatic resume")
    remaining, actual = _remaining_approval(source_job)
    intake = IntakeRequest.model_validate_json(
        (source_job / "inputs" / "intake.json").read_text(encoding="utf-8")
    )
    requirements = RequirementsAnalysis.model_validate_json(
        (source_job / "inputs" / "requirements.json").read_text(encoding="utf-8")
    )
    sources = [
        InternalSource.model_validate(item)
        for item in json.loads((source_job / "inputs" / "sources.json").read_text(encoding="utf-8"))
    ]
    estimate = BudgetEnvelope.model_validate_json(
        (source_job / "inputs" / "budget_estimate.json").read_text(encoding="utf-8")
    )
    if remaining < estimate.minimum_cost_usd:
        raise RuntimeError("the unused original approval is below the minimum resumable budget")
    child = create_job(
        jobs_dir=jobs_dir,
        intake=intake,
        requirements=requirements,
        sources=sources,
        estimate=estimate,
        approved_usd=remaining,
        benchmark_variant=JobStore(source_job).read().benchmark_variant,
        embed_project_snapshot=False,
    )
    source_record = JobStore(source_job).read()
    fingerprint = hashlib.sha256(source_record.message.encode("utf-8")).hexdigest()[:16]
    plan = StructuralResumePlan(
        source_job_id=source_record.job_id,
        remaining_approved_usd=remaining,
        source_actual_usd=actual,
        failure_fingerprint=fingerprint,
    )
    manifest = {
        "schema_version": "onebrief-structural-continuation-v1",
        "source_job_id": source_record.job_id,
        "source_actual_usd": actual,
        "child_approved_usd": remaining,
        "aggregate_approval_ceiling_usd": actual + remaining,
        "authorization_kind": "remaining_parent_approval",
        "failure_fingerprint": fingerprint,
        "policy": "actual parent spend plus child approval cannot exceed the original approval",
    }
    work_dir = child / "work"
    work_dir.mkdir(parents=True, exist_ok=True)
    (work_dir / "continuation_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return child, plan


def can_attempt_bounded_repair_resume(job_dir: Path) -> bool:
    """Return true when a rejected code candidate can continue under unused approval."""

    try:
        record = JobStore(job_dir).read()
        remaining, _ = _remaining_approval(job_dir)
    except (OSError, ValueError):
        return False
    work = job_dir / "work"
    message = record.message.casefold()
    repairable_failure = (
        "development verification failed:" in message
        or "repeating an identical repair candidate" in message
        or "existing file was not included in approved model context" in message
        or (
            "compactproposedprojectcodechangeset" in message
            and (
                "invalid json: eof" in message
                or "provide one bounded new file" in message
                or "provide one catalog anchor" in message
                or "string should have at most 500 characters" in message
            )
        )
    )
    return (
        record.status in {JobStatus.FAILED, JobStatus.PARTIAL, JobStatus.NEEDS_AUTHORIZATION}
        and remaining > 0
        and repairable_failure
        and (work / "code_change_set.json").is_file()
        and (work / "development_verification_failure.txt").is_file()
    )


def create_bounded_repair_resume(
    source_job: Path, jobs_dir: Path
) -> tuple[Path, BoundedRepairResumePlan]:
    """Resume the best rejected candidate without repeating completed context stages."""

    source_job = source_job.resolve()
    if not can_attempt_bounded_repair_resume(source_job):
        raise RuntimeError("the failed job is not eligible for bounded repair resume")
    remaining, actual = _remaining_approval(source_job)
    inputs = source_job / "inputs"
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
    if remaining < estimate.minimum_cost_usd:
        raise RuntimeError("the unused original approval is below the minimum resumable budget")
    source_record = JobStore(source_job).read()
    child = create_job(
        jobs_dir=jobs_dir,
        intake=intake,
        requirements=requirements,
        sources=sources,
        estimate=estimate,
        approved_usd=remaining,
        benchmark_variant=source_record.benchmark_variant,
        embed_project_snapshot=False,
    )
    child_record = JobStore(child).read()
    source_work = source_job / "work"
    child_work = child / "work"
    child_work.mkdir(parents=True, exist_ok=True)
    reused: list[str] = []
    for name in (
        "project_architecture.json",
        "public_research.json",
        "public_research.md",
        "public_research_unavailable.json",
        "analysis.json",
    ):
        source = source_work / name
        if source.is_file():
            shutil.copy2(source, child_work / name)
            reused.append(name)
    candidate = (
        source_work / "development_best_candidate.json"
        if (source_work / "development_best_candidate.json").is_file()
        else source_work / "code_change_set.json"
    )
    failure = (
        source_work / "development_best_failure.txt"
        if (source_work / "development_best_failure.txt").is_file()
        else source_work / "development_verification_failure.txt"
    )
    shutil.copy2(candidate, child_work / "code_change_set.json")
    shutil.copy2(failure, child_work / "development_verification_failure.txt")
    reused.extend([candidate.name, failure.name])
    team_plans = sorted((source_work / "workspace" / "projects").glob(
        "*/02_plan_and_teams/team_plan.json"
    )) if (source_work / "workspace" / "projects").is_dir() else []
    if team_plans:
        team_plan = json.loads(team_plans[-1].read_text("utf-8"))
        team_plan["project_id"] = child_record.job_id
        target = (
            child_work / "workspace" / "projects" / child_record.job_id
            / "02_plan_and_teams" / "team_plan.json"
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(team_plan, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        reused.append("team_plan.json")
    parent_manifest_path = source_work / "continuation_manifest.json"
    parent_manifest = (
        json.loads(parent_manifest_path.read_text("utf-8"))
        if parent_manifest_path.is_file()
        else {}
    )
    ancestor_actual = float(parent_manifest.get("cumulative_actual_usd", 0.0))
    if not ancestor_actual:
        ancestor_actual = float(parent_manifest.get("source_actual_usd", 0.0))
    cumulative_actual = round(ancestor_actual + actual, 6)
    ceiling = float(parent_manifest.get(
        "aggregate_approval_ceiling_usd", round(cumulative_actual + remaining, 6)
    ))
    if cumulative_actual + remaining > ceiling + 0.000001:
        raise RuntimeError("bounded repair continuation exceeds the original aggregate approval")
    plan = BoundedRepairResumePlan(
        source_job_id=source_record.job_id,
        remaining_approved_usd=remaining,
        source_actual_usd=actual,
        cumulative_actual_usd=cumulative_actual,
        aggregate_approval_ceiling_usd=ceiling,
        reused_artifacts=reused,
    )
    manifest = {
        "schema_version": "onebrief-bounded-repair-continuation-v1",
        "source_job_id": source_record.job_id,
        "source_actual_usd": actual,
        "cumulative_actual_usd": cumulative_actual,
        "child_approved_usd": remaining,
        "aggregate_approval_ceiling_usd": ceiling,
        "authorization_kind": "remaining_parent_approval",
        "reused_artifacts": reused,
        "policy": "resume the rejected candidate; do not repeat completed context stages or exceed the original approval",
    }
    (child_work / "continuation_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return child, plan


def revalidate_failed_development(job_dir: Path, project_id: str) -> AutomaticResumePlan:
    """Re-run only trusted deterministic adapters; never call a model or edit the source."""
    job_dir = job_dir.resolve()
    if not can_attempt_automatic_resume(job_dir):
        raise RuntimeError("the failed job is not eligible for trusted automatic resume")
    change_path = job_dir / "work" / "code_change_set.json"
    change_set = ProjectCodeChangeSet.model_validate_json(change_path.read_text(encoding="utf-8"))
    registry_root = job_dir / "work" / "project_snapshot" / "registry"
    if not registry_root.is_dir():
        raise RuntimeError("the approved project snapshot registry is unavailable")
    output_dir = (
        job_dir / "work"
        / f"development_revalidation_{TRUSTED_REVALIDATION_VERSION}"
    )
    existing = output_dir / "development_run.json"
    if existing.is_file():
        run = DevelopmentRun.model_validate_json(existing.read_text(encoding="utf-8"))
        if run.status != "verified":
            raise RuntimeError("the existing trusted revalidation did not pass")
    else:
        pack = ApprovedProjectDevelopmentToolPack(project_id, registry_root)
        inspection, _ = pack.inspect(
            job_dir / "work" / "automatic_resume_inspection",
            focus_text="Trusted revalidation of the preserved development candidate.",
        )
        change_set = pack.bind_change_set_to_inspection(change_set, inspection)
        run = pack.apply_and_verify(
            change_set,
            output_dir,
            verification_goal="Revalidate the preserved candidate with the current trusted adapters.",
        )
    remaining, actual = _remaining_approval(job_dir)
    return AutomaticResumePlan(
        source_job_id=JobStore(job_dir).read().job_id,
        project_id=project_id,
        remaining_approved_usd=remaining,
        source_actual_usd=actual,
        revalidation_dir=str(output_dir),
        revalidated_change_sha256=_sha256(change_path),
    )


def seed_automatic_resume(source_job: Path, target_job: Path, plan: AutomaticResumePlan) -> Path:
    """Seed only immutable compatible work, mapping revalidation to normal development evidence."""
    source_work = source_job.resolve() / "work"
    target_work = target_job.resolve() / "work"
    copied: list[dict[str, str]] = []
    for name in (
        "project_architecture.json",
        "public_research.json",
        "public_research.md",
        "analysis.json",
        "code_change_set.json",
    ):
        source = source_work / name
        if not source.is_file():
            continue
        target = target_work / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        copied.append({"source": name, "target": name, "sha256": _sha256(target)})
    revalidation = Path(plan.revalidation_dir).resolve()
    if not revalidation.is_relative_to(source_work) or not revalidation.is_dir():
        raise PermissionError("trusted revalidation is outside the source job")
    development = target_work / "development"
    shutil.copytree(revalidation, development, dirs_exist_ok=True)
    independent_observations = source_work / "independent_observations"
    if independent_observations.is_dir():
        shutil.copytree(
            independent_observations,
            target_work / "independent_observations",
            dirs_exist_ok=True,
        )
    manifest = target_work / "automatic_resume.json"
    manifest.write_text(json.dumps({
        "schema_version": "onebrief-automatic-resume-v1",
        "source_job_id": plan.source_job_id,
        "project_id": plan.project_id,
        "source_actual_usd": plan.source_actual_usd,
        "child_approved_usd": plan.remaining_approved_usd,
        "revalidated_change_sha256": plan.revalidated_change_sha256,
        "copied_artifacts": copied,
        "rule": "The child approval equals only the unused portion of the original approval.",
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest
