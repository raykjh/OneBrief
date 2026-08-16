from __future__ import annotations

import json
from pathlib import Path

import pytest

from onebrief.completion_ledger import build_completion_ledger
from onebrief.execution_schemas import (
    CriterionCheck,
    ExecutionCheckpoint,
    PipelineStatus,
    VerificationReport,
    Verdict,
)
from onebrief.milestones import MilestoneStore, build_milestone_plan, canonical_sha256
from onebrief.quest_orchestration import (
    QuestFailureOwner,
    QuestState,
    QuestStore,
    infer_failure_owner,
)
from onebrief.schemas import (
    AssuranceSelection,
    AssuranceUse,
    CompletionContract,
    EvaluationMode,
    ExecutionPhase,
    QualityCriterion,
    RequirementsAnalysis,
    SixSensePlan,
)


def requirements() -> RequirementsAnalysis:
    criteria = [
        QualityCriterion(
            criterion_id="Q01",
            description="Unity compilation success",
            evaluation_mode=EvaluationMode.DETERMINISTIC,
            evidence_required="Compilation logs with zero errors.",
        ),
        QualityCriterion(
            criterion_id="Q02",
            description="Login to Lobby flow works",
            evaluation_mode=EvaluationMode.DETERMINISTIC,
            evidence_required="PlayMode authentication and navigation evidence.",
        ),
        QualityCriterion(
            criterion_id="Q03",
            description="Settings remain usable",
            evaluation_mode=EvaluationMode.DETERMINISTIC,
            evidence_required="PlayMode settings evidence.",
        ),
    ]
    return RequirementsAnalysis(
        supported=True,
        support_reason="Supported project work.",
        normalized_goal="Modernize the Unity client while preserving behavior.",
        deliverables=["Verified Unity patch"],
        mandatory_information=[],
        optional_information=[],
        acceptance_criteria=[item.description for item in criteria],
        completion_contract=CompletionContract(
            target_state="A modernized, working Unity client.",
            quality_criteria=criteria,
        ),
        sixsense=SixSensePlan(
            standard_profile="Preserve behavior and prove the running Unity result.",
            assurance=AssuranceSelection(
                intended_use=AssuranceUse.OPERATIONAL_RELEASE,
                rationale="This is runnable software, not a proposal.",
            ),
        ),
        assumptions=["Preserve authentication and server behavior."],
        consolidated_questions=[],
        ready_for_estimate=True,
    )


def new_client_requirements() -> RequirementsAnalysis:
    current = requirements()
    return current.model_copy(update={
        "normalized_goal": (
            "Build a new Unity client presentation layer while reusing approved server contracts and assets."
        ),
        "deliverables": ["New Unity client Login and Lobby production surfaces"],
        "completion_contract": current.completion_contract.model_copy(update={
            "target_state": "A new Unity client presentation layer is runnable.",
        }),
    })


def test_outcome_and_quest_preserve_new_construction_authority(tmp_path: Path) -> None:
    req = new_client_requirements()
    current_plan = build_milestone_plan(
        project_id="julpae",
        goal=req.normalized_goal,
        requirements=req,
        source_revision="a" * 40,
        minimum_cost_usd=1,
        maximum_cost_usd=10,
    )
    milestone_store = MilestoneStore(tmp_path / "milestones", current_plan)
    baseline = current_plan.milestones[0]
    milestone_store.record_pass(
        milestone=baseline,
        source_before="a" * 40,
        source_after="a" * 40,
        candidate_sha256=canonical_sha256({"baseline": True}),
        evidence_paths=[evidence(tmp_path / "baseline-new-client.json")],
    )
    store = QuestStore(
        tmp_path / "quests", plan=current_plan, requirements=req,
        approved_budget_usd=10,
    )

    quest = store.issue(
        milestone=current_plan.milestones[1],
        milestone_store=milestone_store,
        source_revision="a" * 40,
    )

    assert store.outcome.construction_directives
    assert any("Test-only changes" in item for item in quest.required_process)


def plan():
    return build_milestone_plan(
        project_id="julpae",
        goal="Modernize Unity Login, Lobby, and Settings",
        requirements=requirements(),
        source_revision="a" * 40,
        minimum_cost_usd=1,
        maximum_cost_usd=10,
    )


def evidence(path: Path, content: str = "evidence") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def baseline_store(tmp_path: Path):
    current_plan = plan()
    store = MilestoneStore(tmp_path / "milestones", current_plan)
    baseline = current_plan.milestones[0]
    store.record_pass(
        milestone=baseline,
        source_before="a" * 40,
        source_after="a" * 40,
        candidate_sha256=canonical_sha256({"baseline": True}),
        evidence_paths=[evidence(tmp_path / "baseline.json")],
    )
    return current_plan, store


def write_pass(output_dir: Path, contract: CompletionContract) -> ExecutionCheckpoint:
    output_dir.mkdir(parents=True, exist_ok=True)
    report = VerificationReport(
        verdict=Verdict.PASS,
        criterion_checks=[CriterionCheck(
            criterion_id=item.criterion_id,
            criterion=item.description,
            passed=True,
            evidence="Fresh executable evidence.",
        ) for item in contract.quality_criteria],
        blocking_issues=[],
        revision_instructions=[],
        missing_information=[],
    )
    ledger = build_completion_ledger(contract, [(0, report)])
    checkpoint = ExecutionCheckpoint(
        status=PipelineStatus.COMPLETE,
        current_stage="finished",
        completed_stages=["independent_verification"],
        revision_round=0,
        final_verdict=Verdict.PASS,
        message="Independent verification passed.",
    )
    (output_dir / "final_verification.json").write_text(report.model_dump_json(indent=2), encoding="utf-8")
    (output_dir / "completion_ledger.json").write_text(ledger.model_dump_json(indent=2), encoding="utf-8")
    (output_dir / "execution_checkpoint.json").write_text(checkpoint.model_dump_json(indent=2), encoding="utf-8")
    return checkpoint


def test_only_m01_quest_exists_until_m01_receipt_passes(tmp_path: Path) -> None:
    current_plan, milestone_store = baseline_store(tmp_path)
    quests = QuestStore(tmp_path / "quests", plan=current_plan, requirements=requirements())
    m01, m02 = current_plan.milestones[1:3]

    first = quests.issue(milestone=m01, milestone_store=milestone_store, source_revision="a" * 40)
    assert first.milestone_id == "M01"
    assert first.initial_execution_phase == ExecutionPhase.EVIDENCE_CONSTRUCTION
    assert quests.outcome.assurance_policy is not None
    assert quests.outcome.assurance_policy.intended_use == AssuranceUse.OPERATIONAL_RELEASE
    assert first.assurance_profile_sha256 == quests.outcome.assurance_policy.sha256
    assert any("operational_release" in item for item in first.required_process)
    assert list((tmp_path / "quests" / "contracts").glob("*.json")) == [
        tmp_path / "quests" / "contracts" / f"{first.quest_id}.json"
    ]
    with pytest.raises(RuntimeError, match="another Quest is active"):
        quests.issue(milestone=m02, milestone_store=milestone_store, source_revision="a" * 40)

    m01_requirements = requirements().model_copy(update={
        "completion_contract": CompletionContract(
            target_state=m01.contract.target_state,
            quality_criteria=[
                item for item in requirements().completion_contract.quality_criteria
                if item.criterion_id in m01.contract.criterion_ids
            ] + list(m01.contract.slice_quality_criteria),
        )
    })
    output = tmp_path / "M01"
    checkpoint = write_pass(output, m01_requirements.completion_contract)
    receipt = quests.record_result(
        quest=first, checkpoint=checkpoint, output_dir=output,
        failure_owner=QuestFailureOwner.NONE,
    )
    assert receipt.state == QuestState.PASSED
    transition = json.loads((tmp_path / "quests" / "current_transition.json").read_text(
        encoding="utf-8"
    ))
    assert transition["decision_id"].startswith("QD-")
    milestone_store.record_pass(
        milestone=m01,
        source_before="a" * 40,
        source_after="b" * 40,
        candidate_sha256=canonical_sha256({"m01": True}),
        evidence_paths=[output / "completion_ledger.json", output / "final_verification.json", output / "execution_checkpoint.json"],
    )
    second = quests.issue(milestone=m02, milestone_store=milestone_store, source_revision="b" * 40)
    assert second.initial_execution_phase == ExecutionPhase.PRODUCT_IMPLEMENTATION
    assert second.parent_quest_id == first.quest_id
    assert second.input_checkpoint.previous_receipt_id == receipt.receipt_id
    assert receipt.receipt_id in second.preserve_receipt_ids
    assert second.input_checkpoint.dependency_checkpoints["M01"] == milestone_store.checkpoint("M01").checkpoint_id


def test_quest_store_rejects_assurance_profile_changed_after_milestone_plan(tmp_path: Path) -> None:
    current_plan = plan()
    changed = requirements().model_copy(update={
        "sixsense": requirements().sixsense.model_copy(update={
            "assurance": AssuranceSelection(
                intended_use=AssuranceUse.COMMERCIAL_PROPOSAL,
                rationale="This is now only a proposal.",
            )
        })
    })

    with pytest.raises(RuntimeError, match="different assurance profile"):
        QuestStore(tmp_path / "quests", plan=current_plan, requirements=changed)


def test_pass_requires_ledger_and_independent_verification(tmp_path: Path) -> None:
    current_plan, milestone_store = baseline_store(tmp_path)
    quests = QuestStore(tmp_path / "quests", plan=current_plan, requirements=requirements())
    quest = quests.issue(
        milestone=current_plan.milestones[1], milestone_store=milestone_store,
        source_revision="a" * 40,
    )
    output = tmp_path / "missing-proof"
    output.mkdir()
    checkpoint = ExecutionCheckpoint(
        status=PipelineStatus.COMPLETE,
        current_stage="finished",
        completed_stages=[],
        revision_round=0,
        final_verdict=Verdict.PASS,
        message="Outer process claims PASS.",
    )
    receipt = quests.record_result(
        quest=quest, checkpoint=checkpoint, output_dir=output,
        failure_owner=QuestFailureOwner.ORCHESTRATOR,
    )
    assert receipt.state == QuestState.BLOCKED
    assert receipt.verdict == Verdict.PASS
    assert receipt.completion_ledger_sha256 is None


def test_no_progress_authentication_receipt_blocks_successor(tmp_path: Path) -> None:
    current_plan, milestone_store = baseline_store(tmp_path)
    quests = QuestStore(tmp_path / "quests", plan=current_plan, requirements=requirements())
    milestone = current_plan.milestones[1]
    quest = quests.issue(milestone=milestone, milestone_store=milestone_store, source_revision="a" * 40)
    output = tmp_path / "blocked"
    output.mkdir()
    (output / "repair_contract_f03.json").write_text(json.dumps({
        "schema_version": "onebrief-repair-contract-v1",
        "contract_id": "RC-" + "1" * 16,
        "observation_id": "FO-" + "2" * 16,
        "progress_kind": "no_progress",
        "occurrence": 2,
        "hypothesis": {
            "hypothesis_id": "RH-" + "3" * 16,
            "suspected_cause": "Approved authentication fixture requires authorization.",
            "cheapest_probe": "Request the missing authority.",
            "expected_signal": "The approved runtime can cross the boundary.",
            "repair_boundary": "No maker mutation is currently permitted.",
            "requires_model_reasoning": False,
        },
        "permitted_paths": [],
        "preserve_criterion_ids": [],
        "verification_ladder": ["authorization"],
        "execution_allowed": False,
        "escalation_required": True,
        "rationale": "Approved authentication fixture requires authorization.",
    }), encoding="utf-8")
    checkpoint = ExecutionCheckpoint(
        status=PipelineStatus.FAILED,
        current_stage="failed",
        completed_stages=[],
        revision_round=2,
        message="No progress at the authentication fixture boundary.",
    )
    receipt = quests.record_result(
        quest=quest, checkpoint=checkpoint, output_dir=output,
        failure_owner=QuestFailureOwner.EVIDENCE,
    )
    assert receipt.state == QuestState.NEEDS_AUTHORIZATION
    assert receipt.failure_owner == QuestFailureOwner.AUTHORIZATION
    transition = json.loads((tmp_path / "quests" / "current_transition.json").read_text(
        encoding="utf-8"
    ))
    recorded = json.loads(
        (tmp_path / "quests" / transition["path"]).read_text(encoding="utf-8")
    )
    assert recorded["decision"]["transition"] == "needs_authorization"
    with pytest.raises(PermissionError, match="requires authorization"):
        quests.issue(milestone=milestone, milestone_store=milestone_store, source_revision="a" * 40)


def test_failure_ownership_prefers_structured_receipt_and_orchestrator_errors(tmp_path: Path) -> None:
    structured = tmp_path / "structured"
    structured.mkdir()
    (structured / "phase_decision_deterministic_r2.json").write_text(json.dumps({
        "failure_owner": "evidence"
    }), encoding="utf-8")
    checkpoint = ExecutionCheckpoint(
        status=PipelineStatus.FAILED,
        current_stage="failed",
        completed_stages=[],
        revision_round=2,
        message="Product-looking runtime failure.",
    )
    assert infer_failure_owner(structured, checkpoint) == QuestFailureOwner.EVIDENCE

    provider = tmp_path / "provider"
    provider.mkdir()
    provider_checkpoint = checkpoint.model_copy(update={
        "message": "ClientError: 400 INVALID_ARGUMENT in provider schema"
    })
    assert infer_failure_owner(provider, provider_checkpoint) == QuestFailureOwner.ORCHESTRATOR

    (provider / "phase_decision_deterministic_r3.json").write_text(json.dumps({
        "failure_owner": "product"
    }), encoding="utf-8")
    routing_checkpoint = checkpoint.model_copy(update={
        "message": "ValidationError: catalog-anchored product repair cannot target proof artifacts"
    })
    assert infer_failure_owner(provider, routing_checkpoint) == QuestFailureOwner.ORCHESTRATOR


def test_same_milestone_followup_shares_budget_instead_of_reserving_twice(tmp_path: Path) -> None:
    current_plan, milestone_store = baseline_store(tmp_path)
    quests = QuestStore(tmp_path / "quests", plan=current_plan, requirements=requirements())
    milestone = current_plan.milestones[1]
    first = quests.issue(milestone=milestone, milestone_store=milestone_store, source_revision="a" * 40)
    output = tmp_path / "failed"
    output.mkdir()
    (output / "phase_decision_deterministic_r1.json").write_text(json.dumps({
        "schema_version": "onebrief-phase-decision-v1",
        "round_number": 1,
        "failure_code": "evidence_topology_invalid",
        "failure_layer": "evidence_topology",
        "failure_owner": "evidence",
        "next_phase": "evidence_construction",
        "model_repair_allowed": True,
        "rationale": "The trusted Receipt assigns this repair to evidence.",
    }), encoding="utf-8")
    checkpoint = ExecutionCheckpoint(
        status=PipelineStatus.PARTIAL,
        current_stage="finished",
        completed_stages=[],
        revision_round=1,
        final_verdict=Verdict.REVISE,
        message="A bounded evidence repair remains.",
    )
    quests.record_result(
        quest=first, checkpoint=checkpoint, output_dir=output,
        failure_owner=QuestFailureOwner.EVIDENCE,
    )
    issued_before = quests.canvas().issued_budget_usd
    second = quests.issue(milestone=milestone, milestone_store=milestone_store, source_revision="a" * 40)
    assert second.parent_quest_id == first.quest_id
    assert second.initial_execution_phase == ExecutionPhase.EVIDENCE_CONSTRUCTION
    assert "From blocked receipt" in second.objective
    assert quests.canvas().issued_budget_usd == issued_before


def test_structural_stop_cannot_issue_a_followup_quest(tmp_path: Path) -> None:
    current_plan, milestone_store = baseline_store(tmp_path)
    quests = QuestStore(tmp_path / "quests", plan=current_plan, requirements=requirements())
    milestone = current_plan.milestones[1]
    quest = quests.issue(
        milestone=milestone,
        milestone_store=milestone_store,
        source_revision="a" * 40,
    )
    output = tmp_path / "structural-stop"
    output.mkdir()
    (output / "repair_contract_f02.json").write_text(json.dumps({
        "schema_version": "onebrief-repair-contract-v1",
        "contract_id": "RC-" + "4" * 16,
        "observation_id": "FO-" + "5" * 16,
        "progress_kind": "no_progress",
        "occurrence": 2,
        "hypothesis": {
            "hypothesis_id": "RH-" + "6" * 16,
            "suspected_cause": "The same strategy did not change trusted evidence.",
            "cheapest_probe": "Redesign the product construction mechanism.",
            "expected_signal": "A new mechanism changes the trusted failure.",
            "repair_boundary": "No maker mutation is currently permitted.",
            "requires_model_reasoning": False,
        },
        "permitted_paths": [],
        "preserve_criterion_ids": [],
        "verification_ladder": ["structural_review"],
        "execution_allowed": False,
        "escalation_required": True,
        "rationale": "The same strategy produced no new evidence.",
    }), encoding="utf-8")
    checkpoint = ExecutionCheckpoint(
        status=PipelineStatus.FAILED,
        current_stage="failed",
        completed_stages=[],
        revision_round=2,
        message="The same strategy produced no new evidence.",
    )
    quests.record_result(
        quest=quest,
        checkpoint=checkpoint,
        output_dir=output,
        failure_owner=QuestFailureOwner.ORCHESTRATOR,
    )

    with pytest.raises(RuntimeError, match="structural redesign"):
        quests.issue(
            milestone=milestone,
            milestone_store=milestone_store,
            source_revision="a" * 40,
        )


def test_quest_budget_binds_actual_user_approval_not_quote_maximum(tmp_path: Path) -> None:
    current_plan, milestone_store = baseline_store(tmp_path)
    quests = QuestStore(
        tmp_path / "quests", plan=current_plan, requirements=requirements(),
        approved_budget_usd=3.0,
    )
    milestone = current_plan.milestones[1]
    quest = quests.issue(
        milestone=milestone, milestone_store=milestone_store,
        source_revision="a" * 40,
    )
    assert quests.canvas().approved_budget_usd == 3.0
    assert quest.budget.maximum_usd == pytest.approx(3.0 * milestone.budget_weight)
    assert quest.budget.maximum_usd < current_plan.maximum_cost_usd * milestone.budget_weight

    with pytest.raises(ValueError, match="exceeds the quoted maximum"):
        QuestStore(
            tmp_path / "invalid", plan=current_plan, requirements=requirements(),
            approved_budget_usd=current_plan.maximum_cost_usd + 1,
        )
