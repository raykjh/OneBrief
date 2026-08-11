from onebrief.completion_ledger import (
    CompletionStatus,
    build_completion_ledger,
    settle_consistent_verification,
)
from onebrief.execution_schemas import CriterionCheck, VerificationReport, Verdict
from onebrief.schemas import CompletionContract, EvaluationMode, QualityCriterion


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
