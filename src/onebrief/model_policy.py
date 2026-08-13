"""Deterministic model pricing, approval, persistence, and runtime enforcement."""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, TypeVar
from uuid import uuid4

from pydantic import BaseModel, Field

from onebrief.agent_registry import ApprovedModel
from onebrief.budget_guard import BudgetExceeded
from onebrief.execution_graph import STAGE_BY_AGENT
from onebrief.producer import PRICES
from onebrief.schemas import BudgetEnvelope, StageEstimate
from onebrief.team_planning import TeamPlan

T = TypeVar("T", bound=BaseModel)


class ModelBudgetStatus(StrEnum):
    APPROVED = "approved"
    NEEDS_BUDGET = "needs_budget"


class ModelBudgetDecision(BaseModel):
    schema_version: str = "onebrief-model-budget-decision-v1"
    project_id: str
    team_plan_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    approved_usd: float = Field(gt=0)
    minimum_cost_usd: float = Field(ge=0)
    recommended_cost_usd: float = Field(ge=0)
    maximum_cost_usd: float = Field(ge=0)
    status: ModelBudgetStatus
    selected_stages: list[StageEstimate]
    explanation: str


class ModelSelectionCategory(StrEnum):
    BASELINE = "baseline"
    REASONING_INSUFFICIENCY = "reasoning_insufficiency"
    NON_REASONING_FAILURE = "non_reasoning_failure"


class ModelSelectionDecision(BaseModel):
    """Immutable explanation for keeping or escalating one agent's model."""

    schema_version: str = "onebrief-model-selection-decision-v1"
    decision_id: str = Field(default_factory=lambda: str(uuid4()))
    selected_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    stage: str
    call_stage: str
    base_model: ApprovedModel
    selected_model: ApprovedModel
    difficulty: str
    category: ModelSelectionCategory
    escalated: bool
    failure_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    explanation: str


class ModelExecutionPolicy(BaseModel):
    schema_version: str = "onebrief-model-execution-policy-v2"
    project_id: str
    team_plan_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    price_card_version: str
    approved_usd: float = Field(gt=0)
    stage_models: dict[str, ApprovedModel]
    stage_model_ladders: dict[str, list[ApprovedModel]] = Field(default_factory=dict)

    @staticmethod
    def _base_stage(stage: str) -> tuple[str, bool]:
        base_stage = stage
        terminal_retry = re.compile(r"(?:_compact_retry|_verification_retry)$")
        while terminal_retry.search(base_stage):
            base_stage = terminal_retry.sub("", base_stage)
        escalation_requested = bool(
            re.search(r"_reasoning_escalation_r\d+$", base_stage)
        )
        if escalation_requested:
            return (
                re.sub(r"_reasoning_escalation_r\d+$", "", base_stage),
                True,
            )
        retry_suffix = re.compile(
            r"(?:_revision_r\d+|_repair_r\d+|_refinement_r\d+|_r\d+|"
            r"_compact_retry|_verification_retry)$"
        )
        while retry_suffix.search(base_stage):
            base_stage = retry_suffix.sub("", base_stage)
        return base_stage, False

    def model_for(self, stage: str) -> str:
        base_stage, escalation_requested = self._base_stage(stage)
        selected = self.stage_models.get(base_stage)
        if selected is None:
            raise PermissionError(f"stage has no approved model binding: {stage}")
        if escalation_requested:
            ladder = self.stage_model_ladders.get(base_stage, [])
            if len(ladder) < 2 or ladder[0] != selected:
                raise PermissionError(
                    f"stage has no approved reasoning escalation: {stage}"
                )
            return ladder[-1].value
        return selected.value

    def enforce(self, stage: str, model: str) -> None:
        approved = self.model_for(stage)
        if model != approved:
            raise PermissionError(
                f"model blocked before provider call: {stage} requires {approved}, received {model}"
            )

    def select_after_failure(
        self,
        stage: str,
        *,
        failure_text: str,
        difficulty: str,
        attempt: int = 1,
    ) -> ModelSelectionDecision:
        """Select from a pre-approved ladder without changing the agent identity."""

        base_stage, _ = self._base_stage(stage)
        base_model = self.stage_models.get(base_stage)
        if base_model is None:
            raise PermissionError(f"stage has no approved model binding: {stage}")
        ladder = self.stage_model_ladders.get(base_stage, [base_model])
        normalized = " ".join(failure_text.casefold().split())
        failure_sha256 = (
            hashlib.sha256(failure_text.encode("utf-8")).hexdigest()
            if failure_text
            else None
        )
        non_reasoning_markers = (
            "invalid json",
            "eof while parsing",
            "max_tokens",
            "max tokens",
            "deadline exceeded",
            "service unavailable",
            "status code 502",
            "status code 503",
            "stale or missing base hash",
            "edit anchors could not rediscover",
            "catalog anchor is not approved",
            "string_pattern_mismatch",
            "permissionerror",
            "budget exceeded",
            "needs_information",
        )
        reasoning_markers = (
            "independent unity semantic visual observation failed",
            "did not visibly change any text",
            "edits tests or evidence instead of production ui",
            "stalled after two identical candidates",
            "identical candidate",
            "wrong repair strategy",
            "failed acceptance criterion",
            "evidence harness is an atomic bundle",
            "create the missing unity evidence harness as one atomic two-file repair",
        )
        non_reasoning = any(marker in normalized for marker in non_reasoning_markers)
        explicit_reasoning = any(marker in normalized for marker in reasoning_markers)
        complex_product_failure = (
            difficulty.casefold() == "complex"
            and any(marker in normalized for marker in (
                "verification failed",
                "semantic visual",
                "rendered ui defect",
                "responsive layout",
                "glyph",
            ))
        )
        can_escalate = len(ladder) >= 2 and ladder[0] == base_model
        escalate = bool(
            failure_text
            and can_escalate
            and not non_reasoning
            and (explicit_reasoning or complex_product_failure)
        )
        selected_model = ladder[-1] if escalate else base_model
        call_stage = (
            f"{base_stage}_reasoning_escalation_r{max(1, attempt)}"
            if escalate
            else base_stage
        )
        category = (
            ModelSelectionCategory.REASONING_INSUFFICIENCY
            if escalate
            else (
                ModelSelectionCategory.NON_REASONING_FAILURE
                if failure_text and non_reasoning
                else ModelSelectionCategory.BASELINE
            )
        )
        explanation = (
            "The same maker keeps its role, memory, and repair context but uses the approved Pro rung "
            "because a complex semantic/reasoning failure was observed."
            if escalate
            else (
                "The failure is structural, transient, budgetary, or malformed-output related; a stronger "
                "reasoning model would not address its cause, so the approved base model is retained."
                if non_reasoning
                else "No approved reasoning escalation condition was met; retain the base model."
            )
        )
        return ModelSelectionDecision(
            stage=base_stage,
            call_stage=call_stage,
            base_model=base_model,
            selected_model=selected_model,
            difficulty=difficulty,
            category=category,
            escalated=escalate,
            failure_sha256=failure_sha256,
            explanation=explanation,
        )


def _plan_hash(plan: TeamPlan) -> str:
    canonical = json.dumps(
        plan.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _repriced_stage(stage: StageEstimate, model: str) -> StageEstimate:
    price = PRICES[model]
    per_call = (
        stage.input_tokens_per_call * price.input_per_million
        + stage.output_tokens_per_call * price.output_per_million
    ) / 1_000_000 + stage.fixed_cost_usd_per_call
    return stage.model_copy(update={
        "model": model,
        "minimum_cost_usd": round(per_call * stage.minimum_calls, 6),
        "recommended_cost_usd": round(per_call * stage.recommended_calls, 6),
        "maximum_cost_usd": round(per_call * stage.maximum_calls, 6),
    })


def evaluate_model_budget(
    estimate: BudgetEnvelope,
    plan: TeamPlan,
    *,
    approved_usd: float,
    cost_stage_names: set[str] | None = None,
) -> tuple[ModelBudgetDecision, ModelExecutionPolicy]:
    """Reprice the approved work after the owner chooses each instance model."""
    by_id = {member.instance_id: member for member in plan.members}
    stage_models: dict[str, ApprovedModel] = {
        "team_planning": ApprovedModel.GEMINI_3_1_PRO_PREVIEW,
    }
    for stage, owner_id in plan.stage_owners.items():
        stage_models[stage] = by_id[owner_id].model
    for member in plan.members:
        for stage in STAGE_BY_AGENT[member.agent_type]:
            stage_models[stage] = member.model

    stage_model_ladders: dict[str, list[ApprovedModel]] = {}
    maker_model = stage_models.get("long_form_draft")
    if maker_model == ApprovedModel.GEMINI_3_5_FLASH:
        # The Pro rung is approved up front but remains inaccessible unless a
        # deterministic failure classifier selects it. The hard cost ledger is
        # still authoritative for every provider call.
        stage_model_ladders["long_form_draft"] = [
            ApprovedModel.GEMINI_3_5_FLASH,
            ApprovedModel.GEMINI_3_1_PRO_PREVIEW,
        ]

    selected: list[StageEstimate] = []
    for stage in estimate.stages:
        if cost_stage_names is not None and stage.stage not in cost_stage_names:
            continue
        chosen = stage_models.get(stage.stage)
        if chosen is None:
            if stage.minimum_calls == 0:
                continue
            raise ValueError(f"budget stage has no TeamPlan model owner: {stage.stage}")
        selected.append(_repriced_stage(stage, chosen.value))

    escalation_recommended_reserve = 0.0
    escalation_maximum_reserve = 0.0
    for stage in selected:
        ladder = stage_model_ladders.get(stage.stage)
        if not ladder:
            continue
        elevated = _repriced_stage(stage, ladder[-1].value)
        base_per_call = (
            stage.recommended_cost_usd / stage.recommended_calls
            if stage.recommended_calls
            else 0.0
        )
        elevated_per_call = (
            elevated.recommended_cost_usd / elevated.recommended_calls
            if elevated.recommended_calls
            else 0.0
        )
        escalation_recommended_reserve += max(0.0, elevated_per_call - base_per_call)
        escalation_maximum_reserve += max(
            0.0, elevated.maximum_cost_usd - stage.maximum_cost_usd
        )

    minimum = round(sum(item.minimum_cost_usd for item in selected) * 1.10, 4)
    recommended = round(
        (sum(item.recommended_cost_usd for item in selected) + escalation_recommended_reserve)
        * 1.20,
        4,
    )
    maximum = round(
        (sum(item.maximum_cost_usd for item in selected) + escalation_maximum_reserve)
        * 1.25,
        4,
    )
    status = (
        ModelBudgetStatus.APPROVED
        if approved_usd >= minimum
        else ModelBudgetStatus.NEEDS_BUDGET
    )
    digest = _plan_hash(plan)
    decision = ModelBudgetDecision(
        project_id=plan.project_id,
        team_plan_sha256=digest,
        approved_usd=round(approved_usd, 6),
        minimum_cost_usd=minimum,
        recommended_cost_usd=recommended,
        maximum_cost_usd=maximum,
        status=status,
        selected_stages=selected,
        explanation=(
            "Selected models fit the approved minimum execution envelope; every call remains subject "
            "to the immutable hard budget breaker."
            if status == ModelBudgetStatus.APPROVED
            else "Selected models cannot complete even the minimum execution path within approval."
        ),
    )
    policy = ModelExecutionPolicy(
        project_id=plan.project_id,
        team_plan_sha256=digest,
        price_card_version=estimate.price_card_version,
        approved_usd=round(approved_usd, 6),
        stage_models=stage_models,
        stage_model_ladders=stage_model_ladders,
    )
    if status == ModelBudgetStatus.NEEDS_BUDGET:
        raise BudgetExceeded(
            f"selected team models need at least ${minimum:.4f}; approved ${approved_usd:.4f}"
        )
    return decision, policy


def _atomic_model(path: Path, value: BaseModel) -> None:
    content = json.dumps(
        value.model_dump(mode="json"), ensure_ascii=False, indent=2, sort_keys=True
    ) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_text(encoding="utf-8") != content:
            raise FileExistsError(f"refusing to overwrite model policy record: {path}")
        return
    temp = path.with_suffix(path.suffix + f".{uuid4().hex}.tmp")
    temp.write_text(content, encoding="utf-8")
    os.replace(temp, path)


def persist_model_approval(
    *,
    run_dir: Path,
    project_dir: Path,
    decision: ModelBudgetDecision,
    policy: ModelExecutionPolicy,
) -> None:
    _atomic_model(run_dir / "model_execution_policy.json", policy)
    _atomic_model(
        project_dir / "07_budget_and_usage" / "model_budget_decision.json", decision
    )


class ModelPolicyGateway:
    """Fail closed unless each call uses the exact model approved for its stage."""

    def __init__(self, gateway: object, policy: ModelExecutionPolicy):
        self.gateway = gateway
        self.policy = policy

    @property
    def client(self) -> Any:
        return getattr(self.gateway, "client")

    @property
    def store(self) -> Any:
        return getattr(self.gateway, "store")

    @property
    def supports_adk(self) -> bool:
        return callable(getattr(self.gateway, "generate_adk_response", None))

    def model_for(self, stage: str) -> str:
        return self.policy.model_for(stage)

    def select_model_after_failure(
        self,
        stage: str,
        *,
        failure_text: str,
        difficulty: str,
        attempt: int = 1,
    ) -> ModelSelectionDecision:
        decision = self.policy.select_after_failure(
            stage,
            failure_text=failure_text,
            difficulty=difficulty,
            attempt=attempt,
        )
        store = getattr(self.gateway, "store", None)
        run_dir = getattr(store, "run_dir", None)
        if isinstance(run_dir, Path):
            _atomic_model(
                run_dir
                / "model_selection_receipts"
                / f"{decision.decision_id}.json",
                decision,
            )
        return decision

    def generate_json(self, *, stage: str, model: str, **kwargs: Any) -> Any:
        self.policy.enforce(stage, model)
        return self.gateway.generate_json(stage=stage, model=model, **kwargs)

    def generate_json_with_images(
        self, *, stage: str, model: str, **kwargs: Any
    ) -> Any:
        """Keep multimodal observation behind the same stage/model allowlist."""
        self.policy.enforce(stage, model)
        operation = getattr(self.gateway, "generate_json_with_images", None)
        if not callable(operation):
            raise AttributeError("the approved gateway does not support multimodal observation")
        return operation(stage=stage, model=model, **kwargs)

    def generate_text(self, *, stage: str, model: str, **kwargs: Any) -> str:
        self.policy.enforce(stage, model)
        return self.gateway.generate_text(stage=stage, model=model, **kwargs)

    def generate_adk_response(self, *, stage: str, model: str, **kwargs: Any) -> Any:
        """Allow native ADK turns only through the same approved stage/model policy."""
        self.policy.enforce(stage, model)
        return self.gateway.generate_adk_response(stage=stage, model=model, **kwargs)
