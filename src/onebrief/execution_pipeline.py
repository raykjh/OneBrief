"""Checkpointed and resumable analyst-writer-verifier-revision execution loop."""

from __future__ import annotations

import asyncio
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
import os
import re
import shutil
from pathlib import Path, PurePosixPath
from typing import TypeVar
from uuid import uuid4

from pydantic import BaseModel, ValidationError

from onebrief.budget_guard import BudgetExceeded, BudgetStore
from onebrief.adk_convergence import (
    MAKER_STATE_KEY,
    ROUND_STATE_KEY,
    SKIP_VERIFIER_STATE_KEY,
    VERIFICATION_STATE_KEY,
    REVERIFY_EXISTING_STATE_KEY,
    EXACT_EDIT_ANCHORS_STATE_KEY,
    MAKER_DIAGNOSTIC_CONTEXT_STATE_KEY,
    REPAIR_CONTRACT_STATE_KEY,
    REPAIR_PLAN_STATE_KEY,
    VERIFIER_CONTEXT_STATE_KEY,
    build_text_convergence_agent,
    run_convergence_agent,
)
from onebrief.completion_evidence import (
    apply_completion_evidence_override,
    apply_trusted_development_evidence,
    validate_completion_evidence,
)
from onebrief.deterministic_verification import (
    append_authoritative_csv_tables,
    DeterministicVerification,
    apply_deterministic_override,
    validate_draft_grounding,
)
from onebrief.evidence_sufficiency import (
    apply_evidence_sufficiency_override,
    research_reentry_issues,
    validate_evidence_sufficiency,
)
from onebrief.development_toolpack import (
    CodeChangeSet,
    DevelopmentRun,
    DevelopmentVerificationReceipt,
    ExchangeDevelopmentToolPack,
    RepositoryInspection,
)
from onebrief.development_progress import (
    development_failure_quality,
    should_repair_regression_candidate,
)
from onebrief.development_change_tracking import (
    development_change_fingerprint,
    development_change_strategy_fingerprint,
    discover_rejected_change_history,
    discover_rejected_change_fingerprints,
    write_rejected_change_history,
    write_rejected_change_fingerprints,
)
from onebrief.generic_development_toolpack import (
    AtomicUnityEvidenceBundle,
    AnchoredRangeRepairProjectCodeChangeSet,
    CatalogAnchoredProductRepair,
    CatalogAnchoredEvidenceRepair,
    catalog_bound_evidence_repair_schema,
    catalog_bound_product_repair_schema,
    ApprovedProjectDevelopmentToolPack,
    CompactProposedProjectCodeChangeSet,
    ExactRepairProjectCodeChangeSet,
    ProjectCodeChangeSet,
    ProposedProjectCodeChangeSet,
    UnityEvidenceAssemblyRepair,
    UnityEvidenceSourceRepair,
    UnityEvidenceAnchoredSourceRepair,
    UnityRenderTextureEvidenceRepair,
)
from onebrief.greenfield_web_toolpack import GreenfieldWebDevelopmentToolPack
from onebrief.dynamic_role_agents import DynamicRoleAgent, GovernanceAgent, GovernanceDecision, RoleHandoff
from onebrief.execution_agents import (
    AnalystAgent,
    DeveloperAgent,
    VerifierAgent,
    WriterAgent,
)
from onebrief.execution_limits import DEVELOPER_OUTPUT_CAP, VERIFIER_OUTPUT_CAP, WRITER_OUTPUT_CAP
from onebrief.execution_graph import ExecutionGraph, ExecutionGraphRuntime, NodeStatus
from onebrief.execution_schemas import (
    AnalysisPackage,
    CriterionCheck,
    DraftArtifact,
    ExecutionCheckpoint,
    PipelineStatus,
    VerificationReport,
    Verdict,
)
from onebrief.handoff_protocol import (
    ArtifactReference,
    EvidenceKind,
    EvidenceStatus,
    accept_work_handoff,
    canonical_sha256 as handoff_sha256,
    create_evidence_binding,
    create_work_handoff,
    verify_handoff_receipt,
)
from onebrief.completion_ledger import refresh_completion_ledger, settle_consistent_verification
from onebrief.convergence_policy import (
    CURRENT_CONVERGENCE_POLICY_REVISION,
    ConvergenceLedger,
    ConvergencePolicy,
    RepairContract,
    new_convergence_ledger,
    repair_contract_blocks_resume,
)
from onebrief.execution_profile import (
    compact_work_contract,
    effective_revision_rounds,
    is_small_document_task,
    requires_full_csv_preservation,
)
from onebrief.guarded_gemini import BudgetedGeminiClient
from onebrief.grounded_search import run_grounded_research
from onebrief.public_research import merge_public_sources
from onebrief.recovery_policy import RecoveryAction, RecoveryDecision, RecoveryPolicy
from onebrief.repair_planning import RepairPlan, build_repair_plan
from onebrief.reality_check import apply_reality_check_override, evaluate_reality_check
from onebrief.requirements_gate import require_ready_for_estimate
from onebrief.schemas import (
    ExecutionPhase,
    IntakeRequest,
    InternalSource,
    OutputTarget,
    RequirementsAnalysis,
    SourcePriority,
    ToolPackId,
)
from onebrief.public_research import PublicResearchResult
from onebrief.phase_execution import (
    PHASE_DECISION_STATE_KEY,
    PHASE_STATE_KEY,
    PhaseDecision,
    active_execution_phase,
    build_evidence_specification,
    decide_repair_phase,
    is_evidence_path,
    path_allowed_for_phase,
    phase_stage,
)
from onebrief.toolpacks import execute_toolpacks
from onebrief.unity_semantic_observation import (
    contract_requires_strict_visual_quality,
    observe_unity_visual_evidence,
    semantic_observation_contract,
)
from onebrief.unity_layout_diagnostics import compact_unity_layout_diagnostic_context
from onebrief.unity_evidence_plan import (
    UnityEvidenceJourneyPlan,
    render_unity_evidence_journey,
)
from onebrief.workbook_export import export_workbook
from onebrief.temperament import (
    VERIFIER_PROFILE,
    WRITER_PROFILE,
    enforce_temperament_audit,
)

T = TypeVar("T", bound=BaseModel)


def quest_initial_execution_phase(sources: list[InternalSource]) -> ExecutionPhase:
    """Read the digest-bound starting authority from the active Quest source."""

    matches = [
        source for source in sources
        if source.name.startswith("onebrief-active-quest-")
        and source.media_type == "application/json"
    ]
    if not matches:
        return ExecutionPhase.PRODUCT_IMPLEMENTATION
    if len(matches) != 1:
        raise RuntimeError("exactly one active Quest authority source is required")
    try:
        payload = json.loads(matches[0].content)
        return ExecutionPhase(payload["initial_execution_phase"])
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("active Quest has an invalid initial execution phase") from exc


def approved_runtime_authority_source(
    development_pack: ApprovedProjectDevelopmentToolPack,
) -> InternalSource | None:
    """Project the approved non-secret runtime fixture into maker context."""

    profile = development_pack._profile()
    bindings = [
        item.model_dump(mode="json")
        for item in profile.runtime_arguments
        if item.authentication_selector and item.authentication_submit
    ]
    if not bindings:
        return None
    payload = {
        "schema_version": "onebrief-approved-runtime-authority-v1",
        "toolpack_sha256": profile.sha256,
        "instruction": (
            "For protected-destination evidence, use each authentication_selector "
            "before its paired authentication_submit exactly as ordered. Do not substitute "
            "an onboarding or generic Start path."
        ),
        "bindings": bindings,
    }
    content = json.dumps(payload, ensure_ascii=False, indent=2)
    encoded = content.encode("utf-8")
    return InternalSource(
        name="onebrief-approved-runtime-authority.json",
        priority=SourcePriority.MANDATORY,
        requirement_keys=["approved_runtime_authority"],
        summary="Digest-bound non-secret runtime arguments and authentication controls.",
        content=content,
        media_type="application/json",
        size_bytes=len(encoded),
        sha256=hashlib.sha256(encoded).hexdigest(),
    )


def development_proposal_changes(raw: object) -> list[object]:
    """Return proposal changes from either ADK dicts or Pydantic models.

    Structured ADK output is not guaranteed to stay a model instance between
    callbacks.  Phase authority must therefore inspect the serialized form as
    strictly as the model form; treating a dict as an empty proposal silently
    bypasses the product/evidence boundary.
    """

    if isinstance(raw, dict):
        changes = raw.get("changes", [])
    else:
        changes = getattr(raw, "changes", [])
    return list(changes) if isinstance(changes, (list, tuple)) else []


def development_change_path(change: object) -> str:
    if isinstance(change, dict):
        value = change.get("path", "")
    else:
        value = getattr(change, "path", "")
    return str(value or "").replace("\\", "/")


def filter_development_proposal_for_phase(
    raw: object,
    phase: ExecutionPhase,
) -> tuple[object, list[str]]:
    """Retain only changes owned by the active execution phase."""

    changes = development_proposal_changes(raw)
    allowed = [
        change for change in changes
        if path_allowed_for_phase(development_change_path(change), phase)
    ]
    deferred = [
        development_change_path(change)
        for change in changes if change not in allowed
    ]
    if isinstance(raw, dict):
        return {**raw, "changes": allowed}, deferred
    model_copy = getattr(raw, "model_copy", None)
    if not callable(model_copy):
        raise TypeError("development proposal cannot be phase-filtered")
    return model_copy(update={"changes": allowed}), deferred


def phase_owned_handoff_paths(
    *,
    phase: ExecutionPhase,
    proposed_paths: list[str],
    previous_change_set: object | None,
) -> list[str]:
    """Never bind a recipient to a path forbidden by its execution phase.

    A verifier observes the failing proof path, but a product owner must not
    inherit that path as edit authority. An empty product path list means the
    maker may select the smallest production path inside the already approved
    ToolPack scope; it never means tests are permitted.
    """

    owned = [
        path.replace("\\", "/") for path in proposed_paths
        if path_allowed_for_phase(path, phase)
    ]
    if owned or phase != ExecutionPhase.EVIDENCE_CONSTRUCTION:
        return list(dict.fromkeys(owned))
    return list(dict.fromkeys(
        development_change_path(change)
        for change in development_proposal_changes(previous_change_set)
        if is_evidence_path(development_change_path(change))
    ))


def paths_outside_active_repair_contract(
    proposed_paths: list[str],
    contract: RepairContract | None,
    *,
    reverify_existing: bool,
) -> list[str]:
    """Return only newly proposed paths that exceed the active repair contract.

    A continuation may restore a cumulative candidate containing product and
    evidence changes from earlier, separately authorized phases.  A verifier-
    only replay does not propose those files again, so applying the current
    one-surface repair contract to the whole restored candidate produces a
    false authority expansion.  Fresh maker turns remain fail-closed.
    """

    if reverify_existing or contract is None or not contract.permitted_paths:
        return []
    permitted = {
        path.replace("\\", "/").casefold()
        for path in contract.permitted_paths
    }
    normalized_proposed = [
        path.replace("\\", "/").strip("/").casefold()
        for path in proposed_paths
    ]
    evidence_members = [
        path for path in normalized_proposed
        if unity_evidence_contract_target_allowed(path)
    ]
    if (
        len(normalized_proposed) == 2
        and len(evidence_members) == 2
        and {PurePosixPath(path).suffix for path in evidence_members} == {".cs", ".asmdef"}
        and len({PurePosixPath(path).parent for path in evidence_members}) == 1
        and any(path in permitted for path in evidence_members)
    ):
        # The trusted declarative journey compiler emits these siblings together.
        # Treating the asmdef as a separate authority expansion makes the generated
        # test impossible to discover and caused a safe but unnecessary stop.
        permitted.update(evidence_members)
    return [
        path for path in proposed_paths
        if path.replace("\\", "/").casefold() not in permitted
    ]


def visual_repair_production_target_allowed(path: str) -> bool:
    """Fail closed when a rendered-product defect points at proof instead of product code."""

    normalized = path.replace("\\", "/").strip("/").casefold()
    if not normalized:
        return False
    parts = [part for part in normalized.split("/") if part]
    blocked_parts = {
        "test",
        "tests",
        "testing",
        "testresults",
        "evidence",
        "screenshots",
        "observations",
        "independent_observations",
        "reports",
        "coverage",
        "logs",
    }
    if any(part in blocked_parts for part in parts):
        return False
    filename = parts[-1]
    stem = filename.rsplit(".", 1)[0]
    if (
        filename.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".log"))
        or stem.endswith(("test", "tests", "spec"))
        or filename.startswith(("test_", "spec_"))
        or ".test." in filename
        or ".spec." in filename
        or any(marker in stem for marker in (
            "runtime-evidence",
            "runtime_evidence",
            "screenshot",
            "observation_receipt",
        ))
    ):
        return False
    return True


_PRODUCT_REPAIR_SOURCE_STOP_WORDS = {
    "after", "before", "could", "expected", "failed", "failure", "false",
    "from", "must", "onebrief", "should", "tests", "test", "true", "unity",
    "visual", "with", "without",
}


def relevant_product_repair_sources(
    sources: list[dict[str, object]],
    failure_text: str,
    *,
    limit: int = 6,
) -> list[dict[str, object]]:
    """Select a bounded product-source working set for a proven runtime defect.

    A runtime assertion normally names behavior (for example login, lobby, or a
    clicked control) rather than the file that owns it.  Reusing only paths from
    the previous candidate hides the actual production surface and encourages a
    maker to edit the test or emit a whole-project blob.  This deterministic
    selector ranks already-approved repository context by failure terms and
    exposes only a small, phase-safe set.  It grants no new path authority.
    """

    if limit <= 0:
        return []
    expanded = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", failure_text)
    terms = {
        term.casefold()
        for term in re.findall(r"[A-Za-z가-힣][A-Za-z0-9_가-힣]{3,}", expanded)
        if term.casefold() not in _PRODUCT_REPAIR_SOURCE_STOP_WORDS
    }
    if not terms:
        return []

    ranked: list[tuple[int, int, str, dict[str, object]]] = []
    for source in sources:
        path = str(source.get("repository_path", "")).replace("\\", "/")
        content = str(source.get("content", ""))
        if not path or not content or not visual_repair_production_target_allowed(path):
            continue
        path_folded = path.casefold()
        content_folded = content.casefold()
        path_hits = sum(1 for term in terms if term in path_folded)
        content_hits = sum(min(3, content_folded.count(term)) for term in terms)
        score = path_hits * 12 + content_hits
        if score <= 0:
            continue
        ranked.append((score, path_hits, path_folded, dict(source)))
    ranked.sort(key=lambda item: (-item[0], -item[1], item[2]))
    return [item[3] for item in ranked[:limit]]


def product_failure_edit_anchors(
    sources: list[dict[str, object]],
    failure_text: str,
    *,
    source_limit: int = 4,
    anchors_per_source: int = 4,
) -> list[dict[str, object]]:
    """Create digest-bound source windows for one runtime product repair."""

    selected_sources = relevant_product_repair_sources(
        sources, failure_text, limit=source_limit
    )
    expanded = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", failure_text)
    terms = {
        term.casefold()
        for term in re.findall(r"[A-Za-z가-힣][A-Za-z0-9_가-힣]{3,}", expanded)
        if term.casefold() not in _PRODUCT_REPAIR_SOURCE_STOP_WORDS
    }
    groups: list[dict[str, object]] = []
    for source in selected_sources:
        path = str(source.get("repository_path", "")).replace("\\", "/")
        content = str(source.get("content", ""))
        lines = content.splitlines()
        scored: list[tuple[int, int]] = []
        for index, line in enumerate(lines):
            folded = line.casefold()
            hits = sum(1 for term in terms if term in folded)
            if not hits:
                continue
            declaration_bonus = 6 if re.search(
                r"\b(class|struct|interface|void|public|private|protected|async|function|def)\b",
                line,
            ) else 0
            scored.append((hits * 4 + declaration_bonus, index))
        anchors: list[dict[str, object]] = []
        used: set[tuple[int, int]] = set()
        for _score, index in sorted(scored, key=lambda item: (-item[0], item[1])):
            start = max(0, index - 4)
            end = min(len(lines), index + 5)
            span = (start, end)
            if span in used:
                continue
            used.add(span)
            window = "\n".join(lines[start:end])
            if not window or content.count(window) != 1:
                continue
            anchors.append({
                "anchor_id": "A" + hashlib.sha256(
                    (path + "\0" + window).encode("utf-8")
                ).hexdigest()[:12],
                "start_line": start + 1,
                "end_line": end,
                "text": window,
            })
            if len(anchors) >= anchors_per_source:
                break
        if anchors:
            groups.append({"path": path, "anchors": anchors})
    return groups


def active_exact_edit_anchors(
    current: list[dict[str, object]], state_value: object,
) -> list[dict[str, object]]:
    """Adopt only a structurally valid verifier-issued anchor catalog."""

    if not isinstance(state_value, list):
        return current
    validated: list[dict[str, object]] = []
    for group in state_value:
        if not isinstance(group, dict) or not isinstance(group.get("path"), str):
            return current
        anchors = group.get("anchors")
        if not isinstance(anchors, list) or not anchors:
            return current
        valid_anchors: list[dict[str, object]] = []
        for anchor in anchors:
            if (
                not isinstance(anchor, dict)
                or not re.fullmatch(r"A[0-9a-f]{12}", str(anchor.get("anchor_id", "")))
                or not isinstance(anchor.get("text"), str)
                or not str(anchor.get("text", ""))
            ):
                return current
            valid_anchors.append(dict(anchor))
        validated.append({"path": str(group["path"]), "anchors": valid_anchors})
    return validated or current


def candidate_first_edit_anchors(
    candidate_catalog: list[dict[str, object]],
    source_catalog: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Prefer windows from the current candidate over stale HEAD windows."""

    candidate_paths = {
        str(group.get("path", "")) for group in candidate_catalog
    }
    return [
        *candidate_catalog,
        *(
            group for group in source_catalog
            if str(group.get("path", "")) not in candidate_paths
        ),
    ]


def unity_evidence_contract_target_allowed(path: str) -> bool:
    """Allow evidence-topology repairs only in the executed PlayMode harness."""

    normalized = path.replace("\\", "/").strip("/").casefold()
    return (
        "/tests/playmode/" in f"/{normalized}"
        and normalized.endswith((".cs", ".asmdef"))
    )


def is_missing_unity_evidence_harness(feedback: str) -> bool:
    normalized = " ".join(feedback.split()).casefold()
    return any(marker in normalized for marker in (
        "add a discoverable unity playmode test",
        "add a unity test .asmdef",
    ))


def requires_atomic_unity_evidence_pair(feedback: str) -> bool:
    normalized = " ".join(feedback.split()).casefold()
    return (
        "add a discoverable unity playmode test" in normalized
        and "add a unity test .asmdef" in normalized
    )


def missing_unity_evidence_bundle_paths(
    feedback: str,
    *change_sets: object | None,
) -> list[str]:
    """Return missing members of an indivisible Unity evidence harness.

    A PlayMode source without its test-only assembly cannot execute, while an
    asmdef without a discoverable test proves nothing. When trusted feedback
    requests both, never retain either half as platform progress.
    """

    normalized = " ".join(feedback.split()).casefold()
    required: list[tuple[str, str]] = []
    if "add a discoverable unity playmode test" in normalized:
        required.append(("PlayMode test source", ".cs"))
    if "add a unity test .asmdef" in normalized:
        required.append(("test assembly definition", ".asmdef"))
    if not required:
        return []

    paths = {
        development_change_path(change).casefold()
        for change_set in change_sets
        if change_set is not None
        for change in development_proposal_changes(change_set)
    }
    playmode_paths = {
        path for path in paths if "/tests/playmode/" in f"/{path}"
    }
    missing: list[str] = []
    for label, suffix in required:
        if not any(path.endswith(suffix) for path in playmode_paths):
            missing.append(label)
    return missing


def _existing_unity_evidence_paths(
    current_payload: object | None,
) -> tuple[str | None, str | None]:
    test_path: str | None = None
    assembly_path: str | None = None
    for change in development_proposal_changes(current_payload):
        path = development_change_path(change).replace("\\", "/")
        if "/tests/playmode/" not in f"/{path.casefold()}":
            continue
        suffix = PurePosixPath(path).suffix.casefold()
        if suffix == ".cs" and test_path is None:
            test_path = path
        elif suffix == ".asmdef" and assembly_path is None:
            assembly_path = path
    return test_path, assembly_path


def approved_unity_evidence_bundle_bindings(development_pack: object) -> dict[str, str]:
    """Bind one previously committed trusted journey bundle to the active HEAD."""

    _profile, head = development_pack._validate_root()
    tracked = [
        path.replace("\\", "/")
        for path in development_pack._git("ls-files").splitlines()
    ]
    by_directory: dict[str, dict[str, str]] = {}
    for path in tracked:
        pure = PurePosixPath(path)
        if "/tests/playmode/" not in f"/{path.casefold()}":
            continue
        name = pure.name.casefold()
        kind = (
            "test"
            if name == "onebriefgeneratedjourneytest.cs"
            else "assembly"
            if name == "onebrief.generated.visual.tests.asmdef"
            else None
        )
        if kind is not None:
            by_directory.setdefault(str(pure.parent), {})[kind] = path
    complete = [
        paths for paths in by_directory.values()
        if set(paths) == {"test", "assembly"}
    ]
    if len(complete) != 1:
        return {}
    return {
        path: hashlib.sha256(development_pack._blob(head, path)).hexdigest()
        for path in complete[0].values()
    }


def normalize_atomic_unity_evidence_bundle(
    raw: object,
    current_payload: object | None = None,
    approved_existing_evidence: dict[str, str] | None = None,
) -> object:
    """Restore the typed atomic contract after ADK state serialization.

    ADK validates the provider response with the selected Pydantic schema, then
    stores its JSON-safe dictionary in session state.  Pipeline hooks therefore
    must rehydrate the exact schema before applying the indivisible pair.
    """

    serialized_journey = bool(
        isinstance(raw, dict)
        and isinstance(raw.get("steps"), list)
        and isinstance(raw.get("test_directory"), str)
        and not raw.get("changes")
    )
    if isinstance(raw, UnityEvidenceJourneyPlan) or serialized_journey:
        plan = (
            raw if isinstance(raw, UnityEvidenceJourneyPlan)
            else UnityEvidenceJourneyPlan.model_validate({
                **raw,
                "schema_version": "onebrief-unity-evidence-journey-plan-v1",
            })
        )
        existing_test, existing_assembly = _existing_unity_evidence_paths(
            current_payload
        )
        approved_paths = approved_existing_evidence or {}
        if existing_test is None:
            existing_test = next(
                (path for path in approved_paths if path.casefold().endswith(".cs")),
                None,
            )
        if existing_assembly is None:
            existing_assembly = next(
                (path for path in approved_paths if path.casefold().endswith(".asmdef")),
                None,
            )
        rendered = render_unity_evidence_journey(
            plan,
            existing_test_path=existing_test,
            existing_assembly_path=existing_assembly,
        )
        # The provider authored only the bounded plan above. The C# and asmdef
        # below are trusted compiler output, so they must not be forced back
        # through the 8 KiB provider-transport schema. Longer valid journeys
        # still pass through path approval and normal ProjectCodeChangeSet
        # binding before isolated execution.
        return ProjectCodeChangeSet(
            summary=plan.summary,
            changes=[
                {
                    "path": rendered.playmode_test_path,
                    "base_sha256": approved_paths.get(rendered.playmode_test_path),
                    "content": rendered.playmode_test_source,
                    "reason": "Trusted OneBrief compilation of the declarative Unity journey.",
                },
                {
                    "path": rendered.test_assembly_path,
                    "base_sha256": approved_paths.get(rendered.test_assembly_path),
                    "content": rendered.test_assembly_source,
                    "reason": "Trusted sibling TestAssemblies wrapper for the generated journey.",
                },
            ],
        )
    if isinstance(raw, AtomicUnityEvidenceBundle):
        return raw.as_change_set()
    if isinstance(raw, dict) and (
        raw.get("schema_version") == "onebrief-atomic-unity-evidence-bundle-v1"
        or {"playmode_test", "test_assembly"}.issubset(raw)
    ):
        return AtomicUnityEvidenceBundle.model_validate(raw).as_change_set()
    if isinstance(raw, UnityEvidenceSourceRepair):
        return raw.as_change_set()
    if isinstance(raw, UnityEvidenceAssemblyRepair):
        return raw.as_change_set()
    if isinstance(raw, dict) and (
        raw.get("schema_version") == "onebrief-unity-evidence-assembly-repair-v1"
        or (
            "test_assembly" in raw
            and "playmode_test" not in raw
            and "changes" not in raw
        )
    ):
        return UnityEvidenceAssemblyRepair.model_validate(raw).as_change_set()
    if isinstance(raw, dict) and (
        raw.get("schema_version") == "onebrief-unity-evidence-source-repair-v1"
        or (
            "playmode_test" in raw
            and "test_assembly" not in raw
            and "changes" not in raw
        )
    ):
        return UnityEvidenceSourceRepair.model_validate(raw).as_change_set()
    return raw


def development_maker_schema_for(
    report: VerificationReport | None,
    current_payload: object | None,
    exact_edit_anchors: list[dict[str, object]] | None = None,
    active_phase: ExecutionPhase | None = None,
) -> type | None:
    """Select the next bounded software response contract from current state."""

    if report is None or report.verdict != Verdict.REVISE:
        return None
    feedback = " | ".join([
        *report.blocking_issues,
        *report.revision_instructions,
    ])
    normalized_feedback = " ".join(feedback.split()).casefold()
    if (
        active_phase == ExecutionPhase.EVIDENCE_CONSTRUCTION
        and (
            is_unity_evidence_contract_feedback(feedback)
            or is_missing_unity_evidence_harness(feedback)
            or "unity_playmode_visual_tests" in normalized_feedback
        )
    ):
        return UnityEvidenceJourneyPlan
    if (
        "unity_compile" in normalized_feedback
        and "tests\\playmode\\" in normalized_feedback
        and "error cs0246" in normalized_feedback
    ):
        return UnityEvidenceAssemblyRepair
    current_candidate = None
    if current_payload is not None:
        try:
            current_candidate = ProjectCodeChangeSet.model_validate(current_payload)
        except (ValidationError, ValueError):
            current_candidate = None
    if (
        requires_atomic_unity_evidence_pair(feedback)
        and missing_unity_evidence_bundle_paths(feedback, current_candidate)
    ):
        return UnityEvidenceJourneyPlan
    if (
        active_phase != ExecutionPhase.EVIDENCE_CONSTRUCTION
        and is_development_product_target_failure(feedback)
    ):
        if exact_edit_anchors:
            return catalog_bound_product_repair_schema(exact_edit_anchors)
        return ExactRepairProjectCodeChangeSet
    if (
        exact_edit_anchors
        and any(marker in normalized_feedback for marker in (
            "existing file was not included in approved model context",
            "edit anchors could not rediscover one approved source range",
        ))
    ):
        if active_phase == ExecutionPhase.EVIDENCE_CONSTRUCTION:
            return catalog_bound_evidence_repair_schema(exact_edit_anchors)
        return catalog_bound_product_repair_schema(exact_edit_anchors)
    requested_pair = (
        "Unity visual test contract: add a discoverable Unity PlayMode test | "
        "Unity visual test contract: add a Unity test .asmdef"
    )
    if (
        is_unity_evidence_contract_feedback(feedback)
        and current_candidate is not None
        and not missing_unity_evidence_bundle_paths(requested_pair, current_candidate)
    ):
        if (
            "screenspaceoverlay" in normalized_feedback
            and "requested viewport width and height" in normalized_feedback
        ):
            return UnityRenderTextureEvidenceRepair
        return UnityEvidenceAnchoredSourceRepair
    if active_phase == ExecutionPhase.EVIDENCE_CONSTRUCTION:
        if exact_edit_anchors:
            return catalog_bound_evidence_repair_schema(exact_edit_anchors)
        return UnityEvidenceJourneyPlan
    # Dynamic schemas persist on the same LlmAgent. Explicitly restore the
    # normal bounded repair contract after the atomic pair has been created.
    return CompactProposedProjectCodeChangeSet


def is_unity_evidence_contract_feedback(feedback: str) -> bool:
    normalized = " ".join(feedback.split()).casefold()
    executable_evidence_failure = (
        "unity_playmode_visual_tests" in normalized
        and "onebrief.visual" in normalized
    )
    return executable_evidence_failure or "unity visual test contract:" in normalized or any(marker in normalized for marker in (
        "unity visual evidence requires a distinct rendered scenario",
        "responsive unity visual evidence must define and capture",
        "unity visual evidence reused an identical screenshot",
        "unity visual evidence did not exercise requested locale",
        "locale changed_visible_text_count must compare before/after visible text snapshots",
        "identical duplicate local ui-control declaration",
    ))


def is_unity_localization_product_failure(feedback: str) -> bool:
    """Distinguish a proven unchanged locale surface from missing proof metadata."""

    normalized = " ".join(feedback.split()).casefold()
    return (
        "unity visual scenario" in normalized
        and "did not visibly change any text" in normalized
    )


def is_development_product_target_failure(feedback: str) -> bool:
    """Return true when trusted evidence rejects the edited production target.

    Evidence and product defects can be reported together. A proven detached
    or semantically ineffective production edit must take precedence over
    further proof-harness refinement; otherwise the same maker is trapped on
    evidence code that can never make the product criterion pass.
    """

    normalized = " ".join(feedback.split()).casefold()
    explicit_product_failure = is_unity_localization_product_failure(feedback) or (
        "changed unity ui monobehaviour" in normalized
        and "is not reachable" in normalized
        and "active component" in normalized
    )
    if explicit_product_failure:
        return True
    # Reuse the authoritative phase classifier for executable runtime failures.
    # Unknown prose is deliberately excluded: only a typed runtime/semantic
    # failure already owned by product may switch the maker to a bounded product
    # repair schema.
    decision = decide_repair_phase(
        context="development_acceptance_verification",
        failure_text=feedback,
        round_number=0,
    )
    return (
        decision.next_phase == ExecutionPhase.PRODUCT_IMPLEMENTATION
        and decision.failure_code.value in {
            "runtime_failed",
            "semantic_product_defect",
        }
    )


def rollback_detached_development_changes(
    candidate: ProjectCodeChangeSet, feedback: str,
) -> ProjectCodeChangeSet:
    """Drop only edits that trusted verification identified as unreachable.

    This is a rollback of a rejected candidate slice, not a new model-authored
    edit. Executable evidence and every unrelated production change remain in
    the checkpoint for the next bounded maker turn.
    """

    class_names = {
        match.casefold()
        for match in re.findall(
            r"changed\s+unity\s+ui\s+monobehaviour\s+([A-Za-z_][A-Za-z0-9_]*)\s+is\s+not\s+reachable",
            feedback,
            flags=re.IGNORECASE,
        )
    }
    if not class_names:
        return candidate
    kept = [
        change for change in candidate.changes
        if Path(change.path).stem.casefold() not in class_names
    ]
    if len(kept) == len(candidate.changes):
        return candidate
    return candidate.model_copy(update={"changes": kept})


def development_toolpack_focus_text(
    intake: IntakeRequest,
    requirements: RequirementsAnalysis,
) -> str:
    """Describe the whole approved definition of done to source discovery.

    Existing-project goals are often intentionally short.  Tool selection must
    also see the deliverables and observable completion contract, otherwise a
    requirement accepted during intake can disappear from repository context.
    """

    contract = requirements.completion_contract
    values: list[str] = [
        intake.goal,
        intake.desired_output or "",
        requirements.normalized_goal,
        *requirements.deliverables,
        *requirements.acceptance_criteria,
    ]
    if contract is not None:
        values.extend((contract.target_state, contract.pass_condition))
        for criterion in contract.quality_criteria:
            values.extend((criterion.description, criterion.evidence_required))
    seen: set[str] = set()
    focused: list[str] = []
    for value in values:
        normalized = " ".join(str(value).split()).strip()
        key = normalized.casefold()
        if normalized and key not in seen:
            seen.add(key)
            focused.append(normalized)
    return "\n".join(focused)


def development_repair_requires_anchored_range(
    *,
    multi_state_evidence_repair: bool,
    unity_evidence_topology_repair: bool,
    semantic_visual_repair: bool,
) -> bool:
    """Use bounded ranges for every repair against an existing candidate.

    A semantic visual repair used to return the complete generated candidate file.
    Large structured responses then hit the same token ceiling even after a compact
    retry.  The prior candidate is already authoritative ADK state, so a unique
    start/end range is both smaller and safer for committed and uncommitted files.
    """

    return any((
        multi_state_evidence_repair,
        unity_evidence_topology_repair,
        semantic_visual_repair,
    ))


def visual_repair_production_candidate(change_set):
    """Return the candidate view a visual repair maker may inspect."""

    return change_set.model_copy(update={
        "changes": [
            item
            for item in change_set.changes
            if visual_repair_production_target_allowed(
                str(getattr(item, "path", ""))
            )
        ]
    })


def visual_repair_has_uncommitted_product_candidate(change_set) -> bool:
    """A generated product file has candidate authority, not repository-anchor authority."""

    if change_set is None:
        return False
    return any(
        getattr(item, "base_sha256", None) is None
        and visual_repair_production_target_allowed(str(getattr(item, "path", "")))
        for item in getattr(change_set, "changes", [])
    )


def evidence_repair_has_bounded_uncommitted_candidate(change_set) -> bool:
    """A small generated proof file is safer to replace whole than re-anchor."""

    if change_set is None:
        return False
    return any(
        getattr(item, "base_sha256", None) is None
        and unity_evidence_contract_target_allowed(str(getattr(item, "path", "")))
        and len(str(getattr(item, "content", "")).encode("utf-8")) <= 8_000
        for item in getattr(change_set, "changes", [])
    )


def should_preserve_unity_evidence_checkpoint(
    candidate: object | None, failure_text: str,
) -> bool:
    """Keep a newly executable proof harness when it exposes deeper defects.

    A missing harness yields only two coarse topology errors. Once the atomic
    pair exists, static/runtime verification can expose several more specific
    blockers. Counting those blockers makes the new evidence look worse even
    though it is the only candidate from which the maker can learn. Preserve
    it when trusted verification no longer reports either member as absent.
    """

    if not evidence_repair_has_bounded_uncommitted_candidate(candidate):
        return False
    requested_pair = (
        "Unity visual test contract: add a discoverable Unity PlayMode test | "
        "Unity visual test contract: add a Unity test .asmdef"
    )
    return (
        not is_missing_unity_evidence_harness(failure_text)
        and not missing_unity_evidence_bundle_paths(requested_pair, candidate)
    )


def development_repair_difficulty(
    intake: IntakeRequest,
    requirements: RequirementsAnalysis,
    repository_paths: list[str],
) -> str:
    """Predict only the amount of product reasoning a repair turn needs."""

    score = 0
    if intake.output_target == OutputTarget.EXISTING_PROJECT:
        score += 1
    normalized_paths = [path.replace("\\", "/").casefold() for path in repository_paths]
    if (
        intake.output_target == OutputTarget.UNITY_APP
        or any(path.endswith(".cs") or path.startswith("assets/") for path in normalized_paths)
    ):
        score += 2
    criteria = (
        requirements.completion_contract.quality_criteria
        if requirements.completion_contract is not None
        else []
    )
    if len(criteria) >= 4:
        score += 1
    if len(normalized_paths) >= 8:
        score += 1
    goal = intake.goal.casefold()
    protected_concerns = sum(
        marker in goal
        for marker in (
            "protocol",
            "preserve",
            "login",
            "lobby",
            "settings",
            "mobile",
            "desktop",
            "통신",
            "보존",
            "로그인",
            "로비",
            "설정",
        )
    )
    if protected_concerns >= 3:
        score += 1
    if score >= 4:
        return "complex"
    if score >= 2:
        return "moderate"
    return "simple"


class ExecutionPipeline:
    def __init__(
        self,
        run_dir: Path,
        gateway: object | None = None,
        stage_models: dict[str, str] | None = None,
        stage_skills: dict[str, list[str]] | None = None,
        execution_graph: ExecutionGraph | None = None,
        project_registry_root: Path | None = None,
        finalize_budget_on_finish: bool = True,
    ):
        self.run_dir = run_dir
        self.gateway = gateway or BudgetedGeminiClient(run_dir)
        selected = stage_models or {}
        self.stage_models = selected
        assigned_skills = stage_skills or {}
        self.stage_skills = assigned_skills
        self.execution_graph = execution_graph
        self.project_registry_root = project_registry_root
        # A standalone pipeline owns the whole run and closes its ledger. A
        # milestone pipeline is only one slice of a durable run, so the job
        # orchestrator must keep the shared ledger open until every slice and
        # the final integration proof have finished.
        self.finalize_budget_on_finish = finalize_budget_on_finish
        self.analyst = AnalystAgent(
            self.gateway, selected.get("evidence_analysis", "gemini-3.5-flash"), assigned_skills.get("evidence_analysis")
        )
        self.writer = WriterAgent(
            self.gateway, selected.get("long_form_draft", "gemini-3.5-flash"), assigned_skills.get("long_form_draft")
        )
        self.developer = DeveloperAgent(
            self.gateway, selected.get("long_form_draft", "gemini-3.5-flash"), assigned_skills.get("long_form_draft")
        )
        self.verifier = VerifierAgent(
            self.gateway, selected.get("independent_verification", "gemini-3.5-flash"), assigned_skills.get("independent_verification")
        )
        self.recovery_policy = RecoveryPolicy()
        self.recovery_decisions: list[RecoveryDecision] = []

    @staticmethod
    def _is_development(intake: IntakeRequest) -> bool:
        return any(
            item in intake.toolpack_ids
            for item in (
                ToolPackId.EXCHANGE_DEVELOPMENT,
                ToolPackId.PROJECT_DEVELOPMENT,
                ToolPackId.GREENFIELD_WEB_DEVELOPMENT,
            )
        )

    def _development_components(self, intake: IntakeRequest, output_dir: Path | None = None):
        if ToolPackId.PROJECT_DEVELOPMENT in intake.toolpack_ids:
            if not intake.existing_project_id:
                raise ValueError("project development requires a selected imported project")
            pack = ApprovedProjectDevelopmentToolPack(
                intake.existing_project_id, registry_root=self.project_registry_root
            )
            developer = DeveloperAgent(
                self.gateway,
                self.stage_models.get("long_form_draft", "gemini-3.5-flash"),
                self.stage_skills.get("long_form_draft"),
                change_set_schema=ProjectCodeChangeSet,
                source_prefix="project-source/",
                path_approver=pack.approved_edit_path,
            )
            return ProjectCodeChangeSet, pack, developer
        if ToolPackId.GREENFIELD_WEB_DEVELOPMENT in intake.toolpack_ids:
            if output_dir is None:
                raise ValueError("greenfield web development requires an output directory")
            pack = GreenfieldWebDevelopmentToolPack(
                output_dir / "toolpacks" / ToolPackId.GREENFIELD_WEB_DEVELOPMENT.value / "scaffold"
            )
            developer = DeveloperAgent(
                self.gateway,
                self.stage_models.get("long_form_draft", "gemini-3.5-flash"),
                self.stage_skills.get("long_form_draft"),
                change_set_schema=ProjectCodeChangeSet,
                source_prefix="greenfield-source/",
                path_approver=pack.approved_edit_path,
            )
            return ProjectCodeChangeSet, pack, developer
        return CodeChangeSet, ExchangeDevelopmentToolPack(), self.developer

    def _write(self, path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(path.suffix + f".{uuid4().hex}.tmp")
        temp.write_text(text + "\n", encoding="utf-8")
        os.replace(temp, path)

    def _load(self, path: Path, schema: type[T]) -> T | None:
        if not path.exists():
            return None
        return schema.model_validate_json(path.read_text(encoding="utf-8"))


    @staticmethod
    def _merge_development_retry(previous: T, retry: T) -> T:
        """Overlay a bounded retry on the prior full change set."""
        prior_by_path = {item.path: item for item in previous.changes}
        order = [item.path for item in previous.changes]
        merged = dict(prior_by_path)
        directive = " ".join([
            retry.summary,
            *(item.reason for item in retry.changes),
        ]).casefold()
        if any(stem in directive for stem in ("remov", "delet")):
            for path, prior in prior_by_path.items():
                if prior.base_sha256 is None and Path(path).stem.casefold() in directive:
                    merged.pop(path, None)
                    order = [item for item in order if item != path]

        for item in retry.changes:
            prior = prior_by_path.get(item.path)
            removal = bool(
                prior is not None
                and prior.base_sha256 is None
                and item.content == ""
                and any(stem in item.reason.casefold() for stem in ("remov", "delet"))
            )
            if removal:
                merged.pop(item.path, None)
                order = [path for path in order if path != item.path]
                continue
            merged[item.path] = item
            if item.path not in order:
                order.append(item.path)
        payload = retry.model_dump(mode="json")
        payload["changes"] = [merged[path].model_dump(mode="json") for path in order]
        return type(previous).model_validate(payload)

    @staticmethod
    def _same_development_changes(left: BaseModel, right: BaseModel) -> bool:
        """Compare executable file results while ignoring narrative metadata."""

        def fingerprint(candidate: BaseModel) -> list[tuple[str, str | None, str]]:
            return sorted(
                (
                    str(item.path).casefold(),
                    getattr(item, "base_sha256", None),
                    str(item.content),
                )
                for item in getattr(candidate, "changes", [])
            )

        return fingerprint(left) == fingerprint(right)

    def _bind_project_change_set(
        self, intake: IntakeRequest, development_pack, change_set: T, output_dir: Path
    ) -> T:
        if ToolPackId.PROJECT_DEVELOPMENT not in intake.toolpack_ids:
            return change_set
        inspection_path = (
            output_dir / "toolpacks" / ToolPackId.PROJECT_DEVELOPMENT.value
            / "evidence" / "repository_inspection.json"
        )
        inspection = self._load(inspection_path, RepositoryInspection)
        if inspection is None:
            raise RuntimeError("approved project inspection is unavailable")
        return development_pack.bind_change_set_to_inspection(change_set, inspection)

    def _apply_development_change_set(
        self,
        intake: IntakeRequest,
        development_pack,
        change_set,
        development_dir: Path,
        contract: dict[str, object],
    ) -> DevelopmentRun:
        if any(item in intake.toolpack_ids for item in (
            ToolPackId.PROJECT_DEVELOPMENT,
            ToolPackId.GREENFIELD_WEB_DEVELOPMENT,
        )):
            run = development_pack.apply_and_verify(
                change_set,
                development_dir,
                verification_goal=json.dumps(contract, ensure_ascii=False),
            )
            unity_evidence = development_dir / "unity_visual_evidence"
            if unity_evidence.is_dir():
                strict_visual_quality = contract_requires_strict_visual_quality(contract)
                observation_contract = semantic_observation_contract(
                    contract, strict_visual_quality=strict_visual_quality
                )
                try:
                    observe_unity_visual_evidence(
                        self.gateway,
                        model=self.stage_models.get(
                            "independent_verification", "gemini-3.5-flash"
                        ),
                        evidence_dir=unity_evidence,
                        observation_path=(
                            development_dir.parent
                            / "independent_observations"
                            / "unity_ui_observation.json"
                        ),
                        goal_text=json.dumps(observation_contract, ensure_ascii=False),
                        strict_visual_quality=strict_visual_quality,
                    )
                except RuntimeError as exc:
                    diagnostic = (
                        compact_unity_layout_diagnostic_context(
                            development_dir / "unity_layout_diagnostics"
                        )
                        if strict_visual_quality else ""
                    )
                    if diagnostic:
                        raise RuntimeError(
                            f"{exc} | Deterministic Unity layout diagnostics: {diagnostic[:6000]}"
                        ) from exc
                    raise
            return run
        return development_pack.apply_and_verify(change_set, development_dir)
    def _temperament_audit(self, output_dir: Path) -> list[dict[str, object]]:
        """Collect only APT-3 decisions that actually broke an equal-choice tie."""
        decisions: list[dict[str, object]] = []
        seen: set[str] = set()
        for path in sorted(output_dir.glob("*.json")):
            if path.name in {"execution_checkpoint.json", "temperament_decisions.json"}:
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict):
                continue
            for decision in payload.get("temperament_decisions", []):
                if not isinstance(decision, dict):
                    continue
                key = json.dumps(decision, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                if key not in seen:
                    seen.add(key)
                    decisions.append(decision)
        return decisions

    def _append_recovery(self, decision: RecoveryDecision) -> None:
        key = decision.model_dump_json()
        if all(item.model_dump_json() != key for item in self.recovery_decisions):
            self.recovery_decisions.append(decision)

    def _capture_developer_recoveries(self, developer: DeveloperAgent | None = None) -> None:
        active = developer or self.developer
        for decision in active.last_recovery_decisions:
            self._append_recovery(decision)

    def _persist_recoveries(self, output_dir: Path) -> None:
        if not self.recovery_decisions:
            return
        payload = {
            "schema_version": "onebrief-recovery-log-v1",
            "decisions": [item.model_dump(mode="json") for item in self.recovery_decisions],
        }
        self._write(
            output_dir / "recovery_decisions.json",
            json.dumps(payload, ensure_ascii=False, indent=2),
        )

    def _development_evidence(self, output_dir: Path) -> dict[str, object] | None:
        development_dir = output_dir / "development"
        run = self._load(development_dir / "development_run.json", DevelopmentRun)
        verification_receipt = self._load(
            development_dir / "verification_commands.json",
            DevelopmentVerificationReceipt,
        )
        change_set_path = output_dir / "code_change_set.json"
        if run is None or not change_set_path.is_file():
            return None
        try:
            change_set = json.loads(change_set_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        changed_files: list[dict[str, str]] = []
        remaining = 70_000
        for relative in run.changed_paths:
            path = development_dir / "changed_files" / Path(*relative.split("/"))
            if not path.is_file() or remaining <= 0:
                continue
            content = path.read_text(encoding="utf-8", errors="replace")
            excerpt = content[:remaining]
            remaining -= len(excerpt)
            changed_files.append({"path": relative, "content": excerpt})
        # The verifier needs the executed result, not duplicate copies of every
        # provider-authored file.  Keep metadata in the change set and actual
        # source once in changed_files.
        compact_change_set = {
            **change_set,
            "changes": [
                {key: value for key, value in item.items() if key != "content"}
                for item in change_set.get("changes", [])
                if isinstance(item, dict)
            ],
        } if isinstance(change_set, dict) else change_set
        compact_run = run.model_dump(mode="json")
        for command in compact_run.get("commands", []):
            if isinstance(command, dict) and isinstance(command.get("output_tail"), str):
                command["output_tail"] = command["output_tail"][-2_000:]
        runtime_evidence: list[dict[str, str]] = []
        runtime_remaining = 20_000
        for relative in run.evidence_paths:
            evidence_root = self._resolve_development_evidence_root(
                output_dir, development_dir, relative
            )
            if evidence_root is None:
                continue
            for path in sorted(evidence_root.rglob("*")):
                if (
                    not path.is_file()
                    or path.suffix.casefold() != ".json"
                    or runtime_remaining <= 0
                ):
                    continue
                content = path.read_text(encoding="utf-8", errors="replace")
                excerpt = content[:runtime_remaining]
                runtime_remaining -= len(excerpt)
                runtime_evidence.append({
                    "path": path.relative_to(output_dir).as_posix(),
                    "content": excerpt,
                })
        trusted_observation_receipts: list[dict[str, object]] = []
        observation_dir = output_dir / "independent_observations"
        if observation_dir.is_dir():
            for path in sorted(observation_dir.glob("*.json")):
                try:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                if isinstance(payload, dict):
                    trusted_observation_receipts.append(payload)
        return {
            "change_set": compact_change_set,
            "development_run": compact_run,
            "verification_receipt": (
                verification_receipt.model_dump(mode="json")
                if verification_receipt is not None else None
            ),
            "changed_files": changed_files,
            "runtime_evidence": runtime_evidence,
            "trusted_observation_receipts": trusted_observation_receipts,
            "verification_rule": (
                "Compare every deliverable and claimed feature with the actual changed files. "
                "Legacy tests prove regression safety only; new behavior needs relevant deterministic evidence. "
                "For user-facing work, require trusted runtime evidence and reject compile-only proof."
            ),
        }

    @staticmethod
    def _resolve_development_evidence_root(
        output_dir: Path, development_dir: Path, relative: str
    ) -> Path | None:
        """Resolve evidence after a trusted revalidation directory is remapped to development/."""
        parts = tuple(part for part in relative.replace("\\", "/").split("/") if part)
        candidates = [output_dir / Path(*parts), development_dir / Path(*parts)]
        if len(parts) > 1:
            candidates.append(development_dir / Path(*parts[1:]))
        output_root = output_dir.resolve()
        for candidate in candidates:
            resolved = candidate.resolve()
            if resolved.is_relative_to(output_root) and resolved.is_dir():
                return resolved
        return None

    def _observe_seeded_unity_evidence(
        self, output_dir: Path, contract: dict[str, object]
    ) -> None:
        """Create the missing independent visual receipt for a digest-bound resumed result."""
        observation_path = (
            output_dir / "independent_observations" / "unity_ui_observation.json"
        )
        if observation_path.is_file():
            return
        development_dir = output_dir / "development"
        run = self._load(development_dir / "development_run.json", DevelopmentRun)
        if run is None or run.status != "verified":
            return
        for relative in run.evidence_paths:
            evidence_dir = self._resolve_development_evidence_root(
                output_dir, development_dir, relative
            )
            if evidence_dir is None or evidence_dir.name != "unity_visual_evidence":
                continue
            try:
                observe_unity_visual_evidence(
                    self.gateway,
                    model=self.stage_models.get(
                        "independent_verification", "gemini-3.5-flash"
                    ),
                    evidence_dir=evidence_dir,
                    observation_path=observation_path,
                    goal_text=json.dumps(contract, ensure_ascii=False),
                )
            except RuntimeError as exc:
                self._write(
                    output_dir / "development_verification_failure.txt", str(exc)
                )
                raise
            return

    @staticmethod
    def _compact_development_feedback(feedback: str) -> str:
        """Keep the trusted actionable failure while dropping verbose runner shutdown noise."""
        prefix = "development verification failed: "
        detail = feedback[len(prefix):] if feedback.casefold().startswith(prefix) else feedback
        # Unity appends several thousand characters of shutdown, package-manager,
        # and memory diagnostics after a PlayMode failure.  Returning that noise
        # to the maker can bury the one actionable assertion and cause costly
        # identical retries.  Preserve the trusted command identity, but reduce
        # the repair evidence to Unity's explicit failure section when present.
        unity_failure_marker = "UNITY TEST FAILURES"
        marker_index = detail.find(unity_failure_marker)
        if marker_index >= 0:
            command_name = detail.split(" ", 1)[0].strip()
            explicit_failure = " ".join(detail[marker_index:].split())[:4_000]
            detail = f"{command_name}: {explicit_failure}"
        elif "VERIFICATION SIGNALS" in detail:
            command_name = detail.split(" ", 1)[0].strip()
            explicit_failure = detail.split("VERIFICATION SIGNALS", 1)[1]
            explicit_failure = explicit_failure.split("BEE COMPILER DIAGNOSTICS", 1)[0]
            detail = f"{command_name}: " + " ".join(explicit_failure.split())[:4_000]
        return detail

    @staticmethod
    def _development_failure_report(
        feedback: str,
        completion_contract=None,
        verification_receipt: DevelopmentVerificationReceipt | None = None,
    ) -> VerificationReport:
        """Turn deterministic adapter blockers into one repair slice each."""

        detail = ExecutionPipeline._compact_development_feedback(feedback)
        blockers = [item.strip() for item in detail.split(" | ") if item.strip()][:12]
        if not blockers:
            blockers = [feedback]
        def blocker_action(blocker: str) -> str:
            lowered = blocker.casefold()
            if (
                "cannot implicitly convert type" in lowered
                and "onebrief.visual.onebriefatomicscreenshot.scenarioreceipt" in lowered
                and "string" in lowered
            ):
                return (
                    "Repair only the generated evidence test's trusted-helper return type. "
                    "OneBriefAtomicScreenshot.CaptureScenario returns "
                    "OneBriefAtomicScreenshot.ScenarioReceipt, not string. Store the result as var or that "
                    "exact ScenarioReceipt type and pass it unchanged to WriteManifestAtomically. Do not edit "
                    "product source, redefine the helper, or alter the asserted behavior."
                )
            if "unity test failures" in lowered:
                return (
                    "Repair the exact failing PlayMode assertion named in the trusted Unity result. Exercise the "
                    "real requested screen transition before discovering its controls, and change production UI "
                    "wiring when the requested control truly does not exist; never weaken or merely restate the test."
                )
            if "must not declare, duplicate, or replace onebriefatomicscreenshot" in lowered:
                return (
                    "Delete the test-side OneBriefAtomicScreenshot class completely. The isolated runner installs "
                    "the trusted helper beside the test. Call CaptureScenario(scenarioId, observedState, "
                    "interaction, assertionCount, relativePngPath, sceneCamera, width, height, activeCanvases), "
                    "then WriteManifestAtomically(scenarioReceipt). Do not create a fallback or wrapper with the "
                    "same name and do not hand-write runtime-evidence.json."
                )
            if "capture requires exactly" in lowered or "zero-argument log-only call" in lowered:
                return (
                    "Replace the short-form call with the trusted structured API: CaptureScenario(scenarioId, "
                    "observedState, interaction, assertionCount, relativePngPath, sceneCamera, width, height, "
                    "activeCanvases), followed by WriteManifestAtomically(scenarioReceipt). Do not redefine the "
                    "helper, call either method with fewer arguments, or hand-write runtime-evidence.json."
                )
            if "must use the trusted onebriefatomicscreenshot.capturescenario" in lowered:
                return (
                    "Use the runner-supplied structured API instead of hand-writing evidence JSON: "
                    "var scenario = OneBriefAtomicScreenshot.CaptureScenario(scenarioId, observedState, "
                    "interaction, assertionCount, relativePngPath, sceneCamera, width, height, activeCanvases); "
                    "then call OneBriefAtomicScreenshot.WriteManifestAtomically(scenario). The helper derives "
                    "viewport and screenshot fields from durable PNG bytes; do not write runtime-evidence.json "
                    "yourself and do not redefine the helper."
                )
            if "unity visual test contract" in lowered and "synthetic ui" in lowered:
                return (
                    "Delete the generated fallback GameObject/AddComponent UI block. Open or activate the real "
                    "requested settings screen, then discover and interact with its existing scene controls; do not "
                    "create any replacement control inside the test."
                )
            if (
                "approved recording profile" in lowered
                and "committed authentication contract" in lowered
            ):
                return (
                    "Use the exact ToolPack-bound deterministic authentication journey named in the "
                    "failure: select DevPanel/TestAccountDropdown, then click "
                    "DevPanel/DirectEnterButton, wait for the shipped Lobby scene, and capture it. "
                    "Do not substitute StartButton, terms onboarding, direct scene loading, or product edits."
                )
            if (
                "protected destination evidence" in lowered
                or "precondition-free click test" in lowered
            ):
                return (
                    "Inspect the approved repository context, loaded scene, and existing controller path for a "
                    "deterministic authentication fixture before changing product code. Reuse an existing test or "
                    "development account selector, sandbox profile, seeded session, credential-and-terms controls, "
                    "or equivalent authenticated-state setup through the shipped UI/controller. Then invoke the "
                    "shipped authentication, start, or direct-enter control and assert and capture the protected "
                    "destination reached by that real path. Do not use a precondition-free Start click, install or "
                    "replace a product navigation listener, or load the destination scene from the evidence test. "
                    "If no approved deterministic authentication fixture exists, report that missing fixture as the "
                    "blocker instead of changing product navigation."
                )
            if "unity visual test contract" in lowered and "real ui interaction" in lowered:
                return (
                    "Operate the loaded scene's real navigation control (for example its Button.onClick) or activate "
                    "the existing requested panel, then assert the destination state. Scene loading and object "
                    "presence alone are not a transition test."
                )
            if "must not remove or replace the product control" in lowered or (
                "must not install a replacement onclick listener" in lowered
            ):
                return (
                    "Delete RemoveAllListeners and any test-added listener that loads the destination. Invoke the "
                    "existing Button.onClick listeners exactly as shipped, wait for the real transition, then assert "
                    "and capture the observed destination. If the shipped transition cannot run in isolation, report "
                    "the missing product dependency as a product/runtime blocker instead of fabricating success."
                )
            if "must not directly load the destination scene after invoking" in lowered:
                return (
                    "Delete the direct SceneManager.LoadScene call after the UI action. Invoke the shipped "
                    "Button or ExecuteEvents action, wait for the product-owned transition, then assert and "
                    "capture the actual active destination. If it does not transition, return the failure to "
                    "product implementation instead of making the evidence harness perform the navigation."
                )
            if "must be committed in one atomic manifest" in lowered:
                return (
                    "Capture Login before the shipped start action, Lobby after that action, and Settings after "
                    "the shipped settings action. Create one System.Collections.Generic.List<"
                    "OneBriefAtomicScreenshot.ScenarioReceipt>, add each successfully captured receipt to it, "
                    "leave every if/else branch, and place exactly one syntactic call site at the end: "
                    "OneBriefAtomicScreenshot.WriteManifestAtomically(receipts.ToArray()). Do not place the call "
                    "inside both branches; multiple source call sites overwrite the prior manifest."
                )
            if "unity visual test contract" in lowered and "scenarios array" in lowered:
                return (
                    "Rewrite runtime-evidence.json with schema_version onebrief-unity-visual-evidence-v1 and a "
                    "scenarios array measured from the executed real UI states, including screenshot path, "
                    "interaction, assertions, and viewport dimensions."
                )
            if "distinct rendered scenario" in lowered and "real ui surface" in lowered:
                return (
                    "Capture each named real UI surface as its own executed state and PNG. For Login, capture "
                    "before starting; for Lobby, capture after the real login/start transition and before opening "
                    "Settings; for Settings, capture only after invoking the real Settings button. Write one "
                    "runtime-evidence scenario per surface with a surface-specific scenario_id and observed_state. "
                    "A combined final-state name such as LoginToLobbyToSettings is not distinct evidence."
                )
            if (
                "reused an identical screenshot" in lowered
                and "desktop" in lowered
                and "mobile" in lowered
            ):
                return (
                    "The same UI state was captured for desktop and mobile with identical bytes. Change the "
                    "synchronous capture helper to accept the requested width and height for each scenario and "
                    "use them directly for RenderTexture, Texture2D, and ReadPixels. Return those written texture "
                    "dimensions in the evidence. Do not rely on Screen.SetResolution, Screen.width, or extra frame "
                    "delays changing Unity batchmode output."
                )
            if "reused an identical screenshot" in lowered:
                return (
                    "Replace the existing capture region so every scenario writes a unique PNG immediately while "
                    "its named real UI state is visible. Capture Login before the start/login action, Lobby after "
                    "that real transition and before opening Settings, and Settings after invoking the real Settings "
                    "button. Do not duplicate scenario rows, paths, or bytes, and do not relabel one final-state PNG."
                )
            if "unity visual evidence did not exercise requested locale" in lowered:
                return (
                    "The runtime interaction may already select the requested locale, but the evidence manifest does "
                    "not prove it. After operating the real visible language dropdown, append a dedicated locale "
                    "scenario to runtime-evidence.json. That scenario must contain scenario_id, expected_locale, "
                    "observed_locale, changed_visible_text_count, missing_glyph_count, and screenshot_path. Set "
                    "expected_locale and observed_locale to the exact missing locale named by the failure; measure "
                    "changed_visible_text_count from visible TMP text before and after the real selection; derive "
                    "missing_glyph_count by ForceMeshUpdate plus TMP character/font coverage; and capture a new PNG "
                    "while the selected locale is visibly active. Do not record the locale dropdown only as a general "
                    "observed_state scenario, and do not invent passing counts without measuring the running UI."
                )
            if "unity visual scenario" in lowered and "did not visibly change any text" in lowered:
                return (
                    "The real locale selection ran, but the shipped UI stayed visibly unchanged. Do not edit the "
                    "PlayMode test, its labels, or runtime-evidence.json to manufacture a change. Inspect the "
                    "production path from the visible language dropdown through JulpaeLocalization.SetLanguage and "
                    "LanguageChanged to the active LocalizedText or screen binder, then repair the smallest missing "
                    "product binding so real Settings text changes from a deliberately different reference locale. "
                    "Preserve the existing server protocol and assets; let the unchanged observer remeasure it."
                )
            if "not reachable from any committed .unity/.prefab script guid" in lowered:
                return (
                    "The edited MonoBehaviour is detached from the executed Unity UI. Do not revise that file again. "
                    "Use the committed scene catalog's attached_script_paths and the real failing control name to "
                    "select the active production component, then repair one reachable binding or explicitly attach "
                    "the intended binder in an approved scene/prefab. Keep tests and evidence unchanged."
                )
            if "locale changed_visible_text_count must compare before/after" in lowered:
                return (
                    "Repair only the PlayMode observer's temporal order. Snapshot the real target surface's visible "
                    "TMP text before operating the real language dropdown, snapshot it again after the language "
                    "event while that same surface is still active, compute changed_visible_text_count from those "
                    "two snapshots, and capture the locale PNG there. Move Close/Back/Return navigation after this "
                    "measurement. Do not assign label text, dropdown option text, or product layout in the test."
                )
            if "identical duplicate local ui-control declaration" in lowered:
                return (
                    "Remove only the second identical UI-control declaration and its repeated navigation/capture "
                    "block. Preserve the first measured interaction and every unique scenario; do not rename the "
                    "duplicate merely to make it compile."
                )
            if "unity visual test contract" in lowered and "each general ui evidence scenario" in lowered:
                return (
                    "Use the exact OneBrief general UI scenario fields: scenario_id, observed_state, interaction, "
                    "assertion_count, viewport_width, viewport_height, and screenshot_path. Do not invent nested "
                    "assertions or viewport objects."
                )
            if "captured png texture dimensions" in lowered or (
                "viewport" in lowered and "does not match its png" in lowered
            ):
                return (
                    "Do not add more frame delays or repeat Screen.SetResolution. Make the synchronous capture "
                    "return the Texture2D/PNG width and height actually written, then serialize those measured "
                    "values into viewport_width and viewport_height for that exact scenario."
                )
            if "screen.setresolution plus screen.width/screen.height" in lowered:
                return (
                    "Change the synchronous capture helper to accept the requested width and height for each "
                    "scenario and use those arguments directly for RenderTexture, Texture2D, and ReadPixels. "
                    "Return the dimensions of that written texture. Do not rely on Screen.SetResolution changing "
                    "Screen.width or Screen.height in Unity batchmode."
                )
            if "unity visual test contract" in lowered and "duplicate unitytest methods" in lowered:
                return (
                    "Remove the duplicated UnityTest method or class created by the previous repair and keep one "
                    "coherent executable test implementation."
                )
            if (
                "unity visual test contract" in lowered
                and "must observe product text" in lowered
            ):
                return (
                    "Remove the verification-code block that assigns or replaces TMP_Dropdown option text. "
                    "The test must leave shipped labels unchanged and only inspect the real rendered text and "
                    "font glyph coverage. Do not substitute a different test-side label repair."
                )
            if (
                "unity visual test contract" in lowered
                and "must observe the shipped responsive layout" in lowered
            ):
                return (
                    "Remove the verification-code block that assigns CanvasScaler mode, reference resolution, "
                    "screen match mode, match value, anchors, or scale. The test must capture the production "
                    "layout unchanged; responsive behavior is repaired later in production UI source."
                )
            if "unity visual test contract" in lowered and "inert source file" in lowered:
                return (
                    "Connect the new production UI component to the real application: attach it through an approved "
                    "scene/prefab change, invoke it from an already-running production component, or add a safe "
                    "RuntimeInitializeOnLoadMethod entrypoint. Tests referencing the class do not make it execute."
                )
            if (
                "unity visual test contract" in lowered
                and "namespace/full name begins" in lowered
            ):
                return (
                    "Edit the generated PlayMode test source itself so its declared namespace begins exactly with "
                    "OneBrief.Visual. Do not change or re-emit the asmdef for this blocker alone; preserve an "
                    "existing valid test asmdef. When the same trusted failure also "
                    "reports that the test asmdef is absent, create the PlayMode source and sibling asmdef in the "
                    "same atomic repair."
                )
            if "unity visual test contract" in lowered and "png" in lowered:
                return (
                    "Use the trusted evidence-only OneBrief.Visual.OneBriefAtomicScreenshot.CaptureScenario helper "
                    "already installed beside the PlayMode test, then call WriteManifestAtomically with the "
                    "returned ScenarioReceipt. Do not call ScreenCapture.CaptureScreenshot, hand-write the "
                    "manifest, or copy or replace the trusted helper."
                )
            if "unity visual test contract" in lowered and "visible ui" in lowered:
                return (
                "Inside the OneBrief.Visual test, load the real project scene and discover active or inactive scene "
                "UI components with Unity object queries; interact with them directly and never construct synthetic UI."
            )
            if "unity visual test contract" in lowered and "language" in lowered:
                return (
                "Inside the OneBrief.Visual test, load the real scene and operate the real TMP_Dropdown found from "
                "scene objects; do not construct synthetic UI or delegate to a production verification component."
            )
            if "unity visual test contract" in lowered and "glyph" in lowered:
                return (
                "Inside the OneBrief.Visual test, ForceMeshUpdate on real TMP_Text objects and inspect "
                "textInfo.characterInfo with font.HasCharacter to compute missing_glyph_count."
            )
            if "unity visual test contract" in lowered and "directly reference" in lowered:
                return (
                "Remove the production type name and Assembly-CSharp reference from the test. Interact only through "
                "the loaded scene's public UI objects or generic reflection APIs."
            )
            return "Resolve this deterministic blocker in the smallest independently verifiable change."

        unity_contract = any(
            "unity visual test contract" in blocker.casefold() for blocker in blockers
        )
        if unity_contract:
            checks = [{
                "criterion": "Coherent Unity product and runtime evidence contract",
                "passed": False,
                "evidence": " | ".join(blockers),
            }]
            atomic_bundle_instruction = (
                [
                    "Create the missing Unity evidence harness as one atomic two-file repair: exactly one "
                    "discoverable OneBrief.Visual PlayMode .cs source and one sibling .asmdef whose "
                    "optionalUnityReferences contains TestAssemblies. Do not submit or retain only one half. "
                    "The runner supplies immutable OneBriefAtomicScreenshot; never declare a fallback with that "
                    "name. Call CaptureScenario(scenarioId, observedState, interaction, assertionCount, "
                    "relativePngPath, sceneCamera, width, height, activeCanvases), then call "
                    "WriteManifestAtomically(scenarioReceipt). The helper creates the schema and binds measured "
                    "PNG dimensions; do not hand-write runtime-evidence.json."
                ]
                if requires_atomic_unity_evidence_pair(detail)
                else []
            )
            instructions = list(dict.fromkeys(
                atomic_bundle_instruction
                + [blocker_action(item) for item in blockers]
                + [
                    "Resolve the related Unity blockers as one coherent evidence-harness repair. "
                    "Do not edit product UI unless a trusted semantic observation identifies a product defect; "
                    "do not submit only an assembly wrapper."
                ]
            ))[:8]
        else:
            checks = [{
                "criterion": f"Isolated build and test blocker {index}",
                "passed": False,
                "evidence": blocker,
            } for index, blocker in enumerate(blockers, start=1)]
            instructions = [
                blocker_action(blockers[0]) + " Do not repeat unchanged repair files."
            ]
        bound_checks: list[CriterionCheck] = []
        if verification_receipt is not None:
            criteria = list(getattr(completion_contract, "quality_criteria", []) or [])

            def matching_criteria(kind: EvidenceKind) -> list[str]:
                def criterion_kind(item) -> EvidenceKind | None:
                    text = f"{item.description} {item.evidence_required}".casefold()
                    # Compilation may be named as a prerequisite for a richer
                    # interaction or rendered-state criterion. Never let the
                    # weaker receipt satisfy that mixed semantic outcome.
                    semantic_markers = (
                        "visual", "screenshot", "rendered png", "layout", "png",
                        "behavior", "interaction", "transition", "navigation", "playmode",
                    )
                    if not any(marker in text for marker in semantic_markers) and any(marker in text for marker in (
                        "compile", "compilation", "build", "컴파일", "빌드",
                    )):
                        return EvidenceKind.COMPILE
                    if any(marker in text for marker in (
                        "visual", "screenshot", "rendered png", "layout", "png",
                        "시각", "스크린샷", "렌더", "레이아웃",
                    )):
                        return EvidenceKind.VISUAL
                    if any(marker in text for marker in (
                        "behavior", "interaction", "transition", "navigation", "playmode",
                        "동작", "상호작용", "이동",
                    )):
                        return EvidenceKind.BEHAVIOR
                    return None

                return [
                    item.criterion_id for item in criteria
                    if criterion_kind(item) == kind
                ]

            for binding in verification_receipt.evidence_bindings:
                for criterion_id in matching_criteria(binding.kind):
                    rebound = binding.model_copy(update={"criterion_id": criterion_id})
                    bound_checks.append(CriterionCheck(
                        criterion_id=criterion_id,
                        criterion=next(
                            item.description for item in criteria
                            if item.criterion_id == criterion_id
                        ),
                        passed=binding.status == EvidenceStatus.PASSED,
                        evidence=binding.summary,
                        evidence_bindings=[rebound],
                    ))

        failure_bindings = []
        if "screenshot is unavailable" in detail.casefold():
            failure_bindings.append(create_evidence_binding(
                criterion_id=None,
                kind=EvidenceKind.VISUAL,
                status=EvidenceStatus.MISSING,
                summary=next(
                    (item for item in blockers if "screenshot is unavailable" in item.casefold()),
                    "Unity screenshot was not materialized.",
                ),
            ))
        system_checks = [CriterionCheck.model_validate(item) for item in checks]
        if failure_bindings and system_checks:
            system_checks[0] = system_checks[0].model_copy(update={
                "evidence_bindings": failure_bindings,
            })

        # One explicit criterion check per bound proof.  Keep the newest receipt
        # for a criterion so a compile command cannot be duplicated by several
        # generic adapter labels.
        latest_bound: dict[str, CriterionCheck] = {}
        for check in bound_checks:
            if check.criterion_id:
                latest_bound[check.criterion_id] = check
        return VerificationReport(
            verdict=Verdict.REVISE,
            criterion_checks=[*latest_bound.values(), *system_checks],
            blocking_issues=blockers,
            revision_instructions=instructions,
            missing_information=[],
        )

    def _run_adk_document_convergence(
        self,
        *,
        intake: IntakeRequest,
        requirements: RequirementsAnalysis,
        sources: list[InternalSource],
        source_payload: list[dict[str, object]],
        contract: dict[str, object],
        analysis: AnalysisPackage,
        output_dir: Path,
    ) -> tuple[DraftArtifact, VerificationReport, int]:
        """Run the real ADK maker/verifier loop while deterministic gates retain veto power."""

        repair_fingerprints: list[str] = []

        def prepare_repair(
            report: VerificationReport, round_number: int
        ) -> RepairPlan | None:
            if requirements.completion_contract is None or report.verdict != Verdict.REVISE:
                return None
            plan = build_repair_plan(
                requirements.completion_contract,
                report,
                round_number=round_number,
                prior_fingerprints=repair_fingerprints,
            )
            if plan is None:
                return None
            if len(plan.tasks) > 1:
                # Software repairs converge more reliably when one verified
                # blocker is changed and retested at a time. The next adapter
                # run will remove resolved blockers and expose the next slice.
                selected = plan.tasks[0]
                plan = plan.model_copy(update={
                    "tasks": [selected],
                    "stop_after_this_round": selected.disposition.value == "escalate",
                    "rationale": (
                        "Execute one smallest blocker, re-run trusted verification, then select the next blocker."
                    ),
                })
            repair_fingerprints.extend(item.fingerprint for item in plan.tasks)
            self._write(
                output_dir / f"repair_plan_r{round_number}.json",
                plan.model_dump_json(indent=2),
            )
            return plan

        def after_maker(raw: object, _ctx, round_number: int) -> dict[str, object]:
            draft = enforce_temperament_audit(
                DraftArtifact.model_validate(raw), WRITER_PROFILE
            )
            if intake.output_target == OutputTarget.SPREADSHEET:
                draft = append_authoritative_csv_tables(sources, draft)
            self._write(
                output_dir / f"draft_r{round_number}.json",
                draft.model_dump_json(indent=2),
            )
            return {MAKER_STATE_KEY: draft.model_dump(mode="json")}

        def verification_gate(
            raw_report: VerificationReport, _ctx, round_number: int
        ) -> VerificationReport:
            model_report = enforce_temperament_audit(raw_report, VERIFIER_PROFILE)
            self._write(
                output_dir / f"model_verification_r{round_number}.json",
                model_report.model_dump_json(indent=2),
            )
            draft = DraftArtifact.model_validate(_ctx.session.state[MAKER_STATE_KEY])
            grounding = validate_draft_grounding(
                sources, draft,
                require_full_csv_preservation=requires_full_csv_preservation(requirements),
            )
            self._write(
                output_dir / f"deterministic_verification_r{round_number}.json",
                grounding.model_dump_json(indent=2),
            )
            completion = validate_completion_evidence(intake, requirements, None)
            self._write(
                output_dir / f"completion_evidence_r{round_number}.json",
                completion.model_dump_json(indent=2),
            )
            report = apply_completion_evidence_override(model_report, completion)
            report = apply_deterministic_override(report, grounding)
            sufficiency = validate_evidence_sufficiency(
                intake, requirements, sources, draft
            )
            self._write(
                output_dir / f"evidence_sufficiency_r{round_number}.json",
                sufficiency.model_dump_json(indent=2),
            )
            report = apply_evidence_sufficiency_override(report, sufficiency)
            reality = evaluate_reality_check(intake, requirements, None)
            self._write(
                output_dir / f"reality_check_r{round_number}.json",
                reality.model_dump_json(indent=2),
            )
            report = apply_reality_check_override(report, reality)
            if requirements.completion_contract is not None:
                report = settle_consistent_verification(
                    requirements.completion_contract, report
                )
            if report.verdict == Verdict.REVISE:
                failed_ids = [
                    str(check.criterion_id) for check in report.criterion_checks
                    if not check.passed and check.criterion_id
                ]
                passing_ids = [
                    str(check.criterion_id) for check in report.criterion_checks
                    if check.passed and check.criterion_id
                ]
                failure_text = " | ".join([
                    *report.blocking_issues,
                    *[
                        check.evidence for check in report.criterion_checks
                        if not check.passed
                    ],
                ])[:12_000]
                active_paths = [
                    str(item.path)
                    for item in getattr(previous_change_set, "changes", [])
                ]
                convergence_contract = record_convergence_failure(
                    context="development_acceptance_verification",
                    failure_text=failure_text or "Acceptance verification requested revision.",
                    attempt_number=round_number + 1,
                    affected_paths=active_paths,
                    failed_criterion_ids=failed_ids,
                    preserve_criterion_ids=passing_ids,
                    strategy_fingerprint=(
                        development_change_strategy_fingerprint(previous_change_set)
                        if previous_change_set is not None else None
                    ),
                )
                ctx.session.state[REPAIR_CONTRACT_STATE_KEY] = (
                    convergence_contract.model_dump(mode="json")
                )
                if not convergence_contract.execution_allowed:
                    report = VerificationReport(
                        verdict=Verdict.UNVERIFIABLE,
                        criterion_checks=report.criterion_checks,
                        blocking_issues=list(dict.fromkeys([
                            *report.blocking_issues,
                            "The convergence progress gate found no new causal evidence; blind repair is stopped.",
                        ])),
                        revision_instructions=[],
                        missing_information=report.missing_information,
                        temperament_decisions=report.temperament_decisions,
                    )
            repair_plan = prepare_repair(report, round_number)
            if repair_plan is not None:
                _ctx.session.state[REPAIR_PLAN_STATE_KEY] = repair_plan.model_dump(mode="json")
                if repair_plan.stop_after_this_round:
                    message = (
                        "The same evidence failures repeated after bounded repair and decomposition. "
                        "OneBrief stopped blind retry and preserved the unresolved candidate for a new plan."
                    )
                    report = VerificationReport(
                        verdict=Verdict.UNVERIFIABLE,
                        criterion_checks=report.criterion_checks,
                        blocking_issues=list(dict.fromkeys([
                            *report.blocking_issues,
                            message,
                        ])),
                        revision_instructions=[],
                        missing_information=report.missing_information,
                        temperament_decisions=report.temperament_decisions,
                    )
            self._write(
                output_dir / f"verification_r{round_number}.json",
                report.model_dump_json(indent=2),
            )
            return report

        maker_instruction = (
            "You are OneBrief's accountable artifact maker. Create the complete requested artifact from "
            "the work contract, analysis package, and authoritative sources in the user payload. On later "
            "iterations revise your own prior artifact, address every blocking issue, and preserve passing "
            "content. Every material claim must cite supplied F-prefixed finding IDs. Never change the goal, "
            "invent evidence, or make a high-impact human decision. For public research, expose direct source "
            "URLs beside the claims or rows they support; an F-prefixed finding ID alone is not inspectable "
            "evidence. Never infer that no equivalent exists from novelty, a registration date, or category-level "
            "comparison. Use bounded search-scope language and preserve uncertainty. Do not add unsolicited next "
            "steps beyond an explicit scope ceiling. When repair_plan is present, treat it as the complete scope "
            "of the revision, repair each listed evidence failure, and preserve passing criteria. Return only the "
            "required structured object. "
            + WRITER_PROFILE.instruction()
        )
        verifier_instruction = (
            "You are OneBrief's independent verifier and did not create the artifact. Test every acceptance "
            "criterion, grounding, citation, consistency, completeness, and authority boundary against the "
            "user payload and current artifact. PASS only with explicit evidence and no blocker. Use REVISE for "
            "correctable maker work and NEEDS_INFORMATION only for missing authoritative user information. "
            "A category or market segment is not an individually named product or item. If a criterion requires "
            "N products or examples, verify N named, directly source-linked entries. Novelty does not prove absence "
            "of equivalents, and absolute safety or uniqueness claims fail without bounded evidence. Reject any "
            "section beyond an explicit user scope ceiling. "
            "For each completion_contract criterion, return exactly one check with its Q-prefixed criterion_id; "
            "leave criterion_id null only for additional system checks. Give exact revision instructions and "
            "never edit the artifact. Return only the structured object. "
            + VERIFIER_PROFILE.instruction()
        )
        reusable_drafts = sorted(
            output_dir.glob("draft_r*.json"),
            key=lambda path: int(path.stem.rsplit("r", 1)[-1]),
        )
        reverify_only = (
            (output_dir / "reverify_existing_candidate.json").is_file()
            and bool(reusable_drafts)
        )
        initial_state = None
        if reusable_drafts:
            previous_draft = DraftArtifact.model_validate_json(
                reusable_drafts[-1].read_text(encoding="utf-8")
            )
            initial_state = {
                MAKER_STATE_KEY: previous_draft.model_dump(mode="json"),
                # Verify the restored candidate before paying its original
                # maker for a repair. If it fails, later ADK rounds retain the
                # same maker identity and receive the deterministic repair plan.
                REVERIFY_EXISTING_STATE_KEY: True,
            }
        agent = build_text_convergence_agent(
            gateway=self.gateway,
            maker_model=self.stage_models.get("long_form_draft", "gemini-3.5-flash"),
            verifier_model=self.stage_models.get(
                "independent_verification", "gemini-3.5-flash"
            ),
            maker_schema=DraftArtifact,
            max_revision_rounds=(
                0 if reverify_only else effective_revision_rounds(intake, requirements)
            ),
            maker_instruction=maker_instruction,
            verifier_instruction=verifier_instruction,
            maker_output_tokens=WRITER_OUTPUT_CAP,
            verifier_output_tokens=VERIFIER_OUTPUT_CAP,
            after_maker=after_maker,
            verification_gate=verification_gate,
        )
        state, trace = asyncio.run(run_convergence_agent(agent, {
            "work_contract": compact_work_contract(
                contract, enabled=is_small_document_task(intake, requirements)
            ),
            "analysis_package": analysis.model_dump(mode="json"),
            "authoritative_sources": source_payload,
        }, initial_state=initial_state))
        self._write(
            output_dir / "adk_convergence_trace.json",
            json.dumps({
                "schema_version": "onebrief-adk-convergence-trace-v1",
                "agent_tree": {
                    "root": agent.name,
                    "maker": agent.maker.name,
                    "verifier": agent.verifier.name,
                    "same_maker_reused": True,
                },
                "events": trace,
            }, ensure_ascii=False, indent=2),
        )
        round_number = int(state.get(ROUND_STATE_KEY, 0))
        draft = DraftArtifact.model_validate(state[MAKER_STATE_KEY])
        report = VerificationReport.model_validate(state[VERIFICATION_STATE_KEY])
        return draft, report, round_number

    def _run_adk_development_convergence(
        self,
        *,
        intake: IntakeRequest,
        requirements: RequirementsAnalysis,
        sources: list[InternalSource],
        source_payload: list[dict[str, object]],
        contract: dict[str, object],
        analysis: AnalysisPackage,
        output_dir: Path,
    ) -> tuple[DraftArtifact, VerificationReport, int]:
        """Run code creation, isolated verification, review, and same-maker repair in ADK."""

        quest_initial_phase = quest_initial_execution_phase(sources)
        contract = {
            **contract,
            "execution_phase": quest_initial_phase.value,
        }
        self._write(
            output_dir / "development_verification_contract.json",
            json.dumps(contract, ensure_ascii=False, indent=2),
        )

        change_schema, development_pack, developer = self._development_components(intake, output_dir)
        unity_runtime = (
            isinstance(development_pack, ApprovedProjectDevelopmentToolPack)
            and development_pack._uses_unity_runtime(development_pack._profile())
        )
        approved_existing_evidence = (
            approved_unity_evidence_bundle_bindings(development_pack)
            if unity_runtime else {}
        )
        if isinstance(development_pack, ApprovedProjectDevelopmentToolPack):
            runtime_authority = approved_runtime_authority_source(development_pack)
            if runtime_authority is not None:
                source_payload = [
                    *source_payload,
                    runtime_authority.model_dump(mode="json"),
                ]
        prepared_sources: list[dict[str, object]] = []
        for source in source_payload:
            prepared = dict(source)
            name = str(prepared.get("name", ""))
            candidate = (
                name.removeprefix(developer.source_prefix)
                if name.startswith(developer.source_prefix)
                else ""
            )
            repository_path = developer.path_approver(candidate) if candidate else None
            prepared["repository_path"] = repository_path
            normalized = name.casefold().replace("\\", "/")
            if "/tests/" in normalized or normalized.startswith(
                f"{developer.source_prefix}tests/"
            ):
                prepared["source_role"] = "immutable_acceptance_contract"
            elif repository_path is not None:
                prepared["source_role"] = "editable_source"
            else:
                prepared["source_role"] = "read_only_context"
            prepared_sources.append(prepared)

        completion_contract_payload = (
            requirements.completion_contract.model_dump(mode="json")
            if requirements.completion_contract is not None else {}
        )
        evidence_specification = build_evidence_specification(
            completion_contract=completion_contract_payload,
            criterion_ids=(
                [
                    item.criterion_id
                    for item in requirements.completion_contract.quality_criteria
                ]
                if requirements.completion_contract is not None else []
            ),
            immutable_inputs=sorted(
                str(item.get("repository_path") or item.get("name") or "")
                for item in prepared_sources
                if item.get("source_role") == "immutable_acceptance_contract"
            ),
        )
        self._write(
            output_dir / "evidence_specification.json",
            evidence_specification.model_dump_json(indent=2),
        )

        def development_failure_report(feedback: str) -> VerificationReport:
            receipt = self._load(
                output_dir / "development" / "verification_commands.json",
                DevelopmentVerificationReceipt,
            )
            return self._development_failure_report(
                feedback,
                requirements.completion_contract,
                receipt,
            )

        def refresh_diagnostic_repository_context(
            feedback: str, ctx, round_number: int
        ) -> list[dict[str, object]]:
            """Expose new read-only source excerpts only when trusted evidence asks for inspection."""

            normalized = " ".join(feedback.split()).casefold()
            if not isinstance(development_pack, ApprovedProjectDevelopmentToolPack):
                return []
            if not any(marker in normalized for marker in (
                "protected destination evidence",
                "precondition-free click test",
                "inspect the approved repository context",
                "existing file was not included in approved model context",
                "independent unity semantic visual observation failed",
                "independent semantic observation failed",
                "deterministic unity layout diagnostics",
            )):
                return []
            context = development_pack.inspect_diagnostic_context(
                output_dir / "diagnostic_repository_context" / f"r{round_number:02d}",
                feedback,
            )
            if context:
                ctx.session.state[MAKER_DIAGNOSTIC_CONTEXT_STATE_KEY] = context
                if any(marker in normalized for marker in (
                    "semantic visual observation failed",
                    "semantic observation failed",
                    "unity layout diagnostics",
                )):
                    ctx.session.state[EXACT_EDIT_ANCHORS_STATE_KEY] = [
                        {
                            "path": str(item["path"]),
                            "anchors": list(item.get("anchors", [])),
                        }
                        for item in context
                        if item.get("anchors")
                        and path_allowed_for_phase(
                            str(item.get("path", "")),
                            ExecutionPhase.PRODUCT_IMPLEMENTATION,
                        )
                    ]
            return context

        # A Cloud continuation may restore the last verified-or-failed candidate
        # into the fresh child work directory.  Preserve that candidate as the
        # same maker's starting point instead of silently asking the model to
        # reconstruct the whole change set from source context again.
        previous_change_set: BaseModel | None = self._load(
            output_dir / "code_change_set.json", change_schema
        )
        verifier_only_revalidation = (
            previous_change_set is not None
            and (output_dir / "reverify_existing_candidate.json").is_file()
        )
        best_failed_candidate: BaseModel | None = self._load(
            output_dir / "development_best_candidate.json", change_schema
        )
        best_failure_message: str | None = None
        best_failure_quality: tuple[int, int] | None = None
        best_failure_path = output_dir / "development_best_failure.txt"
        if best_failed_candidate is not None and best_failure_path.is_file():
            best_failure_message = best_failure_path.read_text("utf-8")
            best_failure_quality = development_failure_quality(best_failure_message)
        latest_run: DevelopmentRun | None = None
        initial_state: dict[str, object] = {
            PHASE_STATE_KEY: quest_initial_phase.value,
        }
        repair_fingerprints: list[str] = []
        for plan_path in sorted(output_dir.glob("repair_plan_r*.json")):
            try:
                prior_plan = RepairPlan.model_validate_json(
                    plan_path.read_text(encoding="utf-8")
                )
            except (OSError, ValidationError):
                continue
            repair_fingerprints.extend(item.fingerprint for item in prior_plan.tasks)

        convergence_policy = ConvergencePolicy()
        convergence_ledger_path = output_dir / "convergence_ledger.json"
        try:
            convergence_ledger = ConvergenceLedger.model_validate_json(
                convergence_ledger_path.read_text(encoding="utf-8")
            )
        except (OSError, ValidationError):
            convergence_ledger = new_convergence_ledger()
        if (
            verifier_only_revalidation
            and convergence_ledger.policy_revision
            != CURRENT_CONVERGENCE_POLICY_REVISION
        ):
            prior_revision = convergence_ledger.policy_revision
            prior_payload = convergence_ledger.model_dump(mode="json")
            prior_digest = hashlib.sha256(
                json.dumps(
                    prior_payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            self._write(
                output_dir / "convergence_epochs" / f"{prior_digest}.json",
                convergence_ledger.model_dump_json(indent=2),
            )
            convergence_ledger = new_convergence_ledger()
            self._write(
                output_dir / "convergence_epoch_transition.json",
                json.dumps({
                    "schema_version": "onebrief-convergence-epoch-transition-v1",
                    "prior_policy_revision": prior_revision,
                    "prior_ledger_sha256": prior_digest,
                    "active_policy_revision": CURRENT_CONVERGENCE_POLICY_REVISION,
                    "reason": (
                        "The deterministic verification and phase-authority policy changed. "
                        "Historical failures remain immutable audit evidence but cannot count "
                        "as repeated attempts in the new policy epoch until re-observed."
                    ),
                    "candidate_reverification_required": True,
                }, ensure_ascii=False, indent=2),
            )
        latest_repair_contract: RepairContract | None = (
            convergence_ledger.repair_contracts[-1]
            if convergence_ledger.repair_contracts else None
        )

        def record_convergence_failure(
            *,
            context: str,
            failure_text: str,
            attempt_number: int,
            execution_round: int | None = None,
            affected_paths: list[str] | None = None,
            failed_criterion_ids: list[str] | None = None,
            preserve_criterion_ids: list[str] | None = None,
            strategy_fingerprint: str | None = None,
        ) -> RepairContract:
            """Persist the causal observation before authorizing another maker turn."""

            nonlocal convergence_ledger, latest_repair_contract
            observation = convergence_policy.observe(
                context=context,
                failure_text=failure_text,
                attempt_number=attempt_number,
                affected_paths=affected_paths,
                failed_criterion_ids=failed_criterion_ids,
                strategy_fingerprint=strategy_fingerprint,
            )
            contract = convergence_policy.issue_contract(
                convergence_ledger,
                observation,
                preserve_criterion_ids=preserve_criterion_ids,
            )
            convergence_ledger = convergence_policy.record(
                convergence_ledger, observation, contract
            )
            latest_repair_contract = contract
            self._write(
                convergence_ledger_path,
                convergence_ledger.model_dump_json(indent=2),
            )
            self._write(
                output_dir
                / f"repair_contract_f{len(convergence_ledger.repair_contracts):02d}.json",
                contract.model_dump_json(indent=2),
            )
            self._write(
                output_dir / "repair_contract.json",
                contract.model_dump_json(indent=2),
            )
            repair_contract_path = output_dir / "repair_contract.json"
            repair_artifact = ArtifactReference(
                artifact_type="repair_contract",
                path=repair_contract_path.relative_to(output_dir).as_posix(),
                sha256=hashlib.sha256(repair_contract_path.read_bytes()).hexdigest(),
            )
            owner = observation.owner
            stage = (
                ExecutionPhase.EVIDENCE_CONSTRUCTION.value
                if owner.value == "evidence"
                else ExecutionPhase.PRODUCT_IMPLEMENTATION.value
            )
            sender_id = "independent-verifier"
            recipient_id = "maker"
            if self.execution_graph is not None:
                try:
                    sender_id = self.execution_graph.node_for_stage(
                        "independent_verification"
                    ).owner_instance_id
                    recipient_id = self.execution_graph.node_for_stage(
                        "long_form_draft"
                    ).owner_instance_id
                except KeyError:
                    pass
            inspection = self._load(
                output_dir / "toolpacks" / ToolPackId.PROJECT_DEVELOPMENT.value
                / "evidence" / "repository_inspection.json",
                RepositoryInspection,
            )
            source_revision = (
                inspection.head_sha if inspection is not None else "unavailable"
            )
            milestone_match = re.fullmatch(r"M[0-9]{2}", output_dir.name)
            milestone_id = output_dir.name if milestone_match else "M00"
            failed_kind = (
                EvidenceKind.VISUAL
                if observation.code.value == "unity_screenshot_not_materialized"
                else EvidenceKind.OTHER
            )
            permitted_paths = phase_owned_handoff_paths(
                phase=(
                    ExecutionPhase.EVIDENCE_CONSTRUCTION
                    if owner.value == "evidence"
                    else ExecutionPhase.PRODUCT_IMPLEMENTATION
                ),
                proposed_paths=(
                    list(contract.permitted_paths)
                    or list(observation.affected_paths)
                ),
                previous_change_set=previous_change_set,
            )
            handoff = create_work_handoff(
                project_id=(
                    self.execution_graph.project_id
                    if self.execution_graph is not None else output_dir.parent.name
                ),
                milestone_id=milestone_id,
                # Failure recurrence and ADK execution round are different
                # counters. A newly classified failure has recurrence 1 even
                # when it occurs on revision round 4; the handoff must identify
                # the actual maker/verifier turn.
                round_number=(
                    max(0, execution_round)
                    if execution_round is not None
                    else max(0, attempt_number - 1)
                ),
                sender_agent_id=sender_id,
                recipient_agent_id=recipient_id,
                stage=stage,
                goal_digest=handoff_sha256(intake.goal),
                completion_contract_digest=evidence_specification.completion_contract_sha256,
                source_revision=source_revision,
                owned_criterion_ids=list(observation.failed_criterion_ids),
                preserve_passed_criterion_ids=list(contract.preserve_criterion_ids),
                failure_observation_id=observation.observation_id,
                permitted_paths=permitted_paths,
                forbidden_path_patterns=(
                    ["product source outside Tests/PlayMode"]
                    if owner.value == "evidence"
                    else ["tests", "evidence", "screenshots", "reports"]
                ),
                input_artifacts=[repair_artifact],
                evidence_bindings=[create_evidence_binding(
                    criterion_id=(
                        observation.failed_criterion_ids[0]
                        if len(observation.failed_criterion_ids) == 1 else None
                    ),
                    kind=failed_kind,
                    status=EvidenceStatus.MISSING,
                    summary=observation.normalized_signature,
                    artifact=repair_artifact,
                    observation_id=observation.observation_id,
                )],
                expected_output_schema=(
                    "UnityEvidenceSourceRepair"
                    if owner.value == "evidence"
                    else "ProjectCodeChangeSet"
                ),
                required_evidence=list(contract.verification_ladder),
            )
            receipt = accept_work_handoff(
                handoff,
                recipient_agent_id=recipient_id,
            )
            verify_handoff_receipt(handoff, receipt)
            handoff_dir = output_dir / "handoffs"
            receipt_dir = output_dir / "handoff_receipts"
            self._write(
                handoff_dir / f"{handoff.handoff_id}.json",
                handoff.model_dump_json(indent=2),
            )
            self._write(
                receipt_dir / f"{handoff.handoff_id}.json",
                receipt.model_dump_json(indent=2),
            )
            return contract

        def prepare_repair(
            report: VerificationReport, round_number: int
        ) -> RepairPlan | None:
            if requirements.completion_contract is None or report.verdict != Verdict.REVISE:
                return None
            plan = build_repair_plan(
                requirements.completion_contract,
                report,
                round_number=round_number,
                prior_fingerprints=repair_fingerprints,
            )
            if plan is None:
                return None
            if len(plan.tasks) > 1:
                selected = plan.tasks[0]
                plan = plan.model_copy(update={
                    "tasks": [selected],
                    "stop_after_this_round": selected.disposition.value == "escalate",
                    "rationale": (
                        "Execute one smallest software blocker, re-run trusted verification, then select the next blocker."
                    ),
                })
            repair_fingerprints.extend(item.fingerprint for item in plan.tasks)
            self._write(
                output_dir / (
                    f"repair_plan_r{round_number}_n{len(repair_fingerprints):02d}.json"
                ),
                plan.model_dump_json(indent=2),
            )
            return plan

        # Provider-visible context must stay separate from full trusted
        # baselines loaded later for anchored promotion of a large source.
        maker_sources = list(prepared_sources)
        prior_failure = output_dir / "development_verification_failure.txt"
        prior_failure_text = (
            prior_failure.read_text("utf-8") if prior_failure.is_file() else ""
        )
        initial_maker_phase = quest_initial_phase
        if prior_failure_text:
            initial_phase_decision = decide_repair_phase(
                context="development_verification",
                failure_text=prior_failure_text,
                round_number=0,
            )
            if initial_phase_decision.next_phase is not None:
                initial_maker_phase = initial_phase_decision.next_phase
                initial_state[PHASE_STATE_KEY] = initial_maker_phase.value
                initial_state[PHASE_DECISION_STATE_KEY] = (
                    initial_phase_decision.model_dump(mode="json")
                )
        if prior_failure_text:
            # A durable lineage can outlive its failure classifier.  Rebind the
            # primary trusted failure when a newer runtime now recognizes a
            # more precise causal layer, but never duplicate an already issued
            # contract for the same evidence and classification.
            primary_observation = convergence_policy.observe(
                context="development_verification",
                failure_text=prior_failure_text,
                attempt_number=1,
            )
            matching_contracts = [
                contract
                for contract in convergence_ledger.repair_contracts
                if contract.observation_id == primary_observation.observation_id
            ]
            if matching_contracts:
                latest_repair_contract = matching_contracts[-1]
            else:
                latest_repair_contract = record_convergence_failure(
                    context="development_verification",
                    failure_text=prior_failure_text,
                    attempt_number=1,
                )
        if latest_repair_contract is not None:
            if repair_contract_blocks_resume(
                latest_repair_contract,
                verifier_only_revalidation=verifier_only_revalidation,
            ):
                raise RuntimeError(
                    "convergence progress gate requires a new diagnosis or explicit decision before resume: "
                    + latest_repair_contract.rationale
                )
            if latest_repair_contract.execution_allowed:
                initial_state[REPAIR_CONTRACT_STATE_KEY] = (
                    latest_repair_contract.model_dump(mode="json")
                )
            else:
                self._write(
                    output_dir / "verifier_only_revalidation_receipt.json",
                    json.dumps({
                        "schema_version": "onebrief-verifier-only-revalidation-v1",
                        "repair_contract_id": latest_repair_contract.contract_id,
                        "action": "rerun deterministic tools and independent verifier",
                        "maker_mutation_allowed": False,
                        "reason": (
                            "The stopped repair contract remains authoritative for maker "
                            "changes, but does not block read-only revalidation of the "
                            "preserved candidate."
                        ),
                    }, ensure_ascii=False, indent=2),
                )
        multi_state_evidence_repair = "reused an identical screenshot" in prior_failure_text.casefold()
        unity_evidence_topology_repair = is_unity_evidence_contract_feedback(
            prior_failure_text
        )
        missing_unity_evidence_harness = is_missing_unity_evidence_harness(
            prior_failure_text
        )
        localization_product_repair = is_unity_localization_product_failure(
            prior_failure_text
        )
        product_target_repair = is_development_product_target_failure(
            prior_failure_text
        )
        semantic_visual_repair = (
            "independent unity semantic visual observation failed"
            in prior_failure_text.casefold()
            or localization_product_repair
        )
        candidate_file_visual_repair = (
            semantic_visual_repair
            and not localization_product_repair
            and visual_repair_has_uncommitted_product_candidate(previous_change_set)
        )
        anchored_range_repair = development_repair_requires_anchored_range(
            multi_state_evidence_repair=multi_state_evidence_repair,
            unity_evidence_topology_repair=unity_evidence_topology_repair,
            semantic_visual_repair=semantic_visual_repair,
        ) and not product_target_repair
        raw_exact_repair_required = not missing_unity_evidence_harness and (
            "namespace/full name begins" in prior_failure_text.casefold()
            or "the onebrief.visual test must" in prior_failure_text.casefold()
            or "inside the onebrief.visual test" in prior_failure_text.casefold()
            or "runtime evidence test must observe product text" in prior_failure_text.casefold()
            or "runtime evidence test must observe the shipped responsive layout" in prior_failure_text.casefold()
        )
        if previous_change_set is not None:
            initial_state[MAKER_STATE_KEY] = previous_change_set.model_dump(mode="json")
            if verifier_only_revalidation:
                initial_state[REVERIFY_EXISTING_STATE_KEY] = True
            changed_paths = {
                str(getattr(item, "path", ""))
                for item in getattr(previous_change_set, "changes", [])
            }
            # The current candidate is already supplied in ADK state. Avoid
            # sending the original version of the same large file a second
            # time, while retaining the trusted full sources in the closure
            # for exact-edit promotion and base-hash enforcement.
            maker_sources = []
            visible_repair_paths = set(changed_paths)
            if product_target_repair:
                # The observer proved a product localization defect but cannot
                # safely name its source file. Runtime assertions have the same
                # property. Let deterministic failure-term discovery supply a
                # bounded production working set; proof files remain excluded.
                visible_repair_paths.update(
                    str(source.get("repository_path", ""))
                    for source in relevant_product_repair_sources(
                        prepared_sources, prior_failure_text
                    )
                )
            elif latest_repair_contract is not None:
                visible_repair_paths.update(latest_repair_contract.permitted_paths)
            for source in prepared_sources:
                compact = dict(source)
                repository_path = str(compact.get("repository_path", ""))
                if repository_path not in visible_repair_paths:
                    # Repair turns still need the immutable scene/object map
                    # that explains where real Unity controls live. Without
                    # it, a test failure such as an inactive Settings control
                    # looks indistinguishable from a missing product feature
                    # and the maker guesses at the wrong surface.
                    source_name = str(compact.get("name", "")).replace("\\", "/")
                    if source_name.endswith("/unity-scene-catalog.json"):
                        maker_sources.append(compact)
                    continue
                if repository_path in changed_paths:
                    compact["content"] = (
                        "Current candidate content is authoritative in previous_artifact. "
                        "Use an exact small repair against that candidate."
                    )
                maker_sources.append(compact)
        if semantic_visual_repair:
            # Do not even offer tests, screenshots, receipts, or generated
            # evidence as editable candidates for a shipped visual defect.
            maker_sources = [
                source
                for source in maker_sources
                if visual_repair_production_target_allowed(
                    str(source.get("repository_path", ""))
                )
            ]
            if previous_change_set is not None:
                visible_candidate = visual_repair_production_candidate(
                    previous_change_set
                )
                initial_state[MAKER_STATE_KEY] = visible_candidate.model_dump(mode="json")
                if candidate_file_visual_repair:
                    # Generated production files are authoritative in
                    # previous_artifact. Re-sending the large original Unity
                    # repository context can push a small visual repair above
                    # 100k input tokens and cause provider deadlines. Keep the
                    # original sources only in the trusted promotion closure
                    # for path/base-hash checks; the maker sees the complete
                    # current candidate it is actually allowed to repair.
                    maker_sources = []
        if previous_change_set is not None and prior_failure.is_file():
            feedback = self._compact_development_feedback(
                prior_failure.read_text("utf-8")
            )
            initial_state[VERIFICATION_STATE_KEY] = development_failure_report(
                feedback
            ).model_dump(mode="json")
            continuation_report = VerificationReport.model_validate(
                initial_state[VERIFICATION_STATE_KEY]
            )
            continuation_plan = prepare_repair(continuation_report, 0)
            if continuation_plan is not None:
                initial_state[REPAIR_PLAN_STATE_KEY] = continuation_plan.model_dump(mode="json")
        repair_feedback = (
            self._compact_development_feedback(prior_failure.read_text("utf-8"))
            if prior_failure.is_file()
            else None
        )
        candidate_edit_anchors = developer.exact_edit_anchors(
            previous_change_set, repair_feedback
        )
        exact_edit_anchors = candidate_edit_anchors
        if product_target_repair:
            source_edit_anchors = product_failure_edit_anchors(
                prepared_sources, prior_failure_text
            )
            # A verifier can reject behavior introduced only in the current
            # uncommitted candidate.  Candidate windows are then the sole
            # authoritative selectors for that path; replacing them with
            # windows from approved HEAD forces a costly full-file rewrite and
            # can discard prior milestone progress.  Source windows only fill
            # paths the candidate catalog does not already own.
            exact_edit_anchors = candidate_first_edit_anchors(
                candidate_edit_anchors, source_edit_anchors
            )
            anchor_paths = {
                str(group.get("path", "")) for group in exact_edit_anchors
            }
            for source in maker_sources:
                if str(source.get("repository_path", "")) in anchor_paths:
                    source["content"] = (
                        "Full source is retained by the trusted promotion boundary. "
                        "Choose one exact_edit_anchors window and return only anchor_id plus replace."
                    )
        if semantic_visual_repair:
            exact_edit_anchors = [
                anchor
                for anchor in exact_edit_anchors
                if visual_repair_production_target_allowed(
                    str(anchor.get("path", ""))
                )
            ]
        exact_repair_required = raw_exact_repair_required or bool(exact_edit_anchors)
        current_exact_edit_anchors = exact_edit_anchors
        consecutive_identical_candidates = 0
        rejected_change_fingerprints = discover_rejected_change_fingerprints(output_dir)
        rejected_change_history = discover_rejected_change_history(output_dir)
        rejected_strategy_fingerprints = {
            str(item.get("strategy_fingerprint") or development_change_strategy_fingerprint(item))
            for item in rejected_change_history
        }

        # A prior run may already have paid for a bounded proposal that failed
        # only at trusted source promotion.  Re-promote that exact proposal
        # through the current deterministic runtime before buying another model
        # turn.  It remains untrusted until every path, selector, and base hash
        # passes the same promotion boundary.
        pending_promotion_path = output_dir / "development_pending_promotion.json"
        if pending_promotion_path.is_file() and previous_change_set is not None:
            pending_payload: dict[str, object] = {}
            try:
                loaded_pending = json.loads(
                    pending_promotion_path.read_text(encoding="utf-8")
                )
                if not isinstance(loaded_pending, dict):
                    raise ValueError("pending promotion is not a structured object")
                pending_payload = loaded_pending
                pending_proposal = ProposedProjectCodeChangeSet.model_validate(
                    pending_payload
                )
                promoted_pending = developer.promote_candidate(
                    pending_proposal,
                    prepared_sources,
                    previous_change_set,
                    current_exact_edit_anchors,
                )
                resumed_candidate = self._merge_development_retry(
                    previous_change_set, promoted_pending
                )
                resumed_candidate = self._bind_project_change_set(
                    intake, development_pack, resumed_candidate, output_dir
                )
                previous_change_set = resumed_candidate
                self._write(
                    output_dir / "code_change_set.json",
                    resumed_candidate.model_dump_json(indent=2),
                )
                initial_state[MAKER_STATE_KEY] = resumed_candidate.model_dump(mode="json")
                initial_state[REVERIFY_EXISTING_STATE_KEY] = True
                consumed_path = output_dir / "development_consumed_promotion.json"
                pending_promotion_path.replace(consumed_path)
                self._write(
                    output_dir / "development_pending_promotion_receipt.json",
                    json.dumps({
                        "schema_version": "onebrief-pending-promotion-receipt-v1",
                        "proposal_sha256": development_change_fingerprint(pending_payload),
                        "promoted_candidate_sha256": development_change_fingerprint(
                            resumed_candidate
                        ),
                        "changed_paths": [
                            str(item.path)
                            for item in getattr(promoted_pending, "changes", [])
                        ],
                        "model_call_avoided": True,
                    }, ensure_ascii=False, indent=2),
                )
            except (OSError, json.JSONDecodeError, ValidationError, ValueError) as exc:
                pending_contract = record_convergence_failure(
                    context="development_candidate_promotion",
                    failure_text=str(exc),
                    attempt_number=1,
                    affected_paths=[
                        str(item.get("path", ""))
                        for item in pending_payload.get("changes", [])
                        if isinstance(item, dict) and item.get("path")
                    ],
                    strategy_fingerprint=(
                        development_change_strategy_fingerprint(pending_payload)
                        if pending_payload else None
                    ),
                )
                initial_state[REPAIR_CONTRACT_STATE_KEY] = (
                    pending_contract.model_dump(mode="json")
                )
                self._write(
                    output_dir / "development_pending_promotion_failure.txt",
                    str(exc),
                )
                if not pending_contract.execution_allowed:
                    raise RuntimeError(
                        "convergence progress gate blocked repeated pending promotion: "
                        + pending_contract.rationale
                    ) from exc

        def after_maker(raw: object, _ctx, round_number: int) -> dict[str, object]:
            nonlocal previous_change_set, latest_run
            nonlocal best_failed_candidate, best_failure_message, best_failure_quality
            nonlocal current_exact_edit_anchors
            nonlocal consecutive_identical_candidates
            # The deterministic verifier can issue a new digest-bound source
            # catalog after the previous executable check. ADK session state is
            # authoritative for that turn; a closure captured before the check
            # must not reject the newly issued anchor as stale.
            current_exact_edit_anchors = active_exact_edit_anchors(
                current_exact_edit_anchors,
                _ctx.session.state.get(EXACT_EDIT_ANCHORS_STATE_KEY),
            )
            reverify_existing = (
                round_number == 0
                and bool(_ctx.session.state.get(REVERIFY_EXISTING_STATE_KEY))
                and previous_change_set is not None
            )
            active_report_payload = _ctx.session.state.get(VERIFICATION_STATE_KEY)
            active_report = (
                VerificationReport.model_validate(active_report_payload)
                if active_report_payload
                else None
            )
            active_contract_payload = _ctx.session.state.get(REPAIR_CONTRACT_STATE_KEY)
            active_contract = (
                RepairContract.model_validate(active_contract_payload)
                if active_contract_payload else None
            )
            raw_provider_payload = raw
            raw = normalize_atomic_unity_evidence_bundle(
                raw,
                previous_change_set,
                approved_existing_evidence,
            )
            if isinstance(raw_provider_payload, UnityEvidenceJourneyPlan) or (
                isinstance(raw_provider_payload, dict)
                and isinstance(raw_provider_payload.get("steps"), list)
                and isinstance(raw_provider_payload.get("test_directory"), str)
                and not raw_provider_payload.get("changes")
            ):
                plan_text = (
                    raw_provider_payload.model_dump_json(indent=2)
                    if isinstance(raw_provider_payload, BaseModel)
                    else json.dumps(raw_provider_payload, ensure_ascii=False, indent=2)
                )
                self._write(
                    output_dir / f"unity_evidence_journey_plan_r{round_number}.json",
                    plan_text,
                )
            proposed_paths = [
                development_change_path(item)
                for item in development_proposal_changes(raw)
                if development_change_path(item)
            ]
            active_phase = active_execution_phase(_ctx.session.state)
            phase_authority_payload = {
                "schema_version": "onebrief-phase-authority-receipt-v1",
                "round_number": round_number,
                "active_phase": active_phase.value,
                "phase_decision": _ctx.session.state.get(PHASE_DECISION_STATE_KEY),
                "model_binding": _ctx.session.state.get(
                    "onebrief_maker_model_binding"
                ),
                "proposed_paths": proposed_paths,
            }
            self._write(
                output_dir / f"phase_authority_r{round_number}.json",
                json.dumps(phase_authority_payload, ensure_ascii=False, indent=2),
            )
            if active_phase in {
                ExecutionPhase.PRODUCT_IMPLEMENTATION,
                ExecutionPhase.EVIDENCE_CONSTRUCTION,
            } and not reverify_existing:
                raw, deferred_paths = filter_development_proposal_for_phase(
                    raw, active_phase
                )
                allowed_changes = development_proposal_changes(raw)
                if deferred_paths:
                    self._write(
                        output_dir / f"phase_scope_deferred_r{round_number}.json",
                        json.dumps({
                            "schema_version": "onebrief-phase-scope-deferred-v1",
                            "active_phase": active_phase.value,
                            "deferred_paths": deferred_paths,
                            "reason": (
                                "The proposal crossed the product/evidence authority boundary. "
                                "Only changes owned by the active phase were retained."
                            ),
                        }, ensure_ascii=False, indent=2),
                    )
                    if not allowed_changes:
                        required_surface = (
                            "product source rather than tests or evidence"
                            if active_phase == ExecutionPhase.PRODUCT_IMPLEMENTATION
                            else "the executable evidence harness rather than product behavior"
                        )
                        feedback = (
                            f"The trusted failure is owned by {active_phase.value}, but the proposal "
                            f"changed only the other phase: {', '.join(deferred_paths)}. "
                            f"Keep the rejected files unchanged and repair the smallest {required_surface}."
                        )
                        convergence_contract = record_convergence_failure(
                            context="development_phase_scope_mismatch",
                            failure_text=feedback,
                            attempt_number=round_number + 1,
                            execution_round=round_number,
                            # These paths belong to the rejected phase. They
                            # are evidence of the scope mismatch, never edit
                            # authority for the next recipient.
                            affected_paths=[],
                            strategy_fingerprint=development_change_strategy_fingerprint(raw),
                        )
                        if not convergence_contract.execution_allowed:
                            raise RuntimeError(
                                "convergence progress gate blocked repeated cross-phase repair: "
                                + convergence_contract.rationale
                            )
                        report = VerificationReport(
                            verdict=Verdict.REVISE,
                            criterion_checks=[{
                                "criterion": f"Repair the {active_phase.value} failure on its owning surface",
                                "passed": False,
                                "evidence": feedback,
                            }],
                            blocking_issues=[feedback],
                            revision_instructions=[
                                f"Do not request broader authority. Modify only {required_surface}, then rerun the unchanged trusted check."
                            ],
                            missing_information=[],
                        )
                        repair_plan = prepare_repair(report, round_number)
                        return {
                            MAKER_STATE_KEY: previous_change_set.model_dump(mode="json"),
                            VERIFICATION_STATE_KEY: report.model_dump(mode="json"),
                            **({
                                REPAIR_PLAN_STATE_KEY: repair_plan.model_dump(mode="json")
                            } if repair_plan is not None else {}),
                            REPAIR_CONTRACT_STATE_KEY: convergence_contract.model_dump(mode="json"),
                            EXACT_EDIT_ANCHORS_STATE_KEY: current_exact_edit_anchors,
                            SKIP_VERIFIER_STATE_KEY: True,
                        }
                    proposed_paths = [
                        development_change_path(item)
                        for item in allowed_changes
                    ]
            outside_contract = paths_outside_active_repair_contract(
                proposed_paths,
                active_contract,
                reverify_existing=reverify_existing,
            )
            if outside_contract:
                raise PermissionError(
                    "repair proposal is outside the causal contract permitted paths: "
                    + ", ".join(outside_contract)
                )
            active_feedback = " ".join([
                *(active_report.blocking_issues if active_report else []),
                *(active_report.revision_instructions if active_report else []),
            ])
            product_target_repair = is_development_product_target_failure(active_feedback)
            evidence_contract_repair = (
                is_unity_evidence_contract_feedback(active_feedback)
                or is_missing_unity_evidence_harness(active_feedback)
            ) and not product_target_repair
            missing_atomic_members = missing_unity_evidence_bundle_paths(
                active_feedback, previous_change_set, raw
            )
            if missing_atomic_members and not reverify_existing:
                feedback = (
                    "Unity visual test contract: the evidence harness is an atomic bundle and the proposal "
                    "is incomplete. Missing: " + ", ".join(missing_atomic_members) + ". "
                    "Return the PlayMode test source and its sibling TestAssemblies asmdef together; neither "
                    "half is retained as candidate progress."
                )
                convergence_contract = record_convergence_failure(
                    context="development_evidence_topology",
                    failure_text=active_feedback + " | " + feedback,
                    attempt_number=round_number + 1,
                    execution_round=round_number,
                    affected_paths=proposed_paths,
                    strategy_fingerprint=development_change_strategy_fingerprint(raw),
                )
                if not convergence_contract.execution_allowed:
                    raise RuntimeError(
                        "convergence progress gate blocked an incomplete Unity evidence bundle: "
                        + convergence_contract.rationale
                    )
                report = development_failure_report(
                    active_feedback + " | " + feedback
                )
                repair_plan = prepare_repair(report, round_number)
                return {
                    MAKER_STATE_KEY: previous_change_set.model_dump(mode="json"),
                    VERIFICATION_STATE_KEY: report.model_dump(mode="json"),
                    **({
                        REPAIR_PLAN_STATE_KEY: repair_plan.model_dump(mode="json")
                    } if repair_plan is not None else {}),
                    REPAIR_CONTRACT_STATE_KEY: convergence_contract.model_dump(mode="json"),
                    EXACT_EDIT_ANCHORS_STATE_KEY: current_exact_edit_anchors,
                    SKIP_VERIFIER_STATE_KEY: True,
                }
            if evidence_contract_repair and not reverify_existing:
                raw_paths = [
                    development_change_path(item)
                    for item in development_proposal_changes(raw)
                ]
                forbidden = [
                    path for path in raw_paths
                    if not unity_evidence_contract_target_allowed(path)
                ]
                if forbidden:
                    feedback = (
                        "Unity runtime-evidence topology is incomplete, but the proposed repair edits "
                        "product source instead of the executed PlayMode evidence harness: "
                        + ", ".join(forbidden)
                    )
                    convergence_contract = record_convergence_failure(
                        context="development_evidence_topology",
                        failure_text=feedback,
                        attempt_number=round_number + 1,
                        execution_round=round_number,
                        affected_paths=forbidden,
                        strategy_fingerprint=development_change_strategy_fingerprint(raw),
                    )
                    if not convergence_contract.execution_allowed:
                        raise RuntimeError(
                            "convergence progress gate blocked a non-learning evidence repair: "
                            + convergence_contract.rationale
                        )
                    report = VerificationReport(
                        verdict=Verdict.REVISE,
                        criterion_checks=[{
                            "criterion": "Repair the executed Unity evidence topology",
                            "passed": False,
                            "evidence": feedback,
                        }],
                        blocking_issues=[feedback],
                        revision_instructions=[
                            "Edit exactly one C# source under Tests/PlayMode so the real product state is "
                            "captured for every required viewport and surface; do not change product UI "
                            "source for an evidence-topology blocker."
                        ],
                        missing_information=[],
                    )
                    repair_plan = prepare_repair(report, round_number)
                    return {
                        MAKER_STATE_KEY: previous_change_set.model_dump(mode="json"),
                        VERIFICATION_STATE_KEY: report.model_dump(mode="json"),
                        **({
                            REPAIR_PLAN_STATE_KEY: repair_plan.model_dump(mode="json")
                        } if repair_plan is not None else {}),
                        REPAIR_CONTRACT_STATE_KEY: convergence_contract.model_dump(mode="json"),
                        EXACT_EDIT_ANCHORS_STATE_KEY: current_exact_edit_anchors,
                        SKIP_VERIFIER_STATE_KEY: True,
                    }
            if (
                active_phase == ExecutionPhase.PRODUCT_IMPLEMENTATION
                and (semantic_visual_repair or product_target_repair)
                and not evidence_contract_repair
                and not reverify_existing
            ):
                raw_paths = [
                    development_change_path(item)
                    for item in development_proposal_changes(raw)
                ]
                forbidden = [
                    path for path in raw_paths
                    if not visual_repair_production_target_allowed(path)
                ]
                if forbidden:
                    feedback = (
                        "Independent visual observation found a shipped UI defect, but the proposed "
                        "repair edits tests or evidence instead of production UI: "
                        + ", ".join(forbidden)
                    )
                    convergence_contract = record_convergence_failure(
                        context="development_visual_target",
                        failure_text=feedback,
                        attempt_number=round_number + 1,
                        execution_round=round_number,
                        affected_paths=forbidden,
                        strategy_fingerprint=development_change_strategy_fingerprint(raw),
                    )
                    if not convergence_contract.execution_allowed:
                        raise RuntimeError(
                            "convergence progress gate blocked a non-learning visual repair: "
                            + convergence_contract.rationale
                        )
                    report = VerificationReport(
                        verdict=Verdict.REVISE,
                        criterion_checks=[{
                            "criterion": "Repair the shipped UI rather than its proof",
                            "passed": False,
                            "evidence": feedback,
                        }],
                        blocking_issues=[feedback],
                        revision_instructions=[
                            "Choose one production UI source path from previous_artifact and repair its "
                            "responsive layout or glyph handling; do not edit tests, evidence, or screenshots."
                        ],
                        missing_information=[],
                    )
                    repair_plan = prepare_repair(report, round_number)
                    return {
                        MAKER_STATE_KEY: previous_change_set.model_dump(mode="json"),
                        VERIFICATION_STATE_KEY: report.model_dump(mode="json"),
                        **({
                            REPAIR_PLAN_STATE_KEY: repair_plan.model_dump(mode="json")
                        } if repair_plan is not None else {}),
                        REPAIR_CONTRACT_STATE_KEY: convergence_contract.model_dump(mode="json"),
                        EXACT_EDIT_ANCHORS_STATE_KEY: current_exact_edit_anchors,
                        SKIP_VERIFIER_STATE_KEY: True,
                    }
            raw_dump = getattr(raw, "model_dump_json", None)
            raw_text: str | None = None
            if callable(raw_dump):
                raw_text = raw_dump(indent=2)
            elif isinstance(raw, (dict, list)):
                raw_text = json.dumps(raw, ensure_ascii=False, indent=2)
            if raw_text is not None and not reverify_existing:
                self._write(
                    output_dir / f"development_maker_raw_r{round_number}.json",
                    raw_text,
                )
            try:
                delta = (
                    previous_change_set
                    if reverify_existing
                    else developer.promote_candidate(
                        raw,
                        prepared_sources,
                        previous_change_set,
                        current_exact_edit_anchors,
                    )
                )
            except (ValidationError, ValueError) as exc:
                if raw_text is not None:
                    self._write(
                        output_dir
                        / f"development_candidate_promotion_raw_r{round_number}.json",
                        raw_text,
                    )
                    if previous_change_set is not None:
                        self._write(
                            output_dir / "development_pending_promotion.json",
                            raw_text,
                        )
                raw_payload = (
                    raw.model_dump(mode="json")
                    if isinstance(raw, BaseModel)
                    else (raw if isinstance(raw, dict) else {})
                )
                raw_changes = [
                    item for item in raw_payload.get("changes", [])
                    if isinstance(item, dict)
                ]
                source_binding_anchors = developer.source_binding_anchors(
                    raw_payload, prepared_sources
                )
                if source_binding_anchors:
                    current_exact_edit_anchors = source_binding_anchors
                probe = self.recovery_policy.decide(
                    exc,
                    context="development_candidate_promotion",
                    attempt_number=1,
                )
                same_failure_count = sum(
                    item.context == probe.context
                    and item.error_class == probe.error_class
                    and item.error_summary == probe.error_summary
                    for item in self.recovery_decisions
                )
                failure_attempt = same_failure_count + 1
                convergence_contract = record_convergence_failure(
                    context="development_candidate_promotion",
                    failure_text=str(exc),
                    attempt_number=failure_attempt,
                    execution_round=round_number,
                    affected_paths=[
                        str(item.get("path", "")) for item in raw_changes
                        if item.get("path")
                    ],
                    strategy_fingerprint=(
                        development_change_strategy_fingerprint(raw_payload)
                        if raw_payload else None
                    ),
                )
                decision = self.recovery_policy.decide(
                    exc,
                    context="development_candidate_promotion",
                    attempt_number=failure_attempt,
                )
                self._append_recovery(decision)
                self._persist_recoveries(output_dir)
                if not convergence_contract.execution_allowed:
                    raise RuntimeError(
                        "convergence progress gate blocked a non-learning repair: "
                        + convergence_contract.rationale
                    ) from exc
                if (
                    decision.action != RecoveryAction.RETURN_TO_AGENT
                    or not decision.retry_allowed
                ):
                    raise
                feedback = " ".join(str(exc).split())[:12_000]
                self._write(
                    output_dir / f"development_candidate_promotion_failure_r{round_number}.txt",
                    feedback,
                )
                report = VerificationReport(
                    verdict=Verdict.REVISE,
                    criterion_checks=[{
                        "criterion": "Safe structural edit promotion",
                        "passed": False,
                        "evidence": feedback,
                    }],
                    blocking_issues=[feedback],
                    revision_instructions=[
                        (
                            "Choose exactly one anchor_id from exact_edit_anchors and return the complete replacement "
                            "for that displayed approved-source window; do not retype search text."
                            if source_binding_anchors
                            else convergence_contract.hypothesis.cheapest_probe
                        ),
                        convergence_contract.hypothesis.repair_boundary,
                    ],
                    missing_information=[],
                )
                repair_plan = prepare_repair(report, round_number)
                return {
                    MAKER_STATE_KEY: (
                        previous_change_set.model_dump(mode="json")
                        if previous_change_set is not None else raw_payload
                    ),
                    VERIFICATION_STATE_KEY: report.model_dump(mode="json"),
                    **({
                        REPAIR_PLAN_STATE_KEY: repair_plan.model_dump(mode="json")
                    } if repair_plan is not None else {}),
                    REPAIR_CONTRACT_STATE_KEY: convergence_contract.model_dump(mode="json"),
                    EXACT_EDIT_ANCHORS_STATE_KEY: current_exact_edit_anchors,
                    SKIP_VERIFIER_STATE_KEY: True,
                }
            self._write(
                output_dir / f"code_change_set_delta_r{round_number}.json",
                delta.model_dump_json(indent=2),
            )
            delta_fingerprint = development_change_fingerprint(delta)
            delta_strategy_fingerprint = development_change_strategy_fingerprint(delta)
            repeated_exact_delta = delta_fingerprint in rejected_change_fingerprints
            # A declared strategy is diagnostic memory, not executable
            # identity. Two incomplete edits can share the same honest summary
            # while a later implementation combines their missing code paths.
            # Block byte-identical executable results here; let the convergence
            # ledger stop a materially different implementation only after
            # trusted verification proves the same causal failure again.
            if not reverify_existing and repeated_exact_delta:
                consecutive_identical_candidates += 1
                repeated_warning = (
                    "Rejected a repair delta whose exact executable content already failed trusted "
                    "verification in an earlier round. "
                    "Already rejected path(s): "
                    + ", ".join(str(item.path) for item in getattr(delta, "changes", []))
                    + ". Keep the repair on a surface that current primary evidence explicitly marks as "
                    "failing, but use a materially different mechanism or bounded range. Never switch to a "
                    "passing surface merely to avoid the duplicate gate."
                )
                preserved_failure_path = output_dir / "development_verification_failure.txt"
                preserved_failure = (
                    preserved_failure_path.read_text(encoding="utf-8").strip()
                    if preserved_failure_path.is_file() else ""
                )
                feedback = (
                    f"{preserved_failure} | Repair control: {repeated_warning}"
                    if preserved_failure else repeated_warning
                )
                convergence_contract = record_convergence_failure(
                    context="development_rejected_strategy",
                    failure_text=feedback,
                    attempt_number=consecutive_identical_candidates,
                    execution_round=round_number,
                    affected_paths=[
                        str(item.path) for item in getattr(delta, "changes", [])
                    ],
                    strategy_fingerprint=delta_strategy_fingerprint,
                )
                self._write(
                    output_dir / f"development_repeated_delta_r{round_number}.txt",
                    feedback,
                )
                self._write(
                    output_dir / "development_verification_failure.txt", feedback
                )
                if consecutive_identical_candidates >= 2:
                    raise RuntimeError(
                        "development repair stalled after two identical candidates; "
                        "the same maker must resume with a different path or bounded range: "
                        + feedback[:4_000]
                    )
                if not convergence_contract.execution_allowed:
                    raise RuntimeError(
                        "convergence progress gate blocked a repeated repair strategy: "
                        + convergence_contract.rationale
                    )
                repeated_paths = {
                    str(item.path).replace("\\", "/").casefold()
                    for item in getattr(delta, "changes", [])
                }
                current_exact_edit_anchors = [
                    anchor
                    for anchor in developer.exact_edit_anchors(
                        previous_change_set,
                        self._compact_development_feedback(feedback),
                    )
                    if str(anchor.get("path", "")).replace("\\", "/").casefold()
                    not in repeated_paths
                ]
                report = development_failure_report(feedback)
                repair_plan = prepare_repair(report, round_number)
                return {
                    MAKER_STATE_KEY: previous_change_set.model_dump(mode="json"),
                    VERIFICATION_STATE_KEY: report.model_dump(mode="json"),
                    **({
                        REPAIR_PLAN_STATE_KEY: repair_plan.model_dump(mode="json")
                    } if repair_plan is not None else {}),
                    REPAIR_CONTRACT_STATE_KEY: convergence_contract.model_dump(mode="json"),
                    EXACT_EDIT_ANCHORS_STATE_KEY: current_exact_edit_anchors,
                    SKIP_VERIFIER_STATE_KEY: True,
                }
            prior_candidate = previous_change_set
            candidate = previous_change_set if reverify_existing else (
                self._merge_development_retry(previous_change_set, delta)
                if previous_change_set is not None
                else delta
            )
            if isinstance(candidate, ProjectCodeChangeSet):
                # A corrected generated Unity test supersedes an earlier file
                # placed outside Tests/PlayMode. Production files and unrelated
                # generated files are never removed by this normalization.
                corrected_test_names = {
                    Path(item.path).name.casefold()
                    for item in candidate.changes
                    if "tests/playmode/" in item.path.replace("\\", "/").casefold()
                }
                if corrected_test_names:
                    candidate = candidate.model_copy(update={
                        "changes": [
                            item for item in candidate.changes
                            if not (
                                Path(item.path).name.casefold() in corrected_test_names
                                and "tests/playmode/" not in item.path.replace("\\", "/").casefold()
                                and item.base_sha256 is None
                            )
                        ]
                    })
            try:
                candidate = self._bind_project_change_set(
                    intake, development_pack, candidate, output_dir
                )
            except PermissionError as exc:
                feedback = " ".join(str(exc).split())
                if (
                    not isinstance(development_pack, ApprovedProjectDevelopmentToolPack)
                    or "existing file was not included in approved model context"
                    not in feedback.casefold()
                ):
                    raise
                proposed_existing_paths = list(dict.fromkeys(re.findall(
                    r"approved model context:\s*([^\s|]+)",
                    feedback,
                    re.IGNORECASE,
                )))
                if not proposed_existing_paths:
                    raise
                for path in proposed_existing_paths:
                    baseline = development_pack.trusted_promotion_source(path)
                    if not any(
                        str(item.get("repository_path", "")) == path
                        for item in prepared_sources
                    ):
                        prepared_sources.append(baseline)
                diagnostic = development_pack.inspect_diagnostic_context(
                    output_dir / "diagnostic_repository_context" / f"r{round_number:02d}",
                    feedback + " | " + json.dumps(contract, ensure_ascii=False),
                    preferred_paths=proposed_existing_paths,
                )
                current_exact_edit_anchors = [
                    {
                        "path": str(item["path"]),
                        "anchors": list(item.get("anchors", [])),
                    }
                    for item in diagnostic
                    if item.get("anchors")
                    and str(item.get("path", "")) in proposed_existing_paths
                ]
                if not current_exact_edit_anchors:
                    raise
                _ctx.session.state[MAKER_DIAGNOSTIC_CONTEXT_STATE_KEY] = diagnostic
                _ctx.session.state[EXACT_EDIT_ANCHORS_STATE_KEY] = current_exact_edit_anchors
                convergence_contract = record_convergence_failure(
                    context="development_candidate_promotion",
                    failure_text=feedback,
                    attempt_number=round_number + 1,
                    execution_round=round_number,
                    affected_paths=proposed_existing_paths,
                    strategy_fingerprint=development_change_strategy_fingerprint(raw),
                )
                if not convergence_contract.execution_allowed:
                    raise RuntimeError(
                        "convergence progress gate blocked a non-learning source-binding repair: "
                        + convergence_contract.rationale
                    ) from exc
                report = VerificationReport(
                    verdict=Verdict.REVISE,
                    criterion_checks=[{
                        "criterion": "Safe structural edit promotion",
                        "passed": False,
                        "evidence": feedback,
                    }],
                    blocking_issues=[feedback],
                    revision_instructions=[
                        "Select one supplied exact_edit_anchors ID and replace only that displayed committed source window.",
                        "Do not return the complete existing file or request broader authority.",
                    ],
                    missing_information=[],
                )
                repair_plan = prepare_repair(report, round_number)
                return {
                    MAKER_STATE_KEY: (
                        previous_change_set.model_dump(mode="json")
                        if previous_change_set is not None
                        else raw.model_dump(mode="json")
                        if isinstance(raw, BaseModel)
                        else raw
                    ),
                    VERIFICATION_STATE_KEY: report.model_dump(mode="json"),
                    **({
                        REPAIR_PLAN_STATE_KEY: repair_plan.model_dump(mode="json")
                    } if repair_plan is not None else {}),
                    REPAIR_CONTRACT_STATE_KEY: convergence_contract.model_dump(mode="json"),
                    EXACT_EDIT_ANCHORS_STATE_KEY: current_exact_edit_anchors,
                    SKIP_VERIFIER_STATE_KEY: True,
                }
            if (
                prior_candidate is not None
                and not reverify_existing
                and self._same_development_changes(prior_candidate, candidate)
            ):
                consecutive_identical_candidates += 1
                repeated_warning = (
                    "Rejected an identical repair candidate that already failed deterministic verification. "
                    "Do not repeat the same changed file content; diagnose the observed failure and choose a "
                    "different bounded edit against the current approved candidate."
                )
                preserved_failure_path = output_dir / "development_verification_failure.txt"
                preserved_failure = (
                    preserved_failure_path.read_text(encoding="utf-8").strip()
                    if preserved_failure_path.is_file() else ""
                )
                feedback = (
                    f"{preserved_failure} | Repair control: {repeated_warning}"
                    if preserved_failure else repeated_warning
                )
                convergence_contract = record_convergence_failure(
                    context="development_identical_candidate",
                    failure_text=feedback,
                    attempt_number=consecutive_identical_candidates,
                    execution_round=round_number,
                    affected_paths=[
                        str(item.path) for item in getattr(delta, "changes", [])
                    ],
                    strategy_fingerprint=delta_strategy_fingerprint,
                )
                if consecutive_identical_candidates >= 2:
                    raise RuntimeError(
                        "development repair stalled after two identical candidates; "
                        "the same maker must be resumed with a different repair strategy: "
                        + feedback[:4_000]
                    )
                if not convergence_contract.execution_allowed:
                    raise RuntimeError(
                        "convergence progress gate blocked an identical repair candidate: "
                        + convergence_contract.rationale
                    )
                self._write(
                    output_dir / f"development_repeated_candidate_r{round_number}.txt",
                    feedback,
                )
                report = development_failure_report(feedback)
                self._write(
                    output_dir / f"verification_r{round_number}.json",
                    report.model_dump_json(indent=2),
                )
                current_exact_edit_anchors = developer.exact_edit_anchors(
                    prior_candidate, self._compact_development_feedback(feedback)
                )
                repair_plan = prepare_repair(report, round_number)
                return {
                    MAKER_STATE_KEY: prior_candidate.model_dump(mode="json"),
                    VERIFICATION_STATE_KEY: report.model_dump(mode="json"),
                    **({
                        REPAIR_PLAN_STATE_KEY: repair_plan.model_dump(mode="json")
                    } if repair_plan is not None else {}),
                    REPAIR_CONTRACT_STATE_KEY: convergence_contract.model_dump(mode="json"),
                    EXACT_EDIT_ANCHORS_STATE_KEY: current_exact_edit_anchors,
                    SKIP_VERIFIER_STATE_KEY: True,
                }
            consecutive_identical_candidates = 0
            previous_change_set = candidate
            self._write(
                output_dir / f"code_change_set_r{round_number}.json",
                candidate.model_dump_json(indent=2),
            )
            self._write(
                output_dir / "code_change_set.json", candidate.model_dump_json(indent=2)
            )
            development_dir = output_dir / "development"
            if development_dir.exists():
                shutil.rmtree(development_dir)
            try:
                BudgetStore(self.run_dir).reserve_deterministic_attempt(
                    phase=(
                        ExecutionPhase.FINAL_VERIFICATION
                        if reverify_existing else active_phase
                    ),
                    purpose="isolated build, tests, runtime probes, and evidence capture",
                )
                latest_run = self._apply_development_change_set(
                    intake, development_pack, candidate, development_dir, contract
                )
            except RuntimeError as exc:
                latest_run = None
                probe = self.recovery_policy.decide(
                    exc, context="development_verification", attempt_number=1
                )
                same_failure_count = sum(
                    item.context == probe.context
                    and item.error_class == probe.error_class
                    and item.error_summary == probe.error_summary
                    for item in self.recovery_decisions
                )
                decision = self.recovery_policy.decide(
                    exc,
                    context="development_verification",
                    attempt_number=same_failure_count + 1,
                )
                self._append_recovery(decision)
                self._persist_recoveries(output_dir)
                feedback = " ".join(str(exc).split())[:12_000]
                if not reverify_existing:
                    failed_paths = ", ".join(
                        str(item.path) for item in getattr(delta, "changes", [])
                    )
                    feedback += (
                        " | Rejected repair delta changed path(s): " + failed_paths
                        + ". This exact content did not improve the trusted evidence; do not repeat it."
                    )
                self._write(
                    output_dir / f"development_verification_failure_r{round_number}.txt",
                    feedback,
                )
                self._write(output_dir / "development_verification_failure.txt", feedback)
                # Deterministic verification may turn a valid evidence-harness
                # repair into a newly observed product defect (for example, a
                # real PlayMode test proving that a requested scene control is
                # absent).  Re-route the *same* maker before the next turn;
                # otherwise the stale evidence phase keeps editing the proof
                # instead of the shipped product indefinitely.
                failure_phase_decision = decide_repair_phase(
                    context="development_verification",
                    failure_text=str(exc),
                    round_number=round_number,
                    affected_paths=[
                        str(item.path) for item in getattr(delta, "changes", [])
                    ],
                )
                self._write(
                    output_dir / f"phase_decision_deterministic_r{round_number}.json",
                    failure_phase_decision.model_dump_json(indent=2),
                )
                _ctx.session.state[PHASE_DECISION_STATE_KEY] = (
                    failure_phase_decision.model_dump(mode="json")
                )
                if failure_phase_decision.next_phase is not None:
                    _ctx.session.state[PHASE_STATE_KEY] = (
                        failure_phase_decision.next_phase.value
                    )
                if not reverify_existing:
                    rejected_change_fingerprints.add(delta_fingerprint)
                    rejected_strategy_fingerprints.add(delta_strategy_fingerprint)
                    write_rejected_change_fingerprints(
                        output_dir, rejected_change_fingerprints
                    )
                    rejected_change_history = discover_rejected_change_history(output_dir)
                    write_rejected_change_history(output_dir, rejected_change_history)
                quality = development_failure_quality(feedback)
                rolled_back_candidate = rollback_detached_development_changes(
                    candidate, feedback
                )
                if rolled_back_candidate is not candidate:
                    candidate = rolled_back_candidate
                    previous_change_set = rolled_back_candidate
                    self._write(
                        output_dir / "development_detached_target_rollback.json",
                        rolled_back_candidate.model_dump_json(indent=2),
                    )
                    self._write(
                        output_dir / "code_change_set.json",
                        rolled_back_candidate.model_dump_json(indent=2),
                    )
                if (
                    best_failure_quality is None
                    or quality >= best_failure_quality
                    or should_preserve_unity_evidence_checkpoint(candidate, str(exc))
                ):
                    # At the same verifier-owned stage and blocker count, the
                    # latest trusted result supersedes the older checkpoint.
                    # This commonly means one scenario pair was fixed and the
                    # next pair is now exposed; retaining the older failure
                    # would send the maker back to an already solved defect.
                    best_failed_candidate = candidate
                    best_failure_message = feedback
                    best_failure_quality = quality
                    self._write(
                        output_dir / "development_best_candidate.json",
                        candidate.model_dump_json(indent=2),
                    )
                    self._write(output_dir / "development_best_failure.txt", feedback)
                elif best_failed_candidate is not None and best_failure_message is not None:
                    # Do not let a later repair erase already demonstrated
                    # progress.  The next maker turn and any continuation both
                    # resume from the best deterministic checkpoint.  Preserve
                    # the rejected attempt's newer blocker as a regression
                    # constraint, otherwise the maker can repeat the same
                    # locally-improving but globally-regressing edit forever.
                    rejected_attempt_feedback = feedback
                    repair_regression = should_repair_regression_candidate(
                        checkpoint_failure=best_failure_message,
                        attempted_failure=rejected_attempt_feedback,
                    )
                    previous_change_set = (
                        candidate if repair_regression else best_failed_candidate
                    )
                    feedback = (
                        rejected_attempt_feedback if repair_regression
                        else best_failure_message
                    )
                    if (
                        rejected_attempt_feedback.strip()
                        and rejected_attempt_feedback.strip()
                        != best_failure_message.strip()
                    ):
                        feedback = (
                            (
                                f"{rejected_attempt_feedback} | Preserve the last runtime-valid "
                                f"checkpoint after this compiler repair: {best_failure_message}"
                            )
                            if repair_regression else
                            (
                                f"{best_failure_message} | Regression guard from the rejected "
                                f"attempt: {rejected_attempt_feedback}"
                            )
                        )
                    self._write(
                        output_dir / "code_change_set.json",
                        previous_change_set.model_dump_json(indent=2),
                    )
                    self._write(
                        output_dir / "development_verification_failure.txt", feedback
                    )
                # Reverification is an idempotent probe of a preserved
                # candidate, not another maker strategy. Counting it as a new
                # experiment blocks the maker before the first post-resume
                # repair whenever noisy Unity logs slightly change. Keep the
                # already-issued repair contract and expose the fresh trusted
                # failure as feedback. A real maker delta below is still
                # fingerprinted and counted normally.
                phase_changed = bool(
                    failure_phase_decision.next_phase is not None
                    and failure_phase_decision.next_phase != active_phase
                )
                convergence_contract = (
                    latest_repair_contract
                    if reverify_existing and latest_repair_contract is not None
                    else record_convergence_failure(
                        context="development_verification",
                        failure_text=str(exc),
                        attempt_number=same_failure_count + 1,
                        execution_round=round_number,
                        # A cross-phase observation has not yet identified the
                        # product file to edit.  Bind authority to the new phase
                        # and let the smallest source anchor be selected there;
                        # retaining the prior test path would create an
                        # impossible product contract.
                        affected_paths=([] if phase_changed else [
                            str(item.path) for item in getattr(delta, "changes", [])
                        ]),
                        strategy_fingerprint=delta_strategy_fingerprint,
                    )
                )
                if not convergence_contract.execution_allowed:
                    raise RuntimeError(
                        "convergence progress gate stopped verification without new evidence: "
                        + convergence_contract.rationale
                    ) from exc
                if (
                    decision.action != RecoveryAction.RETURN_TO_AGENT
                    or not decision.retry_allowed
                ):
                    raise
                report = development_failure_report(feedback)
                self._write(
                    output_dir / f"verification_r{round_number}.json",
                    report.model_dump_json(indent=2),
                )
                current_exact_edit_anchors = developer.exact_edit_anchors(
                    previous_change_set, self._compact_development_feedback(feedback)
                )
                if failure_phase_decision.next_phase is not None:
                    current_exact_edit_anchors = [
                        anchor for anchor in current_exact_edit_anchors
                        if path_allowed_for_phase(
                            str(anchor.get("path", "")),
                            failure_phase_decision.next_phase,
                        )
                    ]
                repair_plan = prepare_repair(report, round_number)
                return {
                    MAKER_STATE_KEY: previous_change_set.model_dump(mode="json"),
                    VERIFICATION_STATE_KEY: report.model_dump(mode="json"),
                    **({
                        REPAIR_PLAN_STATE_KEY: repair_plan.model_dump(mode="json")
                    } if repair_plan is not None else {}),
                    REPAIR_CONTRACT_STATE_KEY: convergence_contract.model_dump(mode="json"),
                    EXACT_EDIT_ANCHORS_STATE_KEY: current_exact_edit_anchors,
                    SKIP_VERIFIER_STATE_KEY: True,
                }
            evidence = self._development_evidence(output_dir)
            return {
                MAKER_STATE_KEY: candidate.model_dump(mode="json"),
                VERIFIER_CONTEXT_STATE_KEY: evidence or {},
                SKIP_VERIFIER_STATE_KEY: False,
            }

        def verification_gate(
            raw_report: VerificationReport, ctx, round_number: int
        ) -> VerificationReport:
            model_report = enforce_temperament_audit(raw_report, VERIFIER_PROFILE)
            if bool(ctx.session.state.get(SKIP_VERIFIER_STATE_KEY, False)):
                refresh_diagnostic_repository_context(
                    " | ".join([
                        *model_report.blocking_issues,
                        *model_report.revision_instructions,
                    ]),
                    ctx,
                    round_number,
                )
                return model_report
            self._write(
                output_dir / f"model_verification_r{round_number}.json",
                model_report.model_dump_json(indent=2),
            )
            # Source-level product behavior is proven by the isolated implementation
            # evidence. Markdown/CSV grounding applies only to document artifacts.
            grounding = DeterministicVerification()
            self._write(
                output_dir / f"deterministic_verification_r{round_number}.json",
                grounding.model_dump_json(indent=2),
            )
            evidence = self._development_evidence(output_dir)
            completion = validate_completion_evidence(intake, requirements, evidence)
            self._write(
                output_dir / f"completion_evidence_r{round_number}.json",
                completion.model_dump_json(indent=2),
            )
            report = apply_trusted_development_evidence(
                model_report, requirements, evidence
            )
            report = apply_completion_evidence_override(report, completion)
            report = apply_deterministic_override(report, grounding)
            reality = evaluate_reality_check(intake, requirements, evidence)
            self._write(
                output_dir / f"reality_check_r{round_number}.json",
                reality.model_dump_json(indent=2),
            )
            report = apply_reality_check_override(
                report, reality, requirements.completion_contract
            )
            if requirements.completion_contract is not None:
                report = settle_consistent_verification(
                    requirements.completion_contract, report
                )
            if report.verdict == Verdict.REVISE:
                failure_text = " | ".join([
                    *report.blocking_issues,
                    *report.revision_instructions,
                ]).strip()
                affected_paths = [
                    str(getattr(item, "path", ""))
                    for item in getattr(previous_change_set, "changes", [])
                ]
                phase_decision = decide_repair_phase(
                    context="development_acceptance_verification",
                    failure_text=failure_text or "Acceptance verification requested revision.",
                    round_number=round_number,
                    affected_paths=affected_paths,
                )
                self._write(
                    output_dir / f"phase_decision_r{round_number}.json",
                    phase_decision.model_dump_json(indent=2),
                )
                ctx.session.state[PHASE_DECISION_STATE_KEY] = (
                    phase_decision.model_dump(mode="json")
                )
                if phase_decision.next_phase is not None:
                    ctx.session.state[PHASE_STATE_KEY] = phase_decision.next_phase.value
                if not phase_decision.model_repair_allowed:
                    report = VerificationReport(
                        verdict=Verdict.UNVERIFIABLE,
                        criterion_checks=report.criterion_checks,
                        blocking_issues=list(dict.fromkeys([
                            *report.blocking_issues,
                            phase_decision.rationale,
                        ])),
                        revision_instructions=[],
                        missing_information=report.missing_information,
                        temperament_decisions=report.temperament_decisions,
                    )
            repair_plan = prepare_repair(report, round_number)
            if repair_plan is not None:
                ctx.session.state[REPAIR_PLAN_STATE_KEY] = repair_plan.model_dump(mode="json")
                if repair_plan.stop_after_this_round:
                    report = VerificationReport(
                        verdict=Verdict.UNVERIFIABLE,
                        criterion_checks=report.criterion_checks,
                        blocking_issues=list(dict.fromkeys([
                            *report.blocking_issues,
                            "The same completion failure repeated three times; blind retries are stopped.",
                        ])),
                        revision_instructions=[],
                        missing_information=report.missing_information,
                        temperament_decisions=report.temperament_decisions,
                    )
            feedback = " ".join([
                *report.blocking_issues,
                *report.revision_instructions,
            ])[:12_000]
            if report.verdict == Verdict.REVISE:
                refresh_diagnostic_repository_context(feedback, ctx, round_number)
            if is_development_product_target_failure(feedback):
                ctx.session.state[EXACT_EDIT_ANCHORS_STATE_KEY] = (
                    product_failure_edit_anchors(prepared_sources, feedback)
                )
            else:
                ctx.session.state[EXACT_EDIT_ANCHORS_STATE_KEY] = (
                    [] if exact_repair_required else developer.exact_edit_anchors(
                        previous_change_set, feedback
                    )
                )
            self._write(
                output_dir / f"verification_r{round_number}.json",
                report.model_dump_json(indent=2),
            )
            return report

        maker_instruction = (
            "You are OneBrief's accountable software maker. Return the smallest complete runnable source "
            "change set that satisfies the work contract. Existing-file paths must exactly match a non-null "
            "repository_path and retain its exact sha256 as base_sha256; new text source files use null. "
            "When the response schema is UnityEvidenceJourneyPlan, do not author or repair C# or asmdef text. "
            "Return only the ordered declarative scene/control journey. OneBrief compiles that plan into a "
            "trusted PlayMode harness, assertions, screenshots, and an atomic evidence manifest. For protected "
            "login destinations, include the existing approved test-account, terms, session, or credential "
            "precondition before the shipped navigation control. When onebrief-approved-runtime-authority.json "
            "is present, it is the mandatory exact fixture: use authentication_selector before its paired "
            "authentication_submit and do not substitute any other login path. "
            "Files marked immutable_acceptance_contract may not be changed. Never touch secrets, dependencies, "
            "Git metadata, deployment, accounts, financial transactions, or paths outside the approved project. "
            "For every existing-file change, use one exact search/replace edit and never return the entire file; "
            "the search text must occur exactly once in the approved source. "
            "When exact_edit_anchors are supplied, prefer its anchor_id and return the complete replacement for "
            "that displayed source window; OneBrief resolves the ID deterministically. Otherwise copy search text "
            "only from those verbatim windows and keep each edit to the smallest unique anchor. "
            "When execution_phase is product_implementation, a UI goal's initial result must include actual production "
            "UI source changes; test-only output is never a complete implementation. When execution_phase is "
            "evidence_construction, preserve shipped product source and construct only the requested executable proof. "
            "A verification test may interact with and capture the product, "
            "but it must never rewrite visible labels, dropdown option text, fonts, CanvasScaler, anchors, colors, "
            "or other product UI state merely to make evidence pass; repair production source instead. On revision, "
            "repair every build, test, runtime, or independent-review failure while preserving all "
            "previously passing behavior. New files may use complete content; existing files must use exact edits. "
            "A compact repair may add a bounded new text file with complete content and a null base hash when the "
            "verification evidence explicitly requires a missing test, manifest, configuration, or sidecar file. "
            "When verification says a generated test or asmdef is in the wrong directory, do not edit that old new-file "
            "entry in place: add the corrected file under a dedicated Tests/PlayMode path. OneBrief will supersede the "
            "older generated path when both represent the same test contract. "
            "Never echo unchanged previous_artifact files in a repair delta. Keep each new file or replacement under "
            "20,000 characters and return exactly one changed path per repair turn, except when trusted Unity "
            "feedback requires the atomic evidence harness: then return exactly two new files together, one "
            "Tests/PlayMode .cs source and one sibling test .asmdef. Trailing whitespace is "
            "normalized deterministically, so do not spend a repair change only reformatting it. "
            "Prefer small incremental changes that can be verified and extended in later rounds. Return only the schema."
            " When repair_plan is present, treat it as the complete scope of this revision: repair those failed "
            "criterion slices only, preserve every passing criterion listed there, and do not redesign unrelated behavior. "
            "A decompose_scope task must be reduced to one independently verifiable source change before editing."
            " When independent semantic observation reports a visual defect across several real Unity surfaces, "
            "do not keep tuning one local control. Select the smallest production-owned cross-surface styling hook; "
            "when no existing component owns the shared concern, one bounded new runtime theme component with a safe "
            "RuntimeInitializeOnLoadMethod entrypoint is preferable to unrelated local edits. Preserve product assets, "
            "behavior, and identity unless the active contract explicitly authorizes replacing them."
            " When repair_contract is present, it is the causal contract for this turn. Follow its cheapest_probe, "
            "stay inside permitted_paths and repair_boundary, and produce evidence in its verification_ladder order. "
            "Do not claim a different cause merely to evade the progress gate; if the probe cannot distinguish the "
            "hypothesis, make no unrelated product edit."
            " The execution_phase is an authority boundary: product_implementation may edit only shipped product "
            "source, while evidence_construction may edit only tests and executable evidence harnesses. Never cross "
            "that boundary or repair a product defect by weakening its proof."
            " For web language repairs, expose a real select whose identity contains language or locale and whose "
            "option values are canonical locale codes such as ko and en. Activating every option must update "
            "document.documentElement.lang and visibly change all meaningful page copy, not only the status line. "
            "For an English state, translate or conditionally render the headings, controls, cards, explanatory "
            "copy, and disclaimer so the remaining non-Latin copy does not dominate the page. A generic button with only "
            "an aria-label is not a verifiable language control."
            + (("\n\n" + developer.skill_context) if developer.skill_context else "")
        )
        if rejected_change_history:
            maker_instruction += (
                "\n\nREJECTED REPAIR HISTORY (trusted verification failed; never repeat these exact strategies):\n"
                + json.dumps(rejected_change_history, ensure_ascii=False)
                + "\nUse a materially different mechanism or bounded range on a surface that the current "
                "primary evidence explicitly marks as failing. Do not modify a passing surface just to choose "
                "a different path."
            )
        if prior_failure.is_file():
            maker_instruction += (
                "\n\nCOMPACT REPAIR CONTRACT: Return exactly one changed path, except that a missing Unity "
                "evidence harness must return its PlayMode .cs and sibling TestAssemblies .asmdef together as "
                "two paths. Keep the entire JSON "
                "response below 8,000 characters. This turn repairs only the first deterministic blocker in "
                "repair_plan; do not attempt the whole product scope. If the blocker requires a missing Unity "
                "PlayMode test, add one minimal test source below 6,000 characters that loads and interacts with "
                "the real project scene. Do not echo previous_artifact, explanatory comments, helper frameworks, "
                "or unrelated acceptance criteria. Later repair turns will address later blockers."
            )
        if (
            "runtime evidence test must observe product text" in prior_failure_text.casefold()
            or "runtime evidence test must observe the shipped responsive layout" in prior_failure_text.casefold()
        ):
            maker_instruction += (
                "\nEVIDENCE-INTEGRITY REPAIR: The current PlayMode test already contains forbidden product "
                "mutations. Make one exact replacement in that test which removes both the dropdown option-text "
                "rewrite block and the CanvasScaler assignment block. Keep the real UI discovery, interaction, "
                "screenshot capture, and glyph observation. Do not add any replacement mutation and do not try "
                "to solve the underlying visual defect in this cleanup turn; trusted verification will expose "
                "that production defect again on the next turn."
            )
        if anchored_range_repair:
            maker_instruction += (
                "\nANCHORED-RANGE REPAIR: This evidence fix spans an existing capture region in one generated "
                "PlayMode test. Return one change with start_anchor and end_anchor copied verbatim from the current "
                "previous_artifact and replace that one range. Do not use search, anchor_id, or full-file content. "
                "The replacement must preserve measured desktop and mobile viewport setup and execute and write "
                "each unique screenshot at the moment Login, Lobby, and Settings is actually visible, then write "
                "matching scenario metadata. When the failure names a missing locale, add only one real locale "
                "interaction and its measured before/after visible-text evidence; preserve every passing scenario."
            )
        elif semantic_visual_repair and candidate_file_visual_repair:
            maker_instruction += (
                "\nCATALOG-ANCHORED CANDIDATE PRODUCTION REPAIR: The independent observer found a real rendered "
                "UI defect and previous_artifact contains newly generated production files that do not exist in "
                "the source repository yet. Choose exactly one anchor_id displayed in exact_edit_anchors for a "
                "currently failing production UI path and replace that complete displayed window. Retain its null "
                "base_sha256. Do not return start_anchor, end_anchor, the complete file, tests, screenshots, "
                "evidence metadata, or assertions."
            )
        elif exact_repair_required and semantic_visual_repair:
            maker_instruction += (
                "\nCATALOG-ANCHORED PRODUCTION REPAIR: The independent observer found a real rendered UI defect. "
                "Choose exactly one anchor_id displayed in exact_edit_anchors for a currently failing production "
                "UI path and replace that complete displayed window. Do not return start_anchor or end_anchor. "
                "Fix the highest-priority observed defect in shipped UI code; do not edit tests, screenshots, "
                "evidence metadata, or assertions. Preserve server protocol and existing behavior."
            )
        elif product_target_repair and exact_edit_anchors:
            maker_instruction += (
                "\nCATALOG-ANCHORED RUNTIME PRODUCT REPAIR: Trusted executable evidence proved a shipped "
                "runtime defect. Choose exactly one displayed anchor_id from one approved production path and "
                "replace only that complete source window. Do not return full content, search selectors, range "
                "selectors, tests, assertions, screenshots, or evidence metadata. Preserve server protocol and "
                "every previously passing behavior."
            )
        elif exact_repair_required:
            maker_instruction += (
                "\nEXACT-EDIT ONLY: Use one exact search/replace copied verbatim from previous_artifact. "
                "Do not return anchor_id or full-file content."
            )
        verifier_instruction = (
            "You are OneBrief's independent software verifier. You did not author the code. Compare every "
            "deliverable and acceptance criterion against the changed source, isolated build and test commands, "
            "runtime evidence, and trusted observation receipts. Legacy regression tests alone do not prove new "
            "behavior. PASS only when the implementation evidence proves the requested behavior and no blocker "
            "remains. Use REVISE for correctable code and NEEDS_INFORMATION only for an absent authoritative user "
            "decision. For each completion_contract criterion, return exactly one check with its Q-prefixed "
            "criterion_id; leave criterion_id null only for additional system checks. Never edit the code. "
            "Return exact, actionable revision instructions and only the schema. "
            + VERIFIER_PROFILE.instruction()
        )
        maker_model = self.stage_models.get("long_form_draft", "gemini-3.5-flash")
        maker_stage = "long_form_draft"
        model_selection_attempt = len(list(output_dir.glob("model_selection_r*.json")))
        repair_difficulty = development_repair_difficulty(
            intake,
            requirements,
            [
                str(source.get("repository_path", ""))
                for source in prepared_sources
                if source.get("repository_path")
            ],
        )

        def select_maker_model(
            report: VerificationReport | None, _ctx, round_number: int
        ) -> dict[str, object] | None:
            nonlocal model_selection_attempt
            if report is None or report.verdict != Verdict.REVISE:
                return None
            failure_text = " | ".join([
                *report.blocking_issues,
                *report.revision_instructions,
            ]).strip()
            existing_phase_payload = _ctx.session.state.get(
                PHASE_DECISION_STATE_KEY
            )
            try:
                phase_decision = PhaseDecision.model_validate(
                    existing_phase_payload
                )
            except (ValidationError, TypeError):
                affected_paths = [
                    str(getattr(item, "path", ""))
                    for item in getattr(previous_change_set, "changes", [])
                ]
                phase_decision = decide_repair_phase(
                    context="development_acceptance_verification",
                    failure_text=failure_text,
                    round_number=round_number,
                    affected_paths=affected_paths,
                )
            if not phase_decision.model_repair_allowed or phase_decision.next_phase is None:
                return None
            _ctx.session.state[PHASE_STATE_KEY] = phase_decision.next_phase.value
            _ctx.session.state[PHASE_DECISION_STATE_KEY] = (
                phase_decision.model_dump(mode="json")
            )
            selector = getattr(self.gateway, "select_model_after_failure", None)
            selection = None
            if failure_text and callable(selector):
                model_selection_attempt += 1
                selection = selector(
                    "long_form_draft",
                    failure_text=failure_text,
                    difficulty=repair_difficulty,
                    attempt=model_selection_attempt,
                )
                self._write(
                    output_dir / f"model_selection_r{model_selection_attempt}.json",
                    selection.model_dump_json(indent=2),
                )
            return {
                "model": (
                    selection.selected_model.value if selection is not None else maker_model
                ),
                "stage": phase_stage(
                    phase_decision.next_phase,
                    f"repair::{selection.call_stage if selection is not None else 'long_form_draft'}",
                ),
                "round_number": round_number,
                "execution_phase": phase_decision.next_phase.value,
                "phase_decision": phase_decision.model_dump(mode="json"),
                "decision": (
                    selection.model_dump(mode="json") if selection is not None else {
                        "reason": "phase-routed repair retained the approved maker model"
                    }
                ),
            }

        def select_maker_schema(
            report: VerificationReport | None, _ctx, _round_number: int
        ) -> type | None:
            selected = development_maker_schema_for(
                report,
                _ctx.session.state.get(MAKER_STATE_KEY),
                _ctx.session.state.get(EXACT_EDIT_ANCHORS_STATE_KEY),
                active_execution_phase(_ctx.session.state),
            )
            return selected
        agent = build_text_convergence_agent(
            gateway=self.gateway,
            maker_model=maker_model,
            maker_stage=phase_stage(
                initial_maker_phase,
                f"repair::{maker_stage}" if prior_failure_text else maker_stage,
            ),
            verifier_stage=phase_stage(
                ExecutionPhase.FINAL_VERIFICATION, "independent_verification"
            ),
            verifier_model=self.stage_models.get(
                "independent_verification", "gemini-3.5-flash"
            ),
            maker_schema=(
                UnityEvidenceJourneyPlan
                if (
                    unity_runtime
                    and initial_maker_phase == ExecutionPhase.EVIDENCE_CONSTRUCTION
                    and (
                        not prior_failure.is_file()
                        or
                        is_unity_evidence_contract_feedback(prior_failure_text)
                        or is_missing_unity_evidence_harness(prior_failure_text)
                        or "unity_playmode_visual_tests" in prior_failure_text.casefold()
                    )
                )
                else (
                    (
                        catalog_bound_product_repair_schema(exact_edit_anchors)
                        if product_target_repair and exact_edit_anchors
                        else (
                            AnchoredRangeRepairProjectCodeChangeSet
                            if anchored_range_repair
                            else ExactRepairProjectCodeChangeSet
                            if exact_repair_required
                            else CompactProposedProjectCodeChangeSet
                        )
                    )
                    if prior_failure.is_file()
                    else ProposedProjectCodeChangeSet
                )
                if change_schema is ProjectCodeChangeSet
                else change_schema
            ),
            max_revision_rounds=intake.max_revision_rounds,
            maker_instruction=maker_instruction,
            verifier_instruction=verifier_instruction,
            maker_output_tokens=(
                min(
                    DEVELOPER_OUTPUT_CAP,
                    4_000 if product_target_repair and exact_edit_anchors else (
                        8_000 if anchored_range_repair else (
                            8_000 if exact_repair_required else 10_000
                        )
                    ),
                )
                if prior_failure.is_file()
                else DEVELOPER_OUTPUT_CAP
            ),
            verifier_output_tokens=VERIFIER_OUTPUT_CAP,
            maker_model_selector=select_maker_model,
            maker_schema_selector=select_maker_schema,
            after_maker=after_maker,
            verification_gate=verification_gate,
        )
        state, trace = asyncio.run(run_convergence_agent(agent, {
            "work_contract": contract,
            "analysis_package": analysis.model_dump(mode="json"),
            "approved_repository_files": maker_sources,
            "exact_edit_anchors": exact_edit_anchors,
        }, initial_state={
            **initial_state,
            EXACT_EDIT_ANCHORS_STATE_KEY: exact_edit_anchors,
        }))
        self._write(
            output_dir / "adk_convergence_trace.json",
            json.dumps({
                "schema_version": "onebrief-adk-convergence-trace-v1",
                "workflow": "software_creation_isolated_verification_review_revision",
                "agent_tree": {
                    "root": agent.name,
                    "maker": agent.maker.name,
                    "verifier": agent.verifier.name,
                    "same_maker_reused": True,
                },
                "events": trace,
            }, ensure_ascii=False, indent=2),
        )
        round_number = int(state.get(ROUND_STATE_KEY, 0))
        report = VerificationReport.model_validate(state[VERIFICATION_STATE_KEY])
        if latest_run is None:
            draft = DraftArtifact(
                title="소프트웨어 제작 검증 미완료",
                body_markdown=(
                    "격리된 빌드 또는 테스트가 아직 통과하지 못했습니다. 검증 기록과 수정 지시를 "
                    "보존했으며 승인된 수정 횟수 안에서 더 이상 수렴하지 못했습니다."
                ),
                cited_finding_ids=[item.finding_id for item in analysis.findings],
                drafting_decisions=["실행 증거가 없는 코드를 완성본으로 표시하지 않았습니다."],
            )
        else:
            commands = "\n".join(
                f"- `{item.command_id}`: 통과 (종료 코드 {item.exit_code})"
                for item in latest_run.commands
            )
            changed = "\n".join(f"- `{item}`" for item in latest_run.changed_paths)
            draft = DraftArtifact(
                title="격리 빌드·테스트를 통과한 소프트웨어 개선본",
                body_markdown=(
                    "요청된 변경을 원본과 분리된 작업 공간에서 구현하고 검증했습니다.\n\n"
                    f"## 변경 파일\n\n{changed}\n\n## 자동 검증\n\n{commands}\n\n"
                    "## 전달물\n\n- `development/changes.patch`\n- `development/changed_files/`\n"
                    "- `development/development_run.json`"
                ),
                cited_finding_ids=[item.finding_id for item in analysis.findings],
                drafting_decisions=[
                    "원본 대신 격리 복제본에서 변경했습니다.",
                    "실제 빌드·테스트 증거를 독립 검증에 전달했습니다.",
                ],
            )
        self._write(
            output_dir / f"draft_r{round_number}.json", draft.model_dump_json(indent=2)
        )
        return draft, report, round_number
    def _checkpoint(
        self,
        output_dir: Path,
        status: PipelineStatus,
        stage: str,
        completed: list[str],
        revision_round: int,
        verdict: Verdict | None = None,
        message: str = "",
    ) -> None:
        checkpoint = ExecutionCheckpoint(
            status=status,
            current_stage=stage,
            completed_stages=completed,
            revision_round=revision_round,
            final_verdict=verdict,
            message=message,
        )
        self._write(output_dir / "execution_checkpoint.json", checkpoint.model_dump_json(indent=2))
        contract = getattr(self, "_active_completion_contract", None)
        if contract is not None:
            refresh_completion_ledger(contract, output_dir)

    def run(
        self,
        *,
        intake: IntakeRequest,
        requirements: RequirementsAnalysis,
        sources: list[InternalSource],
        output_dir: Path,
    ) -> ExecutionCheckpoint:
        self.recovery_decisions = []
        requirements = require_ready_for_estimate(
            intake.model_copy(update={"internal_sources": sources}), requirements, sources
        )
        self._active_completion_contract = requirements.completion_contract
        contract = {
            "goal": intake.goal,
            "desired_output": intake.desired_output,
            "output_target": intake.output_target.value,
            "normalized_goal": requirements.normalized_goal,
            "deliverables": requirements.deliverables,
            "acceptance_criteria": requirements.acceptance_criteria,
            "completion_contract": (
                requirements.completion_contract.model_dump(mode="json") if requirements.completion_contract else None
            ),
            "assumptions": requirements.assumptions,
            "public_research_allowed": intake.public_research_allowed,
        }
        source_payload = [
            {
                "name": source.name,
                "priority": source.priority.value,
                "requirement_keys": source.requirement_keys,
                "content": source.content,
                "sha256": source.sha256,
            }
            for source in sources
        ]
        completed: list[str] = []
        revision_round = 0
        runtime = (
            ExecutionGraphRuntime(self.execution_graph, output_dir / "execution_graph_state.json")
            if self.execution_graph is not None
            else None
        )

        def graph_begin(stage: str) -> bool:
            if runtime is None:
                return True
            node = runtime.graph.node_for_stage(stage)
            if runtime.state.nodes[node.node_id].status == NodeStatus.COMPLETE:
                return False
            runtime.start(node.node_id)
            return True

        def graph_complete(stage: str, *paths: str, message: str = "") -> None:
            if runtime is None:
                return
            node = runtime.graph.node_for_stage(stage)
            if runtime.state.nodes[node.node_id].status != NodeStatus.COMPLETE:
                runtime.complete(node.node_id, *paths, message=message)

        def has_graph_stage(stage: str) -> bool:
            if runtime is None:
                return False
            try:
                runtime.graph.node_for_stage(stage)
                return True
            except KeyError:
                return False

        def run_handoff(stage: str, payload: dict[str, object]) -> RoleHandoff | None:
            if not has_graph_stage(stage):
                return None
            path = output_dir / f"{stage}.json"
            handoff = self._load(path, RoleHandoff)
            graph_begin(stage)
            if handoff is None:
                handoff = DynamicRoleAgent(
                    self.gateway,
                    stage,
                    self.stage_models.get(stage, "gemini-3.5-flash"),
                ).run(payload)
                self._write(path, handoff.model_dump_json(indent=2))
            graph_complete(stage, path.name)
            return handoff
        def fail_running_graph(message: str, *, blocked: bool = False) -> None:
            if runtime is None:
                return
            for node_id, record in runtime.state.nodes.items():
                if record.status == NodeStatus.RUNNING:
                    runtime.fail(node_id, message, blocked=blocked)


        try:
            architecture = run_handoff(
                "project_architecture",
                {"contract": contract, "sources": source_payload},
            )
            quest_bound = any(
                "active_quest" in source.requirement_keys for source in sources
            )
            if architecture is not None and not quest_bound:
                contract["project_architecture"] = architecture.model_dump(mode="json")

            public_research: PublicResearchResult | None = None
            tool_sources: list[InternalSource] = []
            research_reentry_path = output_dir / "research_reentry_request.json"
            research_reentry_requested = research_reentry_path.is_file()
            parallel_work: dict[str, object] = {}
            with ThreadPoolExecutor(max_workers=2, thread_name_prefix="onebrief-context") as executor:
                if intake.toolpack_ids:
                    graph_begin("tool_execution")
                    self._checkpoint(
                        output_dir, PipelineStatus.RUNNING, "parallel_context", completed, 0
                    )
                    if ToolPackId.PROJECT_DEVELOPMENT in intake.toolpack_ids:
                        parallel_work["tool_execution"] = executor.submit(
                            execute_toolpacks, intake.toolpack_ids,
                            output_dir / "toolpacks", intake.existing_project_id,
                            development_toolpack_focus_text(intake, requirements),
                            self.project_registry_root,
                        )
                    else:
                        parallel_work["tool_execution"] = executor.submit(
                            execute_toolpacks, intake.toolpack_ids, output_dir / "toolpacks"
                        )

                if intake.public_research_allowed:
                    research_path = output_dir / "public_research.json"
                    graph_begin("public_research")
                    self._checkpoint(
                        output_dir, PipelineStatus.RUNNING, "parallel_context", completed, 0
                    )

                    def load_or_research() -> PublicResearchResult | None:
                        existing = self._load(research_path, PublicResearchResult)
                        if existing is not None and not research_reentry_requested:
                            return existing
                        unavailable = output_dir / "public_research_unavailable.json"
                        if unavailable.is_file() and not research_reentry_requested:
                            return None
                        try:
                            request = (
                                json.loads(research_reentry_path.read_text(encoding="utf-8"))
                                if research_reentry_requested else {}
                            )
                            max_calls = int(request.get("max_refinement_calls", 1))
                            max_calls = max(1, min(max_calls, 3))
                            prior = existing
                            blockers = [str(item) for item in request.get("blocking_issues", [])]
                            result = None
                            remaining_issues = []
                            for attempt in range(1, max_calls + 1):
                                stage = (
                                    f"public_research_refinement_r{attempt}"
                                    if research_reentry_requested else "public_research"
                                )
                                result = run_grounded_research(
                                    self.gateway,
                                    goal=intake.goal,
                                    desired_output=intake.desired_output,
                                    completion_contract=(
                                        requirements.completion_contract.model_dump(mode="json")
                                        if requirements.completion_contract else None
                                    ),
                                    stage=stage,
                                    prior_research=(
                                        prior.as_internal_source().content if prior else None
                                    ),
                                    blocking_issues=blockers,
                                )
                                if prior is not None:
                                    result = result.model_copy(update={
                                        "sources": merge_public_sources(
                                            prior.sources, result.sources
                                        ),
                                    })
                                if research_reentry_requested:
                                    self._write(
                                        output_dir / f"public_research_refinement_r{attempt}.json",
                                        result.model_dump_json(indent=2),
                                    )
                                    self._write(
                                        output_dir / f"public_research_refinement_r{attempt}.md",
                                        result.answer_markdown,
                                    )
                                    remaining_issues = research_reentry_issues(
                                        intake,
                                        requirements,
                                        result.answer_markdown,
                                        grounded_sources=result.sources,
                                    )
                                    if not remaining_issues:
                                        break
                                    blockers = [item.message for item in remaining_issues]
                                    prior = result
                            if result is None:
                                raise RuntimeError("research re-entry produced no result")
                            if research_reentry_requested:
                                self._write(
                                    output_dir / "research_reentry_status.json",
                                    json.dumps({
                                        "schema_version": "onebrief-research-reentry-status-v1",
                                        "resolved": not remaining_issues,
                                        "remaining_issues": [
                                            item.model_dump(mode="json") for item in remaining_issues
                                        ],
                                    }, ensure_ascii=False, indent=2),
                                )
                        except ValueError as exc:
                            if (
                                "no grounded source urls" not in str(exc).casefold()
                                or not sources
                            ):
                                raise
                            self._write(
                                output_dir / "public_research_unavailable.json",
                                json.dumps({
                                    "status": "no_grounded_sources",
                                    "message": str(exc),
                                    "fallback": "authoritative internal sources",
                                }, ensure_ascii=False, indent=2),
                            )
                            return None
                        self._write(research_path, result.model_dump_json(indent=2))
                        self._write(output_dir / "public_research.md", result.answer_markdown)
                        if result.search_suggestions_html:
                            self._write(
                                output_dir / "google_search_suggestions.html",
                                result.search_suggestions_html,
                            )
                        return result

                    parallel_work["public_research"] = executor.submit(load_or_research)

                outcomes: dict[str, object] = {}
                failures: dict[str, Exception] = {}
                for stage, future in parallel_work.items():
                    try:
                        outcomes[stage] = future.result()
                    except Exception as exc:
                        failures[stage] = exc

            if "tool_execution" in outcomes:
                _, tool_sources = outcomes["tool_execution"]
                graph_complete(
                    "tool_execution",
                    "toolpacks/toolpack_execution.json",
                    message="Approved ToolPack checks completed in the parallel context phase.",
                )
                completed.append("tool_execution")

            if "public_research" in outcomes:
                public_research = outcomes["public_research"]
                if public_research is None:
                    graph_complete(
                        "public_research",
                        "public_research_unavailable.json",
                        message=(
                            "Google Search returned no grounded URLs; the run continued only because "
                            "authoritative internal sources were already supplied."
                        ),
                    )
                else:
                    graph_complete("public_research", "public_research.json", "public_research.md")
                completed.append("public_research")

            if failures:
                if runtime is not None:
                    for stage, exc in failures.items():
                        node = runtime.graph.node_for_stage(stage)
                        if runtime.state.nodes[node.node_id].status == NodeStatus.RUNNING:
                            runtime.fail(node.node_id, str(exc))
                raise next(iter(failures.values()))

            sources = [*sources, *tool_sources]
            if public_research is not None:
                sources.append(public_research.as_internal_source())
            source_payload = [
                {
                    "name": source.name,
                    "priority": source.priority.value,
                    "requirement_keys": source.requirement_keys,
                    "content": source.content,
                    "sha256": source.sha256,
                }
                for source in sources
            ]
            analysis_path = output_dir / "analysis.json"
            graph_begin("evidence_analysis")
            analysis = (
                None if research_reentry_requested
                else self._load(analysis_path, AnalysisPackage)
            )
            if analysis is None:
                self._checkpoint(output_dir, PipelineStatus.RUNNING, "evidence_analysis", completed, 0)
                analysis = self.analyst.run(contract, source_payload)
                self._write(analysis_path, analysis.model_dump_json(indent=2))
            graph_complete("evidence_analysis", "analysis.json")
            completed.append("evidence_analysis")

            creative = run_handoff(
                "creative_direction",
                {"contract": contract, "analysis": analysis.model_dump(mode="json")},
            )
            if creative is not None:
                contract["creative_direction"] = creative.model_dump(mode="json")

            adk_report: VerificationReport | None = None
            advertised_adk = getattr(self.gateway, "supports_adk", None)
            use_adk_convergence = (
                bool(advertised_adk)
                if advertised_adk is not None
                else callable(getattr(self.gateway, "generate_adk_response", None))
            )
            # A trusted automatic-resume child already carries the same maker's
            # verified change set. Continue with the independent verifier instead
            # of paying a new maker to recreate identical work.
            if (
                use_adk_convergence
                and self._is_development(intake)
                and (output_dir / "automatic_resume.json").is_file()
                and self._development_evidence(output_dir) is not None
            ):
                self._observe_seeded_unity_evidence(output_dir, contract)
                use_adk_convergence = False
            if use_adk_convergence and (output_dir / "adk_convergence_trace.json").is_file():
                completed_rounds = sorted(
                    int(path.stem.rsplit("r", 1)[1])
                    for path in output_dir.glob("verification_r*.json")
                    if path.stem.rsplit("r", 1)[-1].isdigit()
                )
                if not completed_rounds:
                    raise RuntimeError("ADK convergence trace has no verification result")
                revision_round = completed_rounds[-1]
                draft = self._load(
                    output_dir / f"draft_r{revision_round}.json", DraftArtifact
                )
                adk_report = self._load(
                    output_dir / f"verification_r{revision_round}.json", VerificationReport
                )
                if draft is None or adk_report is None:
                    raise RuntimeError("ADK convergence result is incomplete")
            elif use_adk_convergence and not (output_dir / "draft_r0.json").exists():
                self._checkpoint(
                    output_dir, PipelineStatus.RUNNING, "adk_quality_convergence", completed, 0
                )
                convergence_runner = (
                    self._run_adk_development_convergence
                    if self._is_development(intake)
                    else self._run_adk_document_convergence
                )
                draft, adk_report, revision_round = convergence_runner(
                    intake=intake, requirements=requirements, sources=sources,
                    source_payload=source_payload, contract=contract,
                    analysis=analysis, output_dir=output_dir,
                )
            elif (
                use_adk_convergence
                and not self._is_development(intake)
                and (output_dir / "continuation_manifest.json").is_file()
            ):
                # A Cloud continuation restores one canonical narrative
                # candidate. Re-enter the native maker/verifier convergence
                # loop instead of the legacy checkpoint loop so repair plans,
                # repeated-failure stopping, and same-maker identity remain in
                # force after a budget or authorization pause.
                self._checkpoint(
                    output_dir, PipelineStatus.RUNNING, "adk_quality_convergence", completed, 0
                )
                draft, adk_report, revision_round = self._run_adk_document_convergence(
                    intake=intake,
                    requirements=requirements,
                    sources=sources,
                    source_payload=source_payload,
                    contract=contract,
                    analysis=analysis,
                    output_dir=output_dir,
                )
            elif use_adk_convergence:
                # A legacy or interrupted pre-ADK run has no durable ADK session.
                # Resume it through the existing checkpointed path instead of
                # repeating already billed model calls.
                use_adk_convergence = False

            draft_path = output_dir / (
                f"draft_r{revision_round}.json" if adk_report is not None else "draft_r0.json"
            )
            graph_begin("long_form_draft")
            draft = draft if adk_report is not None else self._load(draft_path, DraftArtifact)
            if draft is None:
                self._checkpoint(output_dir, PipelineStatus.RUNNING, "long_form_draft", completed, 0)
                if self._is_development(intake):
                    change_schema, development_pack, developer = self._development_components(intake, output_dir)
                    change_set_path = output_dir / "code_change_set.json"
                    change_set = self._load(change_set_path, change_schema)
                    if change_set is None:
                        change_set = developer.run(contract, analysis, source_payload)
                        self._capture_developer_recoveries(developer)
                        self._persist_recoveries(output_dir)
                        self._write(change_set_path, change_set.model_dump_json(indent=2))
                    development_dir = output_dir / "development"
                    change_set = self._bind_project_change_set(
                        intake, development_pack, change_set, output_dir
                    )
                    self._write(change_set_path, change_set.model_dump_json(indent=2))
                    development_run = self._load(
                        development_dir / "development_run.json", DevelopmentRun
                    )
                    if development_run is None:
                        try:
                            development_run = self._apply_development_change_set(
                                intake, development_pack, change_set, development_dir, contract
                            )
                        except RuntimeError as exc:
                            feedback = str(exc)
                            decision = self.recovery_policy.decide(
                                exc, context="development_verification", attempt_number=1
                            )
                            self._append_recovery(decision)
                            self._persist_recoveries(output_dir)
                            if (
                                decision.action != RecoveryAction.RETURN_TO_AGENT
                                or not decision.retry_allowed
                            ):
                                raise
                            self._write(
                                output_dir / "development_verification_failure_r0.txt",
                                feedback,
                            )
                            retry_change_set = developer.run(
                                contract,
                                analysis,
                                source_payload,
                                verification_feedback=feedback,
                                previous_change_set=change_set,
                            )
                            self._write(
                                output_dir / "code_change_set_retry_delta_r1.json",
                                retry_change_set.model_dump_json(indent=2),
                            )
                            retry_change_set = self._merge_development_retry(
                                change_set, retry_change_set
                            )
                            retry_change_set = self._bind_project_change_set(
                                intake, development_pack, retry_change_set, output_dir
                            )
                            self._capture_developer_recoveries(developer)
                            self._persist_recoveries(output_dir)
                            self._write(
                                output_dir / "code_change_set_retry_r1.json",
                                retry_change_set.model_dump_json(indent=2),
                            )
                            self._write(change_set_path, retry_change_set.model_dump_json(indent=2))
                            try:
                                development_run = self._apply_development_change_set(
                                    intake, development_pack, retry_change_set, development_dir, contract
                                )
                            except RuntimeError as retry_exc:
                                retry_feedback = str(retry_exc)
                                retry_decision = self.recovery_policy.decide(
                                    retry_exc,
                                    context="development_verification",
                                    attempt_number=2,
                                )
                                self._append_recovery(retry_decision)
                                self._persist_recoveries(output_dir)
                                if (
                                    retry_decision.action != RecoveryAction.RETURN_TO_AGENT
                                    or not retry_decision.retry_allowed
                                ):
                                    raise
                                self._write(
                                    output_dir / "development_verification_failure_r1.txt",
                                    retry_feedback,
                                )
                                second_retry_delta = developer.run(
                                    contract,
                                    analysis,
                                    source_payload,
                                    verification_feedback=retry_feedback,
                                    previous_change_set=retry_change_set,
                                )
                                self._write(
                                    output_dir / "code_change_set_retry_delta_r2.json",
                                    second_retry_delta.model_dump_json(indent=2),
                                )
                                second_retry = self._merge_development_retry(
                                    retry_change_set, second_retry_delta
                                )
                                second_retry = self._bind_project_change_set(
                                    intake, development_pack, second_retry, output_dir
                                )
                                self._capture_developer_recoveries(developer)
                                self._persist_recoveries(output_dir)
                                self._write(
                                    output_dir / "code_change_set_retry_r2.json",
                                    second_retry.model_dump_json(indent=2),
                                )
                                self._write(
                                    change_set_path, second_retry.model_dump_json(indent=2)
                                )
                                development_run = self._apply_development_change_set(
                                    intake,
                                    development_pack,
                                    second_retry,
                                    development_dir,
                                    contract,
                                )
                    finding_ids = [item.finding_id for item in analysis.findings]
                    command_lines = "\n".join(
                        f"- `{item.command_id}`: 통과 (종료 코드 {item.exit_code})"
                        for item in development_run.commands
                    )
                    changed_lines = "\n".join(
                        f"- `{item}`" for item in development_run.changed_paths
                    )
                    draft = DraftArtifact(
                        title="검증된 Exchange 웹프로그램 개선본",
                        body_markdown=(
                            "요청된 개선을 원본과 분리된 작업 공간에서 구현하고 검증했습니다. "
                            f"작업 범위는 분석 근거 [{finding_ids[0]}]에 따릅니다.\n\n"
                            "## 변경된 실행 파일\n\n"
                            f"{changed_lines}\n\n"
                            "## 자동 검증\n\n"
                            f"{command_lines}\n\n"
                            "## 전달물\n\n"
                            "- `development/changes.patch`: 검토 후 기존 저장소에 적용할 변경 묶음\n"
                            "- `development/changed_files/`: 변경된 전체 실행 파일\n"
                            "- `development/development_run.json`: 테스트·빌드 및 안전 경계 기록\n\n"
                            "원본 저장소, 원격 저장소, 배포 환경, 계정 및 거래 기능은 변경하지 않았습니다."
                        ),
                        cited_finding_ids=finding_ids,
                        drafting_decisions=[
                            "원본 대신 격리 복제본에서 변경했습니다.",
                            "고정된 테스트와 웹 빌드를 모두 통과한 결과만 반환했습니다.",
                        ],
                    )
                else:
                    draft = self.writer.run(contract, analysis, source_payload)
                    if intake.output_target == OutputTarget.SPREADSHEET:
                        draft = append_authoritative_csv_tables(sources, draft)
                self._write(draft_path, draft.model_dump_json(indent=2))
            graph_complete("long_form_draft", "draft_r0.json")
            completed.append("long_form_draft")

            integration = run_handoff(
                "artifact_integration",
                {
                    "contract": contract,
                    "analysis": analysis.model_dump(mode="json"),
                    "draft": draft.model_dump(mode="json"),
                },
            )
            if integration is not None:
                contract["artifact_integration"] = integration.model_dump(mode="json")

            graph_begin("independent_verification")
            active_verification_round = revision_round if adk_report is not None else 0
            verification_path = output_dir / f"verification_r{active_verification_round}.json"
            grounding_path = output_dir / f"deterministic_verification_r{active_verification_round}.json"
            completion_evidence_path = output_dir / f"completion_evidence_r{active_verification_round}.json"
            reality_check_path = output_dir / f"reality_check_r{active_verification_round}.json"
            evidence_sufficiency_path = (
                output_dir / f"evidence_sufficiency_r{active_verification_round}.json"
            )
            model_verification_path = output_dir / f"model_verification_r{active_verification_round}.json"
            report = adk_report or self._load(verification_path, VerificationReport)
            if (
                report is None
                or not grounding_path.exists()
                or not completion_evidence_path.exists()
                or not reality_check_path.exists()
                or not evidence_sufficiency_path.exists()
            ):
                self._checkpoint(output_dir, PipelineStatus.RUNNING, "verification_r0", completed, 0)
                model_report = self._load(model_verification_path, VerificationReport)
                if model_report is None:
                    model_report = report or self.verifier.run(
                        contract, analysis, draft, 0, source_payload,
                        self._development_evidence(output_dir),
                    )
                    self._write(
                        model_verification_path, model_report.model_dump_json(indent=2)
                    )
                grounding = validate_draft_grounding(
                    sources, draft,
                    require_full_csv_preservation=requires_full_csv_preservation(requirements),
                )
                self._write(grounding_path, grounding.model_dump_json(indent=2))
                completion_evidence = validate_completion_evidence(
                    intake, requirements, self._development_evidence(output_dir)
                )
                self._write(
                    completion_evidence_path,
                    completion_evidence.model_dump_json(indent=2),
                )
                report = apply_trusted_development_evidence(
                    model_report, requirements, self._development_evidence(output_dir)
                )
                report = apply_completion_evidence_override(report, completion_evidence)
                report = apply_deterministic_override(report, grounding)
                evidence_sufficiency = validate_evidence_sufficiency(
                    intake, requirements, sources, draft
                )
                self._write(
                    evidence_sufficiency_path,
                    evidence_sufficiency.model_dump_json(indent=2),
                )
                report = apply_evidence_sufficiency_override(
                    report, evidence_sufficiency
                )
                reality_check = evaluate_reality_check(
                    intake, requirements, self._development_evidence(output_dir)
                )
                self._write(reality_check_path, reality_check.model_dump_json(indent=2))
                report = apply_reality_check_override(report, reality_check)
                self._write(verification_path, report.model_dump_json(indent=2))
            completed.append("verification_r0")

            while report.verdict == Verdict.REVISE and revision_round < intake.max_revision_rounds:
                revision_round += 1
                if self._is_development(intake):
                    change_schema, development_pack, developer = self._development_components(intake, output_dir)
                    revision_stage = f"development_revision_r{revision_round}"
                    self._checkpoint(
                        output_dir,
                        PipelineStatus.RUNNING,
                        revision_stage,
                        completed,
                        revision_round,
                    )
                    previous_change_set = self._load(
                        output_dir / "code_change_set.json", change_schema
                    )
                    if previous_change_set is None:
                        raise RuntimeError("development revision lost the prior change set")
                    feedback = "\n".join([
                        *report.blocking_issues,
                        *report.revision_instructions,
                    ])
                    retry_change_set = developer.run(
                        contract,
                        analysis,
                        source_payload,
                        verification_feedback=feedback,
                        previous_change_set=previous_change_set,
                    )
                    self._write(
                        output_dir / f"code_change_set_revision_delta_r{revision_round}.json",
                        retry_change_set.model_dump_json(indent=2),
                    )
                    retry_change_set = self._merge_development_retry(
                        previous_change_set, retry_change_set
                    )
                    retry_change_set = self._bind_project_change_set(
                        intake, development_pack, retry_change_set, output_dir
                    )
                    self._capture_developer_recoveries(developer)
                    self._persist_recoveries(output_dir)
                    self._write(
                        output_dir / f"code_change_set_revision_r{revision_round}.json",
                        retry_change_set.model_dump_json(indent=2),
                    )
                    self._write(
                        output_dir / "code_change_set.json",
                        retry_change_set.model_dump_json(indent=2),
                    )
                    development_dir = output_dir / "development"
                    if development_dir.exists():
                        shutil.rmtree(development_dir)
                    self._apply_development_change_set(
                        intake, development_pack, retry_change_set, development_dir, contract
                    )
                    draft = DraftArtifact(
                        title=draft.title,
                        body_markdown=(
                            draft.body_markdown
                            + f"\n\n## 구현 수정 {revision_round}\n\n"
                            + "독립 검증의 차단 항목을 소프트웨어 제작자에게 반환하고 "
                            + "변경 코드를 다시 빌드·테스트했습니다."
                        ),
                        cited_finding_ids=draft.cited_finding_ids,
                        drafting_decisions=[
                            *draft.drafting_decisions,
                            *report.revision_instructions,
                        ],
                        temperament_decisions=list(draft.temperament_decisions),
                    )
                    self._write(
                        output_dir / f"draft_r{revision_round}.json",
                        draft.model_dump_json(indent=2),
                    )
                    completed.append(revision_stage)

                    verification_stage = f"verification_r{revision_round}"
                    self._checkpoint(
                        output_dir,
                        PipelineStatus.RUNNING,
                        verification_stage,
                        completed,
                        revision_round,
                    )
                    model_report = self.verifier.run(
                        contract,
                        analysis,
                        draft,
                        revision_round,
                        source_payload,
                        self._development_evidence(output_dir),
                    )
                    self._write(
                        output_dir / f"model_verification_r{revision_round}.json",
                        model_report.model_dump_json(indent=2),
                    )
                    grounding = validate_draft_grounding(
                        sources, draft,
                        require_full_csv_preservation=requires_full_csv_preservation(requirements),
                    )
                    self._write(
                        output_dir / f"deterministic_verification_r{revision_round}.json",
                        grounding.model_dump_json(indent=2),
                    )
                    completion_evidence = validate_completion_evidence(
                        intake, requirements, self._development_evidence(output_dir)
                    )
                    self._write(
                        output_dir / f"completion_evidence_r{revision_round}.json",
                        completion_evidence.model_dump_json(indent=2),
                    )
                    report = apply_trusted_development_evidence(
                        model_report, requirements, self._development_evidence(output_dir)
                    )
                    report = apply_completion_evidence_override(report, completion_evidence)
                    report = apply_deterministic_override(report, grounding)
                    evidence_sufficiency = validate_evidence_sufficiency(
                        intake, requirements, sources, draft
                    )
                    self._write(
                        output_dir / f"evidence_sufficiency_r{revision_round}.json",
                        evidence_sufficiency.model_dump_json(indent=2),
                    )
                    report = apply_evidence_sufficiency_override(
                        report, evidence_sufficiency
                    )
                    reality_check = evaluate_reality_check(
                        intake, requirements, self._development_evidence(output_dir)
                    )
                    self._write(
                        output_dir / f"reality_check_r{revision_round}.json",
                        reality_check.model_dump_json(indent=2),
                    )
                    report = apply_reality_check_override(report, reality_check)
                    self._write(
                        output_dir / f"verification_r{revision_round}.json",
                        report.model_dump_json(indent=2),
                    )
                    completed.append(verification_stage)
                    continue
                revision_stage = f"maker_revision_r{revision_round}"
                revision_path = output_dir / f"draft_r{revision_round}.json"
                revised_draft = self._load(revision_path, DraftArtifact)
                if revised_draft is None:
                    self._checkpoint(
                        output_dir,
                        PipelineStatus.RUNNING,
                        revision_stage,
                        completed,
                        revision_round,
                    )
                    revised_draft = self.writer.run(
                        contract,
                        analysis,
                        source_payload,
                        verification_feedback=report,
                        previous_draft=draft,
                        round_number=revision_round,
                    )
                    if intake.output_target == OutputTarget.SPREADSHEET:
                        revised_draft = append_authoritative_csv_tables(sources, revised_draft)
                    self._write(revision_path, revised_draft.model_dump_json(indent=2))
                draft = revised_draft
                completed.append(revision_stage)

                verification_stage = f"verification_r{revision_round}"
                verification_path = output_dir / f"verification_r{revision_round}.json"
                completion_evidence_path = output_dir / f"completion_evidence_r{revision_round}.json"
                reality_check_path = output_dir / f"reality_check_r{revision_round}.json"
                grounding_path = output_dir / f"deterministic_verification_r{revision_round}.json"
                evidence_sufficiency_path = (
                    output_dir / f"evidence_sufficiency_r{revision_round}.json"
                )
                model_verification_path = output_dir / f"model_verification_r{revision_round}.json"
                next_report = self._load(verification_path, VerificationReport)
                if (
                    next_report is None
                    or not grounding_path.exists()
                    or not completion_evidence_path.exists()
                    or not reality_check_path.exists()
                    or not evidence_sufficiency_path.exists()
                ):
                    self._checkpoint(
                        output_dir,
                        PipelineStatus.RUNNING,
                        verification_stage,
                        completed,
                        revision_round,
                    )
                    model_report = self._load(model_verification_path, VerificationReport)
                    if model_report is None:
                        model_report = next_report or self.verifier.run(
                            contract, analysis, draft, revision_round, source_payload,
                            self._development_evidence(output_dir),
                        )
                        self._write(
                            model_verification_path, model_report.model_dump_json(indent=2)
                        )
                    grounding = validate_draft_grounding(
                        sources, draft,
                        require_full_csv_preservation=requires_full_csv_preservation(requirements),
                    )
                    self._write(grounding_path, grounding.model_dump_json(indent=2))
                    completion_evidence = validate_completion_evidence(
                        intake, requirements, self._development_evidence(output_dir)
                    )
                    self._write(
                        completion_evidence_path,
                        completion_evidence.model_dump_json(indent=2),
                    )
                    next_report = apply_completion_evidence_override(
                        model_report, completion_evidence
                    )
                    next_report = apply_deterministic_override(next_report, grounding)
                    evidence_sufficiency = validate_evidence_sufficiency(
                        intake, requirements, sources, draft
                    )
                    self._write(
                        evidence_sufficiency_path,
                        evidence_sufficiency.model_dump_json(indent=2),
                    )
                    next_report = apply_evidence_sufficiency_override(
                        next_report, evidence_sufficiency
                    )
                    reality_check = evaluate_reality_check(
                        intake, requirements, self._development_evidence(output_dir)
                    )
                    self._write(reality_check_path, reality_check.model_dump_json(indent=2))
                    next_report = apply_reality_check_override(next_report, reality_check)
                    self._write(verification_path, next_report.model_dump_json(indent=2))
                report = next_report
                completed.append(verification_stage)

            graph_complete(
                "independent_verification",
                f"completion_evidence_r{revision_round}.json",
                f"reality_check_r{revision_round}.json",
                f"verification_r{revision_round}.json",
                f"deterministic_verification_r{revision_round}.json",
                f"evidence_sufficiency_r{revision_round}.json",
            )

            governance: GovernanceDecision | None = None
            if has_graph_stage("policy_guard"):
                guard_path = output_dir / "policy_guard.json"
                governance = self._load(guard_path, GovernanceDecision)
                graph_begin("policy_guard")
                if governance is None:
                    governance = GovernanceAgent(
                        self.gateway,
                        "policy_guard",
                        self.stage_models.get("policy_guard", "gemini-3.5-flash"),
                    ).run({
                        "contract": contract,
                        "verification": report.model_dump(mode="json"),
                        "draft": draft.model_dump(mode="json"),
                    })
                    self._write(guard_path, governance.model_dump_json(indent=2))
                graph_complete("policy_guard", "policy_guard.json")

            if report.verdict == Verdict.NEEDS_INFORMATION:
                status = PipelineStatus.NEEDS_INFORMATION
                message = "Verifier found a required conclusion unsupported by supplied evidence."
            elif report.verdict == Verdict.PASS:
                status = PipelineStatus.COMPLETE
                message = "Independent verification passed."
            elif report.verdict == Verdict.UNVERIFIABLE:
                status = PipelineStatus.PARTIAL
                message = (
                    "A required independent observation capability was unavailable; "
                    "the result was not accepted as complete."
                )
            else:
                status = PipelineStatus.PARTIAL
                message = "Revision limit reached before verification passed."

            if governance is not None and governance.verdict != Verdict.PASS:
                status = (
                    PipelineStatus.NEEDS_INFORMATION
                    if governance.verdict == Verdict.NEEDS_INFORMATION
                    else PipelineStatus.PARTIAL
                )
                message = f"Policy guard returned {governance.verdict.value}: {governance.rationale}"

            if has_graph_stage("final_approval"):
                approval_path = output_dir / "final_approval.json"
                approval = self._load(approval_path, GovernanceDecision)
                graph_begin("final_approval")
                if approval is None:
                    approval = GovernanceAgent(
                        self.gateway,
                        "final_approval",
                        self.stage_models.get("final_approval", "gemini-3.5-flash"),
                        max_output_tokens=(
                            600
                            if (output_dir / "development_verification_failure.txt").is_file()
                            else 1200
                        ),
                    ).run({
                        "contract": contract,
                        "pipeline_status": status.value,
                        "verification": report.model_dump(mode="json"),
                        "policy_guard": governance.model_dump(mode="json") if governance else None,
                    })
                    self._write(approval_path, approval.model_dump_json(indent=2))
                if status == PipelineStatus.COMPLETE and approval.verdict != Verdict.PASS:
                    status = (
                        PipelineStatus.NEEDS_INFORMATION
                        if approval.verdict == Verdict.NEEDS_INFORMATION
                        else PipelineStatus.PARTIAL
                    )
                    message = f"Project owner returned {approval.verdict.value}: {approval.rationale}"
                if status != PipelineStatus.COMPLETE and approval.verdict == Verdict.PASS:
                    raise ValueError("project owner cannot override a failed critic or guardian gate")
                graph_complete("final_approval", "final_approval.json")

            body = draft.body_markdown.strip()
            final_text = body if body.startswith("# ") else f"# {draft.title}\n\n{body}"
            self._write(output_dir / "final.md", final_text)
            if intake.output_target in {OutputTarget.AUTO, OutputTarget.SPREADSHEET}:
                export_workbook(final_text, output_dir / "result.xlsx", public_research)
            self._write(output_dir / "final_verification.json", report.model_dump_json(indent=2))
            self._write(
                output_dir / "temperament_decisions.json",
                json.dumps(self._temperament_audit(output_dir), ensure_ascii=False, indent=2),
            )
            if self.finalize_budget_on_finish:
                BudgetStore(self.run_dir).complete()
            self._persist_recoveries(output_dir)
            self._checkpoint(
                output_dir,
                status,
                "finished",
                completed,
                revision_round,
                report.verdict,
                message,
            )
            return ExecutionCheckpoint.model_validate_json(
                (output_dir / "execution_checkpoint.json").read_text(encoding="utf-8")
            )
        except BudgetExceeded as exc:
            self._append_recovery(self.recovery_policy.decide(
                exc, context="budget_gate", attempt_number=1
            ))
            self._persist_recoveries(output_dir)
            fail_running_graph(str(exc), blocked=True)
            self._checkpoint(
                output_dir,
                PipelineStatus.NEEDS_BUDGET,
                "budget_gate",
                completed,
                revision_round,
                message=str(exc),
            )
            raise
        except PermissionError as exc:
            self._append_recovery(self.recovery_policy.decide(
                exc, context="authorization_gate", attempt_number=1
            ))
            self._persist_recoveries(output_dir)
            fail_running_graph(str(exc), blocked=True)
            try:
                BudgetStore(self.run_dir).fail(f"PermissionError: {exc}")
            except Exception:
                pass
            self._checkpoint(
                output_dir,
                PipelineStatus.NEEDS_AUTHORIZATION,
                "authorization_gate",
                completed,
                revision_round,
                message=(
                    "The approved capability boundary is insufficient. "
                    f"Return to stage 1 and approve an amended plan: {exc}"
                ),
            )
            return ExecutionCheckpoint.model_validate_json(
                (output_dir / "execution_checkpoint.json").read_text(encoding="utf-8")
            )
        except Exception as exc:
            self._capture_developer_recoveries()
            decision = self.recovery_policy.decide(
                exc, context="pipeline", attempt_number=1
            )
            if not self.recovery_decisions or (
                self.recovery_decisions[-1].error_summary != decision.error_summary
            ):
                self._append_recovery(decision)
            self._persist_recoveries(output_dir)
            fail_running_graph(f"{type(exc).__name__}: {exc}")
            try:
                BudgetStore(self.run_dir).fail(f"{type(exc).__name__}: {exc}")
            except Exception:
                pass
            self._checkpoint(
                output_dir,
                PipelineStatus.FAILED,
                "failed",
                completed,
                revision_round,
                message=f"{type(exc).__name__}: {exc}",
            )
            raise
