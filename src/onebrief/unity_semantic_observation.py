"""Independent Gemini observation for trusted Unity runtime screenshots."""

from __future__ import annotations

import json
from pathlib import Path, PurePosixPath

from pydantic import BaseModel, Field, model_validator

from onebrief.reality_check import (
    ObservationReceipt,
    ObservationStatus,
    RealityCapability,
)


class UnityFrameObservation(BaseModel):
    artifact_path: str
    visible_text_samples: list[str] = Field(default_factory=list, max_length=40)
    findings: list[str] = Field(min_length=1, max_length=20)
    passed: bool


class UnitySemanticObservation(BaseModel):
    frames: list[UnityFrameObservation] = Field(min_length=1, max_length=6)
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

    prompt = (
        "Independently inspect every attached Unity runtime screenshot. The maker cannot approve "
        "its own output. Judge only what is visibly rendered, not filenames or claims. Reject a "
        "blank, single-color, camera-background-only, clipped, unreadable, missing-glyph, overlapping, "
        "or obviously non-responsive screen. Confirm the requested real UI is visible and usable at "
        "each supplied viewport. Return exactly one frame entry for every artifact path listed below.\n\n"
        f"GOAL AND COMPLETION CONTRACT:\n{goal_text[:24000]}\n\n"
        "ARTIFACT PATHS:\n" + "\n".join(f"- {item}" for item in allowed.values())
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

    passed = all(frame.passed for frame in normalized)
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
    )
    observation_path.parent.mkdir(parents=True, exist_ok=True)
    observation_path.write_text(receipt.model_dump_json(indent=2) + "\n", encoding="utf-8")
    if not passed:
        detail = "; ".join(findings)[:4_000]
        raise RuntimeError("independent Unity semantic visual observation failed: " + detail)
    return receipt
