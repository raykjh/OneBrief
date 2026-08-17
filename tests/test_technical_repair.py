from __future__ import annotations

import pytest

from onebrief.convergence_policy import ConvergenceLedger, ConvergencePolicy
from onebrief.technical_repair import (
    RepairProposalKind,
    TechnicalPreflightDecision,
    TechnicalPreflightRequest,
    evaluate_technical_preflight,
    issue_technical_repair_contract,
)


def _failure(policy: ConvergencePolicy, message: str, attempt: int, strategy: str):
    return policy.observe(
        context="development_verification",
        failure_text=message,
        attempt_number=attempt,
        affected_paths=[f"Assets/Scenes/Attempt{attempt}.unity"],
        strategy_fingerprint=strategy,
    )


def _request(observation, contract, *, kind, paths):
    return TechnicalPreflightRequest(
        quest_id="QC-0123456789abcdef",
        source_revision="abcdef1234567",
        observation=observation,
        convergence_contract=contract,
        proposal_kind=kind,
        proposed_paths=paths,
        authorized_project_prefixes=["Assets"],
        trusted_system_prefixes=["src/onebrief", "tests"],
        forbidden_prefixes=["approved-outcome", "evidence/immutable"],
    )


def test_first_technical_failure_allows_one_local_project_repair() -> None:
    policy = ConvergencePolicy()
    observation = _failure(policy, "compile error: MissingType", 1, "local-a")
    contract = policy.issue_contract(ConvergenceLedger(), observation)
    request = _request(
        observation,
        contract,
        kind=RepairProposalKind.CASE_PATCH,
        paths=["Assets/Scripts/Product.cs"],
    )

    preflight = evaluate_technical_preflight(request)
    repair = issue_technical_repair_contract(request, preflight)

    assert preflight.decision == TechnicalPreflightDecision.LOCAL_TECHNICAL_REPAIR
    assert repair.repair_kind.value == "local"
    assert repair.authorized_paths == ["Assets/Scripts/Product.cs"]


def test_second_topology_variant_blocks_case_patch_and_requests_generalization() -> None:
    policy = ConvergencePolicy()
    ledger = ConvergenceLedger()
    first = _failure(
        policy,
        "whole-product topology contains no actual production scene or prefab",
        1,
        "bootstrap-manager",
    )
    ledger = policy.record(ledger, first, policy.issue_contract(ledger, first))
    second = _failure(
        policy,
        "maker-authored topology mapping is not evidence for the product scene",
        2,
        "region-enum",
    )
    contract = policy.issue_contract(ledger, second)
    request = _request(
        second,
        contract,
        kind=RepairProposalKind.CASE_PATCH,
        paths=["Assets/Scenes/Title.unity"],
    )

    preflight = evaluate_technical_preflight(request)

    assert first.structural_cause_id == second.structural_cause_id
    assert contract.generalization_required is True
    assert preflight.decision == TechnicalPreflightDecision.GENERALIZATION_REVIEW
    assert preflight.execution_allowed is False
    with pytest.raises(PermissionError, match="does not authorize"):
        issue_technical_repair_contract(request, preflight)


def test_second_variant_allows_only_reviewed_systemic_compiler_repair() -> None:
    policy = ConvergencePolicy()
    ledger = ConvergenceLedger()
    first = _failure(
        policy,
        "whole-product topology contains no actual production scene or prefab",
        1,
        "bootstrap-manager",
    )
    ledger = policy.record(ledger, first, policy.issue_contract(ledger, first))
    second = _failure(
        policy,
        "maker-authored topology mapping is not evidence for the product scene",
        2,
        "region-enum",
    )
    contract = policy.issue_contract(ledger, second)
    request = _request(
        second,
        contract,
        kind=RepairProposalKind.SYSTEMIC_RULE,
        paths=["src/onebrief/godot_topology.py", "tests/test_godot_topology.py"],
    )

    preflight = evaluate_technical_preflight(request)
    repair = issue_technical_repair_contract(request, preflight)

    assert preflight.decision == TechnicalPreflightDecision.SYSTEMIC_TECHNICAL_REPAIR
    assert repair.repair_kind.value == "systemic"


def test_third_variant_forbids_case_patch_but_not_a_reviewed_systemic_fix() -> None:
    policy = ConvergencePolicy()
    ledger = ConvergenceLedger()
    messages = [
        "whole-product topology contains no actual production scene or prefab",
        "maker-authored topology mapping is not evidence for the product scene",
        "declares missing scene/prefab path Assets/Scenes/Title.unity",
    ]
    current = None
    contract = None
    for index, message in enumerate(messages, 1):
        current = _failure(policy, message, index, f"variant-{index}")
        contract = policy.issue_contract(ledger, current)
        ledger = policy.record(ledger, current, contract)
    case_request = _request(
        current,
        contract,
        kind=RepairProposalKind.CASE_PATCH,
        paths=["Assets/Scenes/Title.unity"],
    )
    systemic_request = _request(
        current,
        contract,
        kind=RepairProposalKind.SYSTEMIC_RULE,
        paths=["src/onebrief/godot_topology.py"],
    )

    assert evaluate_technical_preflight(case_request).decision == TechnicalPreflightDecision.STRUCTURAL_STOP
    assert evaluate_technical_preflight(systemic_request).decision == TechnicalPreflightDecision.SYSTEMIC_TECHNICAL_REPAIR


def test_technical_repair_cannot_weaken_the_verifier() -> None:
    policy = ConvergencePolicy()
    observation = _failure(policy, "compile error: MissingType", 1, "local-a")
    contract = policy.issue_contract(ConvergenceLedger(), observation)
    request = _request(
        observation,
        contract,
        kind=RepairProposalKind.CASE_PATCH,
        paths=["Assets/Scripts/Product.cs"],
    ).model_copy(update={"weakens_verification": True})

    preflight = evaluate_technical_preflight(request)

    assert preflight.decision == TechnicalPreflightDecision.STRUCTURAL_STOP
    assert preflight.execution_allowed is False


@pytest.mark.parametrize(
    "unsafe_path",
    ["../secrets.txt", "Assets/../../secrets.txt", "/tmp/secrets.txt", "C:\\secrets.txt"],
)
def test_technical_preflight_rejects_paths_outside_the_workspace(
    unsafe_path: str,
) -> None:
    policy = ConvergencePolicy()
    observation = _failure(policy, "compile error: MissingType", 1, "local-a")
    contract = policy.issue_contract(ConvergenceLedger(), observation)
    request = _request(
        observation,
        contract,
        kind=RepairProposalKind.CASE_PATCH,
        paths=[unsafe_path],
    )

    with pytest.raises(ValueError, match="escapes the approved workspace"):
        evaluate_technical_preflight(request)
