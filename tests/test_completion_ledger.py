from onebrief.completion_ledger import (
    CompletionStatus,
    build_completion_ledger,
    settle_consistent_verification,
)
from onebrief.execution_schemas import CriterionCheck, VerificationReport, Verdict
from onebrief.handoff_protocol import EvidenceBinding
from onebrief.development_toolpack import (
    DevelopmentCommandResult,
    DevelopmentVerificationReceipt,
)
from onebrief.handoff_protocol import (
    EvidenceKind,
    EvidenceStatus,
    create_evidence_binding,
)
from onebrief.schemas import CompletionContract, EvaluationMode, QualityCriterion
from onebrief.execution_pipeline import ExecutionPipeline


def contract() -> CompletionContract:
    return CompletionContract(
        target_state="A working web application is delivered.",
        quality_criteria=[
            QualityCriterion(
                criterion_id="Q01",
                description="The application starts successfully.",
                evaluation_mode=EvaluationMode.DETERMINISTIC,
                evidence_required="A successful HTTP health check.",
            ),
            QualityCriterion(
                criterion_id="Q02",
                description="The requested workflow is complete.",
                evidence_required="Independent review against the requested workflow.",
            ),
        ],
    )


def test_ledger_preserves_failure_then_same_criterion_pass() -> None:
    first = VerificationReport(
        verdict=Verdict.REVISE,
        criterion_checks=[
            CriterionCheck(
                criterion_id="Q01", criterion="Startup", passed=True,
                evidence="GET /health returned 200.",
            ),
            CriterionCheck(
                criterion_id="Q02", criterion="Workflow", passed=False,
                evidence="The submit action does not persist the result.",
            ),
        ],
        blocking_issues=["The result is not persisted."],
        revision_instructions=["Persist the result and verify it after reload."],
        missing_information=[],
    )
    second = VerificationReport(
        verdict=Verdict.PASS,
        criterion_checks=[
            CriterionCheck(
                criterion_id="Q01", criterion="Startup", passed=True,
                evidence="GET /health returned 200.",
            ),
            CriterionCheck(
                criterion_id="Q02", criterion="Workflow", passed=True,
                evidence="The saved result remained after reload.",
            ),
        ],
        blocking_issues=[], revision_instructions=[], missing_information=[],
    )

    ledger = build_completion_ledger(contract(), [(0, first), (1, second)])

    assert ledger.complete is True
    assert ledger.required_passed == 2
    assert ledger.criteria[1].status == CompletionStatus.PASSED
    assert [attempt.passed for attempt in ledger.criteria[1].attempts] == [False, True]
    assert ledger.criteria[1].attempts[0].evidence == "The submit action does not persist the result."


def test_model_pass_cannot_complete_an_unmatched_required_criterion() -> None:
    report = VerificationReport(
        verdict=Verdict.PASS,
        criterion_checks=[CriterionCheck(
            criterion="Unrelated system check", passed=True, evidence="A file exists."
        )],
        blocking_issues=[], revision_instructions=[], missing_information=[],
    )

    ledger = build_completion_ledger(contract(), [(0, report)])

    assert ledger.complete is False
    assert ledger.required_passed == 0
    assert all(item.status == CompletionStatus.PENDING for item in ledger.criteria)


def test_same_report_semantic_failure_overrides_png_integrity_pass() -> None:
    report = VerificationReport(
        verdict=Verdict.UNVERIFIABLE,
        criterion_checks=[
            CriterionCheck(
                criterion_id="Q02", criterion="Workflow", passed=True,
                evidence="The PNG exists and its digest is valid.",
            ),
            CriterionCheck(
                criterion_id="Q02", criterion="Workflow", passed=False,
                evidence="Independent observation shows the requested UI is absent.",
            ),
        ],
        blocking_issues=["The requested UI is absent."],
        revision_instructions=[], missing_information=[],
    )

    ledger = build_completion_ledger(contract(), [(0, report)])

    assert ledger.criteria[1].status == CompletionStatus.UNVERIFIABLE
    assert ledger.required_passed == 0


def test_all_passing_checks_settle_a_contradictory_revise() -> None:
    report = VerificationReport(
        verdict=Verdict.REVISE,
        criterion_checks=[
            CriterionCheck(
                criterion_id="Q01", criterion="Startup", passed=True,
                evidence="HTTP health check passed.",
            ),
            CriterionCheck(
                criterion_id="Q02", criterion="Workflow", passed=True,
                evidence="The complete workflow passed.",
            ),
        ],
        blocking_issues=["Revise wording anyway."],
        revision_instructions=["Revise wording anyway."],
        missing_information=[],
    )

    settled = settle_consistent_verification(contract(), report)

    assert settled.verdict == Verdict.PASS
    assert settled.blocking_issues == []
    assert settled.revision_instructions == []


def test_failed_system_check_prevents_settlement() -> None:
    report = VerificationReport(
        verdict=Verdict.REVISE,
        criterion_checks=[
            CriterionCheck(
                criterion_id="Q01", criterion="Startup", passed=True,
                evidence="Build passed.",
            ),
            CriterionCheck(
                criterion_id="Q02", criterion="Workflow", passed=True,
                evidence="Code review passed.",
            ),
            CriterionCheck(
                criterion="Runtime interaction", passed=False,
                evidence="No HTTP evidence exists.",
            ),
        ],
        blocking_issues=["No HTTP evidence exists."],
        revision_instructions=["Run the application."],
        missing_information=[],
    )

    assert settle_consistent_verification(contract(), report).verdict == Verdict.REVISE


def test_generic_pass_cannot_replace_required_criterion_checks() -> None:
    report = VerificationReport(
        verdict=Verdict.PASS,
        criterion_checks=[CriterionCheck(
            criterion="Generic system review",
            passed=True,
            evidence="The document looks complete.",
        )],
        blocking_issues=[],
        revision_instructions=[],
        missing_information=[],
    )

    settled = settle_consistent_verification(contract(), report)

    assert settled.verdict == Verdict.REVISE
    assert "Q01" in settled.blocking_issues[0]
    assert "Q02" in settled.blocking_issues[0]


def test_equal_count_unbound_system_failures_do_not_overwrite_contract_criteria() -> None:
    report = VerificationReport(
        verdict=Verdict.REVISE,
        criterion_checks=[
            CriterionCheck(
                criterion="System blocker 1", passed=False,
                evidence="A screenshot is unavailable.",
            ),
            CriterionCheck(
                criterion="System blocker 2", passed=False,
                evidence="A duplicate repair was rejected.",
            ),
        ],
        blocking_issues=["A screenshot is unavailable."],
        revision_instructions=["Repair evidence only."],
        missing_information=[],
    )

    ledger = build_completion_ledger(contract(), [(0, report)])

    assert all(item.status == CompletionStatus.PENDING for item in ledger.criteria)


def test_compile_behavior_and_visual_evidence_are_independent_dimensions() -> None:
    checks = []
    for kind, status, summary in (
        (EvidenceKind.COMPILE, EvidenceStatus.PASSED, "Unity compile exited 0."),
        (EvidenceKind.BEHAVIOR, EvidenceStatus.PASSED, "Login reached Lobby."),
        (EvidenceKind.VISUAL, EvidenceStatus.MISSING, "Login PNG is unavailable."),
    ):
        binding = create_evidence_binding(
            criterion_id=None, kind=kind, status=status, summary=summary
        )
        checks.append(CriterionCheck(
            criterion=f"System {kind.value}",
            passed=status == EvidenceStatus.PASSED,
            evidence=summary,
            evidence_bindings=[binding],
        ))
    report = VerificationReport(
        verdict=Verdict.REVISE,
        criterion_checks=checks,
        blocking_issues=["Login PNG is unavailable."],
        revision_instructions=["Repair evidence capture only."],
        missing_information=[],
    )

    ledger = build_completion_ledger(contract(), [(0, report)])
    dimensions = {item.kind: item for item in ledger.evidence_dimensions}

    assert dimensions[EvidenceKind.COMPILE].status == CompletionStatus.PASSED
    assert dimensions[EvidenceKind.BEHAVIOR].status == CompletionStatus.PASSED
    assert dimensions[EvidenceKind.VISUAL].status == CompletionStatus.REVISE


def test_unity_screenshot_failure_preserves_bound_compile_and_behavior_passes() -> None:
    unity_contract = CompletionContract(
        target_state="Login reaches Lobby with inspectable visual evidence.",
        quality_criteria=[
            QualityCriterion(
                criterion_id="Q01",
                description="Unity compilation success",
                evaluation_mode=EvaluationMode.DETERMINISTIC,
                evidence_required="Unity compile output with exit code zero.",
            ),
            QualityCriterion(
                criterion_id="Q91",
                description="Login to Lobby PlayMode behavior",
                evaluation_mode=EvaluationMode.DETERMINISTIC,
                evidence_required="PlayMode interaction evidence for Login to Lobby.",
            ),
            QualityCriterion(
                criterion_id="Q92",
                description="Login visual screenshot integrity",
                evaluation_mode=EvaluationMode.DETERMINISTIC,
                evidence_required="A validated rendered PNG screenshot.",
            ),
        ],
    )
    commands = [
        DevelopmentCommandResult(
            command_id="unity_compile", argv=["Unity"], exit_code=0,
            duration_seconds=1, output_tail="compiled",
        ),
        DevelopmentCommandResult(
            command_id="unity_playmode_visual_tests", argv=["Unity"], exit_code=0,
            duration_seconds=1, output_tail="login reached lobby",
        ),
    ]
    receipt = DevelopmentVerificationReceipt(
        repository_name="julpae", base_head_sha="a" * 40,
        candidate_sha256="b" * 64, commands=commands,
        evidence_bindings=[
            create_evidence_binding(
                criterion_id=None, kind=EvidenceKind.COMPILE,
                status=EvidenceStatus.PASSED, summary="Unity compile exited 0.",
                command_id="unity_compile",
            ),
            create_evidence_binding(
                criterion_id=None, kind=EvidenceKind.BEHAVIOR,
                status=EvidenceStatus.PASSED, summary="Login reached Lobby in PlayMode.",
                command_id="unity_playmode_visual_tests",
            ),
        ],
    )

    report = ExecutionPipeline._development_failure_report(
        "Unity visual scenario login_to_lobby screenshot is unavailable",
        unity_contract,
        receipt,
    )
    ledger = build_completion_ledger(unity_contract, [(3, report)])
    states = {item.criterion_id: item.status for item in ledger.criteria}

    assert states["Q01"] == CompletionStatus.PASSED
    assert states["Q91"] == CompletionStatus.PASSED
    assert states["Q92"] == CompletionStatus.PENDING
    dimensions = {item.kind: item.status for item in ledger.evidence_dimensions}
    assert dimensions[EvidenceKind.COMPILE] == CompletionStatus.PASSED
    assert dimensions[EvidenceKind.BEHAVIOR] == CompletionStatus.PASSED
    assert dimensions[EvidenceKind.VISUAL] == CompletionStatus.REVISE


def test_compile_receipt_cannot_satisfy_mixed_behavior_criterion() -> None:
    mixed_contract = CompletionContract(
        target_state="Login reaches Lobby.",
        quality_criteria=[
            QualityCriterion(
                criterion_id="Q91",
                description="Login surface and preserved authentication transition work",
                evaluation_mode=EvaluationMode.DETERMINISTIC,
                evidence_required="Unity compilation and PlayMode interaction evidence for Login to Lobby.",
            ),
        ],
    )
    receipt = DevelopmentVerificationReceipt(
        repository_name="julpae", base_head_sha="a" * 40,
        candidate_sha256="b" * 64,
        commands=[],
        evidence_bindings=[create_evidence_binding(
            criterion_id=None, kind=EvidenceKind.COMPILE,
            status=EvidenceStatus.PASSED, summary="Unity compile exited 0.",
            command_id="unity_compile",
        )],
    )

    report = ExecutionPipeline._development_failure_report(
        "Unity visual scenario login screenshot is unavailable",
        mixed_contract,
        receipt,
    )

    assert all(check.criterion_id != "Q91" for check in report.criterion_checks)
def test_foreign_decision_ids_cannot_become_completion_criteria() -> None:
    report = VerificationReport.model_validate({
        "verdict": "PASS",
        "criterion_checks": [{
            "criterion_id": "S02",
            "criterion": "A SixSense style choice",
            "passed": True,
            "evidence": "The model mentioned a preference decision.",
            "evidence_bindings": [{
                "binding_id": "EB-0123456789abcdef",
                "criterion_id": "S02",
                "kind": "visual",
                "status": "passed",
                "summary": "Visible but not an active completion criterion.",
            }],
        }],
        "blocking_issues": [],
        "revision_instructions": [],
        "missing_information": [],
    })

    assert report.criterion_checks[0].criterion_id is None
    assert report.criterion_checks[0].evidence_bindings[0].criterion_id is None


def test_valid_completion_criterion_ids_are_normalized_and_preserved() -> None:
    binding = EvidenceBinding.model_validate({
        "binding_id": "EB-fedcba9876543210",
        "criterion_id": "q01",
        "kind": "compile",
        "status": "passed",
        "summary": "Unity compiled.",
    })

    assert binding.criterion_id == "Q01"
