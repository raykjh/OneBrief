from types import SimpleNamespace
from fastapi.testclient import TestClient
import pytest

from onebrief.producer import estimate_budget
from onebrief.preparation import build_preparation_plan
from onebrief.jobs import JobRecord, JobStatus
from onebrief.cloud_jobs import CloudExecutionReceipt
from onebrief.project_catalog import RegisteredProject
from onebrief.schemas import (
    IntakeRequest,
    OutputTarget,
    RequirementsAnalysis,
    SixSenseOption,
    SixSensePlan,
    SixSenseQuestion,
    ToolPackId,
)
from onebrief.toolpacks import attach_toolpack_descriptors
from onebrief.web_service import (
    ExecutionLink, InMemoryWebSessionStore, LocalWebSessionStore, WebSession, app, get_session_store,
    _local_project_cloud_configured, validate_approval,
)


def requirements(ready: bool) -> RequirementsAnalysis:
    missing = [] if ready else [{
        "key": "policy",
        "request": "Provide the authoritative policy.",
        "reason": "The agent cannot invent company rules.",
        "acceptable_evidence": ["Policy document"],
    }]
    return RequirementsAnalysis(
        supported=True,
        support_reason="The task is supported.",
        normalized_goal="Create a grounded policy summary.",
        deliverables=["Policy summary"],
        mandatory_information=missing,
        optional_information=[],
        acceptance_criteria=["Every claim is grounded."],
        assumptions=[],
        consolidated_questions=[] if ready else ["Please upload the authoritative policy."],
        ready_for_estimate=ready,
    )


def test_home_serves_the_real_workflow() -> None:
    response = TestClient(app).get("/")
    assert response.status_code == 200
    assert "OneBrief" in response.text
    assert 'class="identity-hero"' in response.text
    assert 'id="languageToggle"' in response.text
    assert 'localStorage.getItem("onebrief-language")||"en"' in response.text
    assert '"What should OneBrief complete?"' in response.text
    assert 'document.documentElement.lang=uiLanguage' in response.text
    assert 'id="completionContract"' in response.text
    assert 'id="qualityCriteria"' in response.text
    assert '.checks label:has(select[name=max_revision_rounds]){display:none}' in response.text
    assert "retrySelect.disabled=true" in response.text
    assert "/api/inspect" in response.text
    assert 'id="sixsensePanel"' in response.text
    assert 'id="sixsenseRecommended"' in response.text
    assert '+sid+"/sixsense"' in response.text
    assert "setTimeout(()=>{sixsenseIndex++" in response.text
    assert 'id="choiceMin"' in response.text
    assert 'id="choiceRec"' in response.text
    assert 'id="choiceMax"' in response.text
    assert 'name="budget_limit_usd"' not in response.text
    assert 'name="desired_output"' not in response.text
    assert 'id="approval" type="number" min=".01" max="10"' in response.text
    assert 'form.delete("uploads")' in response.text
    assert 'this.disabled=true;q("#status").textContent=""' in response.text
    assert 'runButton.disabled=!(activePreparation?.ready_for_authorization)' in response.text
    assert 'class="two-stage-steps"' in response.text
    assert 'id="authorizationPlan"' in response.text
    assert 'authorization_sha256' in response.text
    assert 'needs_authorization' in response.text
    assert 'id="amendRun"' in response.text
    assert '"/amend"' in response.text
    assert 'new URLSearchParams(location.search).get("prepare")' in response.text
    assert 'name="toolpack_ids"' not in response.text
    assert 'name="public_research_disabled"' in response.text
    assert 'id="dropZone"' in response.text
    assert "파일당 1MB · 전체 3MB" in response.text
    assert 'id="supplement"' in response.text
    assert 'id="supplementButton"' in response.text
    assert "/reinspect" in response.text
    assert "답변 반영하여 다시 검수" in response.text
    assert 'if(p.requirements.ready_for_estimate){q("#supplement").value=""}' in response.text
    assert "입력한 답변은 반영됐습니다" in response.text
    assert "/api/sessions/" in response.text and "/graph" in response.text
    assert 'id="criteriaList"' in response.text
    assert 'id="criteriaScore"' in response.text
    assert '"/criteria"' in response.text
    assert "계약 기준 " in response.text
    assert "최종 미완료" in response.text
    assert "에이전트 실행 흐름" in response.text
    assert 'new URLSearchParams(location.search).get("session")' in response.text
    assert "terminalWaits>=5" in response.text

    assert 'name="output_target"' in response.text
    assert "기존 프로젝트 개선" in response.text
    assert "웹프로그램" in response.text
    assert "확정 결과물" in response.text
    assert 'p.requirements.deliverables.join(" · ")' in response.text
    assert 'id="projectPicker"' in response.text
    assert 'id="projectGoalRevision"' in response.text
    assert 'id="projectSupplement"' in response.text
    assert 'id="applyProjectSupplement"' in response.text
    assert 'id="projectImport"' in response.text
    assert 'id="projectManifest"' in response.text
    assert 'id="importProject"' in response.text
    assert 'fetch("/api/projects/import"' in response.text
    assert 'id="selectProjectFolder"' in response.text
    assert 'id="folderDraftPanel"' in response.text
    assert 'id="registerProjectFolder"' in response.text
    assert 'id="toolpackPanel"' in response.text
    assert 'id="generateToolpack"' in response.text
    assert 'id="approveToolpack"' in response.text
    assert '/toolpack/generate' in response.text
    assert '/toolpack/approve' in response.text
    assert 'toolpack_sha256' in response.text
    assert 'fetch("/api/projects/pick-folder"' in response.text
    assert 'fetch("/api/projects/register-folder"' in response.text
    assert 'p.continuation?.canonical_goal' in response.text
    assert 'if(p.canonical_goal)q("#goal").value=p.canonical_goal' in response.text
    assert 'name="existing_project_id"' in response.text
    assert 'id="projectSearch"' in response.text
    assert 'id="repeatNotice"' in response.text
    assert 'id="continuationNotice"' in response.text
    assert 'id="projectLatestResult"' in response.text
    assert 'id="projectPreviewResult"' in response.text
    assert "기술자료 ZIP 다운로드" in response.text
    assert 'fetch("/api/projects?q="' in response.text
    assert "p.previous_attempt" in response.text
    assert 'id="applyResult"' in response.text
    assert "안전하게 프로젝트에 적용" in response.text
    assert "검토용 결과 ZIP" in response.text
    assert 'fetch("/api/sessions/"+sid+"/apply"' in response.text


def sixsense_plan() -> SixSensePlan:
    return SixSensePlan(
        standard_profile="Preserve the existing brand and use a conventional commercial game site.",
        questions=[
            SixSenseQuestion(
                question_id="S02",
                dimension="audience",
                prompt="Who should the homepage persuade first?",
                reason="This changes the information hierarchy.",
                options=[
                    SixSenseOption(
                        option_id="new_players",
                        label="New players",
                        decision="Prioritize new-player understanding.",
                        recommended=True,
                    ),
                    SixSenseOption(
                        option_id="existing_players",
                        label="Existing players",
                        decision="Prioritize existing-player updates.",
                    ),
                ],
            )
        ],
    )


def test_sixsense_is_prepared_once_then_confirmed_before_budget(monkeypatch) -> None:
    store = InMemoryWebSessionStore()
    first = requirements(True).model_copy(update={"sixsense": sixsense_plan()})
    completed = requirements(True).model_copy(
        update={"sixsense": sixsense_plan().model_copy(update={"questions": []})}
    )

    async def fake_inspect(_intake):
        return first

    async def fake_reinspect(intake, _previous, *, sixsense_completed=False):
        assert sixsense_completed is True
        assert "Prioritize new-player understanding." in intake.goal
        assert intake.internal_sources[-1].name.startswith("sixsense-decisions-")
        return completed

    monkeypatch.setattr("onebrief.web_service.inspect_requirements", fake_inspect)
    monkeypatch.setattr("onebrief.web_service.reinspect_requirements", fake_reinspect)
    app.dependency_overrides[get_session_store] = lambda: store
    try:
        inspected = TestClient(app).post(
            "/api/inspect",
            data={"goal": "Improve the existing game homepage."},
        )
        assert inspected.status_code == 200, inspected.text
        pending = inspected.json()
        assert pending["sixsense_pending"] is True
        assert pending["budget"] is None

        confirmed = TestClient(app).post(
            f'/api/sessions/{pending["session_id"]}/sixsense',
            json={"choices": [], "use_recommended": True},
        )
    finally:
        app.dependency_overrides.clear()

    assert confirmed.status_code == 200, confirmed.text
    payload = confirmed.json()
    assert payload["sixsense_pending"] is False
    assert payload["sixsense_confirmed"] is True
    assert payload["budget"] is not None
    assert "Prioritize new-player understanding." in payload["canonical_goal"]

@pytest.mark.parametrize("ready", [False, True])
def test_inspect_returns_questions_or_budget(monkeypatch, ready: bool) -> None:
    store = InMemoryWebSessionStore()

    async def fake_inspect(_intake):
        return requirements(ready)

    monkeypatch.setattr("onebrief.web_service.inspect_requirements", fake_inspect)
    app.dependency_overrides[get_session_store] = lambda: store
    try:
        response = TestClient(app).post(
            "/api/inspect",
            data={
                "goal": "Create a grounded policy summary.",
                "output_target": "web_app",
                "budget_limit_usd": "0.5",
            },
            files={"uploads": ("policy.md", b"Manager approval is required.", "text/markdown")},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    assert (payload["budget"] is not None) is ready
    assert payload["requirements"]["ready_for_estimate"] is ready
    session = store.read(payload["session_id"])
    assert session.intake.internal_sources[0].name == "policy.md"
    assert session.intake.output_target is OutputTarget.WEB_APP
    assert session.intake.public_research_allowed is True

def test_public_research_can_be_explicitly_disabled(monkeypatch) -> None:
    store = InMemoryWebSessionStore()

    async def fake_inspect(_intake):
        return requirements(False)

    monkeypatch.setattr("onebrief.web_service.inspect_requirements", fake_inspect)
    app.dependency_overrides[get_session_store] = lambda: store
    try:
        response = TestClient(app).post(
            "/api/inspect",
            data={
                "goal": "Create a private-only summary.",
                "public_research_disabled": "true",
            },
        )
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 200
    session = store.read(response.json()["session_id"])
    assert session.intake.public_research_allowed is False

def test_supplement_answers_are_reinspected_as_authoritative_input(monkeypatch) -> None:
    store = InMemoryWebSessionStore()
    store.create(
        WebSession(
            session_id="needs-answers",
            created_at="2026-08-06T00:00:00+00:00",
            intake=IntakeRequest(goal="Build a live currency research web application."),
            requirements=requirements(False),
        )
    )

    async def fake_reinspect(intake, previous):
        assert previous.ready_for_estimate is False
        assert intake.goal.startswith("Build a live currency research web application.")
        assert "[\ucd94\uac00 \ud655\uc815\u00b7\ubcf4\uc644\uc0ac\ud56d]" in intake.goal
        supplement = intake.internal_sources[-1]
        assert supplement.priority.value == "mandatory"
        assert supplement.requirement_keys == ["policy"]
        assert "무료 공개 API" in supplement.content
        assert "표준 기술 지표" in supplement.content
        return requirements(True)

    monkeypatch.setattr("onebrief.web_service.reinspect_requirements", fake_reinspect)
    app.dependency_overrides[get_session_store] = lambda: store
    try:
        response = TestClient(app).post(
            "/api/sessions/needs-answers/reinspect",
            data={
                "answers": (
                    "모의 데이터는 안 됨. 무료 공개 API를 사용하고, "
                    "표준 기술 지표를 적용해줘. 선호 프레임워크는 없음."
                )
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    assert payload["session_id"] != "needs-answers"
    assert payload["canonical_goal"].startswith("Build a live currency research web application.")
    assert "[\ucd94\uac00 \ud655\uc815\u00b7\ubcf4\uc644\uc0ac\ud56d]" in payload["canonical_goal"]
    assert payload["requirements"]["ready_for_estimate"] is True
    assert payload["budget"] is not None
    updated = store.read(payload["session_id"])
    assert updated.intake.goal == payload["canonical_goal"]
    assert len(updated.intake.internal_sources) == 1


@pytest.mark.parametrize(
    ("status", "ready", "has_budget"),
    [
        (JobStatus.NEEDS_BUDGET, True, True),
        (JobStatus.NEEDS_INFORMATION, False, False),
        (JobStatus.NEEDS_AUTHORIZATION, False, False),
    ],
)
def test_stopped_run_returns_to_stage_one_for_amendment(
    tmp_path, status: JobStatus, ready: bool, has_budget: bool
) -> None:
    store = InMemoryWebSessionStore()
    intake = IntakeRequest(goal="Complete the approved project.")
    analysis = requirements(True)
    budget = estimate_budget(intake, analysis)
    preparation = build_preparation_plan(intake, analysis, budget)
    store.create(WebSession(
        session_id="stopped-run",
        created_at="2026-08-09T00:00:00+00:00",
        intake=intake,
        requirements=analysis,
        budget=budget,
        preparation=preparation,
    ))
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    record = JobRecord(
        job_id="stopped-job",
        status=status,
        created_at="2026-08-09T00:00:00+00:00",
        updated_at="2026-08-09T00:01:00+00:00",
        attempts=1,
        current_stage="amendment_gate",
        message=f"Runtime stopped with {status.value}.",
        run_id="run",
    )
    (job_dir / "job.json").write_text(record.model_dump_json(), encoding="utf-8")
    store.save_execution(ExecutionLink(
        session_id="stopped-run",
        job_uri=str(job_dir),
        operation_name="local",
        created_at="2026-08-09T00:00:00+00:00",
    ))
    app.dependency_overrides[get_session_store] = lambda: store
    try:
        response = TestClient(app).post("/api/sessions/stopped-run/amend")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["session_id"] != "stopped-run"
    assert payload["parent_session_id"] == "stopped-run"
    assert payload["amendment_kind"] == status.value
    assert payload["requirements"]["ready_for_estimate"] is ready
    assert (payload["budget"] is not None) is has_budget
    if status is JobStatus.NEEDS_BUDGET:
        assert payload["preparation"]["authorization_sha256"] != preparation.authorization_sha256
    else:
        assert payload["preparation"] is None
        assert payload["requirements"]["mandatory_information"][-1]["key"].startswith("runtime_")

    persisted = TestClient(app)
    app.dependency_overrides[get_session_store] = lambda: store
    try:
        restored = persisted.get(f'/api/sessions/{payload["session_id"]}')
    finally:
        app.dependency_overrides.clear()
    assert restored.status_code == 200
    assert restored.json()["parent_session_id"] == "stopped-run"


def test_failed_repair_can_request_fresh_budget_without_discarding_candidate(
    tmp_path, monkeypatch
) -> None:
    store = InMemoryWebSessionStore()
    intake = IntakeRequest(goal="Complete the approved project.")
    analysis = requirements(True)
    budget = estimate_budget(intake, analysis)
    store.create(WebSession(
        session_id="failed-repair",
        created_at="2026-08-09T00:00:00+00:00",
        intake=intake,
        requirements=analysis,
        budget=budget,
        preparation=build_preparation_plan(intake, analysis, budget),
    ))
    job_dir = tmp_path / "failed-job"
    job_dir.mkdir()
    record = JobRecord(
        job_id="failed-job",
        status=JobStatus.FAILED,
        created_at="2026-08-09T00:00:00+00:00",
        updated_at="2026-08-09T00:01:00+00:00",
        attempts=1,
        current_stage="failed",
        message="development verification failed: Unity PlayMode assertion",
        run_id="run",
    )
    (job_dir / "job.json").write_text(record.model_dump_json(), encoding="utf-8")
    store.save_execution(ExecutionLink(
        session_id="failed-repair",
        job_uri=str(job_dir),
        operation_name="local",
        created_at="2026-08-09T00:00:00+00:00",
    ))
    candidate = SimpleNamespace(reusable_artifacts=(
        "development_best_candidate.json", "development_best_failure.txt"
    ))
    monkeypatch.setattr(
        "onebrief.web_service.find_reuse_candidate", lambda *_args, **_kwargs: candidate
    )
    app.dependency_overrides[get_session_store] = lambda: store
    try:
        response = TestClient(app).post("/api/sessions/failed-repair/amend")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["amendment_kind"] == "needs_budget"
    assert payload["budget"] is not None
    assert payload["previous_attempt"]["reusable_artifacts"] == [
        "development_best_candidate.json", "development_best_failure.txt"
    ]




def test_upload_limit_accepts_a_file_larger_than_the_previous_cap(monkeypatch) -> None:
    store = InMemoryWebSessionStore()
    async def fake_inspect(_intake):
        return requirements(False)
    monkeypatch.setattr("onebrief.web_service.inspect_requirements", fake_inspect)
    app.dependency_overrides[get_session_store] = lambda: store
    try:
        response = TestClient(app).post(
            "/api/inspect", data={"goal": "Summarize the supplied material."},
            files={"uploads": ("large.md", b"a" * 750_000, "text/markdown")},
        )
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 200
    payload = response.json()
    source = store.read(payload["session_id"]).intake.internal_sources[0]
    assert source.name == "large.md" and source.size_bytes == 750_000


def test_final_approval_replaces_the_preflight_budget_hint(monkeypatch) -> None:
    monkeypatch.setenv("ONEBRIEF_WEB_MAX_APPROVAL_USD", "0.10")
    session = WebSession(
        session_id="test",
        created_at="2026-08-05T00:00:00+00:00",
        intake=IntakeRequest(goal="Create a summary.", budget_limit_usd=0.05),
        requirements=requirements(True),
        budget={
            "price_card_version": "test", "price_source_url": "https://example.test",
            "endpoint": "global", "estimated_source_tokens": 0, "estimated_contract_tokens": 100,
            "stages": [], "minimum_cost_usd": 0.01, "recommended_cost_usd": 0.03,
            "maximum_cost_usd": 0.20, "recommended_approval_usd": 0.03,
            "budget_limit_usd": 0.05, "status": "within_budget",
            "estimated_minutes_minimum": 1, "estimated_minutes_recommended": 2,
            "estimated_minutes_maximum": 3, "notes": [],
        },
    )
    validate_approval(session, 0.10)
    with pytest.raises(ValueError):
        validate_approval(session, 0.1001)


@pytest.mark.parametrize(
    ("issued_at", "actor_id", "expected"),
    [
        ("2020-01-01T00:00:00+00:00", "local_user", "expired"),
        (None, "another_actor", "actor"),
    ],
)
def test_run_rejects_expired_or_wrong_actor_authorization(
    issued_at, actor_id, expected,
) -> None:
    store = InMemoryWebSessionStore()
    intake = IntakeRequest(goal="Create a grounded summary.", public_research_allowed=True)
    analysis = requirements(True)
    budget = estimate_budget(intake, analysis)
    preparation = build_preparation_plan(
        intake, analysis, budget, issued_at=issued_at
    )
    assert preparation is not None
    store.create(WebSession(
        session_id=f"rejected-{expected}", created_at="2026-08-11T00:00:00+00:00",
        intake=intake, requirements=analysis, budget=budget, preparation=preparation,
    ))
    app.dependency_overrides[get_session_store] = lambda: store
    try:
        response = TestClient(app).post(
            f"/api/sessions/rejected-{expected}/run",
            json={
                "approved_usd": budget.recommended_approval_usd,
                "authorization_sha256": preparation.authorization_sha256,
                "actor_id": actor_id,
            },
        )
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 409
    assert expected in response.text.casefold()


def test_exchange_development_is_queued_as_a_local_job(monkeypatch, tmp_path) -> None:
    store = InMemoryWebSessionStore()
    intake = IntakeRequest(
        goal="Improve the existing Exchange web application.",
        output_target=OutputTarget.EXISTING_PROJECT,
        toolpack_ids=[ToolPackId.EXCHANGE_DEVELOPMENT],
        budget_limit_usd=2.0,
    )
    analysis = requirements(True)
    budget = estimate_budget(intake, analysis)
    intake = attach_toolpack_descriptors(intake)
    store.create(WebSession(
        session_id="local-development",
        created_at="2026-08-06T00:00:00+00:00",
        intake=intake,
        requirements=analysis,
        budget=budget,
    ))
    monkeypatch.setenv("ONEBRIEF_LOCAL_JOBS_ROOT", str(tmp_path / "jobs"))
    monkeypatch.setattr("onebrief.web_service.run_job", lambda _job_dir: None)
    app.dependency_overrides[get_session_store] = lambda: store
    try:
        response = TestClient(app).post(
            "/api/sessions/local-development/run",
            json={"approved_usd": budget.recommended_approval_usd},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "queued"
    link = store.read_execution("local-development")
    assert link.operation_name == "local"
    assert (tmp_path / "jobs").is_dir()


def test_imported_project_development_uses_cloud_snapshot_when_configured(
    monkeypatch, tmp_path
) -> None:
    store = InMemoryWebSessionStore()
    project = RegisteredProject(
        project_id="sample-project", name="Sample", summary="Python", root_path=str(tmp_path / "project"),
        project_type="python", branch="main", head_sha="a" * 40, worktree_status="clean",
        ready_for_isolated_edit=True, toolpack_id=ToolPackId.PROJECT_DEVELOPMENT,
        origin="imported", toolpack_status="approved",
    )
    intake = IntakeRequest(
        goal="Improve the approved Python project.",
        output_target=OutputTarget.EXISTING_PROJECT,
        existing_project_id="sample-project",
        toolpack_ids=[ToolPackId.PROJECT_DEVELOPMENT],
        public_research_allowed=True,
        budget_limit_usd=2.0,
    )
    analysis = requirements(True)
    budget = estimate_budget(intake, analysis)
    store.create(WebSession(
        session_id="cloud-development", created_at="2026-08-09T00:00:00+00:00",
        intake=intake, requirements=analysis, budget=budget,
    ))

    class FakeCatalog:
        def get(self, project_id):
            assert project_id == "sample-project"
            return project

    def fake_create_job(**_kwargs):
        job = tmp_path / "staged-job"
        job.mkdir(exist_ok=True)
        return job

    def fake_submit(job_dir, **kwargs):
        assert job_dir.name == "staged-job"
        assert kwargs["bucket"] == "job-bucket"
        return CloudExecutionReceipt(
            job_uri="gs://job-bucket/jobs/cloud-development",
            cloud_run_job="projects/test/locations/asia-northeast3/jobs/onebrief-worker",
            operation_name="operations/cloud-development",
        )

    monkeypatch.setenv("ONEBRIEF_JOB_BUCKET", "job-bucket")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "test-project")
    monkeypatch.setattr("onebrief.web_service.ProjectCatalog", FakeCatalog)
    monkeypatch.setattr("onebrief.web_service.create_job", fake_create_job)
    monkeypatch.setattr("onebrief.web_service.submit_cloud_job", fake_submit)
    app.dependency_overrides[get_session_store] = lambda: store
    try:
        response = TestClient(app).post(
            "/api/sessions/cloud-development/run",
            json={"approved_usd": budget.recommended_approval_usd},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200, response.text
    link = store.read_execution("cloud-development")
    assert link.operation_name == "operations/cloud-development"
    assert link.job_uri.startswith("gs://job-bucket/")


def test_local_project_cloud_configuration_accepts_legacy_bucket_name(monkeypatch) -> None:
    monkeypatch.delenv("ONEBRIEF_JOB_BUCKET", raising=False)
    monkeypatch.setenv("ONEBRIEF_BUCKET", "job-bucket")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "test-project")

    assert _local_project_cloud_configured() is True


def test_apply_endpoint_downloads_remote_result_and_returns_receipt(monkeypatch, tmp_path) -> None:
    store = InMemoryWebSessionStore()
    store.create(WebSession(
        session_id="apply-cloud", created_at="2026-08-09T00:00:00+00:00",
        intake=IntakeRequest(
            goal="Improve the project.", output_target=OutputTarget.EXISTING_PROJECT,
            existing_project_id="sample-project",
        ),
        requirements=requirements(True),
    ))
    store.save_execution(ExecutionLink(
        session_id="apply-cloud", job_uri="gs://job-bucket/jobs/apply-cloud",
        operation_name="operations/apply-cloud", created_at="2026-08-09T00:00:00+00:00",
    ))

    class FakeRemote:
        def download_result(self, destination):
            destination.mkdir(parents=True)
            (destination / "package_manifest.json").write_text("{}", encoding="utf-8")
            return destination

    class FakeReceipt:
        def model_dump(self, mode="json"):
            assert mode == "json"
            return {
                "status": "applied", "changed_paths": ["src/app.py"],
                "backup_path": str(tmp_path / "backup"), "message": "Applied",
            }

    def fake_apply(project_id, result_root, backup_root):
        assert project_id == "sample-project"
        assert (result_root / "package_manifest.json").is_file()
        assert backup_root == (tmp_path / "backups").resolve()
        return FakeReceipt()

    monkeypatch.setattr("onebrief.web_service.GCSJobStore", lambda _uri: FakeRemote())
    monkeypatch.setattr("onebrief.web_service.apply_verified_project_result", fake_apply)
    monkeypatch.setattr("onebrief.web_service._local_apply_backups_root", lambda: (tmp_path / "backups").resolve())
    app.dependency_overrides[get_session_store] = lambda: store
    try:
        response = TestClient(app).post("/api/sessions/apply-cloud/apply")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "applied"
    assert response.json()["changed_paths"] == ["src/app.py"]


def test_status_exposes_safe_apply_only_for_completed_local_project_session(monkeypatch) -> None:
    store = InMemoryWebSessionStore()
    store.create(WebSession(
        session_id="apply-ready", created_at="2026-08-09T00:00:00+00:00",
        intake=IntakeRequest(
            goal="Improve the project.", output_target=OutputTarget.EXISTING_PROJECT,
            existing_project_id="sample-project",
        ),
        requirements=requirements(True),
    ))
    store.save_execution(ExecutionLink(
        session_id="apply-ready", job_uri="gs://job-bucket/jobs/apply-ready",
        operation_name="operations/apply-ready", created_at="2026-08-09T00:00:00+00:00",
    ))
    record = JobRecord(
        job_id="apply-ready", status=JobStatus.COMPLETE,
        created_at="2026-08-09T00:00:00+00:00", updated_at="2026-08-09T00:01:00+00:00",
        attempts=1, current_stage="finished", message="Complete", run_id="run",
        result_package="packages/result-v001",
    )

    class FakeRemote:
        def read_job(self):
            return record

        def read_json(self, relative):
            if relative in {
                "work/evaluation_metrics.json",
                "work/lineage_summary.json",
                "work/resume_capsule_l0.json",
            }:
                raise FileNotFoundError(relative)
            assert relative == "work/completion_ledger.json"
            return {"complete": True}

    monkeypatch.setattr("onebrief.web_service.GCSJobStore", lambda _uri: FakeRemote())
    app.dependency_overrides[get_session_store] = lambda: store
    try:
        response = TestClient(app).get("/api/sessions/apply-ready/status")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["can_apply"] is True
    assert response.json()["project_id"] == "sample-project"


def test_graph_endpoint_returns_selected_team_and_node_history(monkeypatch) -> None:
    store = InMemoryWebSessionStore()
    store.create(WebSession(
        session_id="download-local", created_at="2026-08-06T00:00:00+00:00",
        intake=IntakeRequest(goal="Download result."), requirements=requirements(True),
    ))
    store.save_execution(ExecutionLink(
        session_id="session-1",
        job_uri="gs://test/jobs/job-1",
        operation_name="operations/1",
        created_at="2026-08-06T00:00:00+00:00",
    ))

    class FakeRepository:
        def read_job(self):


            return SimpleNamespace(
                job_id="job-1",
                status=SimpleNamespace(value="running"),
                current_stage="evidence_analysis",
                message="Analysis is running.",
            )

        def read_json(self, relative):
            if str(relative).endswith("execution_graph.json"):
                return {"nodes": [{
                    "node_id": "evidence_analysis",
                    "stage": "evidence_analysis",
                    "agent_type": "analyst",
                    "owner_instance_id": "analyst-01",
                    "depends_on": [],
                    "activation_reason": "Evidence must be grounded.",
                }]}
            return {"nodes": {"evidence_analysis": {
                "status": "running", "attempt": 1, "message": "Reading sources.",
                "output_paths": [],
            }}}

    monkeypatch.setattr("onebrief.web_service.GCSJobStore", lambda _uri: FakeRepository())
    app.dependency_overrides[get_session_store] = lambda: store
    try:
        response = TestClient(app).get("/api/sessions/session-1/graph")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    assert payload["nodes"][0]["agent_type"] == "analyst"
    assert payload["nodes"][0]["status"] == "running"
    assert payload["nodes"][0]["attempt"] == 1


def test_criteria_endpoint_returns_completion_ledger(monkeypatch) -> None:
    store = InMemoryWebSessionStore()
    store.save_execution(ExecutionLink(
        session_id="criteria-1",
        job_uri="gs://test/jobs/job-criteria",
        operation_name="operations/criteria",
        created_at="2026-08-06T00:00:00+00:00",
    ))

    class FakeRepository:
        def read_job(self):
            return SimpleNamespace(
                status=SimpleNamespace(value="running"), current_stage="verification_r1"
            )

        def read_json(self, relative):
            assert relative == "work/completion_ledger.json"
            return {
                "target_state": "A usable result.", "pass_condition": "All pass.",
                "criteria": [{
                    "criterion_id": "Q01", "description": "It works.",
                    "evaluation_mode": "deterministic", "evidence_required": "Test output.",
                    "required": True, "status": "passed", "attempts": [],
                    "failure_reasons": [], "revision_instructions": [],
                }],
                "required_total": 1, "required_passed": 1, "complete": True,
                "latest_verdict": "PASS", "revision_rounds": 1,
            }

    monkeypatch.setattr("onebrief.web_service.GCSJobStore", lambda _uri: FakeRepository())
    app.dependency_overrides[get_session_store] = lambda: store
    try:
        response = TestClient(app).get("/api/sessions/criteria-1/criteria")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["criteria"][0]["criterion_id"] == "Q01"
    assert response.json()["required_passed"] == 1


def test_graph_endpoint_reports_team_planning_before_artifacts_exist(monkeypatch) -> None:
    store = InMemoryWebSessionStore()
    store.create(WebSession(
        session_id="download-local", created_at="2026-08-06T00:00:00+00:00",
        intake=IntakeRequest(goal="Download result."), requirements=requirements(True),
    ))
    store.save_execution(ExecutionLink(
        session_id="session-2",
        job_uri="gs://test/jobs/job-2",
        operation_name="operations/2",
        created_at="2026-08-06T00:00:00+00:00",
    ))

    class FakeRepository:
        def read_job(self):
            return SimpleNamespace(
                job_id="job-2", status=SimpleNamespace(value="queued"),
                current_stage="queued", message="Queued.",
            )

        def read_json(self, _relative):
            raise FileNotFoundError("not ready")

    monkeypatch.setattr("onebrief.web_service.GCSJobStore", lambda _uri: FakeRepository())
    app.dependency_overrides[get_session_store] = lambda: store
    try:
        response = TestClient(app).get("/api/sessions/session-2/graph")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["nodes"] == []
    assert response.json()["job_status"] == "queued"


def test_project_catalog_endpoint_and_existing_project_requirement(monkeypatch) -> None:
    project = RegisteredProject(
        project_id="exchange",
        name="Exchange Flow",
        summary="FX application",
        root_path="C:/exchange",
        project_type="web",
        branch="main",
        head_sha="a" * 40,
        worktree_status="clean",
        ready_for_isolated_edit=True,
        toolpack_id=ToolPackId.EXCHANGE_DEVELOPMENT,
    )

    class FakeCatalog:
        def list(self, query=""):
            return [project] if not query or query.casefold() in "exchange flow" else []

        def get(self, project_id):
            if project_id != "exchange":
                raise KeyError(project_id)
            return project

    monkeypatch.setattr("onebrief.web_service.ProjectCatalog", FakeCatalog)
    response = TestClient(app).get("/api/projects?q=exchange")
    assert response.status_code == 200
    assert response.json()["projects"][0]["project_id"] == "exchange"

    missing = TestClient(app).post(
        "/api/inspect",
        data={
            "goal": "Improve the approved existing application.",
            "output_target": "existing_project",
        },
    )
    assert missing.status_code == 422


def test_local_result_endpoint_archives_result_directory(tmp_path) -> None:
    job_dir = tmp_path / "job"
    package = job_dir / "packages" / "result-v001"
    package.mkdir(parents=True)
    (package / "package_manifest.json").write_text('{"ok":true}', encoding="utf-8")
    record = JobRecord(
        job_id="job",
        status=JobStatus.COMPLETE,
        created_at="2026-08-06T00:00:00+00:00",
        updated_at="2026-08-06T00:00:00+00:00",
        attempts=1,
        current_stage="complete",
        message="Complete",
        run_id="run",
        result_package="packages/result-v001",
    )
    (job_dir / "job.json").write_text(record.model_dump_json(), encoding="utf-8")
    store = InMemoryWebSessionStore()
    store.create(WebSession(
        session_id="download-local", created_at="2026-08-06T00:00:00+00:00",
        intake=IntakeRequest(goal="Download result."), requirements=requirements(True),
    ))
    store.save_execution(ExecutionLink(
        session_id="download-local",
        job_uri=str(job_dir),
        operation_name="local",
        created_at="2026-08-06T00:00:00+00:00",
    ))
    app.dependency_overrides[get_session_store] = lambda: store
    try:
        response = TestClient(app).get("/api/sessions/download-local/result")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    assert response.content.startswith(b"PK")


def test_local_web_session_store_survives_process_restart(tmp_path) -> None:
    first = LocalWebSessionStore(tmp_path / "sessions")
    session = WebSession(
        session_id="restart-safe",
        created_at="2026-08-06T00:00:00+00:00",
        intake=IntakeRequest(goal="Continue the approved project."),
        requirements=requirements(True),
    )
    first.create(session)
    first.claim_run(session.session_id)
    first.save_execution(ExecutionLink(
        session_id=session.session_id,
        job_uri="C:/tmp/job",
        operation_name="local",
        created_at="2026-08-06T00:00:00+00:00",
    ))

    restarted = LocalWebSessionStore(tmp_path / "sessions")
    assert restarted.read(session.session_id).intake.goal == session.intake.goal
    assert restarted.read_execution(session.session_id).job_uri == "C:/tmp/job"
    with pytest.raises(RuntimeError, match="already submitted"):
        restarted.claim_run(session.session_id)

def test_latest_project_result_recovers_completed_local_package(tmp_path, monkeypatch) -> None:
    project_root = tmp_path / "exchange"
    project_root.mkdir()
    jobs_root = tmp_path / "jobs"
    job_dir = jobs_root / "completed-job"
    package = job_dir / "packages" / "result-v001"
    (job_dir / "inputs").mkdir(parents=True)
    (job_dir / "work").mkdir()
    package.mkdir(parents=True)
    (package / "final.md").write_text("# Complete", encoding="utf-8")
    intake = IntakeRequest(
        goal="Complete Exchange.",
        output_target=OutputTarget.EXISTING_PROJECT,
        existing_project_id="exchange",
    )
    (job_dir / "inputs" / "intake.json").write_text(intake.model_dump_json(), encoding="utf-8")
    (job_dir / "inputs" / "requirements.json").write_text(
        requirements(True).model_dump_json(), encoding="utf-8"
    )
    record = JobRecord(
        job_id="completed-job",
        status=JobStatus.COMPLETE,
        created_at="2026-08-06T00:00:00+00:00",
        updated_at="2026-08-06T01:00:00+00:00",
        attempts=1,
        current_stage="finished",
        message="Complete",
        run_id="run",
        result_package="packages/result-v001",
    )
    (job_dir / "job.json").write_text(record.model_dump_json(), encoding="utf-8")
    project = RegisteredProject(
        project_id="exchange", name="Exchange", summary="FX", root_path=str(project_root),
        project_type="web", branch="main", head_sha="a" * 40, worktree_status="clean",
        ready_for_isolated_edit=True, toolpack_id=ToolPackId.EXCHANGE_DEVELOPMENT,
    )

    class FakeCatalog:
        def get(self, project_id):
            assert project_id == "exchange"
            return project

    monkeypatch.setattr("onebrief.web_service.ProjectCatalog", FakeCatalog)
    monkeypatch.setattr("onebrief.web_service._local_jobs_root", lambda: jobs_root.resolve())
    response = TestClient(app).get("/api/projects/exchange/latest-result")

    assert response.status_code == 200
    assert response.content.startswith(b"PK")

def test_existing_project_inspect_injects_restored_continuation_context(tmp_path, monkeypatch) -> None:
    project_root = tmp_path / "exchange"
    project_root.mkdir()
    jobs_root = tmp_path / "jobs"
    project = RegisteredProject(
        project_id="exchange", name="Exchange", summary="FX", root_path=str(project_root),
        project_type="web", branch="main", head_sha="a" * 40, worktree_status="clean",
        ready_for_isolated_edit=True, toolpack_id=ToolPackId.EXCHANGE_DEVELOPMENT,
    )

    class FakeCatalog:
        def get(self, project_id):
            assert project_id == "exchange"
            return project

    async def fake_inspect(intake):
        restored = [
            source for source in intake.internal_sources
            if source.requirement_keys == ["existing_project_continuation"]
        ]
        assert len(restored) == 1
        assert "canonical_goal" in restored[0].content
        assert "resumes the existing completion contract" in restored[0].summary
        return requirements(True)

    store = InMemoryWebSessionStore()
    monkeypatch.setattr("onebrief.web_service.ProjectCatalog", FakeCatalog)
    monkeypatch.setattr("onebrief.web_service._local_jobs_root", lambda: jobs_root.resolve())
    monkeypatch.setattr("onebrief.web_service.inspect_requirements", fake_inspect)
    app.dependency_overrides[get_session_store] = lambda: store
    try:
        response = TestClient(app).post("/api/inspect", data={
            "goal": "Continue the unfinished work.",
            "output_target": "existing_project",
            "existing_project_id": "exchange",
        })
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200, response.text
    assert response.json()["continuation"]["state"]["project_id"] == "exchange"
    assert response.json()["continuation"]["continuation_kind"] == "resume"


def test_existing_project_new_goal_starts_a_revision_contract(tmp_path, monkeypatch) -> None:
    project_root = tmp_path / "exchange"
    project_root.mkdir()
    project = RegisteredProject(
        project_id="exchange", name="Exchange", summary="FX", root_path=str(project_root),
        project_type="web", branch="main", head_sha="a" * 40, worktree_status="clean",
        ready_for_isolated_edit=True, toolpack_id=ToolPackId.EXCHANGE_DEVELOPMENT,
    )

    class FakeCatalog:
        def get(self, project_id):
            assert project_id == "exchange"
            return project

    async def fake_inspect(intake):
        restored = [
            source for source in intake.internal_sources
            if source.requirement_keys == ["existing_project_continuation"]
        ]
        assert len(restored) == 1
        assert "baseline evidence only" in restored[0].summary
        return requirements(True)

    store = InMemoryWebSessionStore()
    monkeypatch.setattr("onebrief.web_service.ProjectCatalog", FakeCatalog)
    monkeypatch.setattr("onebrief.web_service._local_jobs_root", lambda: (tmp_path / "jobs").resolve())
    monkeypatch.setattr("onebrief.web_service.inspect_requirements", fake_inspect)
    app.dependency_overrides[get_session_store] = lambda: store
    try:
        response = TestClient(app).post("/api/inspect", data={
            "goal": "Add a new mobile dashboard and verify it.",
            "output_target": "existing_project",
            "existing_project_id": "exchange",
        })
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200, response.text
    continuation = response.json()["continuation"]
    assert continuation["continuation_kind"] == "revision"
    assert continuation["state"]["completed_work"] == []
    assert continuation["baseline_state"] is not None

def test_spreadsheet_result_returns_xlsx_instead_of_audit_zip(tmp_path) -> None:
    job_dir = tmp_path / "sheet-job"
    (job_dir / "work").mkdir(parents=True)
    (job_dir / "work" / "result.xlsx").write_bytes(b"PK spreadsheet")
    record = JobRecord(
        job_id="sheet-job", status=JobStatus.COMPLETE,
        created_at="2026-08-06T00:00:00+00:00", updated_at="2026-08-06T00:00:00+00:00",
        attempts=1, current_stage="finished", message="Complete", run_id="run",
    )
    (job_dir / "job.json").write_text(record.model_dump_json(), encoding="utf-8")
    store = InMemoryWebSessionStore()
    session = WebSession(
        session_id="sheet-session", created_at="2026-08-06T00:00:00+00:00",
        intake=IntakeRequest(goal="Create workbook.", output_target=OutputTarget.SPREADSHEET),
        requirements=requirements(True),
    )
    store.create(session)
    store.save_execution(ExecutionLink(
        session_id=session.session_id, job_uri=str(job_dir), operation_name="local",
        created_at="2026-08-06T00:00:00+00:00",
    ))
    app.dependency_overrides[get_session_store] = lambda: store
    try:
        response = TestClient(app).get("/api/sessions/sheet-session/result")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    assert response.content == b"PK spreadsheet"
