"""Typed contracts shared by OneBrief agents."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, Field, model_validator


class SourcePriority(StrEnum):
    """How strongly a supplied source governs the work."""

    MANDATORY = "mandatory"
    OPTIONAL = "optional"


class InternalSource(BaseModel):
    """A private or authoritative source supplied by the user."""

    name: Annotated[str, Field(min_length=1, max_length=200)]
    priority: SourcePriority
    summary: Annotated[str, Field(max_length=2000)] = ""


class IntakeRequest(BaseModel):
    """The smallest user intake OneBrief needs to begin analysis."""

    goal: Annotated[str, Field(min_length=3, max_length=8000)]
    desired_output: Annotated[str | None, Field(max_length=2000)] = None
    internal_sources: list[InternalSource] = Field(default_factory=list)
    public_research_allowed: bool = False
    budget_limit_usd: Annotated[float | None, Field(gt=0)] = None


class InformationRequirement(BaseModel):
    """One missing fact or source that changes the result materially."""

    key: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")]
    request: Annotated[str, Field(min_length=3, max_length=500)]
    reason: Annotated[str, Field(min_length=3, max_length=500)]
    acceptable_evidence: list[Annotated[str, Field(min_length=1, max_length=200)]] = Field(
        min_length=1,
        max_length=5,
    )


class RequirementsAnalysis(BaseModel):
    """Strict output contract for the Requirements Analyst."""

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

