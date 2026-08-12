import json
from pathlib import Path

from onebrief.project_catalog import RegisteredProject
from onebrief.request_reuse import (
    find_reuse_candidate,
    request_fingerprint,
    seed_reusable_artifacts,
)
from onebrief.schemas import (
    IntakeRequest,
    InternalSource,
    OutputTarget,
    SourcePriority,
    ToolPackId,
)


def _intake() -> IntakeRequest:
    return IntakeRequest(
        goal="  Improve   the Exchange web app ",
        output_target=OutputTarget.EXISTING_PROJECT,
        existing_project_id="exchange",
        desired_output="Working web application",
        public_research_allowed=True,
        toolpack_ids=[ToolPackId.EXCHANGE_DEVELOPMENT],
        internal_sources=[
            InternalSource(
                name="Rules.md",
                priority=SourcePriority.MANDATORY,
                content="Use only live data.",
            )
        ],
    )


def _project(head: str, ready: bool = True) -> RegisteredProject:
    return RegisteredProject(
        project_id="exchange",
        name="Exchange Flow",
        summary="An FX web application.",
        root_path="C:/exchange",
        project_type="web",
        branch="main",
        head_sha=head,
        worktree_status="clean" if ready else "modified",
        ready_for_isolated_edit=ready,
        toolpack_id=ToolPackId.EXCHANGE_DEVELOPMENT,
    )


def test_fingerprint_normalizes_whitespace_and_case() -> None:
    first = _intake()
    second = first.model_copy(update={"goal": "improve the exchange WEB APP"})

    assert request_fingerprint(first) == request_fingerprint(second)


def test_reuses_grounded_artifacts_and_only_compatible_code(tmp_path: Path) -> None:
    head = "a" * 40
    old = tmp_path / "old-job"
    (old / "inputs").mkdir(parents=True)
    (old / "work" / "toolpacks" / "exchange_development" / "evidence").mkdir(parents=True)
    (old / "inputs" / "intake.json").write_text(
        _intake().model_dump_json(indent=2), encoding="utf-8"
    )
    (old / "job.json").write_text(
        json.dumps({"job_id": "old-job", "status": "failed"}), encoding="utf-8"
    )
    inspection_path = (
        old / "work" / "toolpacks" / "exchange_development" / "evidence"
        / "repository_inspection.json"
    )
    inspection_path.write_text(json.dumps({"head_sha": head}), encoding="utf-8")
    (old / "work" / "analysis.json").write_text('{"grounded":true}', encoding="utf-8")
    (old / "work" / "code_change_set_retry_r1.json").write_text(
        '{"changes":[]}', encoding="utf-8"
    )
    (old / "work" / "development").mkdir()
    (old / "work" / "development" / "development_run.json").write_text(
        '{"status":"verified"}', encoding="utf-8"
    )

    candidate = find_reuse_candidate(tmp_path, _intake(), _project(head))

    assert candidate is not None
    assert candidate.reusable_artifacts == (
        "analysis.json",
        "code_change_set_retry_r1.json",
    )

    new_job = tmp_path / "new-job"
    (new_job / "work").mkdir(parents=True)
    manifest = seed_reusable_artifacts(candidate, new_job)
    assert (new_job / "work" / "analysis.json").is_file()
    assert (new_job / "work" / "code_change_set.json").is_file()
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    assert payload["source_job_id"] == "old-job"


def test_changed_project_never_reuses_code_patch(tmp_path: Path) -> None:
    head = "a" * 40
    old = tmp_path / "old-job"
    (old / "inputs").mkdir(parents=True)
    evidence = old / "work" / "toolpacks" / "exchange_development" / "evidence"
    evidence.mkdir(parents=True)
    (old / "inputs" / "intake.json").write_text(_intake().model_dump_json(), encoding="utf-8")
    (old / "job.json").write_text('{"job_id":"old-job","status":"failed"}', encoding="utf-8")
    (evidence / "repository_inspection.json").write_text(
        json.dumps({"head_sha": head}), encoding="utf-8"
    )
    (old / "work" / "analysis.json").write_text("{}", encoding="utf-8")
    (old / "work" / "code_change_set.json").write_text("{}", encoding="utf-8")

    candidate = find_reuse_candidate(tmp_path, _intake(), _project("b" * 40))

    assert candidate is not None
    assert candidate.reusable_artifacts == ("analysis.json",)


def test_generic_project_inspection_allows_compatible_code_reuse(tmp_path: Path) -> None:
    head = "c" * 40
    intake = _intake().model_copy(update={
        "existing_project_id": "generic-node",
        "toolpack_ids": [ToolPackId.PROJECT_DEVELOPMENT],
    })
    old = tmp_path / "old-job"
    (old / "inputs").mkdir(parents=True)
    evidence = old / "work" / "toolpacks" / "project_development" / "evidence"
    evidence.mkdir(parents=True)
    (old / "inputs" / "intake.json").write_text(intake.model_dump_json(), encoding="utf-8")
    (old / "job.json").write_text('{"job_id":"old-job","status":"failed"}', encoding="utf-8")
    (evidence / "repository_inspection.json").write_text(
        json.dumps({"head_sha": head}), encoding="utf-8"
    )
    (old / "work" / "code_change_set.json").write_text('{"changes":[]}', encoding="utf-8")
    (old / "work" / "development").mkdir()
    (old / "work" / "development" / "development_run.json").write_text(
        '{"status":"verified"}', encoding="utf-8"
    )
    project = _project(head).model_copy(update={
        "project_id": "generic-node",
        "toolpack_id": ToolPackId.PROJECT_DEVELOPMENT,
    })

    candidate = find_reuse_candidate(tmp_path, intake, project)

    assert candidate is not None
    assert candidate.reusable_artifacts == ("code_change_set.json",)


def test_unverified_code_proposal_is_never_reused(tmp_path: Path) -> None:
    head = "d" * 40
    old = tmp_path / "old-job"
    (old / "inputs").mkdir(parents=True)
    evidence = old / "work" / "toolpacks" / "exchange_development" / "evidence"
    evidence.mkdir(parents=True)
    (old / "inputs" / "intake.json").write_text(
        _intake().model_dump_json(), encoding="utf-8"
    )
    (old / "job.json").write_text(
        '{"job_id":"old-job","status":"failed"}', encoding="utf-8"
    )
    (evidence / "repository_inspection.json").write_text(
        json.dumps({"head_sha": head}), encoding="utf-8"
    )
    (old / "work" / "analysis.json").write_text("{}", encoding="utf-8")
    (old / "work" / "code_change_set.json").write_text(
        '{"changes":[{"path":"scripts/server.mjs"}]}', encoding="utf-8"
    )

    candidate = find_reuse_candidate(tmp_path, _intake(), _project(head))

    assert candidate is not None
    assert candidate.reusable_artifacts == ("analysis.json",)


def test_current_failed_candidate_is_reused_without_numbered_round_pair(
    tmp_path: Path,
) -> None:
    head = "f" * 40
    old = tmp_path / "current-failed-job"
    (old / "inputs").mkdir(parents=True)
    evidence = old / "work" / "toolpacks" / "exchange_development" / "evidence"
    evidence.mkdir(parents=True)
    (old / "inputs" / "intake.json").write_text(
        _intake().model_dump_json(), encoding="utf-8"
    )
    (old / "job.json").write_text(
        '{"job_id":"current-failed-job","status":"failed"}', encoding="utf-8"
    )
    (evidence / "repository_inspection.json").write_text(
        json.dumps({"head_sha": head}), encoding="utf-8"
    )
    (old / "work" / "code_change_set.json").write_text(
        '{"summary":"current"}', encoding="utf-8"
    )
    (old / "work" / "development_verification_failure.txt").write_text(
        "Unity visual evidence requires a distinct rendered scenario for settings",
        encoding="utf-8",
    )

    candidate = find_reuse_candidate(tmp_path, _intake(), _project(head))

    assert candidate is not None
    assert candidate.reusable_artifacts == (
        "code_change_set.json",
        "development_verification_failure.txt",
    )


def test_analysis_only_preferred_job_never_outranks_compatible_code(
    tmp_path: Path,
) -> None:
    head = "9" * 40
    for name, with_code in (("code-job", True), ("analysis-only", False)):
        job = tmp_path / name
        (job / "inputs").mkdir(parents=True)
        evidence = job / "work" / "toolpacks" / "exchange_development" / "evidence"
        evidence.mkdir(parents=True)
        (job / "inputs" / "intake.json").write_text(
            _intake().model_dump_json(), encoding="utf-8"
        )
        (job / "job.json").write_text(
            json.dumps({"job_id": name, "status": "failed"}), encoding="utf-8"
        )
        (evidence / "repository_inspection.json").write_text(
            json.dumps({"head_sha": head}), encoding="utf-8"
        )
        (job / "work" / "analysis.json").write_text("{}", encoding="utf-8")
        if with_code:
            (job / "work" / "code_change_set.json").write_text(
                '{"summary":"checkpoint"}', encoding="utf-8"
            )
            (job / "work" / "development_verification_failure.txt").write_text(
                "development verification failed: unity_playmode_visual_tests",
                encoding="utf-8",
            )

    candidate = find_reuse_candidate(
        tmp_path,
        _intake(),
        _project(head),
        preferred_job_dir=tmp_path / "analysis-only",
    )

    assert candidate is not None
    assert candidate.job_id == "code-job"


def test_reuse_prefers_playmode_checkpoint_over_newer_static_failure(
    tmp_path: Path,
) -> None:
    head = "e" * 40
    for name, failure in (
        (
            "playmode-job",
            "development verification failed: unity_playmode_visual_tests UNITY TEST FAILURES",
        ),
        (
            "newer-static-job",
            "development verification failed: Unity visual test contract rejected",
        ),
    ):
        job = tmp_path / name
        (job / "inputs").mkdir(parents=True)
        evidence = job / "work" / "toolpacks" / "exchange_development" / "evidence"
        evidence.mkdir(parents=True)
        (job / "inputs" / "intake.json").write_text(
            _intake().model_dump_json(), encoding="utf-8"
        )
        (job / "job.json").write_text(
            json.dumps({"job_id": name, "status": "failed"}), encoding="utf-8"
        )
        (evidence / "repository_inspection.json").write_text(
            json.dumps({"head_sha": head}), encoding="utf-8"
        )
        (job / "work" / "development_best_candidate.json").write_text(
            json.dumps({"summary": name}), encoding="utf-8"
        )
        (job / "work" / "development_best_failure.txt").write_text(
            failure, encoding="utf-8"
        )
        (job / "job.json").touch()

    candidate = find_reuse_candidate(tmp_path, _intake(), _project(head))

    assert candidate is not None
    assert candidate.job_id == "playmode-job"
    preferred = find_reuse_candidate(
        tmp_path,
        _intake(),
        _project(head),
        preferred_job_dir=tmp_path / "newer-static-job",
    )
    assert preferred is not None
    assert preferred.job_id == "playmode-job"
    target = tmp_path / "new-job"
    (target / "work").mkdir(parents=True)
    seed_reusable_artifacts(candidate, target)
    assert json.loads((target / "work" / "code_change_set.json").read_text("utf-8"))[
        "summary"
    ] == "playmode-job"
    assert "unity_playmode_visual_tests" in (
        target / "work" / "development_verification_failure.txt"
    ).read_text("utf-8")
    marker = json.loads(
        (target / "work" / "reverify_existing_candidate.json").read_text("utf-8")
    )
    assert marker["source_job_id"] == "playmode-job"
    assert marker["candidate_sha256"]
