from pathlib import Path

import pytest

from onebrief.producer import estimate_budget
from onebrief.schemas import (
    InformationRequirement,
    IntakeRequest,
    InternalSource,
    RequirementsAnalysis,
    SourcePriority,
    UploadEntry,
    UploadManifest,
)
from onebrief.source_loader import load_uploads


def _ready() -> RequirementsAnalysis:
    return RequirementsAnalysis(
        supported=True,
        support_reason="The supported task has complete authoritative inputs.",
        normalized_goal="Create an onboarding guide from internal policy.",
        deliverables=["One-page onboarding guide"],
        mandatory_information=[],
        optional_information=[],
        acceptance_criteria=["Every instruction is grounded in the policy."],
        assumptions=[],
        consolidated_questions=[],
        ready_for_estimate=True,
    )


def test_upload_loader_hashes_and_maps_source(tmp_path: Path) -> None:
    source = tmp_path / "policy.md"
    source.write_text("# Policy\nRemote work requires manager approval.", encoding="utf-8")
    manifest = UploadManifest(
        sources=[UploadEntry(path="policy.md", requirement_keys=["internal_policy"])]
    )
    loaded = load_uploads(manifest, tmp_path)
    assert loaded[0].requirement_keys == ["internal_policy"]
    assert loaded[0].sha256 and len(loaded[0].sha256) == 64


def test_producer_refuses_unready_requirements() -> None:
    blocked = _ready().model_copy(
        update={
            "mandatory_information": [
                InformationRequirement(
                    key="policy",
                    request="Provide policy.",
                    reason="It governs the output.",
                    acceptable_evidence=["policy file"],
                )
            ],
            "consolidated_questions": ["Please provide policy."],
            "ready_for_estimate": False,
        }
    )
    with pytest.raises(ValueError):
        estimate_budget(IntakeRequest(goal="Create a guide."), blocked)


def test_producer_calculates_bounded_estimate() -> None:
    intake = IntakeRequest(
        goal="Create a guide.",
        budget_limit_usd=1.0,
        internal_sources=[
            InternalSource(
                name="policy.md",
                priority=SourcePriority.MANDATORY,
                requirement_keys=["policy"],
                content="Remote work requires manager approval." * 100,
            )
        ],
    )
    budget = estimate_budget(intake, _ready())
    assert budget.minimum_cost_usd <= budget.recommended_cost_usd <= budget.maximum_cost_usd
    assert budget.recommended_approval_usd >= 0.01
    assert budget.status.value == "within_budget"
    revision = next(stage for stage in budget.stages if stage.stage == "revision")
    verification = next(
        stage for stage in budget.stages if stage.stage == "independent_verification"
    )
    assert verification.recommended_calls == revision.recommended_calls + 1
    assert verification.maximum_calls == revision.maximum_calls + 1

