import json
from pathlib import Path

import pytest

from onebrief.reality_check import ObservationStatus
from onebrief.unity_semantic_observation import (
    UnitySemanticObservation,
    observe_unity_visual_evidence,
)


class FakeGateway:
    def __init__(self, result: dict[str, object]) -> None:
        self.result = result
        self.calls: list[dict[str, object]] = []

    def generate_json_with_images(self, **kwargs):
        self.calls.append(kwargs)
        return UnitySemanticObservation.model_validate(self.result)


def _evidence(tmp_path: Path) -> Path:
    root = tmp_path / "unity_visual_evidence"
    screenshots = root / "screenshots"
    screenshots.mkdir(parents=True)
    # The gateway is faked here; the production gateway performs real PNG validation.
    (screenshots / "desktop.png").write_bytes(b"png-desktop")
    (screenshots / "mobile.png").write_bytes(b"png-mobile")
    (root / "summary.json").write_text(json.dumps({
        "screenshot_paths": [
            "screenshots/desktop.png",
            "screenshots/mobile.png",
        ]
    }), encoding="utf-8")
    return root


def test_unity_semantic_observer_writes_independent_pass_receipt(tmp_path: Path) -> None:
    gateway = FakeGateway({
        "frames": [
            {
                "artifact_path": "screenshots/desktop.png",
                "visible_text_samples": ["Settings"],
                "findings": ["The settings panel is visible and readable."],
                "passed": True,
            },
            {
                "artifact_path": "screenshots/mobile.png",
                "visible_text_samples": ["Settings"],
                "findings": ["The portrait layout remains visible and readable."],
                "passed": True,
            },
        ],
        "overall_findings": ["Both requested viewports contain real UI."],
    })
    output = tmp_path / "independent_observations" / "unity.json"

    receipt = observe_unity_visual_evidence(
        gateway,
        model="gemini-test",
        evidence_dir=_evidence(tmp_path),
        observation_path=output,
        goal_text="Verify mobile and desktop settings UI.",
    )

    assert receipt.status == ObservationStatus.OBSERVED
    assert receipt.independent_from_maker is True
    assert output.is_file()
    assert len(gateway.calls[0]["image_paths"]) == 2


def test_unity_semantic_observer_rejects_blank_runtime_frame(tmp_path: Path) -> None:
    gateway = FakeGateway({
        "frames": [
            {
                "artifact_path": "screenshots/desktop.png",
                "visible_text_samples": [],
                "findings": ["Only a flat camera background is visible."],
                "passed": False,
            },
            {
                "artifact_path": "screenshots/mobile.png",
                "visible_text_samples": [],
                "findings": ["No interactive UI is visible."],
                "passed": False,
            },
        ],
        "overall_findings": ["The screenshots do not prove the requested UI."],
    })
    output = tmp_path / "independent_observations" / "unity.json"

    with pytest.raises(RuntimeError, match="semantic visual observation failed"):
        observe_unity_visual_evidence(
            gateway,
            model="gemini-test",
            evidence_dir=_evidence(tmp_path),
            observation_path=output,
            goal_text="Verify mobile and desktop settings UI.",
        )

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["status"] == "failed"
