"""Digest-bound dynamic Quest orchestration over the approved milestone skeleton.

Milestones describe the authorized project envelope.  A Quest is the only active
unit of work: it is issued from the latest verified checkpoint, completed only by
an independent receipt, and cannot expand scope, budget, or authority.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator

from onebrief.delivery_intent import construction_directives
from onebrief.assurance import AssurancePolicy, resolve_assurance_policy
from onebrief.completion_ledger import CompletionLedger, CompletionStatus
from onebrief.execution_schemas import ExecutionCheckpoint, PipelineStatus, VerificationReport, Verdict
from onebrief.schemas import ExecutionPhase, InternalSource, RequirementsAnalysis, SourcePriority
from onebrief.quest_kernel import QuestKernelEngine, collect_raw_quest_receipt
from onebrief.quest_kernel.models import QuestTransition


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _digest(value: object) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        default=lambda item: item.model_dump(mode="json")
        if isinstance(item, BaseModel) else str(item),
    ).encode("utf-8")).hexdigest()


def _file_digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def _atomic_model(path: Path, value: BaseModel) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + f".{uuid4().hex}.tmp")
    temporary.write_text(value.model_dump_json(indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


class QuestType(StrEnum):
    OUTCOME_BOUND = "outcome_bound"
    PROCESS_BOUND = "process_bound"
    EXPLORATION = "exploration"
    DECISION = "decision"


class QuestState(StrEnum):
    ACTIVE = "active"
    PASSED = "passed"
    BLOCKED = "blocked"
    NEEDS_AUTHORIZATION = "needs_authorization"
    REVOKED = "revoked"


class QuestRole(StrEnum):
    PROJECT_OWNER = "project_owner"
    ACCOUNTABLE_MAKER = "accountable_maker"
    EVIDENCE_BUILDER = "evidence_builder"
    INDEPENDENT_VERIFIER = "independent_verifier"
    ORCHESTRATOR = "orchestrator"
    TECHNICAL_REPAIR_AGENT = "technical_repair_agent"
    HUMAN_SOVEREIGN = "human_sovereign"


class QuestFailureOwner(StrEnum):
    NONE = "none"
    MAKER = "maker"
    EVIDENCE = "evidence"
    VERIFIER = "verifier"
    ORCHESTRATOR = "orchestrator"
    TOOLPACK = "toolpack"
    TECHNICAL = "technical"
    AUTHORIZATION = "authorization"
    UNKNOWN = "unknown"


class OutcomeSketch(BaseModel):
    schema_version: Literal["onebrief-outcome-sketch-v1"] = "onebrief-outcome-sketch-v1"
    project_id: str = Field(min_length=1, max_length=120)
    final_outcome: str = Field(min_length=3, max_length=1200)
    must_work: list[str] = Field(min_length=1, max_length=24)
    preserve: list[str] = Field(default_factory=list, max_length=24)
    construction_directives: list[str] = Field(default_factory=list, max_length=12)
    prohibitions: list[str] = Field(default_factory=list, max_length=24)
    objective_completion_conditions: list[str] = Field(min_length=1, max_length=24)
    preference_profile: str = Field(default="Use approved professional defaults.", max_length=1000)
    assurance_policy: AssurancePolicy | None = None
    completion_contract_digest: str = Field(pattern=r"^[a-f0-9]{64}$")

    @property
    def sha256(self) -> str:
        return _digest(self.model_dump(mode="json", exclude_none=True))


class QuestBudget(BaseModel):
    minimum_usd: float = Field(ge=0)
    maximum_usd: float = Field(ge=0)
    consumed_usd: float = Field(default=0, ge=0)

    @model_validator(mode="after")
    def valid_envelope(self) -> "QuestBudget":
        if self.maximum_usd < self.minimum_usd:
            raise ValueError("quest maximum budget cannot be below minimum")
        if self.consumed_usd > self.maximum_usd:
            raise ValueError("quest consumed budget exceeds its approved maximum")
        return self


class QuestInputCheckpoint(BaseModel):
    milestone_plan_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_revision: str = Field(min_length=1, max_length=128)
    dependency_checkpoints: dict[str, str] = Field(default_factory=dict, max_length=14)
    previous_receipt_id: str | None = Field(default=None, pattern=r"^QR-[a-f0-9]{16}$")
    previous_receipt_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


class QuestContract(BaseModel):
    schema_version: Literal["onebrief-quest-contract-v1"] = "onebrief-quest-contract-v1"
    quest_id: str = Field(pattern=r"^QC-[a-f0-9]{16}$")
    parent_quest_id: str | None = Field(default=None, pattern=r"^QC-[a-f0-9]{16}$")
    project_id: str = Field(min_length=1, max_length=120)
    milestone_id: str = Field(pattern=r"^M[0-9]{2}$")
    objective: str = Field(min_length=3, max_length=1600)
    quest_type: QuestType
    initial_execution_phase: ExecutionPhase
    input_checkpoint: QuestInputCheckpoint
    authority_envelope_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    assurance_profile_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    authorized_files: list[str] = Field(default_factory=list, max_length=64)
    forbidden_scope: list[str] = Field(min_length=1, max_length=32)
    required_process: list[str] = Field(min_length=1, max_length=24)
    acceptance_criteria: list[str] = Field(min_length=1, max_length=24)
    required_evidence: list[str] = Field(min_length=1, max_length=32)
    assigned_role: QuestRole
    verifier_role: QuestRole = QuestRole.INDEPENDENT_VERIFIER
    budget: QuestBudget
    dependencies: list[str] = Field(default_factory=list, max_length=14)
    failure_owner: QuestFailureOwner = QuestFailureOwner.UNKNOWN
    preserve_receipt_ids: list[str] = Field(default_factory=list, max_length=32)
    issued_at: str

    @model_validator(mode="after")
    def separation_and_dependencies(self) -> "QuestContract":
        if self.assigned_role == self.verifier_role:
            raise ValueError("quest maker and independent verifier must be different roles")
        if set(self.dependencies) != set(self.input_checkpoint.dependency_checkpoints):
            raise ValueError("quest dependencies must be bound to exact checkpoints")
        if self.parent_quest_id == self.quest_id:
            raise ValueError("quest cannot be its own parent")
        return self

    @property
    def sha256(self) -> str:
        return _digest(self.model_dump(mode="json", exclude_none=True))


class QuestVerificationReceipt(BaseModel):
    schema_version: Literal["onebrief-quest-verification-receipt-v1"] = (
        "onebrief-quest-verification-receipt-v1"
    )
    receipt_id: str = Field(pattern=r"^QR-[a-f0-9]{16}$")
    quest_id: str = Field(pattern=r"^QC-[a-f0-9]{16}$")
    quest_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    milestone_id: str = Field(pattern=r"^M[0-9]{2}$")
    state: QuestState
    verdict: Verdict | None = None
    completion_ledger_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    verification_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    execution_checkpoint_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    passed_criterion_ids: list[str] = Field(default_factory=list, max_length=32)
    preserved_receipt_ids: list[str] = Field(default_factory=list, max_length=32)
    gaps: list[str] = Field(default_factory=list, max_length=32)
    failure_owner: QuestFailureOwner
    verifier_role: QuestRole = QuestRole.INDEPENDENT_VERIFIER
    authorization_required: bool = False
    created_at: str

    @model_validator(mode="after")
    def pass_requires_authoritative_evidence(self) -> "QuestVerificationReceipt":
        if self.state == QuestState.PASSED:
            if self.verdict != Verdict.PASS:
                raise ValueError("passed quest requires PASS verdict")
            if not self.completion_ledger_sha256 or not self.verification_sha256:
                raise ValueError("passed quest requires completion and verification evidence")
            if self.authorization_required or self.failure_owner != QuestFailureOwner.NONE:
                raise ValueError("passed quest cannot retain a failure owner or authorization gap")
        if self.authorization_required and self.state != QuestState.NEEDS_AUTHORIZATION:
            raise ValueError("authorization requirement must produce needs_authorization state")
        return self

    @property
    def sha256(self) -> str:
        return _digest(self)


class ProjectCanvas(BaseModel):
    schema_version: Literal["onebrief-project-canvas-v1"] = "onebrief-project-canvas-v1"
    project_id: str
    outcome_sketch_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    milestone_plan_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    active_quest_id: str | None = Field(default=None, pattern=r"^QC-[a-f0-9]{16}$")
    completed_quest_ids: list[str] = Field(default_factory=list, max_length=32)
    blocked_quest_ids: list[str] = Field(default_factory=list, max_length=32)
    receipt_ids: list[str] = Field(default_factory=list, max_length=64)
    latest_verified_checkpoint: str | None = None
    approved_budget_usd: float = Field(ge=0)
    issued_budget_usd: float = Field(ge=0)
    consumed_budget_usd: float = Field(ge=0)
    current_gaps: list[str] = Field(default_factory=list, max_length=32)
    updated_at: str

    @model_validator(mode="after")
    def one_state_and_budget(self) -> "ProjectCanvas":
        if set(self.completed_quest_ids) & set(self.blocked_quest_ids):
            raise ValueError("a quest cannot be both completed and blocked")
        if self.active_quest_id in set(self.completed_quest_ids + self.blocked_quest_ids):
            raise ValueError("active quest cannot already be terminal")
        if self.issued_budget_usd > self.approved_budget_usd + 1e-9:
            raise ValueError("issued quest budgets exceed project approval")
        if self.consumed_budget_usd > self.approved_budget_usd + 1e-9:
            raise ValueError("consumed quest budget exceeds project approval")
        return self


def build_outcome_sketch(project_id: str, requirements: RequirementsAnalysis) -> OutcomeSketch:
    contract = requirements.completion_contract
    if contract is None:
        raise ValueError("Quest orchestration requires a completion contract")
    preserve = [item for item in requirements.assumptions if any(
        token in item.casefold() for token in ("preserv", "keep", "unchanged", "보존")
    )]
    construction = construction_directives(
        requirements.normalized_goal,
        *requirements.deliverables,
        contract.target_state,
        *(item.description for item in contract.quality_criteria),
    )
    assurance = resolve_assurance_policy(requirements)
    return OutcomeSketch(
        project_id=project_id,
        final_outcome=contract.target_state,
        must_work=[item.description for item in contract.quality_criteria],
        preserve=preserve,
        construction_directives=construction,
        prohibitions=[
            "Do not modify the approved original repository directly.",
            "Do not weaken completion criteria or treat missing evidence as PASS.",
            "Do not expand authorization, budget, or future milestone scope.",
        ],
        objective_completion_conditions=[
            item.evidence_required for item in contract.quality_criteria if item.required
        ],
        preference_profile=(
            requirements.sixsense.standard_profile
            if requirements.sixsense is not None
            else "Use approved professional defaults while preserving project identity."
        ),
        assurance_policy=assurance,
        completion_contract_digest=_digest(contract),
    )


class QuestStore:
    """Append-only Quest contracts/receipts plus one digest-bound Project Canvas."""

    def __init__(
        self, root: Path, *, plan: Any, requirements: RequirementsAnalysis,
        toolpack_sha256: str | None = None,
        allowed_write_prefixes: list[str] | None = None,
        approved_budget_usd: float | None = None,
    ):
        self.root = root
        self.contracts = root / "contracts"
        self.receipts = root / "receipts"
        self.canvas_path = root / "project_canvas.json"
        self.outcome_path = root / "outcome_sketch.json"
        self.plan = plan
        self.requirements = requirements
        assurance = resolve_assurance_policy(requirements)
        planned_assurance = getattr(plan, "assurance_profile_digest", None)
        if planned_assurance not in {None, assurance.sha256}:
            raise RuntimeError("milestone plan is bound to a different assurance profile")
        self.toolpack_sha256 = toolpack_sha256
        self.allowed_write_prefixes = list(allowed_write_prefixes or [])
        self.approved_budget_usd = (
            plan.maximum_cost_usd if approved_budget_usd is None else approved_budget_usd
        )
        if self.approved_budget_usd < plan.minimum_cost_usd:
            raise ValueError("approved Quest budget is below the project minimum")
        if self.approved_budget_usd > plan.maximum_cost_usd + 1e-9:
            raise ValueError("approved Quest budget exceeds the quoted maximum")
        self.outcome = build_outcome_sketch(plan.project_id, requirements)
        root.mkdir(parents=True, exist_ok=True)
        if self.outcome_path.is_file():
            existing = OutcomeSketch.model_validate_json(self.outcome_path.read_text(encoding="utf-8"))
            if existing.sha256 != self.outcome.sha256:
                raise RuntimeError("persisted Outcome Sketch differs from the approved contract")
        else:
            _atomic_model(self.outcome_path, self.outcome)
        if not self.canvas_path.is_file():
            _atomic_model(self.canvas_path, ProjectCanvas(
                project_id=plan.project_id,
                outcome_sketch_sha256=self.outcome.sha256,
                milestone_plan_sha256=plan.sha256,
                approved_budget_usd=self.approved_budget_usd,
                issued_budget_usd=0,
                consumed_budget_usd=0,
                updated_at=_now(),
            ))
        self._validate_canvas(self.canvas())

    def canvas(self) -> ProjectCanvas:
        return ProjectCanvas.model_validate_json(self.canvas_path.read_text(encoding="utf-8"))

    def _validate_canvas(self, canvas: ProjectCanvas) -> None:
        if canvas.milestone_plan_sha256 != self.plan.sha256:
            raise RuntimeError("Project Canvas is bound to a different milestone plan")
        if canvas.outcome_sketch_sha256 != self.outcome.sha256:
            raise RuntimeError("Project Canvas is bound to a different Outcome Sketch")

    def contract(self, quest_id: str) -> QuestContract:
        return QuestContract.model_validate_json((self.contracts / f"{quest_id}.json").read_text(encoding="utf-8"))

    def receipt(self, receipt_id: str) -> QuestVerificationReceipt:
        receipt = QuestVerificationReceipt.model_validate_json(
            (self.receipts / f"{receipt_id}.json").read_text(encoding="utf-8")
        )
        if receipt.receipt_id != f"QR-{_digest(receipt.model_dump(mode='json', exclude={'receipt_id', 'created_at'}))[:16]}":
            raise RuntimeError("Quest receipt integrity failed")
        return receipt

    def latest_receipt(self) -> QuestVerificationReceipt | None:
        canvas = self.canvas()
        return self.receipt(canvas.receipt_ids[-1]) if canvas.receipt_ids else None

    def issue(self, *, milestone: Any, milestone_store: Any, source_revision: str) -> QuestContract:
        canvas = self.canvas()
        if canvas.active_quest_id is not None:
            active = self.contract(canvas.active_quest_id)
            if active.milestone_id != milestone.milestone_id:
                raise RuntimeError("another Quest is active; parallel milestone execution is forbidden")
            return active
        previous = self.latest_receipt()
        kernel_transition = QuestKernelEngine(self.root).latest() if previous is not None else None
        if previous is not None and previous.state != QuestState.PASSED:
            if kernel_transition is None:
                raise RuntimeError(
                    "blocked Quest has no digest-bound Kernel transition; revalidate its Receipt"
                )
            _raw_receipt, transition_decision = kernel_transition
            if (
                previous.authorization_required
                or transition_decision.transition == QuestTransition.NEEDS_AUTHORIZATION
            ):
                raise PermissionError("previous Quest requires authorization before a successor can be issued")
            if transition_decision.transition == QuestTransition.STRUCTURAL_STOP:
                raise RuntimeError(
                    "previous Quest requires structural redesign before a successor can be issued"
                )
            if transition_decision.transition not in {
                QuestTransition.REPAIR_PRODUCT,
                QuestTransition.REPAIR_EVIDENCE,
            }:
                raise RuntimeError(
                    "blocked Quest receipt does not authorize a repair successor"
                )
        dependency_checkpoints = {}
        for dependency in milestone.dependencies:
            checkpoint = milestone_store.checkpoint(dependency)
            if checkpoint is None:
                raise RuntimeError(f"Quest dependency is not verified: {dependency}")
            dependency_checkpoints[dependency] = checkpoint.checkpoint_id
        previous_id = previous.receipt_id if previous else None
        previous_sha = previous.sha256 if previous else None
        preserved = list(canvas.receipt_ids)
        objective = milestone.contract.target_state
        if previous is not None:
            if previous.state == QuestState.PASSED:
                objective = (
                    f"From verified receipt {previous.receipt_id}, complete {milestone.milestone_id}: "
                    f"{milestone.contract.target_state} Preserve all prior PASS evidence."
                )
            else:
                objective = (
                    f"From blocked receipt {previous.receipt_id}, repair {milestone.milestone_id}: "
                    f"{' | '.join(previous.gaps)} Preserve the approved target and all prior PASS evidence."
                )
        budget = QuestBudget(
            minimum_usd=round(self.plan.minimum_cost_usd * milestone.budget_weight, 8),
            maximum_usd=round(self.approved_budget_usd * milestone.budget_weight, 8),
        )
        prior_quest_ids = [*canvas.completed_quest_ids, *canvas.blocked_quest_ids]
        first_for_milestone = not any(
            self.contract(quest_id).milestone_id == milestone.milestone_id
            for quest_id in prior_quest_ids
        )
        criterion_descriptions = {
            item.criterion_id: item.description
            for item in self.requirements.completion_contract.quality_criteria
        } if self.requirements.completion_contract is not None else {}
        acceptance = [
            f"{criterion_id}: {criterion_descriptions.get(criterion_id, criterion_id)}"
            for criterion_id in milestone.contract.criterion_ids
        ]
        acceptance.extend(
            f"{item.criterion_id}: {item.description}"
            for item in milestone.contract.slice_quality_criteria
            if item.criterion_id not in milestone.contract.criterion_ids
        )
        assurance = resolve_assurance_policy(self.requirements)
        stable = {
            "parent_quest_id": previous.quest_id if previous is not None else None,
            "project_id": self.plan.project_id,
            "milestone_id": milestone.milestone_id,
            "objective": objective,
            "quest_type": QuestType.PROCESS_BOUND,
            "initial_execution_phase": (
                kernel_transition[1].next_phase
                if previous is not None
                and previous.state != QuestState.PASSED
                and kernel_transition is not None
                else milestone.initial_execution_phase
            ),
            "input_checkpoint": QuestInputCheckpoint(
                milestone_plan_sha256=self.plan.sha256,
                source_revision=source_revision,
                dependency_checkpoints=dependency_checkpoints,
                previous_receipt_id=previous_id,
                previous_receipt_sha256=previous_sha,
            ),
            "authority_envelope_sha256": _digest({
                "milestone": milestone,
                "milestone_plan_sha256": self.plan.sha256,
                "toolpack_sha256": self.toolpack_sha256,
                "allowed_write_prefixes": self.allowed_write_prefixes,
                "approved_budget_usd": self.approved_budget_usd,
                "assurance_profile_sha256": assurance.sha256,
            }),
            "assurance_profile_sha256": assurance.sha256,
            "authorized_files": self.allowed_write_prefixes or [
                "Only paths permitted by the approved project ToolPack for this milestone."
            ],
            "forbidden_scope": [
                "Original repository writes",
                "Acceptance-criterion weakening",
                "Unapproved budget or runtime authority",
                "Implementation work owned by a future milestone",
                "Maker self-approval",
            ],
            "required_process": [
                "Use the approved ToolPack and source revision.",
                f"Apply the {assurance.intended_use.value} assurance profile without weakening its truth floor.",
                "Preserve previously passed receipts and criteria.",
                "Separate accountable making from independent verification.",
                "Stop on a no-progress or authority-required receipt.",
                *([
                    "The approved outcome requires new product construction. Test-only changes and minor "
                    "legacy-surface edits are baseline or maintenance evidence, never completion evidence."
                ] if self.outcome.construction_directives else []),
            ],
            "acceptance_criteria": acceptance or ["Milestone contract passes"],
            "required_evidence": list(milestone.contract.evidence_requirements),
            "assigned_role": QuestRole.ACCOUNTABLE_MAKER,
            "verifier_role": QuestRole.INDEPENDENT_VERIFIER,
            "budget": budget,
            "dependencies": list(milestone.dependencies),
            "failure_owner": QuestFailureOwner.UNKNOWN,
            "preserve_receipt_ids": preserved,
        }
        quest_id = f"QC-{_digest(stable)[:16]}"
        quest = QuestContract(quest_id=quest_id, issued_at=_now(), **stable)
        path = self.contracts / f"{quest_id}.json"
        if path.is_file():
            existing = QuestContract.model_validate_json(path.read_text(encoding="utf-8"))
            if existing.sha256 != quest.sha256:
                raise RuntimeError("existing Quest ID has different content")
        else:
            _atomic_model(path, quest)
        _atomic_model(self.canvas_path, canvas.model_copy(update={
            "active_quest_id": quest_id,
            "issued_budget_usd": canvas.issued_budget_usd + (
                budget.maximum_usd if first_for_milestone else 0
            ),
            "current_gaps": list(previous.gaps) if previous else [],
            "updated_at": _now(),
        }))
        return quest

    def source(self, quest: QuestContract) -> InternalSource:
        content = quest.model_dump_json(indent=2)
        return InternalSource(
            name=f"onebrief-active-quest-{quest.quest_id}.json",
            priority=SourcePriority.MANDATORY,
            requirement_keys=["active_quest"],
            summary="The only authorized current unit of work and its verification contract.",
            content=content,
            media_type="application/json",
            size_bytes=len(content.encode("utf-8")),
            sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
        )

    def record_result(
        self, *, quest: QuestContract, checkpoint: ExecutionCheckpoint, output_dir: Path,
        failure_owner: QuestFailureOwner = QuestFailureOwner.UNKNOWN,
    ) -> QuestVerificationReceipt:
        canvas = self.canvas()
        if canvas.active_quest_id != quest.quest_id:
            raise RuntimeError("result does not belong to the active Quest")
        checkpoint_path = output_dir / "execution_checkpoint.json"
        if not checkpoint_path.is_file():
            checkpoint_path.write_text(checkpoint.model_dump_json(indent=2) + "\n", encoding="utf-8")
        ledger_path = output_dir / "completion_ledger.json"
        verification_path = output_dir / "final_verification.json"
        try:
            ledger = CompletionLedger.model_validate_json(
                ledger_path.read_text(encoding="utf-8")
            ) if ledger_path.is_file() else None
        except (OSError, ValueError):
            ledger = None
        try:
            verification = VerificationReport.model_validate_json(
                verification_path.read_text(encoding="utf-8")
            ) if verification_path.is_file() else None
        except (OSError, ValueError):
            verification = None
        passed = bool(
            checkpoint.status == PipelineStatus.COMPLETE
            and checkpoint.final_verdict == Verdict.PASS
            and ledger is not None and ledger.complete and ledger.latest_verdict == Verdict.PASS
            and verification is not None and verification.verdict == Verdict.PASS
        )
        blocked_contracts = sorted(output_dir.glob("repair_contract_f*.json"))
        if not blocked_contracts and (output_dir / "repair_contract.json").is_file():
            blocked_contracts = [output_dir / "repair_contract.json"]
        authorization_required = checkpoint.status == PipelineStatus.NEEDS_AUTHORIZATION
        no_progress = False
        if blocked_contracts:
            raw = json.loads(blocked_contracts[-1].read_text(encoding="utf-8"))
            no_progress = not raw.get("execution_allowed", True) and raw.get("escalation_required", False)
            authorization_required = authorization_required or (
                no_progress and "author" in json.dumps(raw, ensure_ascii=False).casefold()
            )
        state = QuestState.PASSED if passed else (
            QuestState.NEEDS_AUTHORIZATION if authorization_required else QuestState.BLOCKED
        )
        if passed:
            failure_owner = QuestFailureOwner.NONE
        elif authorization_required:
            failure_owner = QuestFailureOwner.AUTHORIZATION
        QuestKernelEngine(self.root).record(
            collect_raw_quest_receipt(output_dir, checkpoint)
        )
        gaps: list[str] = []
        if ledger is not None:
            for criterion in ledger.criteria:
                if criterion.required and criterion.status != CompletionStatus.PASSED:
                    gaps.extend(criterion.failure_reasons or [criterion.description])
        if not gaps and checkpoint.message:
            gaps.append(checkpoint.message)
        passed_ids = [
            item.criterion_id for item in ledger.criteria
            if item.status == CompletionStatus.PASSED
        ] if ledger else []
        stable = {
            "schema_version": "onebrief-quest-verification-receipt-v1",
            "quest_id": quest.quest_id,
            "quest_sha256": quest.sha256,
            "milestone_id": quest.milestone_id,
            "state": state,
            "verdict": checkpoint.final_verdict,
            "completion_ledger_sha256": _file_digest(ledger_path) if ledger_path.is_file() else None,
            "verification_sha256": _file_digest(verification_path) if verification_path.is_file() else None,
            "execution_checkpoint_sha256": _file_digest(checkpoint_path),
            "passed_criterion_ids": passed_ids,
            "preserved_receipt_ids": list(quest.preserve_receipt_ids),
            "gaps": list(dict.fromkeys(gaps))[:32],
            "failure_owner": failure_owner,
            "verifier_role": QuestRole.INDEPENDENT_VERIFIER,
            "authorization_required": authorization_required,
        }
        receipt_id = f"QR-{_digest(stable)[:16]}"
        receipt = QuestVerificationReceipt(receipt_id=receipt_id, created_at=_now(), **stable)
        path = self.receipts / f"{receipt_id}.json"
        if not path.is_file():
            _atomic_model(path, receipt)
        completed = list(canvas.completed_quest_ids)
        blocked = list(canvas.blocked_quest_ids)
        if passed:
            completed.append(quest.quest_id)
        else:
            blocked.append(quest.quest_id)
        _atomic_model(self.canvas_path, canvas.model_copy(update={
            "active_quest_id": None,
            "completed_quest_ids": list(dict.fromkeys(completed)),
            "blocked_quest_ids": list(dict.fromkeys(blocked)),
            "receipt_ids": [*canvas.receipt_ids, receipt_id],
            "latest_verified_checkpoint": receipt_id if passed else canvas.latest_verified_checkpoint,
            "current_gaps": receipt.gaps,
            "updated_at": _now(),
        }))
        return receipt


def infer_failure_owner(output_dir: Path, checkpoint: ExecutionCheckpoint) -> QuestFailureOwner:
    """Classify ownership from structured receipts before considering prose."""
    if checkpoint.status == PipelineStatus.NEEDS_AUTHORIZATION:
        return QuestFailureOwner.AUTHORIZATION
    text_parts = [checkpoint.message]
    for pattern in ("development_best_failure.txt", "development_verification_failure.txt"):
        path = output_dir / pattern
        if path.is_file():
            text_parts.append(path.read_text(encoding="utf-8", errors="replace"))
    text = " ".join(text_parts).casefold()
    # A newer orchestration/provider exception supersedes the phase that was
    # active before the envelope failed. It must never authorize product edits.
    if any(token in text for token in (
        "invalid_argument", "invalid argument", "validationerror", "pydantic",
        "provider", "schema", "catalog-anchored product repair cannot target proof",
    )):
        return QuestFailureOwner.ORCHESTRATOR
    phase_paths = sorted(output_dir.glob("phase_decision_deterministic_r*.json"))
    if not phase_paths:
        phase_paths = sorted(output_dir.glob("phase_decision_r*.json"))
    if phase_paths:
        try:
            owner = json.loads(phase_paths[-1].read_text(encoding="utf-8")).get("failure_owner")
            mapped = {
                "product": QuestFailureOwner.MAKER,
                "evidence": QuestFailureOwner.EVIDENCE,
                "environment": QuestFailureOwner.TOOLPACK,
                "contract": QuestFailureOwner.ORCHESTRATOR,
            }.get(owner)
            if mapped is not None:
                return mapped
        except (OSError, ValueError, TypeError):
            pass
    if any(token in text for token in (
        "authentication fixture", "approved authenticated", "needs authorization",
    )) and "no progress" in text:
        return QuestFailureOwner.AUTHORIZATION
    if any(token in text for token in (
        "playmode test", "test.cs", ".asmdef", "evidence harness", "proof artifact",
        "scenario receipt", "scenarioreceipt",
    )):
        return QuestFailureOwner.EVIDENCE
    return QuestFailureOwner.UNKNOWN
