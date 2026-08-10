from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

from onebrief.parallel_campaign import (
    CampaignDefinition,
    FailureObservation,
    LaneEvidence,
    LaneStatus,
    ParallelCampaignStore,
    RevalidationReceipt,
    RevalidationResult,
    SafeApplyOutcome,
)


IMAGE = "sha256:" + "1" * 64


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def _definition() -> CampaignDefinition:
    return CampaignDefinition.model_validate(
        {
            "campaign_id": "parallel-test",
            "baseline_ref": "test-baseline",
            "image_digest": IMAGE,
            "total_budget_usd": 3,
            "lanes": [
                {
                    "lane_id": f"lane-{index}",
                    "kind": kind,
                    "case_path": f"cases/case-{index}.json",
                    "output_root": f"runs/lane-{index}",
                    "max_budget_usd": 1,
                }
                for index, kind in enumerate(
                    ("existing_software", "greenfield_software", "structured_artifact"), 1
                )
            ],
        }
    )


@pytest.fixture
def campaign(tmp_path: Path) -> tuple[ParallelCampaignStore, object]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "OneBrief Test")
    cases = repo / "cases"
    cases.mkdir()
    for index in range(1, 4):
        (cases / f"case-{index}.json").write_text(
            json.dumps(
                {
                    "goal": f"goal {index}",
                    "acceptance_criteria": [f"criterion {index}"],
                    "verification_commands": ["verify"],
                    "execution_rules": {"protect_original_worktree": True},
                }
            ),
            encoding="utf-8",
        )
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "baseline")
    _git(repo, "tag", "test-baseline")
    store = ParallelCampaignStore(tmp_path / "campaign")
    manifest = store.create(repo_root=repo, definition=_definition())
    return store, manifest


def _evidence(manifest: object, lane_index: int, **updates: object) -> LaneEvidence:
    lane = manifest.lanes[lane_index]
    values: dict[str, object] = {
        "campaign_id": manifest.campaign_id,
        "lane_id": lane.lane_id,
        "baseline_commit_sha": manifest.baseline.commit_sha,
        "case_sha256": lane.case_sha256,
        "status": LaneStatus.COMPLETE,
        "verified_complete": True,
        "actual_cost_usd": 0.5,
        "elapsed_seconds": 10 + lane_index,
        "model_calls": 2,
        "denied_calls": 0,
        "observed_source_commit_sha": manifest.baseline.commit_sha,
    }
    values.update(updates)
    return LaneEvidence.model_validate(values)


def test_definition_requires_three_isolated_relative_lanes() -> None:
    raw = _definition().model_dump(mode="json")
    raw["lanes"] = raw["lanes"][:2]
    with pytest.raises(ValidationError, match="exactly three"):
        CampaignDefinition.model_validate(raw)

    raw = _definition().model_dump(mode="json")
    raw["lanes"][1]["output_root"] = raw["lanes"][0]["output_root"]
    with pytest.raises(ValidationError, match="output roots must be unique"):
        CampaignDefinition.model_validate(raw)

    raw = _definition().model_dump(mode="json")
    raw["lanes"][0]["output_root"] = "../shared"
    with pytest.raises(ValidationError, match="repository-relative"):
        CampaignDefinition.model_validate(raw)


def test_create_freezes_baseline_contracts_and_permissions(campaign: tuple) -> None:
    store, manifest = campaign
    assert len(manifest.lanes) == 3
    assert len({lane.output_root for lane in manifest.lanes}) == 3
    assert all(not lane.allows_common_code_changes for lane in manifest.lanes)
    assert all(not lane.allows_safe_apply for lane in manifest.lanes)
    assert manifest.integration_lane.allows_common_code_changes
    assert manifest.integration_lane.allows_safe_apply
    assert (store.root / "campaign.json").is_file()
    assert all(len(lane.contract_sha256) == 64 for lane in manifest.lanes)


def test_record_evidence_enforces_baseline_mutation_and_budget(campaign: tuple) -> None:
    store, manifest = campaign
    with pytest.raises(ValueError, match="different baseline"):
        store.record_evidence(_evidence(manifest, 0, baseline_commit_sha="0" * 40))
    with pytest.raises(PermissionError, match="cannot mutate"):
        store.record_evidence(_evidence(manifest, 0, tracked_source_mutation_detected=True))
    with pytest.raises(PermissionError, match="exceeds approved cap"):
        store.record_evidence(_evidence(manifest, 0, actual_cost_usd=1.01))

    evidence = _evidence(manifest, 0)
    target = store.record_evidence(evidence)
    assert target.is_file()
    with pytest.raises(FileExistsError):
        store.record_evidence(evidence)


def test_failure_fingerprint_ignores_volatile_ids_and_numbers() -> None:
    first = FailureObservation.from_error(
        error_class="RuntimeError",
        stage="verify",
        summary="job 123 failed at 550 ms id 123e4567-e89b-12d3-a456-426614174000",
    )
    second = FailureObservation.from_error(
        error_class="RuntimeError",
        stage="verify",
        summary="job 987 failed at 1200 ms id 223e4567-e89b-12d3-a456-426614174999",
    )
    assert first.fingerprint == second.fingerprint


def test_integration_promotes_only_cross_lane_failures(campaign: tuple) -> None:
    store, manifest = campaign
    shared_a = FailureObservation.from_error(
        error_class="RuntimeError", stage="verify", summary="build failed in job 123"
    )
    shared_b = FailureObservation.from_error(
        error_class="RuntimeError", stage="verify", summary="build failed in job 999"
    )
    local = FailureObservation.from_error(
        error_class="ValueError", stage="artifact", summary="workbook cell A1 is invalid"
    )
    store.record_evidence(
        _evidence(manifest, 0, failures=[shared_a], recovery_attempts=1,
                  recovery_successes=1, user_approvals=1)
    )
    store.record_evidence(_evidence(manifest, 1, failures=[shared_b]))
    store.record_evidence(_evidence(manifest, 2, failures=[local]))

    batch = store.integrate()
    assert len(batch.common_failure_clusters) == 1
    assert set(batch.common_failure_clusters[0].lane_ids) == {"lane-1", "lane-2"}
    assert len(batch.lane_specific_failure_clusters) == 1
    assert batch.aggregate.completion_rate == 1
    assert batch.aggregate.total_actual_cost_usd == 1.5
    assert batch.aggregate.total_user_interventions == 1
    assert batch.aggregate.automatic_recovery_rate == 1


def test_revalidation_requires_all_lanes_and_all_pass_for_promotion(campaign: tuple) -> None:
    store, manifest = campaign
    results = [
        RevalidationResult(
            lane_id=lane.lane_id,
            passed=index != 1,
            evidence_id=f"evidence-{index}",
            safe_apply_outcome=SafeApplyOutcome.NOT_APPLICABLE,
        )
        for index, lane in enumerate(manifest.lanes)
    ]
    receipt = RevalidationReceipt(
        campaign_id=manifest.campaign_id,
        candidate_commit_sha="2" * 40,
        candidate_image_digest="sha256:" + "3" * 64,
        results=results,
    )
    assert not store.record_revalidation(receipt).promotion_allowed

    passed = receipt.model_copy(
        update={"results": [result.model_copy(update={"passed": True}) for result in results]}
    )
    assert store.record_revalidation(passed).promotion_allowed

    incomplete = receipt.model_copy(update={"results": results[:2]})
    with pytest.raises(ValueError, match="each campaign lane"):
        store.record_revalidation(incomplete)
