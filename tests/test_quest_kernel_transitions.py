from __future__ import annotations

from copy import deepcopy

import pytest

from onebrief.convergence_policy import (
    ProgressKind,
    RepairContract,
    RepairHypothesis,
)
from onebrief.execution_schemas import (
    CriterionCheck,
    ExecutionCheckpoint,
    PipelineStatus,
    VerificationReport,
    Verdict,
)
from onebrief.handoff_protocol import FailureCode, FailureOwner
from onebrief.phase_execution import PhaseDecision
from onebrief.quest_kernel import (
    QuestKernelEngine,
    QuestTransition,
    RawQuestReceipt,
    interpret_raw_receipt,
    validate_transition,
)
from onebrief.schemas import ExecutionPhase


def checkpoint(
    status: PipelineStatus = PipelineStatus.FAILED,
    verdict: Verdict | None = None,
    message: str = "Trusted execution did not pass.",
) -> ExecutionCheckpoint:
    return ExecutionCheckpoint(
        status=status,
        current_stage="quest_receipt",
        completed_stages=[],
        revision_round=1,
        final_verdict=verdict,
        message=message,
    )


def report(verdict: Verdict = Verdict.REVISE) -> VerificationReport:
    return VerificationReport(
        verdict=verdict,
        criterion_checks=[CriterionCheck(
            criterion_id="Q01",
            criterion="The product runs in Unity.",
            passed=verdict == Verdict.PASS,
            evidence="Trusted Unity execution receipt.",
        )],
        blocking_issues=[] if verdict == Verdict.PASS else ["The product is inert."],
        revision_instructions=[] if verdict == Verdict.PASS else ["Repair the product entrypoint."],
        missing_information=[],
    )


def phase(owner: FailureOwner, next_phase: ExecutionPhase) -> PhaseDecision:
    return PhaseDecision(
        round_number=1,
        failure_code=(
            FailureCode.SEMANTIC_PRODUCT_DEFECT
            if owner == FailureOwner.PRODUCT
            else FailureCode.EVIDENCE_TOPOLOGY_INVALID
        ),
        failure_layer=(
            "semantic_product" if owner == FailureOwner.PRODUCT else "evidence_topology"
        ),
        failure_owner=owner,
        next_phase=next_phase,
        model_repair_allowed=True,
        rationale=f"Trusted verification assigns the repair to {owner.value}.",
    )


def blocked_repair(rationale: str) -> RepairContract:
    return RepairContract(
        contract_id="RC-" + "1" * 16,
        observation_id="FO-" + "2" * 16,
        progress_kind=ProgressKind.NO_PROGRESS,
        occurrence=2,
        hypothesis=RepairHypothesis(
            hypothesis_id="RH-" + "3" * 16,
            suspected_cause=rationale,
            cheapest_probe="Stop and redesign the structure.",
            expected_signal="A new authorized strategy exists.",
            repair_boundary="No maker mutation is currently permitted.",
        ),
        permitted_paths=[],
        verification_ladder=["structural_review"],
        execution_allowed=False,
        escalation_required=True,
        rationale=rationale,
    )


def test_v69_inert_product_receipt_routes_to_product_repair() -> None:
    raw = RawQuestReceipt(
        checkpoint=checkpoint(),
        verification=report(),
        phase_decision=phase(
            FailureOwner.PRODUCT,
            ExecutionPhase.PRODUCT_IMPLEMENTATION,
        ),
    )

    decision = interpret_raw_receipt(raw)

    assert decision.transition == QuestTransition.REPAIR_PRODUCT
    assert validate_transition(raw, decision) == decision


def test_missing_atomic_harness_routes_to_evidence_not_product() -> None:
    raw = RawQuestReceipt(
        checkpoint=checkpoint(),
        verification=report(),
        phase_decision=phase(
            FailureOwner.EVIDENCE,
            ExecutionPhase.EVIDENCE_CONSTRUCTION,
        ),
    )

    decision = interpret_raw_receipt(raw)

    assert decision.transition == QuestTransition.REPAIR_EVIDENCE
    assert validate_transition(raw, decision) == decision


def test_repeated_strategy_stops_without_another_model_repair() -> None:
    raw = RawQuestReceipt(
        checkpoint=checkpoint(message="The same repair produced no new evidence."),
        verification=report(),
        phase_decision=phase(
            FailureOwner.PRODUCT,
            ExecutionPhase.PRODUCT_IMPLEMENTATION,
        ),
        repair_contract=blocked_repair(
            "The same causal failure and repair strategy produced no new evidence."
        ),
    )

    decision = interpret_raw_receipt(raw)

    assert decision.transition == QuestTransition.STRUCTURAL_STOP
    assert not decision.model_repair_allowed
    assert validate_transition(raw, decision) == decision


def test_authorization_boundary_never_becomes_a_repair() -> None:
    raw = RawQuestReceipt(
        checkpoint=checkpoint(
            status=PipelineStatus.NEEDS_AUTHORIZATION,
            message="The approved authentication fixture requires authorization.",
        ),
        verification=report(),
    )

    decision = interpret_raw_receipt(raw)

    assert decision.transition == QuestTransition.NEEDS_AUTHORIZATION
    assert validate_transition(raw, decision) == decision


def test_pass_requires_all_three_authoritative_proofs() -> None:
    raw = RawQuestReceipt(
        checkpoint=checkpoint(PipelineStatus.COMPLETE, Verdict.PASS),
        verification=report(Verdict.PASS),
        completion_ledger_complete=True,
    )
    decision = interpret_raw_receipt(raw)
    assert decision.transition == QuestTransition.PASS
    assert validate_transition(raw, decision) == decision

    incomplete = raw.model_copy(update={"completion_ledger_complete": False})
    stopped = interpret_raw_receipt(incomplete)
    assert stopped.transition == QuestTransition.STRUCTURAL_STOP

    forged = decision.model_copy(update={
        "raw_receipt_sha256": incomplete.sha256,
    })
    with pytest.raises(PermissionError, match="requires execution, verification, and ledger"):
        validate_transition(incomplete, forged)


def test_decision_cannot_be_replayed_against_another_receipt() -> None:
    first = RawQuestReceipt(
        checkpoint=checkpoint(),
        phase_decision=phase(
            FailureOwner.PRODUCT,
            ExecutionPhase.PRODUCT_IMPLEMENTATION,
        ),
    )
    second_payload = deepcopy(first.model_dump(mode="python"))
    second_payload["checkpoint"]["message"] = "A different trusted failure."
    second = RawQuestReceipt.model_validate(second_payload)

    with pytest.raises(PermissionError, match="different raw receipt"):
        validate_transition(second, interpret_raw_receipt(first))


def test_kernel_records_an_append_only_decision_and_current_pointer(tmp_path) -> None:
    raw = RawQuestReceipt(
        checkpoint=checkpoint(),
        phase_decision=phase(
            FailureOwner.PRODUCT,
            ExecutionPhase.PRODUCT_IMPLEMENTATION,
        ),
    )

    engine = QuestKernelEngine(tmp_path / "quest_state")
    first = engine.record(raw)
    second = engine.record(raw)

    assert first == second
    decisions = list((tmp_path / "quest_state" / "transition_decisions").glob("QD-*.json"))
    assert len(decisions) == 1
    pointer = (tmp_path / "quest_state" / "current_transition.json").read_text(
        encoding="utf-8"
    )
    assert decisions[0].stem in pointer
