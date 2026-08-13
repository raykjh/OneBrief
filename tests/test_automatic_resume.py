import hashlib
import json
from pathlib import Path

import pytest

from onebrief.automatic_resume import (
    AutomaticResumePlan,
    TRUSTED_REVALIDATION_VERSION,
    _continuation_budget_estimate,
    _most_progressed_development_pair,
    can_attempt_automatic_resume,
    can_attempt_bounded_repair_resume,
    can_attempt_structural_resume,
    create_bounded_repair_resume,
    _trusted_semantic_failure_receipt,
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


def test_continuation_budget_can_use_less_than_the_full_run_minimum() -> None:
    original = _estimate().model_copy(update={
        "minimum_cost_usd": 0.99,
        "recommended_cost_usd": 2.0,
        "maximum_cost_usd": 4.0,
        "recommended_approval_usd": 2.0,
    })

    continuation = _continuation_budget_estimate(original, 0.88)

    assert continuation.minimum_cost_usd == 0.88
    assert continuation.recommended_cost_usd == 0.88
    assert continuation.maximum_cost_usd == 0.88
    assert continuation.recommended_approval_usd == 0.88
    assert continuation.budget_limit_usd == 0.88


def test_digest_bound_operator_recovery_precedes_progress_heuristics(tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    recovered = work / "code_change_set.json"
    recovered.write_text('{"candidate":"pre-regression"}', encoding="utf-8")
    failure = work / "development_verification_failure.txt"
    failure.write_text(
        "development verification failed: revalidate with corrected validator",
        encoding="utf-8",
    )
    (work / "code_change_set_r9.json").write_text(
        '{"candidate":"later-but-regressed"}', encoding="utf-8"
    )
    (work / "development_verification_failure_r9.txt").write_text(
        "independent Unity semantic visual observation failed: severe clipping",
        encoding="utf-8",
    )
    (work / "operator_recovery.json").write_text(json.dumps({
        "schema_version": "onebrief-operator-recovery-v1",
        "candidate_sha256": hashlib.sha256(recovered.read_bytes()).hexdigest(),
        "validator_commit": "4e3cec6",
    }), encoding="utf-8")

    candidate, selected_failure = _most_progressed_development_pair(work)

    assert candidate == recovered
    assert selected_failure == failure


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


def test_truncated_verifier_can_reuse_digest_bound_verified_development(
    tmp_path: Path,
) -> None:
    job = tmp_path / "job-verifier-json"
    _failed_job(job)
    record = JobRecord.model_validate_json((job / "job.json").read_text(encoding="utf-8"))
    record = record.model_copy(update={
        "message": "ValidationError: VerificationReport Invalid JSON: EOF while parsing"
    })
    (job / "job.json").write_text(record.model_dump_json(indent=2), encoding="utf-8")
    change_path = job / "work" / "code_change_set.json"
    development = job / "work" / "development"
    development.mkdir()
    run = {
        "status": "verified",
        "repository_name": "sample",
        "base_head_sha": "a" * 40,
        "summary": "Verified candidate",
        "changed_paths": ["Assets/Test.cs"],
        "commands": [],
        "patch_path": "candidate.patch",
        "evidence_paths": [],
        "safety_boundary": ["isolated"],
    }
    (development / "development_run.json").write_text(
        json.dumps(run), encoding="utf-8"
    )
    (job / "work" / "automatic_resume.json").write_text(json.dumps({
        "revalidated_change_sha256": hashlib.sha256(change_path.read_bytes()).hexdigest(),
    }), encoding="utf-8")

    assert can_attempt_automatic_resume(job) is True

    (job / "work" / "automatic_resume.json").write_text(json.dumps({
        "revalidated_change_sha256": "0" * 64,
    }), encoding="utf-8")
    assert can_attempt_automatic_resume(job) is False


def test_missing_observation_for_verified_seed_is_automatic_not_user_information(
    tmp_path: Path,
) -> None:
    job = tmp_path / "job-missing-observer"
    _failed_job(job)
    record = JobRecord.model_validate_json((job / "job.json").read_text(encoding="utf-8"))
    record = record.model_copy(update={
        "status": JobStatus.NEEDS_INFORMATION,
        "message": "Policy guard: no independent semantic observer and no visual evidence.",
    })
    (job / "job.json").write_text(record.model_dump_json(indent=2), encoding="utf-8")
    change_path = job / "work" / "code_change_set.json"
    development = job / "work" / "development"
    development.mkdir()
    (development / "development_run.json").write_text(json.dumps({
        "status": "verified",
        "repository_name": "sample",
        "base_head_sha": "a" * 40,
        "summary": "Verified candidate",
        "changed_paths": [],
        "commands": [],
        "patch_path": "candidate.patch",
        "evidence_paths": ["old_revalidation/unity_visual_evidence"],
        "safety_boundary": ["isolated"],
    }), encoding="utf-8")
    (job / "work" / "automatic_resume.json").write_text(json.dumps({
        "revalidated_change_sha256": hashlib.sha256(change_path.read_bytes()).hexdigest(),
    }), encoding="utf-8")

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
    (work / "convergence_ledger.json").write_text(
        '{"schema_version":"onebrief-convergence-ledger-v1","observations":['
        '{"observation_id":"prior-terminal-observation"}],"repair_contracts":['
        '{"execution_allowed":false}]}',
        encoding="utf-8",
    )
    (work / "repair_contract.json").write_text(
        '{"schema_version":"onebrief-repair-contract-v1","execution_allowed":true}',
        encoding="utf-8",
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
    assert (child / "work" / "convergence_ledger.json").is_file()
    assert (child / "work" / "repair_contract.json").is_file()
    marker = json.loads(
        (child / "work" / "reverify_existing_candidate.json").read_text("utf-8")
    )
    assert marker["source_job_id"] == source.name
    assert marker["candidate_sha256"]
    assert plan.remaining_approved_usd == 0.01
    assert plan.cumulative_actual_usd == 0.002
    assert plan.aggregate_approval_ceiling_usd == 0.012
    assert can_attempt_bounded_repair_resume(source) is False
    with pytest.raises(RuntimeError, match="not eligible"):
        create_bounded_repair_resume(source, tmp_path / "jobs")


def test_bounded_resume_discards_stale_non_executable_contract(tmp_path: Path) -> None:
    source = tmp_path / "source-stale-contract"
    source.mkdir()
    ledger = BudgetStore(source / "run").approve(_estimate(), 0.01)
    record = JobRecord(
        job_id=source.name,
        status=JobStatus.FAILED,
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:01+00:00",
        attempts=1,
        current_stage="failed",
        message=(
            "RuntimeError: convergence progress gate requires a new diagnosis "
            "before resume"
        ),
        run_id=ledger.run_id,
    )
    (source / "job.json").write_text(record.model_dump_json(indent=2), encoding="utf-8")
    inputs = source / "inputs"
    inputs.mkdir()
    intake = IntakeRequest(goal="Improve the Unity project.", public_research_allowed=True)
    requirements = RequirementsAnalysis(
        supported=True,
        support_reason="Ready.",
        normalized_goal=intake.goal,
        deliverables=["Result"],
        mandatory_information=[],
        optional_information=[],
        acceptance_criteria=["Runtime evidence matches the PNG."],
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
        "Unity visual scenario login_desktop viewport width does not match its PNG",
        encoding="utf-8",
    )
    (work / "convergence_ledger.json").write_text(
        '{"schema_version":"onebrief-convergence-ledger-v1","observations":[],"repair_contracts":[]}',
        encoding="utf-8",
    )
    (work / "repair_contract.json").write_text(
        '{"schema_version":"onebrief-repair-contract-v1","execution_allowed":false}',
        encoding="utf-8",
    )

    assert can_attempt_bounded_repair_resume(source) is True
    child, _plan = create_bounded_repair_resume(source, tmp_path / "jobs")

    assert (child / "work" / "convergence_ledger.json").is_file()
    assert not (child / "work" / "repair_contract.json").exists()
    resumed_ledger = json.loads(
        (child / "work" / "convergence_ledger.json").read_text("utf-8")
    )
    assert resumed_ledger["observations"] == []
    assert resumed_ledger["repair_contracts"] == []


def test_static_unity_topology_resume_skips_duplicate_revalidation_and_stale_gate(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source-topology"
    source.mkdir()
    ledger = BudgetStore(source / "run").approve(_estimate(), 0.01)
    record = JobRecord(
        job_id=source.name,
        status=JobStatus.FAILED,
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:01+00:00",
        attempts=1,
        current_stage="failed",
        message=(
            "RuntimeError: convergence progress gate stopped verification without new evidence"
        ),
        run_id=ledger.run_id,
    )
    (source / "job.json").write_text(record.model_dump_json(indent=2), encoding="utf-8")
    inputs = source / "inputs"
    inputs.mkdir()
    intake = IntakeRequest(goal="Improve the Unity project.", public_research_allowed=True)
    requirements = RequirementsAnalysis(
        supported=True, support_reason="Ready.", normalized_goal=intake.goal,
        deliverables=["Result"], mandatory_information=[], optional_information=[],
        acceptance_criteria=["PlayMode evidence passes."], assumptions=[],
        consolidated_questions=[], ready_for_estimate=True,
    )
    (inputs / "intake.json").write_text(intake.model_dump_json(), encoding="utf-8")
    (inputs / "requirements.json").write_text(
        requirements.model_dump_json(), encoding="utf-8"
    )
    (inputs / "sources.json").write_text("[]", encoding="utf-8")
    (inputs / "budget_estimate.json").write_text(
        _estimate().model_dump_json(), encoding="utf-8"
    )
    work = source / "work"
    work.mkdir()
    (work / "analysis.json").write_text('{"ready":true}', encoding="utf-8")
    (work / "code_change_set.json").write_text('{"candidate":true}', encoding="utf-8")
    (work / "development_verification_failure.txt").write_text(
        "development verification failed: Unity visual test contract: add a discoverable "
        "Unity PlayMode test whose namespace/full name begins with OneBrief.Visual | "
        "Unity visual test contract: add a Unity test .asmdef with optionalUnityReferences "
        "containing TestAssemblies",
        encoding="utf-8",
    )
    (work / "convergence_ledger.json").write_text(
        '{"schema_version":"onebrief-convergence-ledger-v1","observations":[],"repair_contracts":[]}',
        encoding="utf-8",
    )
    (work / "repair_contract.json").write_text(
        '{"schema_version":"onebrief-repair-contract-v1","execution_allowed":false}',
        encoding="utf-8",
    )
    provenance = work / "project_snapshot" / "restore_evidence.json"
    provenance.parent.mkdir()
    provenance.write_text(json.dumps({
        "status": "verified_and_approved",
        "source_head_sha": "a" * 40,
        "source_toolpack_sha256": "b" * 64,
    }), encoding="utf-8")

    assert can_attempt_bounded_repair_resume(source) is True
    child, _plan = create_bounded_repair_resume(source, tmp_path / "jobs")

    marker = json.loads(
        (child / "work" / "reverify_existing_candidate.json").read_text("utf-8")
    )
    assert marker["validator_version"] == TRUSTED_REVALIDATION_VERSION
    assert not (child / "work" / "trusted_reused_verification.json").exists()
    assert not (child / "work" / "convergence_ledger.json").exists()
    assert not (child / "work" / "repair_contract.json").exists()


def test_batchmode_framebuffer_resume_discards_stale_convergence_gate(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source-framebuffer-topology"
    source.mkdir()
    ledger = BudgetStore(source / "run").approve(_estimate(), 0.01)
    record = JobRecord(
        job_id=source.name,
        status=JobStatus.FAILED,
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:01+00:00",
        attempts=1,
        current_stage="failed",
        message="RuntimeError: development verification failed",
        run_id=ledger.run_id,
    )
    (source / "job.json").write_text(record.model_dump_json(indent=2), encoding="utf-8")
    inputs = source / "inputs"
    inputs.mkdir()
    intake = IntakeRequest(goal="Improve the Unity project.", public_research_allowed=True)
    requirements = RequirementsAnalysis(
        supported=True, support_reason="Ready.", normalized_goal=intake.goal,
        deliverables=["Result"], mandatory_information=[], optional_information=[],
        acceptance_criteria=["PlayMode evidence passes."], assumptions=[],
        consolidated_questions=[], ready_for_estimate=True,
    )
    (inputs / "intake.json").write_text(intake.model_dump_json(), encoding="utf-8")
    (inputs / "requirements.json").write_text(
        requirements.model_dump_json(), encoding="utf-8"
    )
    (inputs / "sources.json").write_text("[]", encoding="utf-8")
    (inputs / "budget_estimate.json").write_text(
        _estimate().model_dump_json(), encoding="utf-8"
    )
    work = source / "work"
    work.mkdir()
    (work / "analysis.json").write_text('{"ready":true}', encoding="utf-8")
    (work / "code_change_set.json").write_text('{"candidate":true}', encoding="utf-8")
    (work / "development_verification_failure.txt").write_text(
        "development verification failed: UNITY TEST FAILURES: ReadPixels was called "
        "to read pixels from system frame buffer, while not inside drawing frame.",
        encoding="utf-8",
    )
    (work / "convergence_ledger.json").write_text(
        '{"schema_version":"onebrief-convergence-ledger-v1","observations":[],"repair_contracts":[]}',
        encoding="utf-8",
    )
    (work / "repair_contract.json").write_text(
        '{"schema_version":"onebrief-repair-contract-v1","execution_allowed":false}',
        encoding="utf-8",
    )

    assert can_attempt_bounded_repair_resume(source) is True
    child, _plan = create_bounded_repair_resume(source, tmp_path / "jobs")

    assert (child / "work" / "reverify_existing_candidate.json").is_file()
    assert not (child / "work" / "convergence_ledger.json").exists()
    assert not (child / "work" / "repair_contract.json").exists()


def test_bounded_resume_prefers_playmode_checkpoint_over_stale_static_best(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "source-progressed"
    source.mkdir()
    ledger = BudgetStore(source / "run").approve(_estimate(), 0.01)
    BudgetStore(source / "run").fail("development verification failed")
    record = JobRecord(
        job_id=source.name,
        status=JobStatus.FAILED,
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:01+00:00",
        attempts=1,
        current_stage="failed",
        message="RuntimeError: development verification failed",
        run_id=ledger.run_id,
    )
    (source / "job.json").write_text(record.model_dump_json(indent=2), encoding="utf-8")
    inputs = source / "inputs"
    inputs.mkdir()
    intake = IntakeRequest(goal="Improve Unity UI.", public_research_allowed=False)
    requirements = RequirementsAnalysis(
        supported=True,
        support_reason="Ready.",
        normalized_goal=intake.goal,
        deliverables=["Result"],
        mandatory_information=[],
        optional_information=[],
        acceptance_criteria=["PlayMode passes."],
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
    (work / "development_best_candidate.json").write_text('{"candidate":"static"}', encoding="utf-8")
    (work / "development_best_failure.txt").write_text(
        "development verification failed: Unity visual test contract rejected",
        encoding="utf-8",
    )
    (work / "code_change_set.json").write_text('{"candidate":"static"}', encoding="utf-8")
    (work / "development_verification_failure.txt").write_text(
        "development verification failed: Unity visual test contract rejected",
        encoding="utf-8",
    )
    (work / "code_change_set_r3.json").write_text('{"candidate":"playmode"}', encoding="utf-8")
    (work / "development_verification_failure_r3.txt").write_text(
        "development verification failed: unity_playmode_visual_tests UNITY TEST FAILURES",
        encoding="utf-8",
    )
    child = tmp_path / "jobs" / "child-progressed"

    def fake_create_job(**kwargs):
        child.mkdir(parents=True)
        (child / "work").mkdir()
        (child / "job.json").write_text(
            JobRecord(
                job_id=child.name,
                status=JobStatus.QUEUED,
                created_at="2026-01-01T00:00:02+00:00",
                updated_at="2026-01-01T00:00:02+00:00",
                attempts=0,
                run_id="child-run",
            ).model_dump_json(indent=2),
            encoding="utf-8",
        )
        return child

    monkeypatch.setattr("onebrief.automatic_resume.create_job", fake_create_job)
    created, _plan = create_bounded_repair_resume(source, tmp_path / "jobs")

    assert created == child
    assert json.loads((child / "work" / "code_change_set.json").read_text("utf-8")) == {
        "candidate": "playmode"
    }
    assert "unity_playmode_visual_tests" in (
        child / "work" / "development_verification_failure.txt"
    ).read_text("utf-8")


def test_missing_system_visual_observer_resumes_latest_runtime_candidate(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "source-observer"
    source.mkdir()
    ledger = BudgetStore(source / "run").approve(_estimate(), 0.01)
    record = JobRecord(
        job_id=source.name,
        status=JobStatus.NEEDS_INFORMATION,
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:01+00:00",
        attempts=1,
        current_stage="finished",
        message=(
            "The visual and layout criteria require independent review which has not been performed."
        ),
        run_id=ledger.run_id,
    )
    (source / "job.json").write_text(record.model_dump_json(indent=2), encoding="utf-8")
    inputs = source / "inputs"
    inputs.mkdir()
    intake = IntakeRequest(goal="Modernize Unity UI on mobile and desktop.")
    required = RequirementsAnalysis(
        supported=True,
        support_reason="Ready.",
        normalized_goal=intake.goal,
        deliverables=["Unity result"],
        mandatory_information=[],
        optional_information=[],
        acceptance_criteria=["Rendered UI passes independent review."],
        assumptions=[],
        consolidated_questions=[],
        ready_for_estimate=True,
    )
    (inputs / "intake.json").write_text(intake.model_dump_json(), encoding="utf-8")
    (inputs / "requirements.json").write_text(required.model_dump_json(), encoding="utf-8")
    (inputs / "sources.json").write_text("[]", encoding="utf-8")
    (inputs / "budget_estimate.json").write_text(_estimate().model_dump_json(), encoding="utf-8")
    work = source / "work"
    (work / "development" / "unity_visual_evidence").mkdir(parents=True)
    (work / "development" / "unity_visual_evidence" / "summary.json").write_text(
        '{"screenshot_paths":["screenshots/desktop.png"]}', encoding="utf-8"
    )
    (work / "code_change_set.json").write_text('{"candidate":"latest"}', encoding="utf-8")
    (work / "development_best_candidate.json").write_text(
        '{"candidate":"stale"}', encoding="utf-8"
    )
    (work / "development_best_failure.txt").write_text("old static failure", encoding="utf-8")
    child = tmp_path / "jobs" / "observer-child"

    def fake_create_job(**kwargs):
        child.mkdir(parents=True)
        (child / "work").mkdir()
        (child / "job.json").write_text(
            JobRecord(
                job_id=child.name,
                status=JobStatus.QUEUED,
                created_at="2026-01-01T00:00:02+00:00",
                updated_at="2026-01-01T00:00:02+00:00",
                attempts=0,
                run_id="child-run",
            ).model_dump_json(indent=2),
            encoding="utf-8",
        )
        return child

    monkeypatch.setattr("onebrief.automatic_resume.create_job", fake_create_job)

    assert can_attempt_bounded_repair_resume(source) is True
    created, plan = create_bounded_repair_resume(source, tmp_path / "jobs")

    assert created == child
    assert json.loads((child / "work" / "code_change_set.json").read_text("utf-8")) == {
        "candidate": "latest"
    }
    assert "semantic visual PASS" in (
        child / "work" / "development_verification_failure.txt"
    ).read_text("utf-8")
    assert "system_observation_failure" in plan.reused_artifacts


def test_most_progressed_pair_can_recover_a_better_ancestor_checkpoint(tmp_path: Path) -> None:
    jobs = tmp_path / "jobs"
    parent = jobs / "parent"
    child = jobs / "child"
    parent_work = parent / "work"
    child_work = child / "work"
    parent_work.mkdir(parents=True)
    child_work.mkdir(parents=True)
    (parent_work / "development_best_candidate.json").write_text(
        '{"candidate":"camera-backed"}', encoding="utf-8"
    )
    (parent_work / "development_best_failure.txt").write_text(
        "development verification failed: Unity visual test contract: a camera RenderTexture "
        "does not capture ScreenSpaceOverlay UI | Unity visual test contract: responsive Unity "
        "batchmode evidence must pass each requested viewport width and height directly into capture",
        encoding="utf-8",
    )
    (child_work / "development_best_candidate.json").write_text(
        '{"candidate":"system-framebuffer"}', encoding="utf-8"
    )
    (child_work / "development_best_failure.txt").write_text(
        "development verification failed: Unity visual test contract: Texture2D.ReadPixels "
        "must not read the system framebuffer in Unity batchmode",
        encoding="utf-8",
    )
    (child_work / "continuation_manifest.json").write_text(
        json.dumps({"source_job_id": "parent"}), encoding="utf-8"
    )

    candidate, failure = _most_progressed_development_pair(child_work)

    assert candidate == parent_work / "development_best_candidate.json"
    assert "ScreenSpaceOverlay" in failure.read_text(encoding="utf-8")


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


def test_missing_unity_locale_evidence_can_resume_from_preserved_candidate(
    tmp_path: Path,
) -> None:
    job = tmp_path / "missing-locale"
    _failed_job(job)
    record = JobRecord.model_validate_json((job / "job.json").read_text("utf-8"))
    record = record.model_copy(update={
        "status": JobStatus.NEEDS_AUTHORIZATION,
        "message": "[WinError 32] file is being used by another process",
    })
    (job / "job.json").write_text(record.model_dump_json(indent=2), encoding="utf-8")
    (job / "work" / "development_verification_failure.txt").write_text(
        "Unity visual evidence did not exercise requested locale(s): es",
        encoding="utf-8",
    )

    assert can_attempt_bounded_repair_resume(job) is True


def test_trusted_semantic_failure_skips_only_unchanged_candidate_preflight(
    tmp_path: Path,
) -> None:
    work = tmp_path / "work"
    development = work / "development"
    evidence = development / "unity_visual_evidence"
    observations = work / "independent_observations"
    evidence.mkdir(parents=True)
    observations.mkdir()
    candidate = work / "code_change_set.json"
    candidate.write_text('{"candidate":"verified"}', encoding="utf-8")
    (development / "change_set.json").write_bytes(candidate.read_bytes())
    (development / "development_run.json").write_text(json.dumps({
        "status": "verified",
        "base_head_sha": "a" * 40,
    }), encoding="utf-8")
    (evidence / "summary.json").write_text(
        '{"screenshot_paths":["screenshots/lobby.png"]}', encoding="utf-8"
    )
    (observations / "unity_ui_observation.json").write_text(json.dumps({
        "status": "failed",
        "independent_from_maker": True,
    }), encoding="utf-8")
    failure = work / "development_verification_failure.txt"
    failure.write_text(
        "independent Unity semantic visual observation failed: lobby clipped",
        encoding="utf-8",
    )

    receipt = _trusted_semantic_failure_receipt(work, candidate, failure)

    assert receipt is not None
    assert receipt["candidate_sha256"] == hashlib.sha256(candidate.read_bytes()).hexdigest()
    chained = tmp_path / "chained-work"
    chained.mkdir()
    chained_candidate = chained / "code_change_set.json"
    chained_failure = chained / "development_verification_failure.txt"
    chained_candidate.write_bytes(candidate.read_bytes())
    chained_failure.write_bytes(failure.read_bytes())
    (chained / "trusted_reused_verification.json").write_text(
        json.dumps(receipt), encoding="utf-8"
    )
    inherited = _trusted_semantic_failure_receipt(
        chained, chained_candidate, chained_failure
    )
    assert inherited is not None
    assert inherited["inherited_receipt_sha256"]
    (development / "change_set.json").write_text(
        '{"candidate":"different"}', encoding="utf-8"
    )
    assert _trusted_semantic_failure_receipt(work, candidate, failure) is None


def test_failed_semantic_observation_returns_verified_candidate_to_maker(
    tmp_path: Path,
) -> None:
    job = tmp_path / "semantic-failure"
    _failed_job(job)
    record = JobRecord.model_validate_json((job / "job.json").read_text("utf-8"))
    record = record.model_copy(update={
        "message": "RuntimeError: independent Unity semantic visual observation failed"
    })
    (job / "job.json").write_text(record.model_dump_json(indent=2), encoding="utf-8")
    observations = job / "work" / "independent_observations"
    observations.mkdir()
    (observations / "unity_ui_observation.json").write_text(json.dumps({
        "status": "failed",
        "findings": ["Mobile lobby controls overlap.", "Espa□ol has a missing glyph."],
    }), encoding="utf-8")

    assert can_attempt_bounded_repair_resume(job) is True


def test_partial_identical_repair_candidate_can_resume_with_unused_budget(tmp_path: Path) -> None:
    job = tmp_path / "partial-identical-repair"
    _failed_job(job)
    record = JobRecord.model_validate_json((job / "job.json").read_text("utf-8"))
    record = record.model_copy(update={
        "status": JobStatus.PARTIAL,
        "message": (
            "Policy guard returned REVISE: deterministic verification rejected "
            "a repeating an identical repair candidate that previously failed."
        ),
    })
    (job / "job.json").write_text(record.model_dump_json(indent=2), encoding="utf-8")
    (job / "work" / "development_verification_failure.txt").write_text(
        "Unity visual test contract: synthetic UI is not evidence", encoding="utf-8"
    )

    assert can_attempt_bounded_repair_resume(job) is True


def test_phase_wallet_block_can_resume_with_unused_aggregate_budget(tmp_path: Path) -> None:
    job = tmp_path / "phase-wallet-block"
    _failed_job(job)
    record = JobRecord.model_validate_json((job / "job.json").read_text("utf-8"))
    record = record.model_copy(update={
        "status": JobStatus.NEEDS_BUDGET,
        "current_stage": "budget_gate",
        "message": (
            "call blocked before provider invocation: evidence_construction wallet "
            "would be exceeded; phase remaining $0.16; needs $0.22, remaining $4.12"
        ),
    })
    (job / "job.json").write_text(record.model_dump_json(indent=2), encoding="utf-8")
    (job / "work" / "development_verification_failure.txt").write_text(
        "development verification failed: Unity visual test contract rejected",
        encoding="utf-8",
    )

    assert can_attempt_bounded_repair_resume(job) is True


def test_stalled_distinct_unity_surface_evidence_can_resume(tmp_path: Path) -> None:
    job = tmp_path / "stalled-unity-surface"
    _failed_job(job)
    record = JobRecord.model_validate_json((job / "job.json").read_text("utf-8"))
    record = record.model_copy(update={
        "message": (
            "RuntimeError: development repair stalled after two identical candidates; "
            "the same maker must be resumed with a different repair strategy"
        ),
    })
    (job / "job.json").write_text(record.model_dump_json(indent=2), encoding="utf-8")
    (job / "work" / "development_verification_failure.txt").write_text(
        "Unity visual evidence requires a distinct rendered scenario for every requested real UI surface: settings",
        encoding="utf-8",
    )

    assert can_attempt_bounded_repair_resume(job) is True


def test_repeated_strategy_gate_can_revalidate_after_validator_fix(tmp_path: Path) -> None:
    job = tmp_path / "repeated-strategy"
    _failed_job(job)
    record = JobRecord.model_validate_json((job / "job.json").read_text("utf-8"))
    record = record.model_copy(update={
        "message": (
            "convergence progress gate blocked a repeated repair strategy: "
            "no new evidence"
        ),
    })
    (job / "job.json").write_text(record.model_dump_json(indent=2), encoding="utf-8")
    (job / "work" / "development_verification_failure.txt").write_text(
        "Unity visual evidence reused an identical screenshot for lobby and lobby_return",
        encoding="utf-8",
    )

    assert can_attempt_bounded_repair_resume(job) is True


def test_non_learning_promotion_gate_can_resume_after_runtime_improvement(tmp_path: Path) -> None:
    job = tmp_path / "non-learning-promotion"
    _failed_job(job)
    record = JobRecord.model_validate_json((job / "job.json").read_text("utf-8"))
    record = record.model_copy(update={
        "message": (
            "RuntimeError: convergence progress gate blocked a non-learning repair: "
            "The same causal failure and repair strategy produced no new evidence"
        ),
    })
    (job / "job.json").write_text(record.model_dump_json(indent=2), encoding="utf-8")
    (job / "work" / "development_verification_failure.txt").write_text(
        "Unity visual evidence reused an identical screenshot for lobby_desktop and lobby_mobile",
        encoding="utf-8",
    )

    assert can_attempt_bounded_repair_resume(job) is True


def test_interrupted_unity_process_revalidates_candidate_without_maker_repair(tmp_path: Path) -> None:
    job = tmp_path / "interrupted-unity"
    _failed_job(job)
    record = JobRecord.model_validate_json((job / "job.json").read_text("utf-8"))
    record = record.model_copy(update={
        "message": "RuntimeError: development verification failed: unity_playmode_visual_tests (exit_code=4294967295)",
    })
    (job / "job.json").write_text(record.model_dump_json(indent=2), encoding="utf-8")
    (job / "work" / "development_verification_failure.txt").write_text(
        "development verification failed: unity_playmode_visual_tests (exit_code=4294967295) package manager stopped",
        encoding="utf-8",
    )

    assert can_attempt_automatic_resume(job) is True
    assert can_attempt_bounded_repair_resume(job) is False


def test_false_context_authorization_gate_can_resume_after_hash_fix(tmp_path: Path) -> None:
    job = tmp_path / "context-retarget-authorization"
    _failed_job(job)
    record = JobRecord.model_validate_json((job / "job.json").read_text("utf-8"))
    record = record.model_copy(update={
        "status": JobStatus.NEEDS_AUTHORIZATION,
        "message": (
            "The approved capability boundary is insufficient: existing file was not "
            "included in approved model context: Assets/Existing.cs"
        ),
    })
    (job / "job.json").write_text(record.model_dump_json(indent=2), encoding="utf-8")
    (job / "work" / "development_verification_failure.txt").write_text(
        "Unity visual test contract: missing runtime evidence", encoding="utf-8"
    )

    assert can_attempt_bounded_repair_resume(job) is True


def test_exact_repair_shape_error_can_resume_after_schema_normalization(tmp_path: Path) -> None:
    job = tmp_path / "exact-repair-shape"
    _failed_job(job)
    record = JobRecord.model_validate_json((job / "job.json").read_text("utf-8"))
    record = record.model_copy(update={
        "message": "ValidationError: ExactRepairProjectCodeChangeSet invalid selector shape",
    })
    (job / "job.json").write_text(record.model_dump_json(indent=2), encoding="utf-8")
    (job / "work" / "development_verification_failure.txt").write_text(
        "Unity visual test contract: namespace/full name begins with OneBrief.Visual",
        encoding="utf-8",
    )

    assert can_attempt_bounded_repair_resume(job) is True


def test_overlong_anchored_range_can_resume_after_schema_normalization(tmp_path: Path) -> None:
    job = tmp_path / "anchored-range-too-long"
    _failed_job(job)
    record = JobRecord.model_validate_json((job / "job.json").read_text("utf-8"))
    record = record.model_copy(update={
        "message": (
            "ValidationError: AnchoredRangeRepairProjectCodeChangeSet "
            "changes.0.start_anchor String should have at most 1000 characters"
        ),
    })
    (job / "job.json").write_text(record.model_dump_json(indent=2), encoding="utf-8")
    (job / "work" / "development_verification_failure.txt").write_text(
        "Unity visual evidence reused an identical screenshot for lobby desktop and mobile",
        encoding="utf-8",
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
