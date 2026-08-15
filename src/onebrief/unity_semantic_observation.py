"""Independent Gemini observation for trusted Unity runtime screenshots."""

from __future__ import annotations

import json
from pathlib import Path, PurePosixPath

from pydantic import BaseModel, Field, model_validator

from onebrief.reality_check import (
    ObservationCriterionCheck,
    ObservationReceipt,
    ObservationStatus,
    RealityCapability,
)


_VISUAL_QUALITY_MARKERS = (
    "visual style",
    "responsive",
    "layout",
    "typography",
    "overlap",
    "clipped",
    "glyph",
    "rendered key-screen",
    "mobile",
    "desktop aspect",
)


def contract_requires_strict_visual_quality(contract: dict[str, object]) -> bool:
    """Return whether the active contract authorizes presentation-quality review.

    Runtime screenshots are also used as behavioral evidence.  A verifier must
    not turn those screenshots into an implicit whole-product visual milestone.
    Only the active completion criteria can enable strict style/layout review.
    """

    completion = contract.get("completion_contract")
    if not isinstance(completion, dict):
        return False
    criteria = completion.get("quality_criteria")
    if not isinstance(criteria, list):
        return False
    for criterion in criteria:
        if not isinstance(criterion, dict):
            continue
        text = " ".join(
            str(criterion.get(key, ""))
            for key in ("description", "evidence_required")
        ).casefold()
        if any(marker in text for marker in _VISUAL_QUALITY_MARKERS):
            return True
    return False


def semantic_observation_contract(
    contract: dict[str, object], *, strict_visual_quality: bool,
) -> dict[str, object]:
    """Remove advisory whole-project material from a bounded behavioral review."""

    if strict_visual_quality:
        return contract
    allowed = (
        "goal",
        "desired_output",
        "output_target",
        "normalized_goal",
        "deliverables",
        "acceptance_criteria",
        "completion_contract",
    )
    return {key: contract[key] for key in allowed if key in contract}


class UnityFrameObservation(BaseModel):
    artifact_path: str
    visible_text_samples: list[str] = Field(default_factory=list, max_length=40)
    findings: list[str] = Field(min_length=1, max_length=20)
    passed: bool


class UnitySemanticObservation(BaseModel):
    frames: list[UnityFrameObservation] = Field(min_length=1, max_length=6)
    criterion_checks: list[ObservationCriterionCheck] = Field(default_factory=list, max_length=12)
    overall_findings: list[str] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def require_visible_content_for_pass(self) -> "UnitySemanticObservation":
        for frame in self.frames:
            if frame.passed and not frame.visible_text_samples:
                raise ValueError("a passing UI frame requires visible text samples")
        return self


def observe_unity_visual_evidence(
    gateway,
    *,
    model: str,
    evidence_dir: Path,
    observation_path: Path,
    goal_text: str,
    strict_visual_quality: bool = True,
) -> ObservationReceipt:
    """Inspect pipeline-owned screenshots and write one independent receipt."""

    summary_path = evidence_dir / "summary.json"
    if not summary_path.is_file():
        raise RuntimeError("Unity semantic observation requires the trusted evidence summary")
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    relative_paths = summary.get("screenshot_paths")
    if not isinstance(relative_paths, list) or not relative_paths:
        raise RuntimeError("Unity semantic observation requires trusted screenshots")

    paths: list[Path] = []
    allowed: dict[str, str] = {}
    for raw in relative_paths[:6]:
        relative = PurePosixPath(str(raw))
        path = (evidence_dir / Path(*relative.parts)).resolve()
        if not path.is_relative_to(evidence_dir.resolve()) or not path.is_file():
            raise RuntimeError("Unity semantic observation found an unavailable screenshot")
        label = relative.as_posix()
        paths.append(path)
        allowed[label.casefold()] = label

    review_boundary = (
        "This active contract explicitly includes presentation quality. Reject blank, "
        "single-color, camera-background-only, clipped, unreadable, missing-glyph, "
        "overlapping, non-responsive, or visibly noncompliant styled screens."
        if strict_visual_quality else
        "These screenshots are behavioral evidence for a bounded Quest, not approval of the "
        "whole product's presentation. Confirm only that the contracted UI state is visibly "
        "present and usable enough to prove the requested behavior. Reject blank or "
        "camera-background-only frames, but do not fail pre-existing style, layout, debug, "
        "localization, or responsiveness issues unless an active criterion below explicitly "
        "requires them. Record such unrelated observations as non-blocking findings."
    )
    prompt = (
        "Independently inspect every attached Unity runtime screenshot. The maker cannot approve "
        "its own output. Judge only what is visibly rendered, not filenames or claims. Reject a "
        "claim that is not visible. " + review_boundary + " Confirm the requested real UI is visible "
        "at each supplied viewport. Return exactly one frame entry for every artifact path listed below.\n\n"
        f"GOAL AND COMPLETION CONTRACT:\n{goal_text[:24000]}\n\n"
        "ARTIFACT PATHS:\n" + "\n".join(f"- {item}" for item in allowed.values())
        + (
            "\n\nFor every active completion criterion whose truth is visible in these frames, "
            "return criterion_checks with the exact Q-number, passed boolean, and concrete visual evidence. "
            "A frame being present is not proof of style, layout, language, or glyph compliance."
            if strict_visual_quality else ""
        )
    )
    result = gateway.generate_json_with_images(
        stage="independent_verification",
        model=model,
        contents=prompt,
        image_paths=paths,
        schema=UnitySemanticObservation,
        max_output_tokens=1800,
        system_instruction=(
            "You are OneBrief's independent visual verifier. Be conservative, concrete, and "
            "evidence-bound. Never infer invisible UI from test metadata."
        ),
        temperature=0.0,
    )

    seen: set[str] = set()
    normalized: list[UnityFrameObservation] = []
    for frame in result.frames:
        key = frame.artifact_path.casefold()
        if key not in allowed or key in seen:
            raise RuntimeError(
                "Unity semantic observer returned an unknown or duplicate artifact path"
            )
        seen.add(key)
        normalized.append(frame.model_copy(update={"artifact_path": allowed[key]}))
    missing = [path for key, path in allowed.items() if key not in seen]
    if missing:
        raise RuntimeError(
            "Unity semantic observer omitted screenshot(s): " + ", ".join(missing)
        )

    passed = (
        all(frame.passed for frame in normalized)
        and all(item.passed for item in result.criterion_checks)
    )
    findings = [
        f"{frame.artifact_path}: {finding}"
        for frame in normalized
        for finding in frame.findings
    ] + list(result.overall_findings)
    receipt = ObservationReceipt(
        capability=RealityCapability.SEMANTIC_OBSERVATION,
        observer_pack_id="onebrief_unity_semantic_visual_observer_v1",
        status=ObservationStatus.OBSERVED if passed else ObservationStatus.FAILED,
        independent_from_maker=True,
        artifact_paths=[frame.artifact_path for frame in normalized],
        findings=findings,
        limitations=[] if passed else ["At least one rendered Unity state failed semantic inspection."],
        criterion_checks=result.criterion_checks,
    )
    observation_path.parent.mkdir(parents=True, exist_ok=True)
    observation_path.write_text(receipt.model_dump_json(indent=2) + "\n", encoding="utf-8")
    if not passed:
        detail = "; ".join(findings)[:4_000]
        raise RuntimeError("independent Unity semantic visual observation failed: " + detail)
    return receipt
