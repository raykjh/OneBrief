"""Stage-one completion, capability, permission, and budget authorization plan."""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, Field

from onebrief.schemas import BudgetEnvelope, IntakeRequest, RequirementsAnalysis
from onebrief.toolpack_lifecycle import ToolPackLifecycleState


class CapabilityPermissionManifest(BaseModel):
    project_id: str | None = None
    toolpack_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    capabilities: list[str] = Field(default_factory=list)
    allowed_read_prefixes: list[str] = Field(default_factory=list)
    allowed_write_prefixes: list[str] = Field(default_factory=list)
    validation_adapters: list[str] = Field(default_factory=list)
    blocked_boundaries: list[str] = Field(default_factory=list)
    approval_required: bool = False


class PreparationPlan(BaseModel):
    """Everything the user approves before autonomous work begins."""

    schema_version: Literal["onebrief-preparation-v1"] = "onebrief-preparation-v1"
    stage: Literal["definition_and_authorization"] = "definition_and_authorization"
    canonical_goal: str = Field(min_length=3, max_length=8000)
    output_target: str
    amendment_reason: str | None = Field(default=None, max_length=4000)
    completion_contract: dict[str, object]
    permission_manifest: CapabilityPermissionManifest
    minimum_cost_usd: float = Field(ge=0)
    recommended_cost_usd: float = Field(ge=0)
    maximum_cost_usd: float = Field(ge=0)
    ready_for_authorization: bool
    blockers: list[str] = Field(default_factory=list)
    authorization_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


def _manifest(intake: IntakeRequest, state: ToolPackLifecycleState | None) -> CapabilityPermissionManifest:
    if state is None or state.generated is None:
        capabilities = [
            "use approved Gemini models through the budget gateway",
            "write only the requested result package",
        ]
        if intake.public_research_allowed:
            capabilities.append("perform bounded Google Search research and preserve source URLs")
        return CapabilityPermissionManifest(
            capabilities=capabilities,
            blocked_boundaries=[
                "credentials, accounts, payments, and personal data",
                "unapproved external side effects",
                "budget use beyond the approved amount",
            ],
        )
    generated = state.generated
    return CapabilityPermissionManifest(
        project_id=state.project_id,
        toolpack_sha256=generated.sha256,
        capabilities=list(generated.capabilities),
        allowed_read_prefixes=list(generated.allowed_read_prefixes),
        allowed_write_prefixes=list(generated.allowed_write_prefixes),
        validation_adapters=[
            item.label for item in generated.adapters if item.enabled
        ],
        blocked_boundaries=list(generated.blocked_boundaries),
        approval_required=state.status != "approved",
    )


def build_preparation_plan(
    intake: IntakeRequest,
    requirements: RequirementsAnalysis,
    budget: BudgetEnvelope | None,
    toolpack_state: ToolPackLifecycleState | None = None,
    amendment_reason: str | None = None,
) -> PreparationPlan | None:
    if not requirements.ready_for_estimate or budget is None:
        return None
    manifest = _manifest(intake, toolpack_state)
    blockers: list[str] = []
    if toolpack_state is not None:
        if toolpack_state.status not in {"validated", "approved"}:
            blockers.append("The generated ToolPack has not passed qualification.")
        blockers.extend(
            item for item in toolpack_state.execution_blockers
            if "approval" not in item.casefold()
        )
    contract = requirements.completion_contract.model_dump(mode="json")
    approval_payload = {
        "canonical_goal": intake.goal,
        "output_target": intake.output_target.value,
        "amendment_reason": amendment_reason,
        "completion_contract": contract,
        "permission_manifest": manifest.model_dump(mode="json"),
        "budget": {
            "minimum_cost_usd": budget.minimum_cost_usd,
            "recommended_cost_usd": budget.recommended_cost_usd,
            "maximum_cost_usd": budget.maximum_cost_usd,
        },
    }
    digest = hashlib.sha256(json.dumps(
        approval_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")).hexdigest()
    return PreparationPlan(
        canonical_goal=intake.goal,
        output_target=intake.output_target.value,
        amendment_reason=amendment_reason,
        completion_contract=contract,
        permission_manifest=manifest,
        minimum_cost_usd=budget.minimum_cost_usd,
        recommended_cost_usd=budget.recommended_cost_usd,
        maximum_cost_usd=budget.maximum_cost_usd,
        ready_for_authorization=not blockers,
        blockers=list(dict.fromkeys(blockers)),
        authorization_sha256=digest,
    )
