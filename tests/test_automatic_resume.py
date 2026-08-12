import hashlib
import json
from pathlib import Path

from onebrief.automatic_resume import (
    AutomaticResumePlan,
    can_attempt_automatic_resume,
    can_attempt_bounded_repair_resume,
    can_attempt_structural_resume,
    create_bounded_repair_resume,
    create_structural_resume,
    seed_automatic_resume,
)
from onebrief.budget_guard import BudgetStore
from onebrief.jobs import JobRecord, JobStatus
from onebrief.schemas import (
    BudgetEnvelope,
    BudgetStatus,
    IntakeRequest,
    InternalSource,
    RequirementsAnalysis,
    SourcePriority,
    StageEstimate,
)


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


def test_missing_stage_owner_restarts_under_only_unused_parent_budget(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "source-job"
    source.mkdir()
    ledger = BudgetStore(source / "run").approve(_estimate(), 0.01)
    record = JobRecord(
        job_id="source-job",
        status=JobStatus.FAILED,
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:01+00:00",
        attempts=1,
        current_stage="failed",
        message="ValueError: team plan is missing stage owner: public_research",
        run_id=ledger.run_id,
    )
    (source / "job.json").write_text(record.model_dump_json(indent=2), encoding="utf-8")
    inputs = source / "inputs"
    inputs.mkdir()
    intake = IntakeRequest(goal="Improve the project.", public_research_allowed=True)
    requirements = RequirementsAnalysis(
        supported=True,
        support_reason="Ready.",
        normalized_goal=intake.goal,
        deliverables=["Result"],
        mandatory_information=[],
        optional_information=[],
        acceptance_criteria=["Result passes."],
        assumptions=[],
        consolidated_questions=[],
        ready_for_estimate=True,
    )
    internal_source = InternalSource(
        name="context.md",
        priority=SourcePriority.MANDATORY,
        content="Approved context",
    )
    (inputs / "intake.json").write_text(intake.model_dump_json(), encoding="utf-8")
    (inputs / "requirements.json").write_text(requirements.model_dump_json(), encoding="utf-8")
    (inputs / "sources.json").write_text(
        json.dumps([internal_source.model_dump(mode="json")]), encoding="utf-8"
    )
    (inputs / "budget_estimate.json").write_text(_estimate().model_dump_json(), encoding="utf-8")
    child = tmp_path / "jobs" / "child-job"

    def fake_create_job(**kwargs):
        assert kwargs["approved_usd"] == 0.01
        assert kwargs["embed_project_snapshot"] is False
        child.mkdir(parents=True)
        (child / "work").mkdir()
        return child

    monkeypatch.setattr("onebrief.automatic_resume.create_job", fake_create_job)

    assert can_attempt_structural_resume(source) is True
    created, plan = create_structural_resume(source, tmp_path / "jobs")

    assert created == child
    assert plan.source_actual_usd == 0
    assert plan.remaining_approved_usd == 0.01
    manifest = json.loads((child / "work" / "continuation_manifest.json").read_text())
    assert manifest["aggregate_approval_ceiling_usd"] == 0.01
    assert manifest["authorization_kind"] == "remaining_parent_approval"


def test_rejected_candidate_resumes_without_repeating_completed_context(tmp_path: Path) -> None:
    source = tmp_path / "source-repair"
    source.mkdir()
    ledger = BudgetStore(source / "run").approve(_estimate(), 0.01)
    record = JobRecord(
        job_id=source.name,
        status=JobStatus.FAILED,
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:01+00:00",
        attempts=1,
        current_stage="failed",
        message="RuntimeError: development verification failed: missing test assembly",
        run_id=ledger.run_id,
    )
    (source / "job.json").write_text(record.model_dump_json(indent=2), encoding="utf-8")
    inputs = source / "inputs"
    inputs.mkdir()
    intake = IntakeRequest(goal="Improve the project.", public_research_allowed=True)
    requirements = RequirementsAnalysis(
        supported=True,
        support_reason="Ready.",
        normalized_goal=intake.goal,
        deliverables=["Result"],
        mandatory_information=[],
        optional_information=[],
        acceptance_criteria=["Result passes."],
        assumptions=[],
        consolidated_questions=[],
        ready_for_estimate=True,
    )
    (inputs / "intake.json").write_text(intake.model_dump_json(), encoding="utf-8")
    (inputs / "requirements.json").write_text(requirements.model_dump_json(), encoding="utf-8")
    (inputs / "sources.json").write_text("[]", encoding="utf-8")
    (inputs / "budget_estimate.json").write_text(_estimate().model_dump_json(), encoding="utf-8")
    work = source / "work"
    work.mkdir()
    (work / "analysis.json").write_text('{"ready":true}', encoding="utf-8")
    (work / "code_change_set.json").write_text('{"candidate":true}', encoding="utf-8")
    (work / "development_verification_failure.txt").write_text(
        "missing test assembly", encoding="utf-8"
    )
    (work / "continuation_manifest.json").write_text(json.dumps({
        "source_actual_usd": 0.002,
        "aggregate_approval_ceiling_usd": 0.012,
    }), encoding="utf-8")

    assert can_attempt_bounded_repair_resume(source) is True
    child, plan = create_bounded_repair_resume(source, tmp_path / "jobs")

    assert (child / "work" / "analysis.json").is_file()
    assert (child / "work" / "code_change_set.json").is_file()
    assert (child / "work" / "development_verification_failure.txt").is_file()
    assert plan.remaining_approved_usd == 0.01
    assert plan.cumulative_actual_usd == 0.002
    assert plan.aggregate_approval_ceiling_usd == 0.012


def test_truncated_compact_repair_can_resume_from_preserved_candidate(tmp_path: Path) -> None:
    job = tmp_path / "truncated-repair"
    _failed_job(job)
    record = JobRecord.model_validate_json((job / "job.json").read_text("utf-8"))
    record = record.model_copy(update={
        "message": (
            "ValidationError: CompactProposedProjectCodeChangeSet "
            "Invalid JSON: EOF while parsing a string"
        )
    })
    (job / "job.json").write_text(record.model_dump_json(indent=2), encoding="utf-8")
    (job / "work" / "development_verification_failure.txt").write_text(
        "missing Unity test assembly", encoding="utf-8"
    )

    assert can_attempt_bounded_repair_resume(job) is True


def test_invalid_compact_edit_shape_can_resume_from_preserved_candidate(tmp_path: Path) -> None:
    job = tmp_path / "invalid-compact-shape"
    _failed_job(job)
    record = JobRecord.model_validate_json((job / "job.json").read_text("utf-8"))
    record = record.model_copy(update={
        "message": (
            "ValidationError: CompactProposedProjectCodeChangeSet changes.0 "
            "provide one bounded new file, catalog anchor, exact edit, or anchored edit"
        )
    })
    (job / "job.json").write_text(record.model_dump_json(indent=2), encoding="utf-8")
    (job / "work" / "development_verification_failure.txt").write_text(
        "Unity PlayMode locale mismatch", encoding="utf-8"
    )

    assert can_attempt_bounded_repair_resume(job) is True


def test_overlong_compact_reason_can_resume_from_preserved_candidate(tmp_path: Path) -> None:
    job = tmp_path / "overlong-compact-reason"
    _failed_job(job)
    record = JobRecord.model_validate_json((job / "job.json").read_text("utf-8"))
    record = record.model_copy(update={
        "message": (
            "ValidationError: CompactProposedProjectCodeChangeSet changes.0.reason "
            "String should have at most 500 characters"
        )
    })
    (job / "job.json").write_text(record.model_dump_json(indent=2), encoding="utf-8")
    (job / "work" / "development_verification_failure.txt").write_text(
        "Unity PlayMode locale mismatch", encoding="utf-8"
    )

    assert can_attempt_bounded_repair_resume(job) is True
