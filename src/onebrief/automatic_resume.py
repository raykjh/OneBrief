"""Trusted local revalidation and budget-preserving continuation of failed work."""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

from pydantic import BaseModel, Field

from onebrief.budget_guard import BudgetStore, micros_to_dollars
from onebrief.development_progress import development_failure_quality
from onebrief.development_change_tracking import (
    discover_rejected_change_history,
    discover_rejected_change_fingerprints,
    write_rejected_change_history,
    write_rejected_change_fingerprints,
)
from onebrief.development_toolpack import DevelopmentRun
from onebrief.generic_development_toolpack import (
    ApprovedProjectDevelopmentToolPack,
    ProjectCodeChangeSet,
)
from onebrief.jobs import JobStatus, JobStore, create_job
from onebrief.schemas import BudgetEnvelope, IntakeRequest, InternalSource, RequirementsAnalysis

TRUSTED_REVALIDATION_VERSION = "web-observer-v8"

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


def _most_progressed_development_pair(work: Path) -> tuple[Path, Path]:
    """Choose the verifier-demonstrated checkpoint, including newer rounds.

    An explicit best checkpoint can become stale when the progress rank itself
    is corrected.  Comparing it with every complete candidate/failure pair
    makes continuation self-healing without trusting model-authored claims.
    """
    pairs: list[tuple[tuple[int, int], int, Path, Path]] = []
    best_candidate = work / "development_best_candidate.json"
    best_failure = work / "development_best_failure.txt"
    if best_candidate.is_file() and best_failure.is_file():
        pairs.append((
            development_failure_quality(best_failure.read_text("utf-8")),
            -1,
            best_candidate,
            best_failure,
        ))
    for candidate in work.glob("code_change_set_r*.json"):
        suffix = candidate.stem.removeprefix("code_change_set_r")
        if not suffix.isdigit():
            continue
        failure = work / f"development_verification_failure_r{suffix}.txt"
        if failure.is_file():
            pairs.append((
                development_failure_quality(failure.read_text("utf-8")),
                int(suffix),
                candidate,
                failure,
            ))
    if pairs:
        _quality, _round, candidate, failure = max(
            pairs, key=lambda item: (item[0], item[1])
        )
        return candidate, failure
    return (
        work / "code_change_set.json",
        work / "development_verification_failure.txt",
    )


def _trusted_semantic_failure_receipt(
    source_work: Path, candidate: Path, failure: Path
) -> dict[str, object] | None:
    """Bind a fresh semantic failure to the exact candidate Unity verified.

    A bounded continuation does not need to spend four minutes reproducing an
    unchanged local Unity import when the previous terminal job already holds a
    verified development run and an independent failed observation for the same
    serialized change set. Any new maker delta still runs the complete trusted
    ToolPack before it can progress.
    """

    if not candidate.is_file() or not failure.is_file():
        return None
    inherited = source_work / "trusted_reused_verification.json"
    if inherited.is_file():
        try:
            inherited_payload = json.loads(inherited.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, AttributeError):
            inherited_payload = {}
        if (
            inherited_payload.get("schema_version")
            == "onebrief-trusted-semantic-failure-reuse-v1"
            and inherited_payload.get("validator_version")
            == TRUSTED_REVALIDATION_VERSION
            and inherited_payload.get("candidate_sha256") == _sha256(candidate)
            and inherited_payload.get("failure_sha256") == _sha256(failure)
            and inherited_payload.get("base_head_sha")
        ):
            return {
                **inherited_payload,
                "inherited_receipt_sha256": _sha256(inherited),
                "guarantee": (
                    "The unchanged candidate and failure match the prior trusted receipt; "
                    "every new delta still requires complete ToolPack verification."
                ),
            }

    development_change = source_work / "development" / "change_set.json"
    development_run = source_work / "development" / "development_run.json"
    evidence_summary = (
        source_work / "development" / "unity_visual_evidence" / "summary.json"
    )
    observation = (
        source_work / "independent_observations" / "unity_ui_observation.json"
    )
    required = (
        development_change,
        development_run,
        evidence_summary,
        observation,
    )
    if not all(path.is_file() for path in required):
        return None
    failure_text = failure.read_text(encoding="utf-8", errors="replace")
    if not failure_text.casefold().startswith(
        "independent unity semantic visual observation failed"
    ):
        return None
    try:
        run_payload = json.loads(development_run.read_text(encoding="utf-8"))
        observation_payload = json.loads(observation.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, AttributeError):
        return None
    if run_payload.get("status") != "verified" or not run_payload.get("base_head_sha"):
        return None
    if (
        observation_payload.get("status") != "failed"
        or observation_payload.get("independent_from_maker") is not True
    ):
        return None
    candidate_sha = _sha256(candidate)
    if candidate_sha != _sha256(development_change):
        return None
    return {
        "schema_version": "onebrief-trusted-semantic-failure-reuse-v1",
        "validator_version": TRUSTED_REVALIDATION_VERSION,
        "candidate_sha256": candidate_sha,
        "development_run_sha256": _sha256(development_run),
        "evidence_summary_sha256": _sha256(evidence_summary),
        "observation_sha256": _sha256(observation),
        "failure_sha256": _sha256(failure),
        "base_head_sha": str(run_payload["base_head_sha"]),
        "guarantee": (
            "Only the unchanged rejected candidate skips duplicate preflight; "
            "every new delta still requires complete ToolPack verification."
        ),
    }


def _is_unity_evidence_topology_failure(value: str) -> bool:
    text = " ".join(value.split()).casefold()
    return any(marker in text for marker in (
        "unity visual test contract: add",
        "unity visual evidence requires a distinct rendered scenario",
        "responsive unity visual evidence must define and capture",
        "unity visual evidence reused an identical screenshot",
    ))


def _trusted_static_topology_failure_receipt(
    source_work: Path, candidate: Path, failure: Path
) -> dict[str, object] | None:
    """Bind a deterministic static evidence-topology failure to one candidate."""

    if not candidate.is_file() or not failure.is_file():
        return None
    failure_text = failure.read_text(encoding="utf-8", errors="replace")
    if not _is_unity_evidence_topology_failure(failure_text):
        return None
    provenance = source_work / "project_snapshot" / "restore_evidence.json"
    if not provenance.is_file():
        return None
    try:
        payload = json.loads(provenance.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, AttributeError):
        return None
    if (
        payload.get("status") != "verified_and_approved"
        or not payload.get("source_head_sha")
        or not payload.get("source_toolpack_sha256")
    ):
        return None
    return {
        "schema_version": "onebrief-trusted-static-topology-failure-reuse-v1",
        "validator_version": TRUSTED_REVALIDATION_VERSION,
        "candidate_sha256": _sha256(candidate),
        "failure_sha256": _sha256(failure),
        "provenance_sha256": _sha256(provenance),
        "base_head_sha": str(payload["source_head_sha"]),
        "toolpack_sha256": str(payload["source_toolpack_sha256"]),
        "guarantee": (
            "The unchanged candidate skips only the already-proven static topology failure; "
            "every new evidence delta still requires complete ToolPack verification."
        ),
    }
def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _remaining_approval(job_dir: Path) -> tuple[float, float]:
    ledger = BudgetStore(job_dir / "run").read()
    approved = ledger.approval.approved_usd_micros
    remaining = approved - ledger.actual_usd_micros - ledger.reserved_usd_micros
    return micros_to_dollars(max(remaining, 0)), micros_to_dollars(ledger.actual_usd_micros)


def _continuation_budget_estimate(
    estimate: BudgetEnvelope, remaining_approved_usd: float
) -> BudgetEnvelope:
    """Describe a bounded repair using only its inherited remaining cap.

    A repair can have less approval left than the original whole-pipeline
    minimum. Job creation must still succeed; the per-call gateway remains the
    final authority and requests more budget before any call that does not fit.
    """

    cap = max(float(remaining_approved_usd), 0.000001)
    minimum = min(float(estimate.minimum_cost_usd), cap)
    recommended = min(max(minimum, float(estimate.recommended_cost_usd)), cap)
    maximum = min(max(recommended, float(estimate.maximum_cost_usd)), cap)
    return estimate.model_copy(update={
        "minimum_cost_usd": minimum,
        "recommended_cost_usd": recommended,
        "maximum_cost_usd": maximum,
        "recommended_approval_usd": cap,
        "budget_limit_usd": cap,
        "notes": [
            *estimate.notes,
            "Continuation envelope is bounded by unused parent approval; per-call reservations remain authoritative.",
        ],
    })


def _trusted_seeded_development_dir(job_dir: Path) -> Path | None:
    """Return already-revalidated evidence only when its receipt still binds the candidate."""
    work = job_dir / "work"
    manifest_path = work / "automatic_resume.json"
    change_path = work / "code_change_set.json"
    run_path = work / "development" / "development_run.json"
    if not (manifest_path.is_file() and change_path.is_file() and run_path.is_file()):
        return None
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        run = DevelopmentRun.model_validate_json(run_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    if run.status != "verified":
        return None
    if manifest.get("revalidated_change_sha256") != _sha256(change_path):
        return None
    return run_path.parent


def _failed_semantic_observation(work: Path) -> tuple[Path, str] | None:
    path = work / "independent_observations" / "unity_ui_observation.json"
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("status") != "failed":
        return None
    findings = [str(item) for item in payload.get("findings", []) if str(item).strip()]
    return path, (
        "Independent Unity semantic visual observation failed. Repair the production UI, "
        "then regenerate and re-observe all requested desktop and mobile states.\n- "
        + "\n- ".join(findings)
    )


def can_attempt_automatic_resume(job_dir: Path) -> bool:
    """Return true for a rejected development result that retains budget and work."""
    try:
        record = JobStore(job_dir).read()
        remaining, _ = _remaining_approval(job_dir)
    except (OSError, ValueError):
        return False
    if record.status not in {
        JobStatus.FAILED,
        JobStatus.PARTIAL,
        JobStatus.NEEDS_INFORMATION,
    } or remaining <= 0:
        return False
    message = record.message.casefold()
    failure_path = job_dir / "work" / "development_verification_failure.txt"
    try:
        preserved_failure = failure_path.read_text(encoding="utf-8").casefold()
    except OSError:
        preserved_failure = ""
    validation_failure = any(token in message for token in (
        "web observation failed",
        "development verification failed",
        "development patch hygiene failed",
        "approved web observation",
        "independent observation capability was unavailable",
    )) or any(token in preserved_failure for token in (
        "web observation failed",
        "development verification failed",
        "development patch hygiene failed",
    ))
    verifier_transport_failure = (
        "verificationreport" in message
        and "invalid json" in message
        and _trusted_seeded_development_dir(job_dir) is not None
    )
    missing_seeded_observation = (
        _trusted_seeded_development_dir(job_dir) is not None
        and any(token in message for token in (
            "no independent semantic observer",
            "lacks visual evidence",
            "no visual evidence",
        ))
    )
    return (validation_failure or verifier_transport_failure or missing_seeded_observation) and (
        job_dir / "work" / "code_change_set.json"
    ).is_file()


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
    preserved_failure_path = work / "development_verification_failure.txt"
    try:
        preserved_failure = preserved_failure_path.read_text(encoding="utf-8").casefold()
    except OSError:
        preserved_failure = ""
    missing_system_observer = (
        record.status in {JobStatus.NEEDS_INFORMATION, JobStatus.FAILED}
        and (work / "development" / "unity_visual_evidence" / "summary.json").is_file()
        and (
            (
                any(marker in message for marker in (
                    "independent review",
                    "independent semantic observer",
                    "visual and layout criteria",
                ))
                and any(marker in message for marker in (
                    "not been performed",
                    "has not been performed",
                    "unavailable",
                    "missing",
                ))
            )
            or "generate_json_with_images" in message
            or "unitysemanticobservation" in message
        )
    )
    semantic_observation_failed = _failed_semantic_observation(work) is not None
    interrupted_unity_runtime = (
        "unity_playmode_visual_tests (exit_code=4294967295)" in preserved_failure
        and "test run completed" not in preserved_failure
    )
    repairable_failure = (
        "development verification failed:" in message
        or "development verification failed:" in preserved_failure
        or "development repair stalled" in message
        or "convergence progress gate stopped verification" in message
        or "convergence progress gate requires" in message
        or "repeating an identical repair candidate" in message
        or "existing file was not included in approved model context" in message
        or "exactrepairprojectcodechangeset" in message
        or "catalog anchor is not approved" in message
        or "edit anchors could not rediscover" in message
        or "independent unity semantic visual observation failed" in message
        or "independent unity semantic visual observation failed" in preserved_failure
        or "unity visual evidence requires" in message
        or "unity visual evidence requires" in preserved_failure
        or missing_system_observer
        or semantic_observation_failed
        or (
            "compactproposedprojectcodechangeset" in message
            and (
                "invalid json: eof" in message
                or "provide one bounded new file" in message
                or "a compact full-file repair must" in message
                or "provide one catalog anchor" in message
                or "string should have at most 500 characters" in message
            )
        )
    )
    return (
        record.status in {
            JobStatus.FAILED,
            JobStatus.PARTIAL,
            JobStatus.NEEDS_AUTHORIZATION,
            JobStatus.NEEDS_INFORMATION,
        }
        and remaining > 0
        and not (job_dir / ".bounded-repair-resume-claim.json").exists()
        and not interrupted_unity_runtime
        and repairable_failure
        and (work / "code_change_set.json").is_file()
        and (
            missing_system_observer
            or semantic_observation_failed
            or (work / "development_verification_failure.txt").is_file()
        )
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
    # Planning, context collection and the first candidate already completed.
    # Comparing a bounded repair with the whole-pipeline minimum incorrectly
    # blocks useful final repairs.  The per-call budget gateway still returns
    # NEEDS_BUDGET before any provider call that does not fit.
    source_record = JobStore(source_job).read()
    claim_path = source_job / ".bounded-repair-resume-claim.json"
    try:
        with claim_path.open("x", encoding="utf-8") as stream:
            json.dump({
                "schema_version": "onebrief-bounded-repair-resume-claim-v1",
                "status": "claimed",
            }, stream)
    except FileExistsError as exc:
        raise RuntimeError("this failed job already produced a bounded repair continuation") from exc
    try:
        child = create_job(
            jobs_dir=jobs_dir,
            intake=intake,
            requirements=requirements,
            sources=sources,
            estimate=_continuation_budget_estimate(estimate, remaining),
            approved_usd=remaining,
            benchmark_variant=source_record.benchmark_variant,
            embed_project_snapshot=False,
        )
    except Exception:
        # No child exists, so the idempotency claim can safely be retried.
        claim_path.unlink(missing_ok=True)
        raise
    child_record = JobStore(child).read()
    source_work = source_job / "work"
    child_work = child / "work"
    child_work.mkdir(parents=True, exist_ok=True)
    source_failure_path = source_work / "development_verification_failure.txt"
    source_failure_text = (
        source_failure_path.read_text(encoding="utf-8", errors="replace")
        if source_failure_path.is_file() else ""
    )
    topology_failure = _is_unity_evidence_topology_failure(source_failure_text)
    ledger_path = source_work / "convergence_ledger.json"
    try:
        source_ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
        ledger_contracts = source_ledger.get("repair_contracts", [])
        terminal_ledger_gate = bool(
            ledger_contracts
            and not bool(ledger_contracts[-1].get("execution_allowed"))
        )
    except (OSError, json.JSONDecodeError, AttributeError, TypeError):
        source_ledger = None
        terminal_ledger_gate = False
    reused: list[str] = []
    reusable_names = [
        "project_architecture.json",
        "public_research.json",
        "public_research.md",
        "public_research_unavailable.json",
        "analysis.json",
    ]
    if not topology_failure:
        if not terminal_ledger_gate:
            reusable_names.append("convergence_ledger.json")
        contract_path = source_work / "repair_contract.json"
        try:
            reusable_contract = bool(json.loads(
                contract_path.read_text(encoding="utf-8")
            ).get("execution_allowed"))
        except (OSError, json.JSONDecodeError, AttributeError):
            reusable_contract = False
        # A bounded continuation is itself a new, budget-bound execution
        # attempt. Reusing a terminal negative contract would block the
        # selected newer verifier checkpoint before it can be revalidated.
        # Keep executable contracts and the audit ledger; discard only a stale
        # non-executable gate.
        if reusable_contract:
            reusable_names.append("repair_contract.json")
    for name in reusable_names:
        source = source_work / name
        if source.is_file():
            shutil.copy2(source, child_work / name)
            reused.append(name)
    if (
        not topology_failure
        and terminal_ledger_gate
        and isinstance(source_ledger, dict)
    ):
        # The immutable parent remains the audit source for every denied
        # contract. A user-authorized continuation keeps its observations and
        # executable history, but a terminal denial cannot remain the active
        # child gate or it would block before the selected newer checkpoint is
        # revalidated.
        resumed_ledger = dict(source_ledger)
        resumed_ledger["repair_contracts"] = [
            item for item in source_ledger.get("repair_contracts", [])
            if isinstance(item, dict) and bool(item.get("execution_allowed"))
        ]
        (child_work / "convergence_ledger.json").write_text(
            json.dumps(resumed_ledger, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        reused.append("convergence_ledger.json")
    pending_candidates = [
        source_work / "development_pending_promotion.json",
        *sorted(
            source_work.glob("development_candidate_promotion_raw_r*.json"),
            key=lambda path: (
                int(path.stem.removeprefix("development_candidate_promotion_raw_r"))
                if path.stem.removeprefix("development_candidate_promotion_raw_r").isdigit()
                else -1
            ),
        ),
    ]
    pending_candidates = [path for path in pending_candidates if path.is_file()]
    if pending_candidates:
        pending = pending_candidates[-1]
        shutil.copy2(pending, child_work / "development_pending_promotion.json")
        reused.append(pending.name)
    system_observation_capability_missing = (
        source_record.status in {JobStatus.NEEDS_INFORMATION, JobStatus.FAILED}
        and (source_work / "development" / "unity_visual_evidence" / "summary.json").is_file()
        and (
            source_record.status == JobStatus.NEEDS_INFORMATION
            or "generate_json_with_images" in source_record.message.casefold()
            or "unitysemanticobservation" in source_record.message.casefold()
        )
    )
    semantic_observation_failure = _failed_semantic_observation(source_work)
    trusted_failure = None
    if semantic_observation_failure is not None:
        observation_path, observation_feedback = semantic_observation_failure
        candidate = source_work / "code_change_set.json"
        shutil.copy2(candidate, child_work / "code_change_set.json")
        (child_work / "development_verification_failure.txt").write_text(
            observation_feedback + "\n", encoding="utf-8"
        )
        reused.extend([candidate.name, observation_path.name, "semantic_observation_failure"])
        source_failure = source_work / "development_verification_failure.txt"
        if source_failure.is_file():
            trusted_failure = _trusted_semantic_failure_receipt(
                source_work, candidate, source_failure
            )
    elif system_observation_capability_missing:
        candidate = source_work / "code_change_set.json"
        shutil.copy2(candidate, child_work / "code_change_set.json")
        (child_work / "development_verification_failure.txt").write_text(
            "The latest runtime candidate reached Unity PlayMode but lacks an independent semantic "
            "visual PASS. Re-run it through the current trusted visual preflight and observer; repair "
            "any blank, clipped, unreadable, or non-responsive rendered UI evidence.\n",
            encoding="utf-8",
        )
        reused.extend([candidate.name, "system_observation_failure"])
    else:
        candidate, failure = _most_progressed_development_pair(source_work)
        shutil.copy2(candidate, child_work / "code_change_set.json")
        shutil.copy2(failure, child_work / "development_verification_failure.txt")
        reused.extend([candidate.name, failure.name])
        trusted_failure = _trusted_semantic_failure_receipt(
            source_work, candidate, failure
        )
        if trusted_failure is None:
            trusted_failure = _trusted_static_topology_failure_receipt(
                source_work, candidate, failure
            )
    rejected_fingerprints = discover_rejected_change_fingerprints(source_work)
    if rejected_fingerprints:
        write_rejected_change_fingerprints(child_work, rejected_fingerprints)
        reused.append("development_rejected_change_fingerprints.json")
        write_rejected_change_history(
            child_work, discover_rejected_change_history(source_work)
        )
        reused.append("development_rejected_change_history.json")
    if trusted_failure is not None:
        (child_work / "trusted_reused_verification.json").write_text(
            json.dumps(trusted_failure, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        reused.append("trusted_reused_verification.json")
    else:
        # A continuation can run under newer trusted validators than its parent.
        # Revalidate preserved candidates unless an exact, independent semantic
        # failure receipt above proves the unchanged state was just verified.
        revalidation_marker = {
            "schema_version": "onebrief-reverify-existing-candidate-v1",
            "validator_version": TRUSTED_REVALIDATION_VERSION,
            "source_job_id": source_record.job_id,
            "candidate_sha256": _sha256(child_work / "code_change_set.json"),
        }
        (child_work / "reverify_existing_candidate.json").write_text(
            json.dumps(revalidation_marker, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        reused.append("reverify_existing_candidate.json")
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
    claim_path.write_text(json.dumps({
        "schema_version": "onebrief-bounded-repair-resume-claim-v1",
        "status": "created",
        "child_job_id": child_record.job_id,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return child, plan


def revalidate_failed_development(job_dir: Path, project_id: str) -> AutomaticResumePlan:
    """Re-run only trusted deterministic adapters; never call a model or edit the source."""
    job_dir = job_dir.resolve()
    if not can_attempt_automatic_resume(job_dir):
        raise RuntimeError("the failed job is not eligible for trusted automatic resume")
    change_path = job_dir / "work" / "code_change_set.json"
    change_set = ProjectCodeChangeSet.model_validate_json(change_path.read_text(encoding="utf-8"))
    intake = IntakeRequest.model_validate_json(
        (job_dir / "inputs" / "intake.json").read_text(encoding="utf-8")
    )
    registry_root = job_dir / "work" / "project_snapshot" / "registry"
    output_dir = _trusted_seeded_development_dir(job_dir) or (
        job_dir / "work" / f"development_revalidation_{TRUSTED_REVALIDATION_VERSION}"
    )
    existing = output_dir / "development_run.json"
    if existing.is_file():
        run = DevelopmentRun.model_validate_json(existing.read_text(encoding="utf-8"))
        if run.status != "verified":
            raise RuntimeError("the existing trusted revalidation did not pass")
    else:
        # Older bounded continuations retain the cryptographic restore receipt
        # but not a second copy of the registry. In that case the coordinator
        # has already verified the current approved ToolPack hash and source
        # revision before entering this function; the pack itself rechecks a
        # clean root and exact approved HEAD before any disposable clone runs.
        pack = ApprovedProjectDevelopmentToolPack(
            project_id, registry_root if registry_root.is_dir() else None
        )
        inspection, _ = pack.inspect(
            job_dir / "work" / "automatic_resume_inspection",
            focus_text="Trusted revalidation of the preserved development candidate.",
        )
        change_set = pack.bind_change_set_to_inspection(change_set, inspection)
        run = pack.apply_and_verify(
            change_set,
            output_dir,
            verification_goal=(
                "Revalidate the preserved candidate with the current trusted adapters. "
                + intake.goal
            ),
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
