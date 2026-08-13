"""Durable milestone planning and dependency-aware project convergence.

Milestones do not replace OneBrief's completion contract or maker/verifier loop.
They scope that loop to independently verifiable vertical slices, preserve every
passed slice as an immutable receipt, and run the final accumulated candidate
against the clean approved baseline.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Callable, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator

from onebrief.execution_schemas import ExecutionCheckpoint, PipelineStatus, Verdict
from onebrief.generic_development_toolpack import ProjectCodeChangeSet, ProjectFileChange
from onebrief.project_import import ExternalProjectImporter, MANIFEST_NAME, ProjectManifest
from onebrief.schemas import (
    CompletionContract,
    EvaluationMode,
    IntakeRequest,
    InternalSource,
    QualityCriterion,
    RequirementsAnalysis,
)
from onebrief.toolpack_lifecycle import ProjectToolPackLifecycle


def _now() -> str:
    return datetime.now(UTC).isoformat()


def canonical_sha256(value: object) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _atomic_model(path: Path, model: BaseModel) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + f".{uuid4().hex}.tmp")
    temporary.write_text(model.model_dump_json(indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _git(root: Path, *args: str, timeout: int = 180) -> str:
    result = subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=timeout,
        shell=False, check=False,
    )
    if result.returncode:
        detail = "\n".join(
            item.strip() for item in (result.stdout, result.stderr) if item.strip()
        )
        raise RuntimeError(detail[:4000] or f"git {' '.join(args)} failed")
    return result.stdout


class MilestoneKind(StrEnum):
    BASELINE = "baseline"
    IMPLEMENTATION = "implementation"
    INTEGRATION = "integration"


class VerificationScope(StrEnum):
    TARGETED = "targeted"
    AFFECTED = "affected_dependencies"
    FULL = "full_regression"


class MilestoneState(StrEnum):
    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    PASSED = "passed"
    INVALIDATED = "invalidated"
    BLOCKED = "blocked"


class MilestoneCompletionContract(BaseModel):
    target_state: str = Field(min_length=3, max_length=1200)
    primary_criterion_ids: list[str] = Field(default_factory=list, max_length=12)
    criterion_ids: list[str] = Field(default_factory=list, max_length=12)
    slice_quality_criteria: list[QualityCriterion] = Field(default_factory=list, max_length=6)
    deliverables: list[str] = Field(min_length=1, max_length=12)
    evidence_requirements: list[str] = Field(min_length=1, max_length=20)
    verification_scope: VerificationScope
    max_revision_rounds: int = Field(default=3, ge=0, le=6)

    @model_validator(mode="after")
    def validate_criterion_scope(self) -> "MilestoneCompletionContract":
        if not set(self.primary_criterion_ids).issubset(set(self.criterion_ids)):
            raise ValueError("primary milestone criteria must be included in its verification contract")
        if len(self.criterion_ids) != len(set(self.criterion_ids)):
            raise ValueError("milestone criterion IDs must be unique")
        all_ids = [*self.criterion_ids, *(
            item.criterion_id for item in self.slice_quality_criteria
        )]
        if len(all_ids) != len(set(all_ids)):
            raise ValueError("milestone overall and slice criterion IDs must be unique")
        return self


class MilestoneSpec(BaseModel):
    milestone_id: str = Field(pattern=r"^M(?:0[0-9]|[1-8][0-9]|99)$")
    title: str = Field(min_length=3, max_length=160)
    outcome: str = Field(min_length=3, max_length=1000)
    kind: MilestoneKind
    dependencies: list[str] = Field(default_factory=list, max_length=12)
    contract: MilestoneCompletionContract
    budget_weight: float = Field(gt=0, le=1)


class MilestonePlan(BaseModel):
    schema_version: Literal["onebrief-milestone-plan-v1"] = "onebrief-milestone-plan-v1"
    project_id: str
    goal_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    completion_contract_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    overall_criterion_ids: list[str] = Field(min_length=1, max_length=12)
    source_revision: str = Field(min_length=1, max_length=128)
    minimum_cost_usd: float = Field(ge=0)
    maximum_cost_usd: float = Field(ge=0)
    reserve_fraction: float = Field(default=0.15, ge=0.05, le=0.4)
    milestones: list[MilestoneSpec] = Field(min_length=2, max_length=14)

    @model_validator(mode="after")
    def validate_graph(self) -> "MilestonePlan":
        ids = [item.milestone_id for item in self.milestones]
        if len(ids) != len(set(ids)):
            raise ValueError("milestone IDs must be unique")
        known = set(ids)
        for item in self.milestones:
            if item.milestone_id in item.dependencies:
                raise ValueError("a milestone cannot depend on itself")
            if not set(item.dependencies).issubset(known):
                raise ValueError(f"unknown milestone dependency in {item.milestone_id}")
            if item.kind == MilestoneKind.BASELINE and item.dependencies:
                raise ValueError("baseline milestone cannot have dependencies")
        visiting: set[str] = set()
        visited: set[str] = set()
        by_id = {item.milestone_id: item for item in self.milestones}

        def visit(milestone_id: str) -> None:
            if milestone_id in visiting:
                raise ValueError("milestone dependency graph contains a cycle")
            if milestone_id in visited:
                return
            visiting.add(milestone_id)
            for dependency in by_id[milestone_id].dependencies:
                visit(dependency)
            visiting.remove(milestone_id)
            visited.add(milestone_id)

        for milestone_id in ids:
            visit(milestone_id)
        integrations = [item for item in self.milestones if item.kind == MilestoneKind.INTEGRATION]
        if (
            len(integrations) != 1
            or integrations[0].milestone_id != self.milestones[-1].milestone_id
        ):
            raise ValueError("milestone plan requires one final integration milestone")
        assigned = [
            criterion_id for item in self.milestones
            for criterion_id in item.contract.primary_criterion_ids
        ]
        if len(assigned) != len(set(assigned)):
            raise ValueError("an overall criterion must have one primary milestone owner")
        if set(assigned) != set(self.overall_criterion_ids):
            raise ValueError("every overall criterion requires exactly one primary milestone owner")
        if set(integrations[0].contract.criterion_ids) != set(self.overall_criterion_ids):
            raise ValueError("final integration must revalidate every overall criterion")
        if self.maximum_cost_usd < self.minimum_cost_usd:
            raise ValueError("milestone maximum cost cannot be below minimum cost")
        if sum(item.budget_weight for item in self.milestones) > 1.000001:
            raise ValueError("milestone budget weights exceed the approved project envelope")
        return self

    @property
    def sha256(self) -> str:
        return canonical_sha256(self)


class MilestoneCheckpoint(BaseModel):
    schema_version: Literal["onebrief-milestone-checkpoint-v1"] = (
        "onebrief-milestone-checkpoint-v1"
    )
    checkpoint_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    milestone_id: str
    plan_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    contract_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_revision_before: str
    source_revision_after: str
    candidate_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    dependency_checkpoints: dict[str, str]
    evidence_sha256: dict[str, str]
    verdict: Literal["PASS"] = "PASS"
    created_at: str


class MilestonePointer(BaseModel):
    milestone_id: str
    state: MilestoneState
    checkpoint_id: str | None = None
    invalidation_id: str | None = None


class MilestoneInvalidation(BaseModel):
    schema_version: Literal["onebrief-milestone-invalidation-v1"] = (
        "onebrief-milestone-invalidation-v1"
    )
    invalidation_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    milestone_id: str
    invalidated_checkpoint_id: str
    caused_by_milestone_id: str
    reason: str
    created_at: str


class MilestoneStore:
    """Append-only checkpoint receipts plus mutable current pointers."""

    def __init__(self, root: Path, plan: MilestonePlan):
        self.root = root.resolve()
        self.plan = plan
        self.receipts = self.root / "receipts"
        self.pointers = self.root / "current"
        self.invalidations = self.root / "invalidations"
        self.root.mkdir(parents=True, exist_ok=True)
        plan_path = self.root / "milestone_plan.json"
        if plan_path.is_file():
            existing = MilestonePlan.model_validate_json(plan_path.read_text(encoding="utf-8"))
            if existing.sha256 != plan.sha256:
                raise RuntimeError("persisted milestone plan differs from the approved plan")
        else:
            _atomic_model(plan_path, plan)

    def _pointer_path(self, milestone_id: str) -> Path:
        return self.pointers / f"{milestone_id}.json"

    def pointer(self, milestone_id: str) -> MilestonePointer:
        path = self._pointer_path(milestone_id)
        if not path.is_file():
            return MilestonePointer(milestone_id=milestone_id, state=MilestoneState.PENDING)
        return MilestonePointer.model_validate_json(path.read_text(encoding="utf-8"))

    def checkpoint(self, milestone_id: str) -> MilestoneCheckpoint | None:
        pointer = self.pointer(milestone_id)
        if pointer.state != MilestoneState.PASSED or pointer.checkpoint_id is None:
            return None
        path = self.receipts / f"{pointer.checkpoint_id}.json"
        checkpoint = MilestoneCheckpoint.model_validate_json(path.read_text(encoding="utf-8"))
        if canonical_sha256(checkpoint.model_dump(
            mode="json", exclude={"checkpoint_id", "created_at"}
        )) != checkpoint.checkpoint_id:
            raise RuntimeError(f"milestone checkpoint integrity failed: {milestone_id}")
        return checkpoint

    def ready(self, milestone: MilestoneSpec) -> bool:
        return all(self.checkpoint(item) is not None for item in milestone.dependencies)

    def next_ready(self) -> MilestoneSpec | None:
        for milestone in self.plan.milestones:
            if self.checkpoint(milestone.milestone_id) is None and self.ready(milestone):
                return milestone
        return None

    def record_pass(
        self, *, milestone: MilestoneSpec, source_before: str, source_after: str,
        candidate_sha256: str, evidence_paths: list[Path],
    ) -> MilestoneCheckpoint:
        dependencies = {
            item: self.checkpoint(item).checkpoint_id  # type: ignore[union-attr]
            for item in milestone.dependencies
        }
        missing = [str(path) for path in evidence_paths if not path.is_file()]
        if missing:
            raise RuntimeError("milestone PASS evidence is missing: " + ", ".join(missing))
        payload = {
            "schema_version": "onebrief-milestone-checkpoint-v1",
            "milestone_id": milestone.milestone_id,
            "plan_sha256": self.plan.sha256,
            "contract_sha256": canonical_sha256(milestone.contract),
            "source_revision_before": source_before,
            "source_revision_after": source_after,
            "candidate_sha256": candidate_sha256,
            "dependency_checkpoints": dependencies,
            "evidence_sha256": {
                path.name: _file_sha256(path) for path in sorted(evidence_paths)
            },
            "verdict": "PASS",
            "created_at": _now(),
        }
        stable = dict(payload)
        stable.pop("created_at")
        checkpoint_id = canonical_sha256(stable)
        checkpoint = MilestoneCheckpoint(checkpoint_id=checkpoint_id, **payload)
        receipt_path = self.receipts / f"{checkpoint_id}.json"
        if not receipt_path.is_file():
            _atomic_model(receipt_path, checkpoint)
        _atomic_model(self._pointer_path(milestone.milestone_id), MilestonePointer(
            milestone_id=milestone.milestone_id,
            state=MilestoneState.PASSED,
            checkpoint_id=checkpoint_id,
        ))
        return checkpoint

    def descendants(self, milestone_id: str) -> list[str]:
        result: list[str] = []
        frontier = [milestone_id]
        while frontier:
            parent = frontier.pop(0)
            for item in self.plan.milestones:
                if parent in item.dependencies and item.milestone_id not in result:
                    result.append(item.milestone_id)
                    frontier.append(item.milestone_id)
        return result

    def invalidate_descendants(self, milestone_id: str, reason: str) -> list[MilestoneInvalidation]:
        records: list[MilestoneInvalidation] = []
        for descendant in self.descendants(milestone_id):
            checkpoint = self.checkpoint(descendant)
            if checkpoint is None:
                continue
            stable = {
                "milestone_id": descendant,
                "invalidated_checkpoint_id": checkpoint.checkpoint_id,
                "caused_by_milestone_id": milestone_id,
                "reason": reason,
            }
            invalidation = MilestoneInvalidation(
                invalidation_id=canonical_sha256(stable),
                created_at=_now(),
                **stable,
            )
            path = self.invalidations / f"{invalidation.invalidation_id}.json"
            if not path.is_file():
                _atomic_model(path, invalidation)
            _atomic_model(self._pointer_path(descendant), MilestonePointer(
                milestone_id=descendant,
                state=MilestoneState.INVALIDATED,
                invalidation_id=invalidation.invalidation_id,
            ))
            records.append(invalidation)
        return records


def _criterion_text(criterion: QualityCriterion) -> str:
    return f"{criterion.description} {criterion.evidence_required}".casefold()


def _matches(criterion: QualityCriterion, tokens: tuple[str, ...]) -> bool:
    text = _criterion_text(criterion)
    return any(token in text for token in tokens)


def build_milestone_plan(
    *, project_id: str, goal: str, requirements: RequirementsAnalysis,
    source_revision: str, minimum_cost_usd: float, maximum_cost_usd: float,
) -> MilestonePlan:
    """Create conservative vertical slices from the already-approved contract.

    The split is deterministic: it cannot invent scope, weaken criteria, or
    spend budget. Project-specific makers still choose implementation details.
    """
    contract = requirements.completion_contract
    if contract is None:
        raise ValueError("milestone planning requires a completion contract")
    criteria = list(contract.quality_criteria)
    remaining = {item.criterion_id: item for item in criteria}

    def take(tokens: tuple[str, ...]) -> list[QualityCriterion]:
        selected = [item for item in remaining.values() if _matches(item, tokens)]
        for item in selected:
            remaining.pop(item.criterion_id, None)
        return selected

    presentation_items = take((
        "responsive", "visual style", "glyph", "localization", "layout", "rendered",
        "반응형", "시각", "글리프", "다국어", "레이아웃",
    ))
    settings_items = take(("settings", "volume", "bgm", "sfx", "설정", "음량"))
    flow_items = take(("flow", "navigation", "login", "lobby", "화면 이동", "로그인", "로비"))
    compile_items = take(("compil", "build success", "컴파일", "빌드 성공"))
    preservation_items = take((
        "regression", "preserv", "unmodified", "isolated", "do not modify",
        "회귀", "보존", "원본", "격리",
    ))
    groups: list[tuple[str, str, list[QualityCriterion], list[QualityCriterion]]] = []
    target_text = " ".join([
        goal,
        requirements.normalized_goal,
        contract.target_state,
        *(item.description for item in criteria),
    ]).casefold()
    unity_surface_flow = (
        "unity" in target_text
        and all(token in target_text for token in ("login", "lobby", "settings"))
    )
    if unity_surface_flow:
        groups.extend([
            (
                "Login vertical slice",
                "The modernized Login surface uses the preserved authentication path and reaches Lobby in PlayMode.",
                compile_items,
                [QualityCriterion(
                    criterion_id="Q91",
                    description="Login surface and preserved authentication transition work",
                    evaluation_mode=EvaluationMode.DETERMINISTIC,
                    evidence_required="Unity compile output and PlayMode evidence for Login to Lobby.",
                )],
            ),
            (
                "Lobby vertical slice",
                "The modernized Lobby renders preserved server-backed data and exposes working navigation targets.",
                [],
                [QualityCriterion(
                    criterion_id="Q92",
                    description="Lobby renders preserved data and navigation without regressions",
                    evaluation_mode=EvaluationMode.DETERMINISTIC,
                    evidence_required="PlayMode evidence for Lobby data, buttons, and navigation targets.",
                )],
            ),
            (
                "Settings vertical slice and complete flow",
                "Settings controls bind to preserved client systems and the full Login to Lobby to Settings to Lobby flow works.",
                [*flow_items, *settings_items],
                [],
            ),
        ])
    elif flow_items or compile_items:
        groups.append((
            "Executable primary flow",
            "The primary user flow works in the target runtime on a compiling candidate.",
            [*compile_items, *flow_items],
            [],
        ))
    if settings_items and not unity_surface_flow:
        groups.append((
            "Secondary controls",
            "The requested secondary controls work against the preserved underlying systems.",
            settings_items,
            [],
        ))
    if presentation_items:
        groups.append((
            "Presentation and adaptability",
            "The implemented surfaces meet the responsive, visual, and language requirements.",
            presentation_items,
            [],
        ))
    leftovers = list(remaining.values())
    # Preserve at most four implementation passes for a medium job. This is a
    # vertical-slice boundary, not one agent or one criterion per call.
    while leftovers:
        chunk, leftovers = leftovers[:3], leftovers[3:]
        groups.append((
            "Remaining contract slice",
            "The remaining independently verifiable contract slice is implemented.",
            chunk,
            [],
        ))
    if not groups:
        groups.append(("Requested outcome", contract.target_state, criteria, []))

    milestones: list[MilestoneSpec] = [MilestoneSpec(
        milestone_id="M00",
        title="Immutable approved baseline",
        outcome="The exact source revision, authority, and completion contract are frozen before edits.",
        kind=MilestoneKind.BASELINE,
        dependencies=[],
        contract=MilestoneCompletionContract(
            target_state="The approved baseline is reproducible and the original remains untouched.",
            deliverables=["Source and contract digest receipt"],
            evidence_requirements=["Approved ToolPack state and clean source revision"],
            verification_scope=VerificationScope.TARGETED,
            max_revision_rounds=0,
        ),
        budget_weight=0.01,
    )]
    cumulative: list[str] = []
    cumulative_slice_criteria: list[QualityCriterion] = []
    previous = "M00"
    implementation_count = len(groups)
    working_fraction = 0.84
    per_group = working_fraction / max(1, implementation_count)
    for index, (title, outcome, primary, slice_criteria) in enumerate(groups, start=1):
        primary_ids = [item.criterion_id for item in primary]
        cumulative.extend(primary_ids)
        cumulative_slice_criteria.extend(slice_criteria)
        inherited = [
            item.criterion_id for item in criteria
            if item.criterion_id in cumulative
        ]
        evidence = [
            item.evidence_required for item in criteria
            if item.criterion_id in inherited
        ]
        milestone_id = f"M{index:02d}"
        milestones.append(MilestoneSpec(
            milestone_id=milestone_id,
            title=title,
            outcome=outcome,
            kind=MilestoneKind.IMPLEMENTATION,
            dependencies=[previous],
            contract=MilestoneCompletionContract(
                target_state=outcome,
                primary_criterion_ids=primary_ids,
                criterion_ids=inherited,
                slice_quality_criteria=list(cumulative_slice_criteria),
                deliverables=[f"Verified vertical slice: {title}"],
                evidence_requirements=(
                    [
                        *evidence,
                        *(item.evidence_required for item in cumulative_slice_criteria),
                    ]
                    or ["Executable evidence for the milestone outcome"]
                ),
                verification_scope=(
                    VerificationScope.TARGETED if index == 1 else VerificationScope.AFFECTED
                ),
                max_revision_rounds=min(4, requirements.completion_contract.quality_criteria.__len__()),
            ),
            budget_weight=per_group,
        ))
        previous = milestone_id
    preservation_ids = [item.criterion_id for item in preservation_items]
    already_owned = {
        criterion_id for item in milestones
        for criterion_id in item.contract.primary_criterion_ids
    }
    final_primary = [
        item.criterion_id for item in criteria
        if item.criterion_id not in already_owned
    ]
    # Preservation criteria are intentionally owned by the final clean-baseline
    # integration proof, even if a generic grouping encountered them earlier.
    final_primary = list(dict.fromkeys([*preservation_ids, *final_primary]))
    milestones.append(MilestoneSpec(
        milestone_id="M99",
        title="Clean-baseline full integration",
        outcome=contract.target_state,
        kind=MilestoneKind.INTEGRATION,
        dependencies=[previous],
        contract=MilestoneCompletionContract(
            target_state=contract.target_state,
            primary_criterion_ids=final_primary,
            criterion_ids=[item.criterion_id for item in criteria],
            deliverables=list(requirements.deliverables),
            evidence_requirements=[item.evidence_required for item in criteria],
            verification_scope=VerificationScope.FULL,
            max_revision_rounds=6,
        ),
        budget_weight=0.15,
    ))
    return MilestonePlan(
        project_id=project_id,
        goal_digest=canonical_sha256({"goal": goal}),
        completion_contract_digest=canonical_sha256(contract),
        overall_criterion_ids=[item.criterion_id for item in criteria],
        source_revision=source_revision,
        minimum_cost_usd=minimum_cost_usd,
        maximum_cost_usd=maximum_cost_usd,
        milestones=milestones,
    )


def requirements_for_milestone(
    requirements: RequirementsAnalysis, milestone: MilestoneSpec,
) -> RequirementsAnalysis:
    contract = requirements.completion_contract
    if contract is None or milestone.kind == MilestoneKind.BASELINE:
        return requirements
    selected = [
        item for item in contract.quality_criteria
        if item.criterion_id in milestone.contract.criterion_ids
    ]
    selected.extend(milestone.contract.slice_quality_criteria)
    scoped = CompletionContract(
        target_state=milestone.contract.target_state,
        quality_criteria=selected,
        pass_condition="Every required milestone criterion passes with fresh executable evidence.",
    )
    return requirements.model_copy(update={
        "normalized_goal": (
            f"Milestone {milestone.milestone_id}: {milestone.outcome} "
            "Preserve every already-passed dependency; do not implement unrelated future scope."
        ),
        "deliverables": list(milestone.contract.deliverables),
        "acceptance_criteria": [item.description for item in selected],
        "completion_contract": scoped,
        "ready_for_estimate": True,
    })


class MilestoneWorkspace(BaseModel):
    project_id: str
    baseline_root: str
    baseline_registry_root: str
    integration_root: str
    integration_registry_root: str
    source_revision: str
    integration_base_revision: str


def prepare_milestone_workspace(
    *, work_dir: Path, project_id: str, baseline_registry_root: Path,
) -> MilestoneWorkspace:
    baseline_lifecycle = ProjectToolPackLifecycle(project_id, baseline_registry_root)
    baseline_state = baseline_lifecycle.state()
    if not baseline_state.execution_ready or baseline_state.generated is None:
        raise PermissionError("approved baseline ToolPack is not execution-ready")
    baseline_root = Path(baseline_state.generated.project_root).resolve()
    source_revision = baseline_state.generated.repository_head_sha or _git(
        baseline_root, "rev-parse", "HEAD"
    ).strip()
    restore_evidence = baseline_registry_root.parent / "restore_evidence.json"
    if restore_evidence.is_file():
        provenance = json.loads(restore_evidence.read_text(encoding="utf-8"))
        approved_source_revision = str(provenance.get("source_head_sha", ""))
        if re.fullmatch(r"[a-f0-9]{40}", approved_source_revision):
            source_revision = approved_source_revision
    workspace_root = work_dir / "milestone_workspace"
    integration_root = workspace_root / "repository"
    integration_registry = workspace_root / "registry"
    if not integration_root.exists():
        workspace_root.mkdir(parents=True, exist_ok=True)
        _git(workspace_root, "clone", "--local", "--no-hardlinks", str(baseline_root), str(integration_root))
        resident = baseline_root / MANIFEST_NAME
        manifest = ProjectManifest.model_validate_json(resident.read_text(encoding="utf-8"))
        adjusted = manifest.model_copy(update={"project_root": str(integration_root.resolve())})
        (integration_root / MANIFEST_NAME).write_text(
            adjusted.model_dump_json(indent=2) + "\n", encoding="utf-8"
        )
        _git(integration_root, "config", "user.name", "OneBrief Milestone Runtime")
        _git(integration_root, "config", "user.email", "onebrief-milestone@example.invalid")
        _git(integration_root, "add", MANIFEST_NAME)
        _git(integration_root, "commit", "-m", "Bind isolated milestone workspace")
        ExternalProjectImporter(integration_registry).import_bytes(
            (integration_root / MANIFEST_NAME).read_bytes()
        )
        state = ProjectToolPackLifecycle(project_id, integration_registry).generate_and_qualify()
        if state.qualification is None or state.qualification.status != "passed":
            raise RuntimeError("isolated milestone ToolPack qualification failed")
        ProjectToolPackLifecycle(project_id, integration_registry).approve(
            state.qualification.toolpack_sha256
        )
    base_file = workspace_root / "baseline_revision.txt"
    if not base_file.is_file():
        base_file.write_text(_git(integration_root, "rev-parse", "HEAD").strip() + "\n", encoding="utf-8")
    return MilestoneWorkspace(
        project_id=project_id,
        baseline_root=str(baseline_root),
        baseline_registry_root=str(baseline_registry_root.resolve()),
        integration_root=str(integration_root.resolve()),
        integration_registry_root=str(integration_registry.resolve()),
        source_revision=source_revision,
        integration_base_revision=base_file.read_text(encoding="utf-8").strip(),
    )


def advance_milestone_candidate(
    *, workspace: MilestoneWorkspace, milestone: MilestoneSpec, output_dir: Path,
) -> tuple[str, str, str]:
    """Atomically advance only the isolated integration repository."""
    root = Path(workspace.integration_root)
    before = _git(root, "rev-parse", "HEAD").strip()
    change_set = ProjectCodeChangeSet.model_validate_json(
        (output_dir / "development" / "change_set.json").read_text(encoding="utf-8")
    )
    for change in change_set.changes:
        pure = PurePosixPath(change.path)
        target = (root / Path(*pure.parts)).resolve()
        if not target.is_relative_to(root) or target.is_symlink():
            raise PermissionError(f"unsafe milestone path: {change.path}")
        try:
            prior = subprocess.run(
                ["git", "show", f"HEAD:{change.path}"], cwd=root,
                capture_output=True, check=True,
            ).stdout
            prior_sha = hashlib.sha256(prior).hexdigest()
        except subprocess.CalledProcessError:
            prior_sha = None
        if prior_sha != change.base_sha256:
            raise RuntimeError(f"milestone candidate base changed: {change.path}")
        artifact = output_dir / "development" / "changed_files" / Path(*pure.parts)
        artifact_content = artifact.read_text(encoding="utf-8") if artifact.is_file() else ""
        if (
            not artifact.is_file()
            or artifact_content.replace("\r\n", "\n")
            != change.content.replace("\r\n", "\n")
        ):
            raise RuntimeError(f"milestone changed-file artifact mismatch: {change.path}")
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(target.suffix + f".{uuid4().hex}.tmp")
        temporary.write_text(artifact_content, encoding="utf-8", newline="\n")
        os.replace(temporary, target)
    _git(root, "add", "--", *[item.path for item in change_set.changes])
    _git(root, "diff", "--cached", "--check")
    _git(root, "commit", "-m", f"OneBrief milestone {milestone.milestone_id}: {milestone.title}")
    after = _git(root, "rev-parse", "HEAD").strip()
    lifecycle = ProjectToolPackLifecycle(
        workspace.project_id, Path(workspace.integration_registry_root)
    )
    state = lifecycle.generate_and_qualify()
    if state.qualification is None or state.qualification.status != "passed":
        raise RuntimeError("advanced milestone ToolPack qualification failed")
    approved = lifecycle.approve(state.qualification.toolpack_sha256)
    if not approved.execution_ready:
        raise RuntimeError("advanced milestone ToolPack is not execution-ready")
    return before, after, canonical_sha256(change_set)


def cumulative_change_set(workspace: MilestoneWorkspace) -> ProjectCodeChangeSet:
    root = Path(workspace.integration_root)
    base = workspace.integration_base_revision
    paths = [
        item for item in _git(root, "diff", "--name-only", f"{base}..HEAD").splitlines()
        if item and item != MANIFEST_NAME
    ]
    changes: list[ProjectFileChange] = []
    baseline_root = Path(workspace.baseline_root)
    for relative in paths:
        target = root / Path(*PurePosixPath(relative).parts)
        if not target.is_file():
            raise RuntimeError(f"milestone aggregation cannot represent deleted file: {relative}")
        baseline = baseline_root / Path(*PurePosixPath(relative).parts)
        changes.append(ProjectFileChange(
            path=relative,
            base_sha256=_file_sha256(baseline) if baseline.is_file() else None,
            content=target.read_text(encoding="utf-8"),
            reason="Accumulated, checkpointed milestone implementation.",
        ))
    if not changes:
        raise RuntimeError("milestone execution produced no accumulated project changes")
    return ProjectCodeChangeSet(
        summary="Accumulated OneBrief milestone candidate for clean-baseline integration.",
        changes=changes,
    )


def seed_integration_candidate(output_dir: Path, candidate: ProjectCodeChangeSet) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "code_change_set.json").write_text(
        candidate.model_dump_json(indent=2) + "\n", encoding="utf-8"
    )
    (output_dir / "reverify_existing_candidate.json").write_text(
        json.dumps({"reason": "Verify accumulated milestone candidate on clean baseline"}, indent=2) + "\n",
        encoding="utf-8",
    )


def milestone_evidence_paths(output_dir: Path) -> list[Path]:
    candidates = [
        output_dir / "final_verification.json",
        output_dir / "final_approval.json",
        output_dir / "completion_ledger.json",
        output_dir / "development" / "development_run.json",
    ]
    return [item for item in candidates if item.is_file()]


def require_milestone_pass(checkpoint: ExecutionCheckpoint, output_dir: Path) -> None:
    if checkpoint.status != PipelineStatus.COMPLETE or checkpoint.final_verdict != Verdict.PASS:
        raise RuntimeError(
            f"milestone did not pass: {checkpoint.status.value}/{checkpoint.final_verdict} "
            f"{checkpoint.message}"
        )
    evidence = milestone_evidence_paths(output_dir)
    if len(evidence) < 4:
        raise RuntimeError("milestone PASS is missing completion or executable evidence")


def publish_final_milestone(work_dir: Path, final_dir: Path) -> None:
    """Expose the final full-contract artifacts at the legacy package paths."""
    development = final_dir / "development"
    if development.is_dir():
        shutil.copytree(development, work_dir / "development", dirs_exist_ok=True)
    for name in (
        "code_change_set.json", "execution_checkpoint.json", "completion_ledger.json",
        "final_verification.json", "final_approval.json", "draft_r0.json",
    ):
        source = final_dir / name
        if source.is_file():
            shutil.copy2(source, work_dir / name)


def publish_failed_milestone_progress(work_dir: Path, milestone_dir: Path) -> None:
    """Project the active slice onto legacy recovery paths without claiming PASS."""
    names = [
        "code_change_set.json",
        "development_best_candidate.json",
        "development_best_failure.txt",
        "development_pending_promotion.json",
        "development_verification_failure.txt",
        "convergence_ledger.json",
        "repair_contract.json",
        *(f"code_change_set_r{index}.json" for index in range(13)),
        *(f"development_verification_failure_r{index}.txt" for index in range(13)),
    ]
    for name in names:
        source = milestone_dir / name
        if source.is_file():
            shutil.copy2(source, work_dir / name)


def execute_milestone_plan(
    *, plan: MilestonePlan, requirements: RequirementsAnalysis, work_dir: Path,
    workspace: MilestoneWorkspace,
    pipeline_factory: Callable[[Path], object],
    intake: IntakeRequest, sources: list[InternalSource],
) -> ExecutionCheckpoint:
    """Run vertical slices and a final clean-baseline full-contract proof."""
    store = MilestoneStore(work_dir / "milestone_state", plan)
    baseline = plan.milestones[0]
    if store.checkpoint(baseline.milestone_id) is None:
        lifecycle = ProjectToolPackLifecycle(
            workspace.project_id, Path(workspace.baseline_registry_root)
        )
        state = lifecycle.state()
        if not state.execution_ready or state.generated is None:
            raise PermissionError("baseline ToolPack changed before milestone execution")
        baseline_evidence = work_dir / "milestone_state" / "baseline_evidence.json"
        baseline_evidence.write_text(json.dumps({
            "source_revision": workspace.source_revision,
            "toolpack_sha256": state.generated.sha256,
            "completion_contract_sha256": plan.completion_contract_digest,
            "original_write_allowed": False,
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        store.record_pass(
            milestone=baseline,
            source_before=workspace.source_revision,
            source_after=workspace.source_revision,
            candidate_sha256=canonical_sha256({"baseline": workspace.source_revision}),
            evidence_paths=[baseline_evidence],
        )

    replayed_root = work_dir / "milestone_workspace" / "replayed"
    replayed_root.mkdir(parents=True, exist_ok=True)
    for prior in plan.milestones[1:]:
        if prior.kind == MilestoneKind.INTEGRATION:
            break
        checkpoint = store.checkpoint(prior.milestone_id)
        if checkpoint is None:
            break
        marker = replayed_root / f"{prior.milestone_id}.json"
        if marker.is_file():
            continue
        prior_dir = work_dir / "milestones" / prior.milestone_id
        change_set_path = prior_dir / "development" / "change_set.json"
        if not change_set_path.is_file():
            raise RuntimeError(
                f"passed milestone is missing its replay candidate: {prior.milestone_id}"
            )
        change_set = ProjectCodeChangeSet.model_validate_json(
            change_set_path.read_text(encoding="utf-8")
        )
        expected_sha = canonical_sha256(change_set)
        if expected_sha != checkpoint.candidate_sha256:
            raise RuntimeError(
                f"passed milestone replay candidate changed: {prior.milestone_id}"
            )
        root = Path(workspace.integration_root)
        already_present = all(
            (root / Path(*PurePosixPath(change.path).parts)).is_file()
            and (
                root / Path(*PurePosixPath(change.path).parts)
            ).read_text(encoding="utf-8").replace("\r\n", "\n")
            == change.content.replace("\r\n", "\n")
            for change in change_set.changes
        )
        if not already_present:
            _before, _after, replayed_sha = advance_milestone_candidate(
                workspace=workspace, milestone=prior, output_dir=prior_dir
            )
            if replayed_sha != checkpoint.candidate_sha256:
                raise RuntimeError(
                    f"replayed milestone digest changed: {prior.milestone_id}"
                )
        marker.write_text(json.dumps({
            "milestone_id": prior.milestone_id,
            "checkpoint_id": checkpoint.checkpoint_id,
            "candidate_sha256": checkpoint.candidate_sha256,
            "replayed_at": _now(),
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    for milestone in plan.milestones[1:]:
        existing = store.checkpoint(milestone.milestone_id)
        if existing is not None:
            continue
        if not store.ready(milestone):
            raise RuntimeError(f"milestone dependencies are not complete: {milestone.milestone_id}")
        milestone_dir = work_dir / "milestones" / milestone.milestone_id
        scoped = requirements_for_milestone(requirements, milestone)
        if milestone.kind == MilestoneKind.INTEGRATION:
            candidate = cumulative_change_set(workspace)
            seed_integration_candidate(milestone_dir, candidate)
            pipeline = pipeline_factory(Path(workspace.baseline_registry_root))
            checkpoint = pipeline.run(
                intake=intake,
                requirements=requirements,
                sources=sources,
                output_dir=milestone_dir,
            )
            require_milestone_pass(checkpoint, milestone_dir)
            store.record_pass(
                milestone=milestone,
                source_before=workspace.source_revision,
                source_after=workspace.source_revision,
                candidate_sha256=canonical_sha256(candidate),
                evidence_paths=milestone_evidence_paths(milestone_dir),
            )
            publish_final_milestone(work_dir, milestone_dir)
            return checkpoint
        pipeline = pipeline_factory(Path(workspace.integration_registry_root))
        milestone_intake = intake.model_copy(update={
            "goal": scoped.normalized_goal,
            "max_revision_rounds": milestone.contract.max_revision_rounds,
        })
        checkpoint = pipeline.run(
            intake=milestone_intake,
            requirements=scoped,
            sources=sources,
            output_dir=milestone_dir,
        )
        if checkpoint.status != PipelineStatus.COMPLETE or checkpoint.final_verdict != Verdict.PASS:
            publish_failed_milestone_progress(work_dir, milestone_dir)
            (work_dir / "execution_checkpoint.json").write_text(
                checkpoint.model_dump_json(indent=2) + "\n", encoding="utf-8"
            )
            return checkpoint
        require_milestone_pass(checkpoint, milestone_dir)
        before, after, candidate_sha = advance_milestone_candidate(
            workspace=workspace, milestone=milestone, output_dir=milestone_dir
        )
        store.record_pass(
            milestone=milestone,
            source_before=before,
            source_after=after,
            candidate_sha256=candidate_sha,
            evidence_paths=milestone_evidence_paths(milestone_dir),
        )
    raise RuntimeError("milestone plan exhausted without a final integration result")
