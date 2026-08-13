from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from onebrief.milestones import (
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
    requirements_for_milestone,
)
from onebrief.execution_schemas import ExecutionCheckpoint, PipelineStatus, Verdict
from onebrief.generic_development_toolpack import ProjectCodeChangeSet, ProjectFileChange
from onebrief.schemas import (
    CompletionContract,
    EvaluationMode,
    QualityCriterion,
    RequirementsAnalysis,
    IntakeRequest,
    ToolPackId,
)
from onebrief.producer import estimate_budget


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


def _evidence(root: Path, name: str) -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(name, encoding="utf-8")
    return path


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
    assert "unrelated future scope" in scoped.normalized_goal


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
    calls: list[tuple[Path, str]] = []
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
            calls.append((self.registry, intake.goal))
            development = output_dir / "development"
            (development / "changed_files" / "Assets").mkdir(parents=True, exist_ok=True)
            (development / "change_set.json").write_text(
                candidate.model_dump_json(indent=2), encoding="utf-8"
            )
            (development / "development_run.json").write_text("{}", encoding="utf-8")
            (development / "changed_files" / "Assets" / "App.cs").write_text(
                candidate.changes[0].content, encoding="utf-8"
            )
            for name in ("final_verification.json", "final_approval.json", "completion_ledger.json"):
                (output_dir / name).write_text("{}", encoding="utf-8")
            (output_dir / "execution_checkpoint.json").write_text("{}", encoding="utf-8")
            return ExecutionCheckpoint(
                status=PipelineStatus.COMPLETE,
                current_stage="finished",
                completed_stages=["verification"],
                revision_round=0,
                final_verdict=Verdict.PASS,
            )

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
    assert all("Milestone M" in goal for _registry, goal in calls[:-1])
    assert (tmp_path / "work" / "development" / "change_set.json").is_file()
    store = MilestoneStore(tmp_path / "work" / "milestone_state", plan)
    assert all(store.checkpoint(item.milestone_id) is not None for item in plan.milestones)
    assert durable_checkpoints == [item.milestone_id for item in plan.milestones]
