"""APT-3 discretion profiles and auditable tie-breaker decisions."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TypeVar

from pydantic import BaseModel, model_validator


class Pace(StrEnum):
    RAPID = "R"
    THOROUGH = "T"


class Orientation(StrEnum):
    NOVEL = "N"
    FAITHFUL = "F"


class Scope(StrEnum):
    GLOBAL = "G"
    LOCAL = "L"


class TemperamentAxis(StrEnum):
    PACE = "pace"
    ORIENTATION = "orientation"
    SCOPE = "scope"


class TemperamentDecision(BaseModel):
    agent: str
    agent_type: str
    options: list[str]
    selected: str
    deciding_axis: TemperamentAxis
    reason: str

    @model_validator(mode="after")
    def validate_real_tie_breaker(self) -> "TemperamentDecision":
        if len(self.options) < 2 or len(self.options) != len(set(self.options)):
            raise ValueError("temperament decision requires at least two distinct options")
        if self.selected not in self.options:
            raise ValueError("selected option must be one of the audited options")
        if not self.reason.strip():
            raise ValueError("temperament decision requires a reason")
        return self


@dataclass(frozen=True)
class Apt3Profile:
    agent: str
    pace: Pace
    orientation: Orientation
    scope: Scope

    @property
    def code(self) -> str:
        return f"{self.pace.value}{self.orientation.value}{self.scope.value}"

    def instruction(self) -> str:
        pace = {
            Pace.RAPID: "prefer the sufficient faster path",
            Pace.THOROUGH: "prefer additional justified examination",
        }[self.pace]
        orientation = {
            Orientation.NOVEL: "prefer an original expression inside the approved contract",
            Orientation.FAITHFUL: "prefer fidelity to authoritative sources and approved handoffs",
        }[self.orientation]
        scope = {
            Scope.GLOBAL: "prefer whole-deliverable coherence",
            Scope.LOCAL: "prefer precision in the immediate section or issue",
        }[self.scope]
        return (
            f"APT-3 discretion profile: {self.code}. This is not authority and has no intensity. "
            "Apply priorities in this order: (1) explicit user and work-contract instructions; "
            "(2) authoritative evidence, budget, safety, law, and role boundaries; "
            "(3) accepted prior handoffs; (4) only when two or more choices remain equally valid, "
            f"use the profile as a tie-breaker: {pace}; {orientation}; {scope}. "
            "Never force a temperament choice. If and only if APT-3 actually breaks a tie, append "
            f"one temperament_decisions record with agent='{self.agent}', agent_type='{self.code}', "
            "the real options, selected option, deciding axis, and reason. Otherwise return an empty "
            "temperament_decisions list."
        )


ANALYST_PROFILE = Apt3Profile(
    agent="analyst",
    pace=Pace.THOROUGH,
    orientation=Orientation.FAITHFUL,
    scope=Scope.GLOBAL,
)

WRITER_PROFILE = Apt3Profile(
    agent="writer",
    pace=Pace.THOROUGH,
    orientation=Orientation.NOVEL,
    scope=Scope.LOCAL,
)

VERIFIER_PROFILE = Apt3Profile(
    agent="verifier",
    pace=Pace.THOROUGH,
    orientation=Orientation.FAITHFUL,
    scope=Scope.LOCAL,
)

REVISION_PROFILE = Apt3Profile(
    agent="reviser",
    pace=Pace.THOROUGH,
    orientation=Orientation.FAITHFUL,
    scope=Scope.LOCAL,
)

EXECUTION_PROFILES = {
    profile.agent: profile
    for profile in (ANALYST_PROFILE, WRITER_PROFILE, VERIFIER_PROFILE, REVISION_PROFILE)
}


T = TypeVar("T", bound=BaseModel)


def enforce_temperament_audit(output: T, profile: Apt3Profile) -> T:
    decisions = getattr(output, "temperament_decisions", None)
    if decisions is None:
        raise ValueError("agent output is missing temperament_decisions")
    for decision in decisions:
        if decision.agent != profile.agent:
            raise ValueError(
                f"temperament decision agent {decision.agent!r} does not match {profile.agent!r}"
            )
        if decision.agent_type != profile.code:
            raise ValueError(
                f"temperament decision type {decision.agent_type!r} does not match {profile.code!r}"
            )
    return output
