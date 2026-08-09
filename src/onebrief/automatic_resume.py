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
from onebrief.jobs import JobStatus, JobStore

TRUSTED_REVALIDATION_VERSION = "web-observer-v7"

class AutomaticResumePlan(BaseModel):
    source_job_id: str
    project_id: str
    remaining_approved_usd: float = Field(gt=0)
    source_actual_usd: float = Field(ge=0)
    revalidation_dir: str
    revalidated_change_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


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
        "approved web observation",
        "independent observation capability was unavailable",
    ))
    return validation_failure and (job_dir / "work" / "code_change_set.json").is_file()


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
        run = ApprovedProjectDevelopmentToolPack(
            project_id,
            registry_root,
        ).apply_and_verify(
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
