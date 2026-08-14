"""Structured agents for optional DAG roles and the project-owner approval boundary."""

from __future__ import annotations

import json
from typing import Protocol

from pydantic import BaseModel, Field

from onebrief.execution_schemas import Verdict


class StructuredGateway(Protocol):
    def generate_json(self, **kwargs: object) -> BaseModel: ...


class RoleHandoff(BaseModel):
    summary: str
    directives: list[str]
    constraints: list[str]
    risks: list[str]
    evidence_references: list[str] = Field(default_factory=list)


class GovernanceDecision(BaseModel):
    verdict: Verdict
    rationale: str
    blocking_issues: list[str] = Field(default_factory=list)
    required_actions: list[str] = Field(default_factory=list)


class DynamicRoleAgent:
    def __init__(self, gateway: StructuredGateway, stage: str, model: str):
        self.gateway = gateway
        self.stage = stage
        self.model = model

    def run(self, payload: dict[str, object]) -> RoleHandoff:
        result = self.gateway.generate_json(
            stage=self.stage,
            model=self.model,
            contents=json.dumps(payload, ensure_ascii=False),
            schema=RoleHandoff,
            max_output_tokens=1800,
            temperature=0.15,
            system_instruction=(
                f"You own the {self.stage} node in an execution DAG. Produce a concise, actionable "
                "handoff for downstream agents. Stay within the supplied project contract and evidence. "
                "Do not create unsupported facts, approve your own work, or perform another role's duty."
            ),
        )
        if not isinstance(result, RoleHandoff):
            raise TypeError(f"{self.stage} returned an invalid handoff")
        return result


class GovernanceAgent:
    def __init__(
        self,
        gateway: StructuredGateway,
        stage: str,
        model: str,
        *,
        max_output_tokens: int = 1200,
    ):
        self.gateway = gateway
        self.stage = stage
        self.model = model
        self.max_output_tokens = max_output_tokens

    def run(self, payload: dict[str, object]) -> GovernanceDecision:
        result = self.gateway.generate_json(
            stage=self.stage,
            model=self.model,
            contents=json.dumps(payload, ensure_ascii=False),
            schema=GovernanceDecision,
            max_output_tokens=self.max_output_tokens,
            temperature=0.0,
            system_instruction=(
                f"You own the {self.stage} governance gate. Return PASS only when the supplied evidence, "
                "verification, constraints, and acceptance criteria support release. Use REVISE for a "
                "correctable defect and NEEDS_INFORMATION when the user must supply missing authority or "
                "facts. Never silently repair a blocked result."
            ),
        )
        if not isinstance(result, GovernanceDecision):
            raise TypeError(f"{self.stage} returned an invalid governance decision")
        return result
