import json
from pathlib import Path

import pytest

from onebrief.reality_check import ObservationStatus
from onebrief.unity_semantic_observation import (
    UnitySemanticObservation,
    contract_requires_strict_visual_quality,
    observe_unity_visual_evidence,
    semantic_observation_contract,
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


def test_unity_semantic_observer_persists_failed_criterion_binding(tmp_path: Path) -> None:
    gateway = FakeGateway({
        "frames": [
            {"artifact_path": "screenshots/desktop.png", "visible_text_samples": ["Lobby"],
             "findings": ["The panel is light and opaque."], "passed": False},
            {"artifact_path": "screenshots/mobile.png", "visible_text_samples": ["Lobby"],
             "findings": ["The typography is serif."], "passed": False},
        ],
        "criterion_checks": [{
            "criterion_id": "Q05",
            "passed": False,
            "evidence": "Both rendered lobby views violate the active visual style.",
        }],
        "overall_findings": ["Q05 is not satisfied."],
    })
    output = tmp_path / "independent_observations" / "unity.json"

    with pytest.raises(RuntimeError, match="semantic visual observation failed"):
        observe_unity_visual_evidence(
            gateway,
            model="gemini-test",
            evidence_dir=_evidence(tmp_path),
            observation_path=output,
            goal_text="Q05 requires a dark sans-serif lobby.",
            strict_visual_quality=True,
        )

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["criterion_checks"] == [{
        "criterion_id": "Q05",
        "passed": False,
        "evidence": "Both rendered lobby views violate the active visual style.",
    }]
    assert "exact Q-number" in str(gateway.calls[0]["contents"])


def test_visual_quality_review_is_bound_to_active_criteria() -> None:
    behavioral = {
        "goal": "The Login surface reaches Lobby.",
        "completion_contract": {
            "quality_criteria": [{
                "description": "Preserved authentication transition works",
                "evidence_required": "PlayMode evidence for Login to Lobby.",
            }]
        },
    }
    presentation = {
        "completion_contract": {
            "quality_criteria": [{
                "description": "Responsive visual style compliance",
                "evidence_required": "Rendered mobile and desktop screenshots.",
            }]
        },
    }

    assert contract_requires_strict_visual_quality(behavioral) is False
    assert contract_requires_strict_visual_quality(presentation) is True


def test_behavioral_observation_prompt_does_not_expand_to_visual_milestone(
    tmp_path: Path,
) -> None:
    gateway = FakeGateway({
        "frames": [
            {"artifact_path": "screenshots/desktop.png", "visible_text_samples": ["Login"],
             "findings": ["Login is visible."], "passed": True},
            {"artifact_path": "screenshots/mobile.png", "visible_text_samples": ["Lobby"],
             "findings": ["Lobby is visible."], "passed": True},
        ],
        "overall_findings": ["The contracted transition is visibly evidenced."],
    })

    observe_unity_visual_evidence(
        gateway,
        model="gemini-test",
        evidence_dir=_evidence(tmp_path),
        observation_path=tmp_path / "observation.json",
        goal_text="Login reaches Lobby.",
        strict_visual_quality=False,
    )

    prompt = str(gateway.calls[0]["contents"])
    assert "bounded Quest" in prompt
    assert "do not fail pre-existing style" in prompt


def test_behavioral_observation_excludes_advisory_whole_project_architecture() -> None:
    contract = {
        "goal": "Login reaches Lobby.",
        "completion_contract": {"quality_criteria": [{
            "criterion_id": "Q91",
            "description": "Login transition works.",
        }]},
        "project_architecture": {
            "directive": "Implement the future S02 visual redesign now."
        },
        "assumptions": ["Whole-project preference context."],
    }

    bounded = semantic_observation_contract(contract, strict_visual_quality=False)

    assert bounded["goal"] == "Login reaches Lobby."
    assert "project_architecture" not in bounded
    assert "assumptions" not in bounded
    assert semantic_observation_contract(
        contract, strict_visual_quality=True
    ) is contract
