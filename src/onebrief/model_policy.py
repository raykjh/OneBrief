"""Deterministic model pricing, approval, persistence, and runtime enforcement."""

from __future__ import annotations

import hashlib
import json
import os
import re
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


class ModelExecutionPolicy(BaseModel):
    schema_version: str = "onebrief-model-execution-policy-v1"
    project_id: str
    team_plan_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    price_card_version: str
    approved_usd: float = Field(gt=0)
    stage_models: dict[str, ApprovedModel]

    def model_for(self, stage: str) -> str:
        base_stage = stage
        retry_suffix = re.compile(
            r"(?:_revision_r\d+|_repair_r\d+|_refinement_r\d+|_r\d+|"
            r"_compact_retry|_verification_retry)$"
        )
        while retry_suffix.search(base_stage):
            base_stage = retry_suffix.sub("", base_stage)
        selected = self.stage_models.get(base_stage)
        if selected is None:
            raise PermissionError(f"stage has no approved model binding: {stage}")
        return selected.value

    def enforce(self, stage: str, model: str) -> None:
        approved = self.model_for(stage)
        if model != approved:
            raise PermissionError(
                f"model blocked before provider call: {stage} requires {approved}, received {model}"
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

    selected: list[StageEstimate] = []
    for stage in estimate.stages:
        chosen = stage_models.get(stage.stage)
        if chosen is None:
            if stage.minimum_calls == 0:
                continue
            raise ValueError(f"budget stage has no TeamPlan model owner: {stage.stage}")
        selected.append(_repriced_stage(stage, chosen.value))

    minimum = round(sum(item.minimum_cost_usd for item in selected) * 1.10, 4)
    recommended = round(sum(item.recommended_cost_usd for item in selected) * 1.20, 4)
    maximum = round(sum(item.maximum_cost_usd for item in selected) * 1.25, 4)
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

    def generate_json(self, *, stage: str, model: str, **kwargs: Any) -> Any:
        self.policy.enforce(stage, model)
        return self.gateway.generate_json(stage=stage, model=model, **kwargs)

    def generate_text(self, *, stage: str, model: str, **kwargs: Any) -> str:
        self.policy.enforce(stage, model)
        return self.gateway.generate_text(stage=stage, model=model, **kwargs)

    def generate_adk_response(self, *, stage: str, model: str, **kwargs: Any) -> Any:
        """Allow native ADK turns only through the same approved stage/model policy."""
        self.policy.enforce(stage, model)
        return self.gateway.generate_adk_response(stage=stage, model=model, **kwargs)
