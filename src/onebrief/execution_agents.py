"""Four specialized agents whose every call uses the budgeted gateway."""

from __future__ import annotations

import json
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel

from onebrief.execution_limits import (
    ANALYST_OUTPUT_CAP,
    REVISION_OUTPUT_CAP,
    VERIFIER_OUTPUT_CAP,
    WRITER_OUTPUT_CAP,
)
from onebrief.execution_schemas import AnalysisPackage, DraftArtifact, RevisionArtifact, VerificationReport

T = TypeVar("T", bound=BaseModel)


class StructuredGateway(Protocol):
    def generate_json(
        self,
        *,
        stage: str,
        model: str,
        contents: str,
        schema: type[T],
        max_output_tokens: int,
        system_instruction: str,
        temperature: float = 0.1,
    ) -> T: ...


def _json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)


class AnalystAgent:
    stage = "evidence_analysis"

    def __init__(self, gateway: StructuredGateway):
        self.gateway = gateway

    def run(self, contract: dict[str, Any], sources: list[dict[str, Any]]) -> AnalysisPackage:
        return self.gateway.generate_json(
            stage=self.stage,
            model="gemini-3.5-flash",
            contents=_json({"work_contract": contract, "authoritative_sources": sources}),
            schema=AnalysisPackage,
            max_output_tokens=ANALYST_OUTPUT_CAP,
            system_instruction=(
                "You are OneBrief's evidence analyst: deep, conservative, autonomous, and explicit. "
                "Use only supplied authoritative sources. Extract decision-relevant findings, assign "
                "stable F01-style IDs, preserve conflicts, and design a structure for the requested "
                "deliverable. Do not draft the final artifact or invent missing facts. Write in the "
                "goal's language and return only the required structured object."
            ),
        )


class WriterAgent:
    stage = "long_form_draft"

    def __init__(self, gateway: StructuredGateway):
        self.gateway = gateway

    def run(self, contract: dict[str, Any], analysis: AnalysisPackage) -> DraftArtifact:
        return self.gateway.generate_json(
            stage=self.stage,
            model="gemini-3.5-flash",
            contents=_json({"work_contract": contract, "analysis_package": analysis.model_dump(mode="json")}),
            schema=DraftArtifact,
            max_output_tokens=WRITER_OUTPUT_CAP,
            system_instruction=(
                "You are OneBrief's long-form writer: deep, creative only inside the contract, "
                "autonomous, and focused. Draft the complete requested artifact from the analysis "
                "package. Cover every requested item, but keep wording concise enough for the output "
                "cap. Every material claim must be traceable to cited finding IDs. Never change the "
                "goal, invent a source, or make a high-impact decision for a human. Write in the goal's "
                "language and return only the required structured object."
            ),
        )


class VerifierAgent:
    stage = "independent_verification"

    def __init__(self, gateway: StructuredGateway):
        self.gateway = gateway

    def run(
        self,
        contract: dict[str, Any],
        analysis: AnalysisPackage,
        draft: DraftArtifact,
        round_number: int,
    ) -> VerificationReport:
        return self.gateway.generate_json(
            stage=f"{self.stage}_r{round_number}",
            model="gemini-3.5-flash",
            contents=_json(
                {
                    "work_contract": contract,
                    "analysis_package": analysis.model_dump(mode="json"),
                    "draft": draft.model_dump(mode="json"),
                }
            ),
            schema=VerificationReport,
            max_output_tokens=VERIFIER_OUTPUT_CAP,
            system_instruction=(
                "You are OneBrief's independent verifier: deep, conservative, reporting-oriented, "
                "and focused. You did not write the draft. Test every acceptance criterion, factual "
                "grounding, citation coverage, internal consistency, completeness, and human-authority "
                "boundary. PASS only when no blocking issue remains. Use REVISE for correctable issues "
                "and NEEDS_INFORMATION only when supplied evidence cannot support a required conclusion. "
                "Give exact revision instructions. Write every user-facing field in the goal's language. "
                "Return only the structured object."
            ),
        )


class RevisionAgent:
    stage = "revision"

    def __init__(self, gateway: StructuredGateway):
        self.gateway = gateway

    def run(
        self,
        contract: dict[str, Any],
        analysis: AnalysisPackage,
        draft: DraftArtifact,
        report: VerificationReport,
        round_number: int,
    ) -> RevisionArtifact:
        return self.gateway.generate_json(
            stage=f"{self.stage}_r{round_number}",
            model="gemini-3.5-flash",
            contents=_json(
                {
                    "work_contract": contract,
                    "analysis_package": analysis.model_dump(mode="json"),
                    "current_draft": draft.model_dump(mode="json"),
                    "verification_report": report.model_dump(mode="json"),
                }
            ),
            schema=RevisionArtifact,
            max_output_tokens=REVISION_OUTPUT_CAP,
            system_instruction=(
                "You are OneBrief's revision specialist: deep, conservative, autonomous, and focused. "
                "Apply every blocking revision instruction while preserving correct grounded content. "
                "Do not hide unresolved issues or broaden scope. Keep material claims linked to existing "
                "finding IDs. Write in the goal's language and return only the structured object."
            ),
        )

