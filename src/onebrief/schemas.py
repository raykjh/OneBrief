"""Typed contracts shared by OneBrief agents and deterministic gates."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, Field, model_validator


class SourcePriority(StrEnum):
    MANDATORY = "mandatory"
    OPTIONAL = "optional"


class InternalSource(BaseModel):
    """Private or authoritative evidence supplied by the user."""

    name: Annotated[str, Field(min_length=1, max_length=200)]
    priority: SourcePriority
    requirement_keys: list[Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")]] = Field(
        default_factory=list,
        max_length=10,
    )
    summary: Annotated[str, Field(max_length=2000)] = ""
    content: Annotated[str, Field(max_length=500_000)] = ""
    media_type: Annotated[str, Field(max_length=100)] = "text/plain"
    size_bytes: Annotated[int, Field(ge=0)] = 0
    sha256: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")] | None = None


class IntakeRequest(BaseModel):
    goal: Annotated[str, Field(min_length=3, max_length=8000)]
    desired_output: Annotated[str | None, Field(max_length=2000)] = None
    internal_sources: list[InternalSource] = Field(default_factory=list, max_length=50)
    public_research_allowed: bool = False
    budget_limit_usd: Annotated[float | None, Field(gt=0)] = None
    max_revision_rounds: Annotated[int, Field(ge=0, le=2)] = 2


class InformationRequirement(BaseModel):
    key: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")]
    request: Annotated[str, Field(min_length=3, max_length=500)]
    reason: Annotated[str, Field(min_length=3, max_length=500)]
    acceptable_evidence: list[Annotated[str, Field(min_length=1, max_length=200)]] = Field(
        min_length=1,
        max_length=5,
    )


class RequirementsAnalysis(BaseModel):
    supported: bool
    support_reason: Annotated[str, Field(min_length=3, max_length=500)]
    normalized_goal: Annotated[str, Field(min_length=3, max_length=1000)]
    deliverables: list[Annotated[str, Field(min_length=1, max_length=300)]] = Field(
        min_length=1,
        max_length=10,
    )
    mandatory_information: list[InformationRequirement] = Field(max_length=10)
    optional_information: list[InformationRequirement] = Field(max_length=10)
    acceptance_criteria: list[Annotated[str, Field(min_length=3, max_length=300)]] = Field(
        min_length=1,
        max_length=12,
    )
    assumptions: list[Annotated[str, Field(min_length=1, max_length=300)]] = Field(max_length=10)
    consolidated_questions: list[Annotated[str, Field(min_length=3, max_length=500)]] = Field(
        max_length=10
    )
    ready_for_estimate: bool

    @model_validator(mode="after")
    def enforce_readiness(self) -> "RequirementsAnalysis":
        if not self.supported and self.ready_for_estimate:
            raise ValueError("unsupported work cannot be ready for estimation")
        if self.mandatory_information and self.ready_for_estimate:
            raise ValueError("missing mandatory information blocks estimation")
        if self.mandatory_information and not self.consolidated_questions:
            raise ValueError("mandatory gaps must be surfaced in one question set")
        return self


class UploadEntry(BaseModel):
    path: Annotated[str, Field(min_length=1, max_length=1000)]
    requirement_keys: list[Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")]] = Field(
        min_length=1,
        max_length=10,
    )
    priority: SourcePriority = SourcePriority.MANDATORY
    summary: Annotated[str, Field(max_length=2000)] = ""


class UploadManifest(BaseModel):
    sources: list[UploadEntry] = Field(min_length=1, max_length=50)


class SourceRecord(BaseModel):
    name: str
    requirement_keys: list[str]
    priority: SourcePriority
    media_type: str
    size_bytes: int
    sha256: str


class BudgetStatus(StrEnum):
    AWAITING_APPROVAL = "awaiting_approval"
    WITHIN_BUDGET = "within_budget"
    LIMITED_BUDGET = "limited_budget"
    NEEDS_BUDGET = "needs_budget"


class StageEstimate(BaseModel):
    stage: str
    model: str
    input_tokens_per_call: int
    output_tokens_per_call: int
    minimum_calls: int
    recommended_calls: int
    maximum_calls: int
    minimum_cost_usd: float
    recommended_cost_usd: float
    maximum_cost_usd: float
    estimated_minutes_per_call: int


class BudgetEnvelope(BaseModel):
    price_card_version: str
    price_source_url: str
    endpoint: str
    estimated_source_tokens: int
    estimated_contract_tokens: int
    stages: list[StageEstimate]
    minimum_cost_usd: float
    recommended_cost_usd: float
    maximum_cost_usd: float
    recommended_approval_usd: float
    budget_limit_usd: float | None
    status: BudgetStatus
    estimated_minutes_minimum: int
    estimated_minutes_recommended: int
    estimated_minutes_maximum: int
    notes: list[str]

