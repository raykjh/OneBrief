"""Typed contracts shared by OneBrief agents and deterministic gates."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, Field, model_validator


class SourcePriority(StrEnum):
    MANDATORY = "mandatory"
    OPTIONAL = "optional"


class ToolPackId(StrEnum):
    EXCHANGE = "exchange"
    EXCHANGE_DEVELOPMENT = "exchange_development"
    PROJECT_DEVELOPMENT = "project_development"


class OutputTarget(StrEnum):
    """The native form the user expects to receive and use."""

    AUTO = "auto"
    EXISTING_PROJECT = "existing_project"
    WEB_APP = "web_app"
    UNITY_APP = "unity_app"
    SPREADSHEET = "spreadsheet"
    DOCUMENT = "document"
    TEXT_FILE = "text_file"


class EvaluationMode(StrEnum):
    """How a completion criterion can be proven."""

    DETERMINISTIC = "deterministic"
    INDEPENDENT_REVIEW = "independent_review"


class QualityCriterion(BaseModel):
    criterion_id: Annotated[str, Field(pattern=r"^Q[0-9]{2}$")]
    description: Annotated[str, Field(min_length=3, max_length=300)]
    evaluation_mode: EvaluationMode = EvaluationMode.INDEPENDENT_REVIEW
    evidence_required: Annotated[str, Field(min_length=3, max_length=300)]
    required: bool = True


class CompletionContract(BaseModel):
    """Observable definition of done, independent of any example or agent roster."""

    target_state: Annotated[str, Field(min_length=3, max_length=1000)]
    quality_criteria: list[QualityCriterion] = Field(min_length=1, max_length=12)
    pass_condition: Annotated[str, Field(min_length=3, max_length=300)] = (
        "All required criteria pass with the specified evidence."
    )


class SixSenseOption(BaseModel):
    """One fast, user-facing decision with a disclosed working default."""

    option_id: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,31}$")]
    label: Annotated[str, Field(min_length=1, max_length=120)]
    decision: Annotated[str, Field(min_length=1, max_length=300)]
    recommended: bool = False


class SixSenseQuestion(BaseModel):
    """A material decision shown as one tap in the rapid SixSense sequence."""

    question_id: Annotated[str, Field(pattern=r"^S0[2-6]$")]
    dimension: Annotated[str, Field(min_length=1, max_length=80)]
    prompt: Annotated[str, Field(min_length=3, max_length=300)]
    reason: Annotated[str, Field(min_length=3, max_length=300)]
    options: list[SixSenseOption] = Field(min_length=2, max_length=4)
    allow_custom: bool = True

    @model_validator(mode="after")
    def one_recommended_default(self) -> "SixSenseQuestion":
        if sum(option.recommended for option in self.options) != 1:
            raise ValueError("SixSense questions require exactly one recommended option")
        if len({option.option_id for option in self.options}) != len(self.options):
            raise ValueError("SixSense option IDs must be unique inside a question")
        return self


class SixSensePlan(BaseModel):
    """One model pass, then an instant client-side sequence of at most five choices."""

    standard_profile: Annotated[str, Field(min_length=3, max_length=800)]
    questions: list[SixSenseQuestion] = Field(default_factory=list, max_length=5)
    interaction_target_seconds: Annotated[int, Field(ge=10, le=90)] = 30


class InternalSource(BaseModel):
    """Private or authoritative evidence supplied by the user."""

    name: Annotated[str, Field(min_length=1, max_length=200)]
    priority: SourcePriority
    requirement_keys: list[Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")]] = Field(
        default_factory=list,
        max_length=10,
    )
    summary: Annotated[str, Field(max_length=2000)] = ""
    content: Annotated[str, Field(max_length=1_000_000)] = ""
    media_type: Annotated[str, Field(max_length=100)] = "text/plain"
    size_bytes: Annotated[int, Field(ge=0)] = 0
    sha256: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")] | None = None


class IntakeRequest(BaseModel):
    goal: Annotated[str, Field(min_length=3, max_length=8000)]
    output_target: OutputTarget = OutputTarget.AUTO
    existing_project_id: Annotated[
        str | None, Field(pattern=r"^[a-z][a-z0-9_-]{1,63}$")
    ] = None
    desired_output: Annotated[str | None, Field(max_length=2000)] = None
    internal_sources: list[InternalSource] = Field(default_factory=list, max_length=50)
    public_research_allowed: bool = False
    budget_limit_usd: Annotated[float | None, Field(gt=0)] = None
    max_revision_rounds: Annotated[int, Field(ge=0, le=6)] = 6
    toolpack_ids: list[ToolPackId] = Field(default_factory=list, max_length=5)

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
    completion_contract: CompletionContract | None = None
    sixsense: SixSensePlan | None = None
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
        if self.completion_contract is None:
            self.completion_contract = CompletionContract(
                target_state=(
                    f"{self.normalized_goal} The listed deliverables are usable in their requested form."
                ),
                quality_criteria=[
                    QualityCriterion(
                        criterion_id=f"Q{index:02d}",
                        description=criterion,
                        evidence_required="Independent evidence showing the criterion is satisfied.",
                    )
                    for index, criterion in enumerate(self.acceptance_criteria, start=1)
                ],
            )
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
    fixed_cost_usd_per_call: float = 0.0


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
