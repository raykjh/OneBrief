"""Goal-specific observers that plug into the generic Reality Check contract.

The core never contains Unity, localization, or product-specific judgement. Packs
may produce a signed-shaped receipt, but only an independent observer can mark it
as observed.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, model_validator

from onebrief.reality_check import (
    ObservationReceipt,
    ObservationStatus,
    RealityCapability,
)


class SemanticFrameFinding(BaseModel):
    artifact_path: str
    expected_state: str
    visible_text_samples: list[str] = Field(default_factory=list)
    semantic_findings: list[str] = Field(default_factory=list)
    passed: bool

    @model_validator(mode="after")
    def require_direct_observation(self) -> "SemanticFrameFinding":
        if not self.visible_text_samples:
            raise ValueError("semantic observation requires visible text samples")
        if not self.semantic_findings:
            raise ValueError("semantic observation requires explicit findings")
        return self


class UnityLocalizationObservationPack:
    """Temporary JULPAE pack; reusable core sees only its generic receipt."""

    pack_id = "julpae_unity_localization_semantic_observer_v1"

    def build_receipt(
        self,
        findings: list[SemanticFrameFinding],
        *,
        observer_is_independent: bool,
    ) -> ObservationReceipt:
        passed = bool(findings) and all(item.passed for item in findings)
        limitations: list[str] = []
        if not observer_is_independent:
            limitations.append("The maker cannot independently approve its own visual output.")
        if any(not item.passed for item in findings):
            limitations.append("At least one rendered state failed semantic inspection.")
        return ObservationReceipt(
            capability=RealityCapability.SEMANTIC_OBSERVATION,
            observer_pack_id=self.pack_id,
            status=(
                ObservationStatus.OBSERVED
                if passed and observer_is_independent
                else ObservationStatus.FAILED
            ),
            independent_from_maker=observer_is_independent,
            artifact_paths=[item.artifact_path for item in findings],
            findings=[
                f"{item.expected_state}: {finding}"
                for item in findings
                for finding in item.semantic_findings
            ],
            limitations=limitations,
        )
