"""Vertex-compatible structured handoffs for the execution team."""

from __future__ import annotations

from collections import Counter
from enum import StrEnum
import re

from pydantic import BaseModel, Field, field_validator, model_validator

from onebrief.handoff_protocol import (
    EvidenceBinding,
    canonical_sha256,
    normalize_criterion_id,
)
from onebrief.temperament import TemperamentDecision


class EvidenceFinding(BaseModel):
    finding_id: str
    source_name: str
    source_refs: list[str] = Field(default_factory=list, max_length=12)
    evidence: str
    implication: str


class AnalysisPackage(BaseModel):
    objective: str
    findings: list[EvidenceFinding]
    recommended_structure: list[str]
    constraints: list[str]
    risks: list[str]
    temperament_decisions: list[TemperamentDecision] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def assign_missing_or_duplicate_finding_ids(cls, value: object) -> object:
        """Treat provider-authored finding IDs as transport keys, not evidence."""

        if not isinstance(value, dict) or not isinstance(value.get("findings"), list):
            return value
        normalized = dict(value)
        findings = [dict(item) if isinstance(item, dict) else item for item in value["findings"]]
        raw_ids = [
            str(item.get("finding_id") or "") if isinstance(item, dict) else ""
            for item in findings
        ]
        counts = Counter(raw_ids)
        used = {
            finding_id for finding_id, count in counts.items()
            if finding_id.startswith("F") and count == 1
        }
        next_index = 1
        for item, finding_id in zip(findings, raw_ids, strict=True):
            if not isinstance(item, dict):
                continue
            if finding_id.startswith("F") and counts[finding_id] == 1:
                continue
            while (candidate := f"F{next_index:03d}") in used:
                next_index += 1
            item["finding_id"] = candidate
            used.add(candidate)
            next_index += 1
        normalized["findings"] = findings
        return normalized

    @model_validator(mode="after")
    def validate_findings(self) -> "AnalysisPackage":
        if not self.findings:
            raise ValueError("analysis requires at least one finding")
        ids = [item.finding_id for item in self.findings]
        if len(ids) != len(set(ids)) or any(not item.startswith("F") for item in ids):
            raise ValueError("finding IDs must be unique F-prefixed values")
        return self


class DraftArtifact(BaseModel):
    title: str
    body_markdown: str
    cited_finding_ids: list[str]
    drafting_decisions: list[str]
    temperament_decisions: list[TemperamentDecision] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_draft(self) -> "DraftArtifact":
        if len(self.body_markdown.strip()) < 50:
            raise ValueError("draft body is too short")
        if not self.cited_finding_ids:
            raise ValueError("draft must cite at least one finding")
        return self


class Verdict(StrEnum):
    PASS = "PASS"
    REVISE = "REVISE"
    NEEDS_INFORMATION = "NEEDS_INFORMATION"
    UNVERIFIABLE = "UNVERIFIABLE"


class CriterionCheck(BaseModel):
    criterion_id: str | None = Field(default=None, pattern=r"^Q[0-9]{2}$")
    criterion: str
    passed: bool
    evidence: str
    evidence_bindings: list[EvidenceBinding] = Field(default_factory=list, max_length=32)

    @field_validator("criterion_id", mode="before")
    @classmethod
    def discard_non_contract_criterion_id(cls, value: object) -> object:
        return normalize_criterion_id(value)

    @field_validator("evidence_bindings", mode="before")
    @classmethod
    def normalize_model_authored_evidence_bindings(cls, value: object) -> object:
        """Normalize verifier transport fields without granting provenance."""

        if not isinstance(value, list):
            return value
        normalized: list[object] = []
        for item in value:
            if not isinstance(item, dict):
                normalized.append(item)
                continue
            candidate = dict(item)
            artifact = candidate.get("artifact")
            if isinstance(artifact, dict) and not re.fullmatch(
                r"[a-f0-9]{64}", str(artifact.get("sha256") or "")
            ):
                candidate["artifact"] = None
            observation_id = str(candidate.get("observation_id") or "").strip()
            if not re.fullmatch(r"FO-[a-f0-9]{16}", observation_id):
                candidate["observation_id"] = None
            binding_id = str(candidate.get("binding_id") or "").strip()
            if not re.fullmatch(r"EB-[a-f0-9]{16}", binding_id):
                identity = {
                    key: item_value
                    for key, item_value in candidate.items()
                    if key != "binding_id"
                }
                candidate["binding_id"] = "EB-" + canonical_sha256(identity)[:16]
            normalized.append(candidate)
        return normalized

    @model_validator(mode="after")
    def bindings_match_criterion(self) -> "CriterionCheck":
        mismatched = [
            item.binding_id
            for item in self.evidence_bindings
            if item.criterion_id is not None and item.criterion_id != self.criterion_id
        ]
        if mismatched:
            raise ValueError(
                "criterion check contains evidence bound to a different criterion: "
                + ", ".join(mismatched)
            )
        return self


class VerificationReport(BaseModel):
    verdict: Verdict
    criterion_checks: list[CriterionCheck]
    blocking_issues: list[str]
    revision_instructions: list[str]
    missing_information: list[str]
    temperament_decisions: list[TemperamentDecision] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_verdict(self) -> "VerificationReport":
        if not self.criterion_checks:
            raise ValueError("verification requires criterion checks")
        if self.verdict == Verdict.PASS and (self.blocking_issues or self.missing_information):
            raise ValueError("PASS cannot retain blocking issues or missing information")
        if self.verdict == Verdict.REVISE and not self.revision_instructions:
            raise ValueError("REVISE requires instructions")
        if self.verdict == Verdict.UNVERIFIABLE and not self.blocking_issues:
            raise ValueError("UNVERIFIABLE requires a blocking issue")
        return self


class RevisionArtifact(BaseModel):
    title: str
    revised_body_markdown: str
    addressed_issues: list[str]
    cited_finding_ids: list[str]
    temperament_decisions: list[TemperamentDecision] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_revision(self) -> "RevisionArtifact":
        if len(self.revised_body_markdown.strip()) < 50:
            raise ValueError("revised body is too short")
        if not self.addressed_issues or not self.cited_finding_ids:
            raise ValueError("revision must address issues and retain citations")
        return self


class PipelineStatus(StrEnum):
    RUNNING = "running"
    COMPLETE = "complete"
    PARTIAL = "partial"
    NEEDS_INFORMATION = "needs_information"
    NEEDS_BUDGET = "needs_budget"
    NEEDS_AUTHORIZATION = "needs_authorization"
    FAILED = "failed"


class ExecutionCheckpoint(BaseModel):
    status: PipelineStatus
    current_stage: str
    completed_stages: list[str]
    revision_round: int = Field(ge=0, le=8)
    final_verdict: Verdict | None = None
    message: str = ""
