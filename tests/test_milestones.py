from __future__ import annotations

import hashlib
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from onebrief.milestones import (
    advance_milestone_candidate,
    MilestoneCheckpoint,
    MilestoneKind,
    MilestonePlan,
    MilestoneState,
    MilestoneStore,
    MilestoneWorkspace,
    VerificationScope,
    build_milestone_plan,
    canonical_sha256,
    execute_milestone_plan,
    milestone_scope_source,
    _materialize_approved_empty_roots,
    prepare_milestone_workspace,
    requirements_for_milestone,
)
from onebrief.completion_ledger import build_completion_ledger
from onebrief.execution_schemas import (
    CriterionCheck,
    ExecutionCheckpoint,
    PipelineStatus,
    VerificationReport,
    Verdict,
)
from onebrief.generic_development_toolpack import ProjectCodeChangeSet, ProjectFileChange
from onebrief.schemas import (
    CompletionContract,
    ExecutionPhase,
    EvaluationMode,
    QualityCriterion,
    RequirementsAnalysis,
    SixSensePlan,
    IntakeRequest,
    ToolPackId,
)
from onebrief.producer import estimate_budget
from onebrief.project_import import MANIFEST_NAME, ProjectManifest


def _requirements() -> RequirementsAnalysis:
    criteria = [
        QualityCriterion(
            criterion_id="Q01",
            description="Unity compilation success",
            evaluation_mode=EvaluationMode.DETERMINISTIC,
            evidence_required="Compilation logs showing zero errors.",
        ),
        QualityCriterion(
            criterion_id="Q02",
            description="Login to Lobby PlayMode UI flow verification",
            evaluation_mode=EvaluationMode.DETERMINISTIC,
            evidence_required="PlayMode navigation logs.",
        ),
        QualityCriterion(
            criterion_id="Q03",
            description="Settings BGM, SFX, and volume controls work",
            evaluation_mode=EvaluationMode.DETERMINISTIC,
            evidence_required="Settings interaction logs.",
        ),
        QualityCriterion(
            criterion_id="Q04",
            description="Responsive visual layout and localization glyph integrity",
            evaluation_mode=EvaluationMode.INDEPENDENT_REVIEW,
            evidence_required="Rendered mobile and desktop screenshots.",
        ),
        QualityCriterion(
            criterion_id="Q05",
            description="Existing systems are preserved without regression",
            evaluation_mode=EvaluationMode.INDEPENDENT_REVIEW,
            evidence_required="Diff and regression evidence.",
        ),
        QualityCriterion(
            criterion_id="Q06",
            description="Do not modify the original repository; use an isolated snapshot",
            evaluation_mode=EvaluationMode.INDEPENDENT_REVIEW,
            evidence_required="Snapshot provenance receipt.",
        ),
    ]
    return RequirementsAnalysis(
        supported=True,
        support_reason="Existing Unity project development is supported.",
        normalized_goal="Modernize the Unity client while preserving its systems.",
        deliverables=["Runnable Unity client patch", "Executable verification evidence"],
        mandatory_information=[],
        optional_information=[],
        acceptance_criteria=[item.description for item in criteria],
        completion_contract=CompletionContract(
            target_state="A runnable modernized Unity client.",
            quality_criteria=criteria,
        ),
        assumptions=[],
        consolidated_questions=[],
        ready_for_estimate=True,
    )


def _plan() -> MilestonePlan:
    return build_milestone_plan(
        project_id="julpae",
        goal="Modernize JULPAE",
        requirements=_requirements(),
        source_revision="a" * 40,
        minimum_cost_usd=1.0,
        maximum_cost_usd=10.0,
    )


def test_new_client_goal_keeps_construction_in_first_vertical_slices() -> None:
    current = build_milestone_plan(
        project_id="julpae",
        goal=(
            "Build a new Unity client presentation layer for Login, Lobby, and Settings while "
            "reusing approved server contracts, scripts, and image assets."
        ),
        requirements=_requirements().model_copy(update={
            "normalized_goal": (
                "Build a new Unity client presentation layer for Login, Lobby, and Settings."
            ),
            "completion_contract": _requirements().completion_contract.model_copy(update={
                "target_state": "A new Unity client with Login, Lobby, and Settings."
            }),
        }),
        source_revision="a" * 40,
        minimum_cost_usd=1.0,
        maximum_cost_usd=10.0,
    )

    login, lobby, settings = current.milestones[1:4]
    assert "newly constructed Login" in login.contract.target_state
    assert login.initial_execution_phase.value == "product_implementation"
    assert "New Login production surface" in login.contract.slice_quality_criteria[0].description
    assert "newly constructed Lobby" in lobby.contract.target_state
    assert "newly constructed Settings" in settings.contract.target_state


def _evidence(root: Path, name: str) -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(name, encoding="utf-8")
    return path


def test_verified_existing_state_advances_without_changed_file_artifacts(tmp_path: Path) -> None:
    root = tmp_path / "integration"
    root.mkdir()
    subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=root, check=True)
    source = root / "Assets" / "App.cs"
    source.parent.mkdir()
    source.write_text("class App {}\n", encoding="utf-8", newline="\n")
    subprocess.run(["git", "add", "Assets/App.cs"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-m", "baseline"], cwd=root, check=True, capture_output=True)
    blob = subprocess.run(
        ["git", "show", "HEAD:Assets/App.cs"], cwd=root, check=True, capture_output=True
    ).stdout
    candidate = ProjectCodeChangeSet(
        summary="Verify the existing implementation.",
        changes=[ProjectFileChange(
            path="Assets/App.cs",
            base_sha256=hashlib.sha256(blob).hexdigest(),
            content="class App {}\n",
            reason="The approved state already satisfies this slice.",
        )],
    )
    output = tmp_path / "M03"
    development = output / "development"
    development.mkdir(parents=True)
    (development / "change_set.json").write_text(
        candidate.model_dump_json(indent=2), encoding="utf-8"
    )
    (development / "development_run.json").write_text(
        '{"result_mode":"existing_state_verified","changed_paths":[]}', encoding="utf-8"
    )
    (development / "changes.patch").write_text("", encoding="utf-8")
    workspace = MilestoneWorkspace(
        project_id="julpae",
        baseline_root=str(root),
        baseline_registry_root=str(tmp_path / "baseline-registry"),
        integration_root=str(root),
        integration_registry_root=str(tmp_path / "integration-registry"),
        source_revision="a" * 40,
        integration_base_revision="b" * 40,
    )

    before, after, digest = advance_milestone_candidate(
        workspace=workspace, milestone=_plan().milestones[3], output_dir=output
    )

    assert before == after
    assert digest == canonical_sha256(candidate)
    assert subprocess.run(
        ["git", "status", "--porcelain"], cwd=root, check=True,
        capture_output=True, text=True,
    ).stdout == ""


def test_verified_existing_state_rejects_content_mismatch(tmp_path: Path) -> None:
    root = tmp_path / "integration"
    root.mkdir()
    subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=root, check=True)
    source = root / "Assets" / "App.cs"
    source.parent.mkdir()
    source.write_text("class App {}\n", encoding="utf-8", newline="\n")
    subprocess.run(["git", "add", "Assets/App.cs"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-m", "baseline"], cwd=root, check=True, capture_output=True)
    blob = subprocess.run(
        ["git", "show", "HEAD:Assets/App.cs"], cwd=root, check=True, capture_output=True
    ).stdout
    candidate = ProjectCodeChangeSet(
        summary="Claim an existing implementation.",
        changes=[ProjectFileChange(
            path="Assets/App.cs",
            base_sha256=hashlib.sha256(blob).hexdigest(),
            content="class App { int changed; }\n",
            reason="This must not be accepted as existing state.",
        )],
    )
    output = tmp_path / "M03"
    development = output / "development"
    development.mkdir(parents=True)
    (development / "change_set.json").write_text(
        candidate.model_dump_json(indent=2), encoding="utf-8"
    )
    (development / "development_run.json").write_text(
        '{"result_mode":"existing_state_verified","changed_paths":[]}', encoding="utf-8"
    )
    (development / "changes.patch").write_text("", encoding="utf-8")
    workspace = MilestoneWorkspace(
        project_id="julpae",
        baseline_root=str(root),
        baseline_registry_root=str(tmp_path / "baseline-registry"),
        integration_root=str(root),
        integration_registry_root=str(tmp_path / "integration-registry"),
        source_revision="a" * 40,
        integration_base_revision="b" * 40,
    )

    with pytest.raises(RuntimeError, match="existing-state milestone content mismatch"):
        advance_milestone_candidate(
            workspace=workspace, milestone=_plan().milestones[3], output_dir=output
        )


def test_plan_assigns_every_criterion_once_and_finishes_with_full_regression() -> None:
    plan = _plan()
    assigned = [
        criterion_id for milestone in plan.milestones
        for criterion_id in milestone.contract.primary_criterion_ids
    ]
    assert set(assigned) == set(plan.overall_criterion_ids)
    assert len(assigned) == len(set(assigned))
    assert plan.milestones[0].kind == MilestoneKind.BASELINE
    assert plan.milestones[-1].kind == MilestoneKind.INTEGRATION
    assert plan.milestones[-1].contract.verification_scope == VerificationScope.FULL
    assert set(plan.milestones[-1].contract.criterion_ids) == set(plan.overall_criterion_ids)
    assert sum(item.budget_weight for item in plan.milestones) == pytest.approx(1.0)


def test_greenfield_product_uses_progressive_whole_product_maturity_passes() -> None:
    criteria = [
        QualityCriterion(
            criterion_id="Q01",
            description="The Unity project compiles and builds successfully.",
            evaluation_mode=EvaluationMode.DETERMINISTIC,
            evidence_required="Unity build log.",
        ),
        QualityCriterion(
            criterion_id="Q02",
            description="The title screen opens the chamber selection screen.",
            evaluation_mode=EvaluationMode.DETERMINISTIC,
            evidence_required="Executed screen transition.",
        ),
        QualityCriterion(
            criterion_id="Q03",
            description="Chamber progress and scores persist locally.",
            evaluation_mode=EvaluationMode.DETERMINISTIC,
            evidence_required="Persistence test.",
        ),
        QualityCriterion(
            criterion_id="Q04",
            description="The rendered 9x9 board has a polished visual layout.",
            evaluation_mode=EvaluationMode.INDEPENDENT_REVIEW,
            evidence_required="Rendered gameplay screenshot.",
        ),
    ]
    requirements = RequirementsAnalysis(
        supported=True,
        support_reason="The greenfield Unity product is approved.",
        normalized_goal="Create an offline Unity puzzle.",
        deliverables=["Playable Unity puzzle"],
        mandatory_information=[],
        optional_information=[],
        acceptance_criteria=[item.description for item in criteria],
        completion_contract=CompletionContract(
            target_state="A playable offline Unity puzzle.",
            quality_criteria=criteria,
        ),
        assumptions=[],
        consolidated_questions=[],
        ready_for_estimate=True,
    )

    plan = build_milestone_plan(
        project_id="puzzle-telos",
        goal="Create PUZZLE TELOS from an empty Unity foundation.",
        requirements=requirements,
        source_revision="a" * 40,
        minimum_cost_usd=1.0,
        maximum_cost_usd=10.0,
    )

    assert [item.title for item in plan.milestones[1:5]] == [
        "Whole-product executable topology",
        "Connected whole-product prototype",
        "Whole-product functional completion",
        "Whole-product experience refinement",
    ]
    topology, connected, functional, refinement = plan.milestones[1:5]
    assert topology.contract.primary_criterion_ids == []
    assert [item.criterion_id for item in topology.contract.slice_quality_criteria] == ["Q89"]
    assert [item.criterion_id for item in connected.contract.slice_quality_criteria] == [
        "Q89", "Q88",
    ]
    assert functional.contract.primary_criterion_ids == ["Q01", "Q02", "Q03"]
    assert refinement.contract.primary_criterion_ids == ["Q04"]
    assert [item.criterion_id for item in refinement.contract.slice_quality_criteria] == [
        "Q89", "Q88", "Q87",
    ]


def test_scoped_requirements_revalidate_passed_dependencies() -> None:
    plan = _plan()
    requirements = _requirements()
    settings = next(item for item in plan.milestones if "Settings vertical" in item.title)
    scoped = requirements_for_milestone(requirements, settings)
    selected = {
        item.criterion_id for item in scoped.completion_contract.quality_criteria
    }
    assert set(settings.contract.primary_criterion_ids).issubset(selected)
    assert "Q01" in selected


def test_scoped_requirements_remove_whole_project_sixsense_and_publish_exact_scope() -> None:
    plan = _plan()
    login = plan.milestones[1]
    requirements = _requirements().model_copy(update={
        "sixsense": SixSensePlan(
            standard_profile="Responsive multilingual presentation for the finished product."
        )
    })

    scoped = requirements_for_milestone(requirements, login)
    source = milestone_scope_source(login)

    assert scoped.sixsense is None
    assert [item.criterion_id for item in scoped.completion_contract.quality_criteria] == [
        "Q01", "Q91"
    ]
    assert '"milestone_id": "M01"' in source.content
    assert '"authorized_criterion_ids"' in source.content
    assert "future milestone" in source.content
    assert "unrelated future scope" in scoped.normalized_goal
    assert "modernized" not in login.outcome.casefold()


def test_unity_flow_is_decomposed_into_real_vertical_slices() -> None:
    plan = _plan()
    titles = [item.title for item in plan.milestones]
    assert titles[:5] == [
        "Immutable approved baseline",
        "Login vertical slice",
        "Lobby vertical slice",
        "Settings vertical slice and complete flow",
        "Presentation and adaptability",
    ]
    lobby = plan.milestones[2]
    assert lobby.contract.primary_criterion_ids == []
    assert [item.criterion_id for item in lobby.contract.slice_quality_criteria] == [
        "Q91", "Q92"
    ]
    scoped = requirements_for_milestone(_requirements(), lobby)
    assert [item.criterion_id for item in scoped.completion_contract.quality_criteria] == [
        "Q01", "Q91", "Q92"
    ]
    presentation = plan.milestones[4]
    assert presentation.initial_execution_phase == ExecutionPhase.EVIDENCE_CONSTRUCTION
    assert lobby.initial_execution_phase == ExecutionPhase.PRODUCT_IMPLEMENTATION


def test_budget_reserves_every_milestone_before_owner_approval() -> None:
    requirements = _requirements()
    intake = IntakeRequest(
        goal="Modernize JULPAE Login, Lobby, and Settings in Unity.",
        existing_project_id="julpae",
        toolpack_ids=[ToolPackId.PROJECT_DEVELOPMENT],
        max_revision_rounds=3,
    )
    plan = build_milestone_plan(
        project_id="julpae",
        goal=intake.goal,
        requirements=requirements,
        source_revision="approval-pending",
        minimum_cost_usd=0.0,
        maximum_cost_usd=0.0,
    )
    estimate = estimate_budget(intake, requirements)
    by_name = {item.stage: item for item in estimate.stages}
    implementations = sum(
        item.kind == MilestoneKind.IMPLEMENTATION for item in plan.milestones
    )
    executions = len(plan.milestones) - 1

    assert by_name["team_planning"].minimum_calls == 1
    assert by_name["long_form_draft"].minimum_calls == implementations
    assert by_name["evidence_analysis"].minimum_calls == executions
    assert by_name["independent_verification"].minimum_calls == executions
    assert by_name["final_approval"].minimum_calls == executions
    assert max(item.max_ai_repair_calls for item in estimate.phase_budgets) == min(
        24, intake.max_revision_rounds * executions
    )
    assert any("vertical-slice passes" in item for item in estimate.notes)


def test_execution_caps_quest_allocation_at_quoted_maximum(
    tmp_path: Path, monkeypatch,
) -> None:
    plan = _plan()
    captured: dict[str, float] = {}

    class CapturingQuestStore:
        def __init__(self, _root, **kwargs):
            captured["approved_budget_usd"] = kwargs["approved_budget_usd"]
            raise RuntimeError("captured")

    monkeypatch.setattr("onebrief.milestones.QuestStore", CapturingQuestStore)
    workspace = MilestoneWorkspace(
        project_id="julpae",
        baseline_root=str(tmp_path),
        baseline_registry_root=str(tmp_path),
        integration_root=str(tmp_path),
        integration_registry_root=str(tmp_path),
        source_revision="a" * 40,
        integration_base_revision="a" * 40,
    )
    state = type("State", (), {
        "execution_ready": True,
        "generated": type("Generated", (), {
            "sha256": "b" * 64,
            "allowed_write_prefixes": ["Assets/"],
        })(),
    })()
    monkeypatch.setattr(
        "onebrief.milestones.ProjectToolPackLifecycle.state", lambda _self: state
    )

    with pytest.raises(RuntimeError, match="captured"):
        execute_milestone_plan(
            plan=plan,
            requirements=_requirements(),
            work_dir=tmp_path / "work",
            workspace=workspace,
            pipeline_factory=lambda _root: object(),
            intake=IntakeRequest(goal="Modernize Unity UI."),
            sources=[],
            approved_budget_usd=plan.maximum_cost_usd + 5,
        )

    assert captured["approved_budget_usd"] == plan.maximum_cost_usd


def test_workspace_clone_enables_windows_long_paths_before_checkout(
    tmp_path: Path, monkeypatch,
) -> None:
    baseline = tmp_path / "baseline"
    baseline.mkdir()
    subprocess.run(["git", "init"], cwd=baseline, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=baseline, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=baseline, check=True)
    manifest = ProjectManifest(
        project_id="julpae",
        name="JULPAE",
        project_type="unity project",
        project_root=str(baseline),
        canonical_goal="Verify the exact milestone clone configuration.",
    )
    (baseline / MANIFEST_NAME).write_text(manifest.model_dump_json(indent=2), encoding="utf-8")
    (baseline / "Assets").mkdir()
    (baseline / "Assets" / "App.cs").write_text("class App {}\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=baseline, check=True)
    subprocess.run(["git", "commit", "-m", "baseline"], cwd=baseline, check=True, capture_output=True)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=baseline, check=True,
        capture_output=True, text=True,
    ).stdout.strip()

    class FakeLifecycle:
        def __init__(self, _project_id, _registry_root):
            pass

        def state(self):
            return SimpleNamespace(
                execution_ready=True,
                generated=SimpleNamespace(project_root=str(baseline), repository_head_sha=head),
            )

        def generate_and_qualify(self):
            return SimpleNamespace(
                qualification=SimpleNamespace(status="passed", toolpack_sha256="a" * 64),
            )

        def approve(self, _sha256):
            return SimpleNamespace(execution_ready=True)

    class FakeImporter:
        def __init__(self, _root):
            pass

        def import_bytes(self, _payload):
            return None

    monkeypatch.setattr("onebrief.milestones.ProjectToolPackLifecycle", FakeLifecycle)
    monkeypatch.setattr("onebrief.milestones.ExternalProjectImporter", FakeImporter)
    workspace = prepare_milestone_workspace(
        work_dir=tmp_path / "work",
        project_id="julpae",
        baseline_registry_root=tmp_path / "baseline-registry",
    )
    integration = Path(workspace.integration_root)

    def config(name: str) -> str:
        return subprocess.run(
            ["git", "config", "--get", name], cwd=integration, check=True,
            capture_output=True, text=True,
        ).stdout.strip()

    assert config("core.longpaths") == "true"
    assert config("core.autocrlf") == "false"


def test_workspace_restores_approved_empty_engine_roots(tmp_path: Path) -> None:
    root = tmp_path / "repository"
    root.mkdir()

    _materialize_approved_empty_roots(
        root,
        ["Assets/", "Packages/", "nested/not-a-root/", "../escape/"],
    )

    assert (root / "Assets").is_dir()
    assert (root / "Packages").is_dir()
    assert not (root / "nested").exists()
    assert not (tmp_path / "escape").exists()


def test_checkpoints_are_idempotent_and_dependency_bound(tmp_path: Path) -> None:
    plan = _plan()
    store = MilestoneStore(tmp_path / "state", plan)
    baseline = plan.milestones[0]
    evidence = _evidence(tmp_path, "baseline.json")
    first = store.record_pass(
        milestone=baseline,
        source_before="a" * 40,
        source_after="a" * 40,
        candidate_sha256=canonical_sha256({"baseline": True}),
        evidence_paths=[evidence],
    )
    second = store.record_pass(
        milestone=baseline,
        source_before="a" * 40,
        source_after="a" * 40,
        candidate_sha256=canonical_sha256({"baseline": True}),
        evidence_paths=[evidence],
    )
    assert first.checkpoint_id == second.checkpoint_id
    assert len(list((tmp_path / "state" / "receipts").glob("*.json"))) == 1

    milestone = plan.milestones[1]
    receipt = store.record_pass(
        milestone=milestone,
        source_before="a" * 40,
        source_after="b" * 40,
        candidate_sha256=canonical_sha256({"candidate": 1}),
        evidence_paths=[_evidence(tmp_path, "verification.json")],
    )
    assert receipt.dependency_checkpoints == {"M00": first.checkpoint_id}
    assert store.checkpoint(milestone.milestone_id) == receipt


def test_upstream_change_invalidates_descendants_only(tmp_path: Path) -> None:
    plan = _plan()
    store = MilestoneStore(tmp_path / "state", plan)
    evidence = _evidence(tmp_path, "pass.json")
    previous_revision = "a" * 40
    for index, milestone in enumerate(plan.milestones):
        store.record_pass(
            milestone=milestone,
            source_before=previous_revision,
            source_after=f"{index + 1:040x}",
            candidate_sha256=canonical_sha256({"milestone": milestone.milestone_id}),
            evidence_paths=[evidence],
        )
        previous_revision = f"{index + 1:040x}"

    changed = plan.milestones[1]
    records = store.invalidate_descendants(changed.milestone_id, "upstream candidate changed")
    assert {item.milestone_id for item in records} == set(store.descendants(changed.milestone_id))
    assert store.pointer("M00").state == MilestoneState.PASSED
    assert store.pointer(changed.milestone_id).state == MilestoneState.PASSED
    for descendant in store.descendants(changed.milestone_id):
        assert store.pointer(descendant).state == MilestoneState.INVALIDATED
        assert store.checkpoint(descendant) is None


def test_cycle_and_missing_primary_ownership_are_rejected() -> None:
    plan = _plan()
    payload = plan.model_dump(mode="json")
    payload["milestones"][0]["dependencies"] = [payload["milestones"][1]["milestone_id"]]
    with pytest.raises(ValueError, match="baseline milestone"):
        MilestonePlan.model_validate(payload)

    payload = plan.model_dump(mode="json")
    payload["milestones"][1]["contract"]["primary_criterion_ids"] = []
    with pytest.raises(ValueError, match="every overall criterion"):
        MilestonePlan.model_validate(payload)


def test_executor_scopes_each_slice_and_finishes_on_clean_baseline(
    tmp_path: Path, monkeypatch,
) -> None:
    plan = _plan()
    requirements = _requirements()
    candidate = ProjectCodeChangeSet(
        summary="Verified milestone candidate.",
        changes=[ProjectFileChange(
            path="Assets/App.cs",
            base_sha256=None,
            content="public class App {}\n",
            reason="Implement the milestone.",
        )],
    )
    baseline_registry = tmp_path / "baseline-registry"
    integration_registry = tmp_path / "integration-registry"
    baseline_registry.mkdir()
    integration_registry.mkdir()
    workspace = MilestoneWorkspace(
        project_id="julpae",
        baseline_root=str(tmp_path / "baseline"),
        baseline_registry_root=str(baseline_registry),
        integration_root=str(tmp_path / "integration"),
        integration_registry_root=str(integration_registry),
        source_revision="a" * 40,
        integration_base_revision="b" * 40,
    )
    calls: list[tuple[Path, str, str | None, list[str]]] = []
    durable_checkpoints: list[str] = []

    class FakeLifecycle:
        def __init__(self, _project_id, _registry_root):
            pass

        def state(self):
            return SimpleNamespace(
                execution_ready=True,
                generated=SimpleNamespace(sha256="c" * 64),
            )

    class FakePipeline:
        def __init__(self, registry: Path):
            self.registry = registry

        def run(self, *, intake, requirements, sources, output_dir):
            calls.append((
                self.registry,
                intake.goal,
                intake.desired_output,
                [item.name for item in sources],
            ))
            development = output_dir / "development"
            (development / "changed_files" / "Assets").mkdir(parents=True, exist_ok=True)
            (development / "change_set.json").write_text(
                candidate.model_dump_json(indent=2), encoding="utf-8"
            )
            (development / "development_run.json").write_text("{}", encoding="utf-8")
            (development / "changed_files" / "Assets" / "App.cs").write_text(
                candidate.changes[0].content, encoding="utf-8"
            )
            report = VerificationReport(
                verdict=Verdict.PASS,
                criterion_checks=[CriterionCheck(
                    criterion_id=item.criterion_id,
                    criterion=item.description,
                    passed=True,
                    evidence="Fresh executable test evidence.",
                ) for item in requirements.completion_contract.quality_criteria],
                blocking_issues=[],
                revision_instructions=[],
                missing_information=[],
            )
            ledger = build_completion_ledger(requirements.completion_contract, [(0, report)])
            (output_dir / "final_verification.json").write_text(
                report.model_dump_json(indent=2), encoding="utf-8"
            )
            (output_dir / "final_approval.json").write_text("{}", encoding="utf-8")
            (output_dir / "completion_ledger.json").write_text(
                ledger.model_dump_json(indent=2), encoding="utf-8"
            )
            checkpoint = ExecutionCheckpoint(
                status=PipelineStatus.COMPLETE,
                current_stage="finished",
                completed_stages=["verification"],
                revision_round=0,
                final_verdict=Verdict.PASS,
            )
            (output_dir / "execution_checkpoint.json").write_text(
                checkpoint.model_dump_json(indent=2), encoding="utf-8"
            )
            return checkpoint

    monkeypatch.setattr("onebrief.milestones.ProjectToolPackLifecycle", FakeLifecycle)
    monkeypatch.setattr(
        "onebrief.milestones.advance_milestone_candidate",
        lambda **_kwargs: ("d" * 40, "e" * 40, canonical_sha256(candidate)),
    )
    monkeypatch.setattr("onebrief.milestones.cumulative_change_set", lambda _workspace: candidate)

    result = execute_milestone_plan(
        plan=plan,
        requirements=requirements,
        work_dir=tmp_path / "work",
        workspace=workspace,
        pipeline_factory=FakePipeline,
        intake=IntakeRequest(goal="Modernize JULPAE"),
        sources=[],
        checkpoint_callback=durable_checkpoints.append,
    )

    assert result.status == PipelineStatus.COMPLETE
    assert calls[-1][0] == baseline_registry
    assert calls[-1][1] == "Modernize JULPAE"
    assert all("Milestone M" in goal for _registry, goal, _output, _sources in calls[:-1])
    first_slice_output = calls[0][2] or ""
    assert "Milestone outcome:" in first_slice_output
    assert "Responsive visual layout" not in first_slice_output
    assert "localization glyph integrity" not in first_slice_output
    assert calls[0][3][0].startswith("onebrief-active-quest-QC-")
    assert calls[0][3][1] == "onebrief-active-milestone-M01.json"
    assert (tmp_path / "work" / "development" / "change_set.json").is_file()
    store = MilestoneStore(tmp_path / "work" / "milestone_state", plan)
    assert all(store.checkpoint(item.milestone_id) is not None for item in plan.milestones)
    assert durable_checkpoints == [item.milestone_id for item in plan.milestones]
