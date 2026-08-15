"""Trusted assurance policy derived from the SixSense intended-use decision."""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum

from pydantic import BaseModel

from onebrief.schemas import AssuranceSelection, AssuranceUse, RequirementsAnalysis


class ClaimMode(StrEnum):
    HYPOTHESIS = "hypothesis"
    BOUNDED_INFERENCE = "bounded_inference"
    SOURCE_BACKED = "source_backed"
    VALIDATED_SUBMISSION = "validated_submission"
    DETERMINISTIC_RUNTIME = "deterministic_runtime"


class AssurancePolicy(BaseModel):
    intended_use: AssuranceUse
    claim_mode: ClaimMode
    allow_labeled_estimates: bool
    allow_bounded_inference: bool
    require_direct_evidence_for_health_claims: bool = True
    require_deterministic_artifact_verification: bool
    immutable_truth_floor: tuple[str, ...] = (
        "No invented source, test, certification, quote, or observed result.",
        "Do not present possibility or inference as an established fact.",
        "Do not exceed approved authority, budget, or scope.",
        "Health prevention or treatment claims require direct supporting evidence.",
    )

    @property
    def sha256(self) -> str:
        payload = json.dumps(
            self.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


_POLICIES: dict[AssuranceUse, dict[str, object]] = {
    AssuranceUse.EXPLORATION: {
        "claim_mode": ClaimMode.HYPOTHESIS,
        "allow_labeled_estimates": True,
        "allow_bounded_inference": True,
        "require_deterministic_artifact_verification": False,
    },
    AssuranceUse.INTERNAL_DECISION: {
        "claim_mode": ClaimMode.BOUNDED_INFERENCE,
        "allow_labeled_estimates": True,
        "allow_bounded_inference": True,
        "require_deterministic_artifact_verification": False,
    },
    AssuranceUse.COMMERCIAL_PROPOSAL: {
        "claim_mode": ClaimMode.BOUNDED_INFERENCE,
        "allow_labeled_estimates": True,
        "allow_bounded_inference": True,
        "require_deterministic_artifact_verification": False,
    },
    AssuranceUse.PUBLIC_MARKETING: {
        "claim_mode": ClaimMode.SOURCE_BACKED,
        "allow_labeled_estimates": False,
        "allow_bounded_inference": False,
        "require_deterministic_artifact_verification": False,
    },
    AssuranceUse.REGULATORY_SUBMISSION: {
        "claim_mode": ClaimMode.VALIDATED_SUBMISSION,
        "allow_labeled_estimates": False,
        "allow_bounded_inference": False,
        "require_deterministic_artifact_verification": True,
    },
    AssuranceUse.OPERATIONAL_RELEASE: {
        "claim_mode": ClaimMode.DETERMINISTIC_RUNTIME,
        "allow_labeled_estimates": False,
        "allow_bounded_inference": False,
        "require_deterministic_artifact_verification": True,
    },
}


def assurance_selection(requirements: RequirementsAnalysis) -> AssuranceSelection:
    if requirements.sixsense is not None:
        return requirements.sixsense.assurance
    return AssuranceSelection()


def policy_for_use(intended_use: AssuranceUse) -> AssurancePolicy:
    return AssurancePolicy(intended_use=intended_use, **_POLICIES[intended_use])


def resolve_assurance_policy(requirements: RequirementsAnalysis) -> AssurancePolicy:
    return policy_for_use(assurance_selection(requirements).intended_use)
