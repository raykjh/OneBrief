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
