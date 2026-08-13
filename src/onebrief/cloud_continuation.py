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
from onebrief.agent_registry import ApprovedModel
from onebrief.budget_guard import BudgetStore, micros_to_dollars
from onebrief.cloud_jobs import GCSJobStore, upload_cloud_job
from onebrief.jobs import JobStatus, JobStore, create_job
from onebrief.phase_execution import FailureOwner, classify_failure_owner
from onebrief.producer import PRICES
from onebrief.schemas import (
    BudgetEnvelope,
    ExecutionPhase,
    IntakeRequest,
    InternalSource,
    PhaseBudgetEstimate,
    RequirementsAnalysis,
)
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


def _targeted_repair_estimate(
    estimate: BudgetEnvelope,
    *,
    low_cost_models: bool = False,
    failure_text: str = "",
) -> BudgetEnvelope:
    """Re-estimate one evidenced code repair without charging completed stages."""
    caps = {
        "long_form_draft": (40_000, 8_000),
        "independent_verification": (25_000, 1_000),
        "final_approval": (10_000, 500),
    }
    selected = []
    for stage in estimate.stages:
        if stage.stage not in caps:
            continue
        input_tokens, output_tokens = caps[stage.stage]
        model = "gemini-3.5-flash-lite" if low_cost_models else stage.model
        price = PRICES[model]
        per_call = (
            input_tokens * price.input_per_million
            + output_tokens * price.output_per_million
        ) / 1_000_000 + stage.fixed_cost_usd_per_call
        selected.append(stage.model_copy(update={
            "model": model,
            "input_tokens_per_call": input_tokens,
            "output_tokens_per_call": output_tokens,
            "minimum_calls": 1,
            "recommended_calls": 1,
            "maximum_calls": 1,
            "minimum_cost_usd": round(per_call, 6),
            "recommended_cost_usd": round(per_call, 6),
            "maximum_cost_usd": round(per_call, 6),
        }))
    if len(selected) != len(caps):
        raise RuntimeError("targeted repair estimate is missing a required model stage")
    base = sum(item.minimum_cost_usd for item in selected)
    maker_cost = next(
        item.minimum_cost_usd for item in selected
        if item.stage == "long_form_draft"
    )
    verification_cost = base - maker_cost
    owner = classify_failure_owner(
        context="development_verification",
        failure_text=failure_text or "targeted product repair",
    )
    repair_phase = (
        ExecutionPhase.EVIDENCE_CONSTRUCTION
        if owner == FailureOwner.EVIDENCE
        else ExecutionPhase.PRODUCT_IMPLEMENTATION
    )
    phase_budgets = [
        PhaseBudgetEstimate(
            phase=repair_phase,
            minimum_cost_usd=round(maker_cost * 1.10, 6),
            recommended_cost_usd=round(maker_cost * 1.20, 6),
            maximum_cost_usd=round(maker_cost * 1.25, 6),
            max_ai_repair_calls=1,
            max_deterministic_attempts=2,
            editable_scope=(
                ["tests and executable evidence harness; excludes product behavior"]
                if repair_phase == ExecutionPhase.EVIDENCE_CONSTRUCTION
                else ["product source; excludes tests, evidence, screenshots, and reports"]
            ),
        ),
        PhaseBudgetEstimate(
            phase=ExecutionPhase.FINAL_VERIFICATION,
            minimum_cost_usd=round(verification_cost * 1.10, 6),
            recommended_cost_usd=round(verification_cost * 1.20, 6),
            maximum_cost_usd=round(verification_cost * 1.25, 6),
            max_deterministic_attempts=2,
        ),
    ]
    return estimate.model_copy(update={
        "stages": selected,
        "minimum_cost_usd": round(base * 1.10, 4),
        "recommended_cost_usd": round(base * 1.20, 4),
        "maximum_cost_usd": round(base * 1.25, 4),
        "recommended_approval_usd": round(base * 1.20, 4),
        "estimated_minutes_minimum": 17,
        "estimated_minutes_recommended": 17,
        "estimated_minutes_maximum": 17,
        "notes": [
            *estimate.notes,
            "Targeted continuation charges only one evidenced maker repair, independent verification, and final approval.",
        ],
        "phase_budgets": phase_budgets,
    })


def create_budget_preserving_continuation(
    *,
    source_job_dir: Path,
    source_job_uri: str,
    jobs_dir: Path,
    reusable_source: GCSJobStore,
    supplemental_reusable_source: GCSJobStore | None = None,
    reusable_team_plan: TeamPlan | None = None,
    explicit_child_approval_usd: float | None = None,
    targeted_repair: bool = False,
    low_cost_targeted_repair: bool = False,
    reverify_existing_candidate: bool = False,
    research_reentry: bool = False,
    research_blocking_issues: list[str] | None = None,
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
        JobStatus.NEEDS_AUTHORIZATION,
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
    if remaining_micros <= 0 and explicit_child_approval_usd is None:
        raise RuntimeError("source approval has no unused budget")

    source_actual_usd = micros_to_dollars(source_ledger.actual_usd_micros)
    child_approved_usd = (
        float(explicit_child_approval_usd)
        if explicit_child_approval_usd is not None
        else micros_to_dollars(remaining_micros)
    )
    intake, requirements, sources, estimate = _read_models(source_job_dir)
    if targeted_repair:
        failure_path = source_job_dir / "work" / "development_verification_failure.txt"
        estimate = _targeted_repair_estimate(
            estimate,
            low_cost_models=low_cost_targeted_repair,
            failure_text=(
                failure_path.read_text(encoding="utf-8")
                if failure_path.is_file() else ""
            ),
        )
    if child_approved_usd < estimate.minimum_cost_usd:
        raise RuntimeError(
            "child approval is below the immutable minimum estimate; "
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
    if supplemental_reusable_source is not None:
        reused_items.extend(
            supplemental_reusable_source.download_reusable_artifacts(child_dir / "work")
        )
    if reusable_team_plan is not None:
        child_id = JobStore(child_dir).read().job_id
        plan = reusable_team_plan.model_copy(update={"project_id": child_id})
        if low_cost_targeted_repair:
            repair_owner_ids = {
                plan.stage_owners[stage]
                for stage in (
                    "long_form_draft",
                    "independent_verification",
                    "final_approval",
                )
            }
            plan = plan.model_copy(update={
                "members": [
                    member.model_copy(update={"model": ApprovedModel.GEMINI_3_5_FLASH_LITE})
                    if member.instance_id in repair_owner_ids
                    else member
                    for member in plan.members
                ]
            })
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
    if reverify_existing_candidate:
        _write_continuation_marker(
            child_dir,
            "reverify_existing_candidate.json",
            {"enabled": True},
        )
    if research_reentry:
        _write_continuation_marker(
            child_dir,
            "research_reentry_request.json",
            {
                "schema_version": "onebrief-research-reentry-request-v1",
                "reason": "verification found evidence that only the investigator can replace",
                "blocking_issues": research_blocking_issues or [],
                "max_refinement_calls": 2,
            },
        )
    from onebrief.lineage import ancestor_lineage_payload

    manifest = {
        "schema_version": "onebrief-cloud-continuation-v1",
        "source_job_uri": source_job_uri,
        "source_job_id": source_record.job_id,
        "source_run_id": source_record.run_id,
        "source_actual_usd": source_actual_usd,
        "child_approved_usd": child_approved_usd,
        "aggregate_approval_ceiling_usd": micros_to_dollars(
            source_ledger.approval.approved_usd_micros
        ) if explicit_child_approval_usd is None else child_approved_usd,
        "authorization_kind": (
            "remaining_parent_approval"
            if explicit_child_approval_usd is None
            else "explicit_additional_user_approval"
        ),
        "source_unused_usd_abandoned": (
            0.0
            if explicit_child_approval_usd is None
            else micros_to_dollars(max(0, remaining_micros))
        ),
        "targeted_repair": targeted_repair,
        "low_cost_targeted_repair": low_cost_targeted_repair,
        "reverify_existing_candidate": reverify_existing_candidate,
        "research_reentry": research_reentry,
        "reused_artifacts": list(reused),
        "ancestor_lineage": ancestor_lineage_payload(source_job_dir),
        "policy": (
            "actual parent spend plus child approval cannot exceed the original approval"
            if explicit_child_approval_usd is None
            else "child has a new explicit approval; unused parent approval is not transferred"
        ),
    }
    _write_continuation_marker(
        child_dir,
        "continuation_manifest.json",
        manifest,
    )
    return child_dir, source_actual_usd, child_approved_usd, reused


def _write_continuation_marker(
    child_dir: Path, name: str, payload: dict[str, object]
) -> None:
    """Write a resume marker where both legacy and milestone runners see it."""

    work = child_dir / "work"
    targets = [work]
    milestone_root = work / "milestones"
    if milestone_root.is_dir():
        for milestone_dir in sorted(path for path in milestone_root.iterdir() if path.is_dir()):
            if any((milestone_dir / candidate).is_file() for candidate in (
                "code_change_set.json",
                "development_best_candidate.json",
                "draft_r0.json",
            )):
                targets.append(milestone_dir)
    encoded = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    for target_dir in targets:
        target_dir.mkdir(parents=True, exist_ok=True)
        (target_dir / name).write_text(encoded, encoding="utf-8")


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


def _has_verification_failure(job_dir: Path) -> bool:
    return (job_dir / "work" / "development_verification_failure.txt").is_file()


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
    explicit_child_approval_usd: float | None = None,
    targeted_repair: bool = False,
    low_cost_targeted_repair: bool = False,
    reverify_existing_candidate: bool = False,
    research_reentry: bool = False,
    research_blocking_issues: list[str] | None = None,
) -> CloudContinuationReceipt:
    source_store = GCSJobStore(source_job_uri, client=storage_client)
    with tempfile.TemporaryDirectory(prefix="onebrief_cloud_resume_") as temp:
        temp_root = Path(temp)
        source_job_dir = source_store.download_job(temp_root / "source")
        reusable_plan = _find_team_plan(source_job_dir)
        reusable_artifact_store = source_store
        failure_artifact_store = source_store if _has_verification_failure(source_job_dir) else None
        has_code_candidate = _has_code_candidate(source_job_dir)
        ancestor_uri = source_job_uri
        visited = {ancestor_uri}
        depth = 0
        cursor = source_job_dir
        while (
            reusable_plan is None
            or not has_code_candidate
            or failure_artifact_store is None
        ) and depth < 8:
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
            if failure_artifact_store is None and _has_verification_failure(cursor):
                failure_artifact_store = ancestor_store
        child_dir, actual, approved, reused = create_budget_preserving_continuation(
            source_job_dir=source_job_dir,
            source_job_uri=source_job_uri,
            jobs_dir=jobs_dir,
            reusable_source=reusable_artifact_store,
            supplemental_reusable_source=failure_artifact_store or source_store,
            reusable_team_plan=reusable_plan,
            explicit_child_approval_usd=explicit_child_approval_usd,
            targeted_repair=targeted_repair,
            low_cost_targeted_repair=low_cost_targeted_repair,
            reverify_existing_candidate=reverify_existing_candidate,
            research_reentry=research_reentry,
            research_blocking_issues=research_blocking_issues,
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
    ceiling = approved if explicit_child_approval_usd is not None else actual + approved
    return CloudContinuationReceipt(
        source_job_uri=source_job_uri,
        child_job_uri=child_uri,
        source_actual_usd=actual,
        child_approved_usd=approved,
        aggregate_approval_ceiling_usd=ceiling,
        reused_artifacts=reused,
        dispatch=dispatch,
    )
