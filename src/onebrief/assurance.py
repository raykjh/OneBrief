"""Trusted assurance policy derived from the SixSense intended-use decision."""

from __future__ import annotations

import hashlib
import json
import re
from enum import StrEnum

from pydantic import BaseModel

from onebrief.schemas import (
    AssuranceSelection,
    AssuranceUse,
    IntakeRequest,
    OutputTarget,
    RequirementsAnalysis,
    SixSensePlan,
)


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

_REGULATORY_USE = re.compile(
    r"regulatory[ -]submission|submission\s+to\s+(?:mfds|a\s+regulator)|"
    r"인허가\s*제출용|허가\s*신청서|규제기관\s*제출",
    re.IGNORECASE,
)
_PUBLIC_MARKETING_USE = re.compile(
    r"public[ -]marketing|consumer[ -]facing\s+(?:advertising|marketing)|"
    r"advertising\s+copy|광고문|소비자용\s*(?:광고|판매)\s*문구",
    re.IGNORECASE,
)
_PROPOSAL_USE = re.compile(
    r"commercial[ -]proposal|OEM\s+(?:proposal|RFQ)|\bRFQ\b|"
    r"제안서|견적\s*요청서|OEM\s*(?:생산|판매|제안)",
    re.IGNORECASE,
)
_EXPLORATION_USE = re.compile(
    r"exploration\s+only|concept\s+exploration|research\s+only|아이디어\s*탐색|탐색\s*전용",
    re.IGNORECASE,
)


def _explicit_intended_use(intake: IntakeRequest) -> AssuranceUse | None:
    text = "\n".join(filter(None, [intake.goal, intake.desired_output or ""]))
    if _REGULATORY_USE.search(text):
        return AssuranceUse.REGULATORY_SUBMISSION
    if _PUBLIC_MARKETING_USE.search(text):
        return AssuranceUse.PUBLIC_MARKETING
    if _PROPOSAL_USE.search(text):
        return AssuranceUse.COMMERCIAL_PROPOSAL
    if intake.output_target in {
        OutputTarget.EXISTING_PROJECT,
        OutputTarget.WEB_APP,
        OutputTarget.UNITY_APP,
    }:
        return AssuranceUse.OPERATIONAL_RELEASE
    if _EXPLORATION_USE.search(text):
        return AssuranceUse.EXPLORATION
    return None


def apply_assurance_policy(
    intake: IntakeRequest, requirements: RequirementsAnalysis
) -> RequirementsAnalysis:
    """Bind an explicit delivery use deterministically instead of trusting model defaults."""

    intended_use = _explicit_intended_use(intake)
    if intended_use is None:
        return requirements
    plan = requirements.sixsense or SixSensePlan(
        standard_profile="Use approved professional defaults for the requested delivery."
    )
    if plan.assurance.intended_use == intended_use:
        return requirements
    rationale = {
        AssuranceUse.EXPLORATION: "The requested delivery is explicitly limited to exploration.",
        AssuranceUse.COMMERCIAL_PROPOSAL: "The requested delivery is an OEM or commercial proposal, not public advertising or a filing.",
        AssuranceUse.PUBLIC_MARKETING: "The requested delivery will be used as public-facing marketing.",
        AssuranceUse.REGULATORY_SUBMISSION: "The requested delivery will be submitted to a regulator.",
        AssuranceUse.OPERATIONAL_RELEASE: "The requested delivery is a runnable software or project release.",
    }[intended_use]
    assurance = AssuranceSelection(intended_use=intended_use, rationale=rationale)
    return requirements.model_copy(update={
        "sixsense": plan.model_copy(update={"assurance": assurance})
    })


def assurance_selection(requirements: RequirementsAnalysis) -> AssuranceSelection:
    if requirements.sixsense is not None:
        return requirements.sixsense.assurance
    return AssuranceSelection()


def policy_for_use(intended_use: AssuranceUse) -> AssurancePolicy:
    return AssurancePolicy(intended_use=intended_use, **_POLICIES[intended_use])


def resolve_assurance_policy(requirements: RequirementsAnalysis) -> AssurancePolicy:
    return policy_for_use(assurance_selection(requirements).intended_use)
