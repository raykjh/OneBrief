import hashlib
import json
from pathlib import Path

from onebrief.automatic_resume import (
    AutomaticResumePlan,
    can_attempt_automatic_resume,
    seed_automatic_resume,
)
from onebrief.budget_guard import BudgetStore
from onebrief.jobs import JobRecord, JobStatus
from onebrief.schemas import BudgetEnvelope, BudgetStatus, StageEstimate


def _estimate() -> BudgetEnvelope:
    return BudgetEnvelope(
        price_card_version="google-agent-platform-global-standard-search-2026-08-12",
        price_source_url="https://example.test/pricing",
        endpoint="global-standard",
        estimated_source_tokens=100,
        estimated_contract_tokens=100,
        stages=[StageEstimate(
            stage="draft",
            model="gemini-3.5-flash",
            input_tokens_per_call=100,
            output_tokens_per_call=100,
            minimum_calls=1,
            recommended_calls=1,
            maximum_calls=1,
            minimum_cost_usd=0.001,
            recommended_cost_usd=0.01,
            maximum_cost_usd=0.02,
            estimated_minutes_per_call=1,
        )],
        minimum_cost_usd=0.001,
        recommended_cost_usd=0.01,
        maximum_cost_usd=0.02,
        recommended_approval_usd=0.01,
        budget_limit_usd=None,
        status=BudgetStatus.WITHIN_BUDGET,
        estimated_minutes_minimum=1,
        estimated_minutes_recommended=1,
        estimated_minutes_maximum=1,
        notes=[],
    )


def _failed_job(root: Path) -> None:
    root.mkdir(parents=True)
    ledger = BudgetStore(root / "run").approve(_estimate(), 0.01)
    BudgetStore(root / "run").fail("web observation failed")
    record = JobRecord(
        job_id=root.name,
        status=JobStatus.FAILED,
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
        attempts=1,
        current_stage="failed",
        message="RuntimeError: web observation failed: unsupported control",
        run_id=ledger.run_id,
    )
    (root / "job.json").write_text(record.model_dump_json(indent=2), encoding="utf-8")
    work = root / "work"
    work.mkdir()
    (work / "code_change_set.json").write_text("{}", encoding="utf-8")


def test_failed_validation_with_unused_budget_is_resume_candidate(tmp_path: Path) -> None:
    job = tmp_path / "job-1"
    _failed_job(job)

    assert can_attempt_automatic_resume(job) is True


def test_patch_hygiene_failure_with_candidate_is_resume_candidate(tmp_path: Path) -> None:
    job = tmp_path / "job-hygiene"
    _failed_job(job)
    record = JobRecord.model_validate_json((job / "job.json").read_text(encoding="utf-8"))
    record = record.model_copy(update={
        "message": "RuntimeError: development patch hygiene failed: trailing whitespace"
    })
    (job / "job.json").write_text(record.model_dump_json(indent=2), encoding="utf-8")

    assert can_attempt_automatic_resume(job) is True


def test_partial_semantic_observation_hold_is_resume_candidate(tmp_path: Path) -> None:
    job = tmp_path / "job-partial"
    _failed_job(job)
    record = JobRecord.model_validate_json((job / "job.json").read_text(encoding="utf-8"))
    record = record.model_copy(update={
        "status": JobStatus.PARTIAL,
        "current_stage": "finished",
        "message": "A required independent observation capability was unavailable; the result was not accepted as complete.",
    })
    (job / "job.json").write_text(record.model_dump_json(indent=2), encoding="utf-8")

    assert can_attempt_automatic_resume(job) is True


def test_seed_maps_trusted_revalidation_to_child_development(tmp_path: Path) -> None:
    source = tmp_path / "source"
    target = tmp_path / "target"
    (source / "work" / "development_revalidation" / "changed_files" / "src").mkdir(parents=True)
    (source / "work" / "development_revalidation" / "development_run.json").write_text(
        '{"status":"verified"}', encoding="utf-8"
    )
    (source / "work" / "development_revalidation" / "changed_files" / "src" / "index.html").write_text(
        "verified", encoding="utf-8"
    )
    (source / "work" / "code_change_set.json").write_text("{}", encoding="utf-8")
    (source / "work" / "independent_observations").mkdir()
    (source / "work" / "independent_observations" / "web_ui_observation.json").write_text(
        '{"capability":"semantic_observation","status":"observed"}', encoding="utf-8"
    )
    target.mkdir()
    plan = AutomaticResumePlan(
        source_job_id="source",
        project_id="sample-project",
        remaining_approved_usd=0.008,
        source_actual_usd=0.002,
        revalidation_dir=str(source / "work" / "development_revalidation"),
        revalidated_change_sha256=hashlib.sha256(b"{}").hexdigest(),
    )

    manifest = seed_automatic_resume(source, target, plan)

    assert (target / "work" / "development" / "development_run.json").is_file()
    assert (target / "work" / "development" / "changed_files" / "src" / "index.html").read_text() == "verified"
    assert (target / "work" / "independent_observations" / "web_ui_observation.json").is_file()
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    assert payload["source_actual_usd"] + payload["child_approved_usd"] == 0.01
