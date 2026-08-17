"""Bounded preflight and repair authority for technical execution failures.

Technical repair cannot reinterpret the approved product. It may repair a local
compiler/runtime defect once, or a reviewed KHALINOS rule/compiler when variants
show that case patches are no longer converging.
"""

from __future__ import annotations

import hashlib
import json
import re
from enum import StrEnum
from pathlib import PurePosixPath

from pydantic import BaseModel, Field, model_validator

from onebrief.convergence_policy import (
    FailureLayer,
    FailureObservation,
    RepairContract,
)


class RepairProposalKind(StrEnum):
    CASE_PATCH = "case_patch"
    SYSTEMIC_RULE = "systemic_rule"


class TechnicalPreflightDecision(StrEnum):
    LOCAL_TECHNICAL_REPAIR = "local_technical_repair"
    SYSTEMIC_TECHNICAL_REPAIR = "systemic_technical_repair"
    GENERALIZATION_REVIEW = "generalization_review"
    STRUCTURAL_STOP = "structural_stop"
    NOT_TECHNICAL = "not_technical"


class TechnicalRepairKind(StrEnum):
    LOCAL = "local"
    SYSTEMIC = "systemic"


class TechnicalPreflightRequest(BaseModel):
    schema_version: str = "khalinos-technical-preflight-request-v1"
    quest_id: str = Field(min_length=3, max_length=120)
    source_revision: str = Field(min_length=7, max_length=128)
    observation: FailureObservation
    convergence_contract: RepairContract
    proposal_kind: RepairProposalKind
    proposed_paths: list[str] = Field(min_length=1, max_length=64)
    authorized_project_prefixes: list[str] = Field(default_factory=list, max_length=32)
    trusted_system_prefixes: list[str] = Field(default_factory=list, max_length=32)
    forbidden_prefixes: list[str] = Field(default_factory=list, max_length=32)
    modifies_acceptance_criteria: bool = False
    expands_authority: bool = False
    weakens_verification: bool = False

    @model_validator(mode="after")
    def binds_exact_failure(self) -> "TechnicalPreflightRequest":
        if self.convergence_contract.observation_id != self.observation.observation_id:
            raise ValueError("technical preflight is bound to another failure observation")
        if (
            self.convergence_contract.structural_cause_id
            and self.observation.structural_cause_id
            and self.convergence_contract.structural_cause_id
            != self.observation.structural_cause_id
        ):
            raise ValueError("technical preflight structural cause digest changed")
        return self


class TechnicalPreflightReport(BaseModel):
    schema_version: str = "khalinos-technical-preflight-report-v1"
    decision: TechnicalPreflightDecision
    structural_cause_id: str = Field(pattern=r"^SC-[a-f0-9]{16}$")
    variant_count: int = Field(ge=1)
    normalized_paths: list[str] = Field(min_length=1, max_length=64)
    execution_allowed: bool
    rationale: str = Field(min_length=3, max_length=1000)
    request_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class TechnicalRepairContract(BaseModel):
    schema_version: str = "khalinos-technical-repair-contract-v1"
    repair_id: str = Field(pattern=r"^TR-[a-f0-9]{16}$")
    quest_id: str
    source_revision: str
    structural_cause_id: str = Field(pattern=r"^SC-[a-f0-9]{16}$")
    observation_id: str = Field(pattern=r"^FO-[a-f0-9]{16}$")
    repair_kind: TechnicalRepairKind
    authorized_paths: list[str] = Field(min_length=1, max_length=64)
    forbidden_changes: list[str] = Field(min_length=1, max_length=16)
    required_verification: list[str] = Field(min_length=1, max_length=16)
    preflight_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


def _sha256(value: object) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json", exclude_none=True)
    return hashlib.sha256(json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


def _path(value: str) -> str:
    candidate = value.replace("\\", "/").strip()
    relative = PurePosixPath(candidate)
    if (
        not candidate
        or candidate.startswith("/")
        or re.match(r"^[A-Za-z]:", candidate)
        or any(part == ".." for part in relative.parts)
    ):
        raise ValueError("technical repair path escapes the approved workspace")
    normalized = relative.as_posix()
    if normalized in {"", "."}:
        raise ValueError("technical repair path escapes the approved workspace")
    return normalized


def _within(path: str, prefixes: list[str]) -> bool:
    normalized = _path(path).casefold()
    return any(
        normalized == _path(prefix).casefold().rstrip("/")
        or normalized.startswith(_path(prefix).casefold().rstrip("/") + "/")
        for prefix in prefixes
    )


def _technical(observation: FailureObservation) -> bool:
    if observation.layer in {
        FailureLayer.BUILD,
        FailureLayer.RUNTIME,
        FailureLayer.SOURCE_BINDING,
        FailureLayer.EVIDENCE_RUNTIME,
        FailureLayer.EVIDENCE_TOPOLOGY,
    }:
        return True
    signature = observation.normalized_signature.casefold()
    return observation.layer == FailureLayer.SEMANTIC_PRODUCT and any(
        marker in signature
        for marker in (
            "production scene or prefab",
            "missing scene/prefab path",
            "maker-authored topology mapping",
            "not reachable from any committed .unity/.prefab",
        )
    )


def evaluate_technical_preflight(
    request: TechnicalPreflightRequest,
) -> TechnicalPreflightReport:
    paths = list(dict.fromkeys(_path(path) for path in request.proposed_paths))
    cause_id = (
        request.observation.structural_cause_id
        or request.convergence_contract.structural_cause_id
    )
    if cause_id is None:
        raise ValueError("technical preflight requires a structural cause identity")
    variant_count = request.convergence_contract.variant_count
    request_sha = _sha256(request)

    def report(
        decision: TechnicalPreflightDecision,
        allowed: bool,
        rationale: str,
    ) -> TechnicalPreflightReport:
        return TechnicalPreflightReport(
            decision=decision,
            structural_cause_id=cause_id,
            variant_count=variant_count,
            normalized_paths=paths,
            execution_allowed=allowed,
            rationale=rationale,
            request_sha256=request_sha,
        )

    if not _technical(request.observation):
        return report(
            TechnicalPreflightDecision.NOT_TECHNICAL,
            False,
            "The failure changes product meaning or quality and remains owned by the accountable maker.",
        )
    if request.modifies_acceptance_criteria or request.expands_authority or request.weakens_verification:
        return report(
            TechnicalPreflightDecision.STRUCTURAL_STOP,
            False,
            "Technical repair cannot change acceptance criteria, expand authority, or weaken verification.",
        )
    if any(_within(path, request.forbidden_prefixes) for path in paths):
        return report(
            TechnicalPreflightDecision.STRUCTURAL_STOP,
            False,
            "The proposed repair touches a forbidden path prefix.",
        )

    systemic = request.proposal_kind == RepairProposalKind.SYSTEMIC_RULE
    inside_system = bool(request.trusted_system_prefixes) and all(
        _within(path, request.trusted_system_prefixes) for path in paths
    )
    inside_project = bool(request.authorized_project_prefixes) and all(
        _within(path, request.authorized_project_prefixes) for path in paths
    )
    if variant_count >= 2:
        if systemic and inside_system:
            return report(
                TechnicalPreflightDecision.SYSTEMIC_TECHNICAL_REPAIR,
                True,
                "The repeated structural cause is repaired in an approved KHALINOS rule or trusted compiler boundary.",
            )
        if variant_count >= 3:
            return report(
                TechnicalPreflightDecision.STRUCTURAL_STOP,
                False,
                "A third structural variant forbids another case-specific project repair.",
            )
        return report(
            TechnicalPreflightDecision.GENERALIZATION_REVIEW,
            False,
            "The second structural variant requires a general rule, schema, or trusted compiler proposal before execution.",
        )
    if systemic and inside_system:
        return report(
            TechnicalPreflightDecision.SYSTEMIC_TECHNICAL_REPAIR,
            True,
            "The first observation exposes a trusted-system defect with an explicitly bounded systemic repair.",
        )
    if not systemic and inside_project and request.convergence_contract.execution_allowed:
        return report(
            TechnicalPreflightDecision.LOCAL_TECHNICAL_REPAIR,
            True,
            "One bounded local technical repair is authorized by the first trusted observation.",
        )
    return report(
        TechnicalPreflightDecision.STRUCTURAL_STOP,
        False,
        "The proposed paths do not match either the approved project scope or the trusted systemic scope.",
    )


def issue_technical_repair_contract(
    request: TechnicalPreflightRequest,
    preflight: TechnicalPreflightReport,
) -> TechnicalRepairContract:
    if preflight.request_sha256 != _sha256(request):
        raise PermissionError("technical preflight report is bound to another request")
    kinds = {
        TechnicalPreflightDecision.LOCAL_TECHNICAL_REPAIR: TechnicalRepairKind.LOCAL,
        TechnicalPreflightDecision.SYSTEMIC_TECHNICAL_REPAIR: TechnicalRepairKind.SYSTEMIC,
    }
    if not preflight.execution_allowed or preflight.decision not in kinds:
        raise PermissionError("technical preflight does not authorize a repair")
    payload = {
        "quest_id": request.quest_id,
        "source_revision": request.source_revision,
        "cause": preflight.structural_cause_id,
        "observation": request.observation.observation_id,
        "kind": kinds[preflight.decision].value,
        "paths": preflight.normalized_paths,
        "preflight": _sha256(preflight),
    }
    return TechnicalRepairContract(
        repair_id="TR-" + _sha256(payload)[:16],
        quest_id=request.quest_id,
        source_revision=request.source_revision,
        structural_cause_id=preflight.structural_cause_id,
        observation_id=request.observation.observation_id,
        repair_kind=kinds[preflight.decision],
        authorized_paths=preflight.normalized_paths,
        forbidden_changes=[
            "approved outcome or product semantics",
            "acceptance criteria or evidence truth floor",
            "authority, budget, tools, or external actions",
            "previously passing product behavior",
        ],
        required_verification=[
            "targeted deterministic reproduction passes",
            "relevant regression tests pass",
            "independent verifier re-evaluates the unchanged criterion",
        ],
        preflight_sha256=_sha256(preflight),
    )
