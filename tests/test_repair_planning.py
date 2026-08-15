from onebrief.execution_schemas import CriterionCheck, VerificationReport, Verdict
from onebrief.repair_planning import RepairDisposition, build_repair_plan
from onebrief.schemas import CompletionContract, EvaluationMode, QualityCriterion


def _contract() -> CompletionContract:
    return CompletionContract(
        target_state="A verified bilingual web application is runnable.",
        quality_criteria=[
            QualityCriterion(
                criterion_id="Q01",
                description="The existing application still starts.",
                evaluation_mode=EvaluationMode.DETERMINISTIC,
                evidence_required="A successful production build and HTTP probe.",
            ),
            QualityCriterion(
                criterion_id="Q02",
                description="Changing locale visibly translates the meaningful page copy.",
                evaluation_mode=EvaluationMode.DETERMINISTIC,
                evidence_required="Browser observations from two visibly distinct locale states.",
            ),
        ],
    )


def _report() -> VerificationReport:
    return VerificationReport(
        verdict=Verdict.REVISE,
        criterion_checks=[
            CriterionCheck(
                criterion_id="Q01",
                criterion="Startup",
                passed=True,
                evidence="Build and HTTP probe passed.",
            ),
            CriterionCheck(
                criterion_id="Q02",
                criterion="Locale switch",
                passed=False,
                evidence="Visible text did not change after selecting English.",
            ),
        ],
        blocking_issues=["English and Korean rendered states are semantically identical."],
        revision_instructions=["Connect every meaningful text region to the selected locale."],
        missing_information=[],
    )


def test_repair_plan_freezes_passing_criteria_and_scopes_failed_slice() -> None:
    plan = build_repair_plan(_contract(), _report(), round_number=1)

    assert plan is not None
    assert plan.passing_criterion_ids == ["Q01"]
    assert len(plan.tasks) == 1
    assert plan.tasks[0].criterion_id == "Q02"
    assert plan.tasks[0].preserve_criterion_ids == ["Q01"]
    assert plan.tasks[0].disposition == RepairDisposition.BOUNDED_REPAIR


def test_repeated_failure_decomposes_then_stops_blind_retry() -> None:
    first = build_repair_plan(_contract(), _report(), round_number=0)
    assert first is not None
    fingerprint = first.tasks[0].fingerprint

    second = build_repair_plan(
        _contract(), _report(), round_number=1, prior_fingerprints=[fingerprint]
    )
    third = build_repair_plan(
        _contract(), _report(), round_number=2,
        prior_fingerprints=[fingerprint, fingerprint],
    )

    assert second is not None and third is not None
    assert second.tasks[0].disposition == RepairDisposition.DECOMPOSE_SCOPE
    assert "independently verifiable" in second.tasks[0].revision_instructions[0]
    assert third.tasks[0].disposition == RepairDisposition.ESCALATE
    assert third.stop_after_this_round is True


def test_multiple_system_blockers_remain_separate_repair_tasks() -> None:
    report = VerificationReport(
        verdict=Verdict.REVISE,
        criterion_checks=[
            CriterionCheck(criterion="PNG", passed=False, evidence="capture PNG"),
            CriterionCheck(criterion="Scene", passed=False, evidence="operate real scene"),
        ],
        blocking_issues=["capture PNG", "operate real scene"],
        revision_instructions=["Resolve every blocker."],
        missing_information=[],
    )

    plan = build_repair_plan(_contract(), report, round_number=0)

    assert plan is not None
    assert [task.failure_evidence for task in plan.tasks] == [
        ["capture PNG"], ["operate real scene"]
    ]


def test_criterion_cannot_be_preserved_when_an_independent_check_fails_it() -> None:
    report = VerificationReport(
        verdict=Verdict.REVISE,
        criterion_checks=[
            CriterionCheck(
                criterion_id="Q02", criterion="Locale switch", passed=True,
                evidence="The PNG file passed integrity checks.",
            ),
            CriterionCheck(
                criterion_id="Q02", criterion="Locale switch", passed=False,
                evidence="Independent observation found unchanged visible text.",
            ),
        ],
        blocking_issues=["The rendered locale did not change."],
        revision_instructions=["Repair the production locale binding."],
        missing_information=[],
    )

    plan = build_repair_plan(_contract(), report, round_number=1)

    assert plan is not None
    assert plan.passing_criterion_ids == []
    assert plan.tasks[0].criterion_id == "Q02"
    assert plan.tasks[0].preserve_criterion_ids == []
