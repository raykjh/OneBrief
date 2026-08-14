from onebrief.completion_evidence import (
    CompletionEvidenceKind,
    apply_completion_evidence_override,
    apply_trusted_development_evidence,
    validate_completion_evidence,
)
from onebrief.execution_schemas import CriterionCheck, VerificationReport, Verdict
from onebrief.handoff_protocol import (
    ArtifactReference,
    EvidenceKind,
    EvidenceStatus,
    create_evidence_binding,
)
from onebrief.schemas import (
    CompletionContract,
    EvaluationMode,
    IntakeRequest,
    OutputTarget,
    QualityCriterion,
    RequirementsAnalysis,
)


def requirements(goal: str) -> RequirementsAnalysis:
    return RequirementsAnalysis(
        supported=True,
        support_reason="The imported project can be improved safely.",
        normalized_goal=goal,
        deliverables=["A verified project patch"],
        mandatory_information=[],
        optional_information=[],
        acceptance_criteria=["The requested behavior works in the running artifact."],
        assumptions=[],
        consolidated_questions=[],
        ready_for_estimate=True,
    )


def evidence(*command_ids: str) -> dict[str, object]:
    return {
        "development_run": {
            "commands": [
                {"command_id": command_id, "exit_code": 0}
                for command_id in command_ids
            ]
        }
    }


def passing_report() -> VerificationReport:
    return VerificationReport(
        verdict=Verdict.PASS,
        criterion_checks=[
            CriterionCheck(criterion="Model review", passed=True, evidence="Looks complete.")
        ],
        blocking_issues=[],
        revision_instructions=[],
        missing_information=[],
    )


def test_unity_localization_compile_only_cannot_claim_completion() -> None:
    goal = "Unity 게임에 중국어, 일본어, 스페인어 다국어 화면과 언어 드롭다운을 추가한다."
    result = validate_completion_evidence(
        IntakeRequest(goal=goal, output_target=OutputTarget.UNITY_APP),
        requirements(goal),
        evidence("unity_compile"),
    )

    assert result.verdict_override == Verdict.REVISE
    assert {item.kind for item in result.issues} == {
        CompletionEvidenceKind.AUTOMATED_TEST,
        CompletionEvidenceKind.RUNTIME_INTERACTION,
        CompletionEvidenceKind.VISUAL_INTEGRITY,
    }
    overridden = apply_completion_evidence_override(passing_report(), result)
    assert overridden.verdict == Verdict.REVISE
    assert any("missing glyph" in item for item in overridden.blocking_issues)


def test_unity_localization_requires_test_runtime_and_visual_evidence() -> None:
    goal = "Unity multilingual UI localization with a visible language dropdown."
    result = validate_completion_evidence(
        IntakeRequest(goal=goal, output_target=OutputTarget.UNITY_APP),
        requirements(goal),
        evidence("unity_editmode_tests", "unity_playmode_tests", "unity_visual_glyph_check"),
    )

    assert result.verdict_override is None
    assert result.issues == []


def test_non_ui_code_work_is_not_forced_to_produce_screenshots() -> None:
    goal = "Parser error handling and schema validation을 개선한다."
    result = validate_completion_evidence(
        IntakeRequest(goal=goal, output_target=OutputTarget.EXISTING_PROJECT),
        requirements(goal),
        evidence("python_tests"),
    )

    assert result.required == [CompletionEvidenceKind.AUTOMATED_TEST]
    assert result.verdict_override is None


def test_web_ui_runtime_smoke_is_enough_when_visual_localization_is_not_promised() -> None:
    goal = "웹 화면에서 작업 상태를 조회할 수 있게 한다."
    result = validate_completion_evidence(
        IntakeRequest(goal=goal, output_target=OutputTarget.WEB_APP),

        requirements(goal),
        evidence("node_test", "production_http_smoke"),
    )

    assert CompletionEvidenceKind.VISUAL_INTEGRITY not in result.required
    assert result.verdict_override is None


def test_spreadsheet_dashboard_does_not_require_browser_runtime() -> None:
    goal = "운영 현황을 표시하는 대시보드 화면이 포함된 Excel 파일을 만든다."
    result = validate_completion_evidence(
        IntakeRequest(goal=goal, output_target=OutputTarget.SPREADSHEET),
        requirements(goal),
        evidence("spreadsheet_verification"),
    )

    assert result.required == [CompletionEvidenceKind.AUTOMATED_TEST]
    assert result.verdict_override is None


def test_web_artifact_without_development_runtime_is_unverifiable_not_retried() -> None:
    goal = "웹 화면에서 공공 데이터를 조회한다."
    result = validate_completion_evidence(
        IntakeRequest(goal=goal, output_target=OutputTarget.WEB_APP),
        requirements(goal),
        None,
    )

    assert result.verdict_override == Verdict.UNVERIFIABLE
    overridden = apply_completion_evidence_override(passing_report(), result)
    assert overridden.verdict == Verdict.UNVERIFIABLE
    assert overridden.revision_instructions == []


def test_missing_user_information_still_outranks_missing_runtime_proof() -> None:
    goal = "Implement a Unity multilingual user interface."
    completion = validate_completion_evidence(
        IntakeRequest(goal=goal, output_target=OutputTarget.UNITY_APP),
        requirements(goal),
        evidence("unity_compile"),
    )
    model_report = VerificationReport(
        verdict=Verdict.NEEDS_INFORMATION,
        criterion_checks=[
            CriterionCheck(
                criterion="Authoritative locale list",
                passed=False,
                evidence="The required locale list was not supplied.",
            )
        ],
        blocking_issues=["Locale list is missing."],
        revision_instructions=[],
        missing_information=["Provide the required locale list."],
    )

    overridden = apply_completion_evidence_override(model_report, completion)
    assert overridden.verdict == Verdict.NEEDS_INFORMATION
    assert overridden.missing_information == ["Provide the required locale list."]


def test_trusted_compile_receipt_replaces_model_log_misclassification() -> None:
    base = requirements("Modernize the Unity login surface.")
    contract = CompletionContract(
        target_state="The Unity login surface compiles.",
        quality_criteria=[QualityCriterion(
            criterion_id="Q01",
            description="Unity compilation success",
            evaluation_mode=EvaluationMode.DETERMINISTIC,
            evidence_required="Unity compilation log with exit code zero.",
        )],
    )
    report = VerificationReport(
        verdict=Verdict.REVISE,
        criterion_checks=[CriterionCheck(
            criterion_id="Q01",
            criterion="Unity compilation success",
            passed=False,
            evidence="A warning line looked like an exception.",
        )],
        blocking_issues=["Unity compilation failed."],
        revision_instructions=["Repair the reported compiler failure."],
        missing_information=[],
    )

    corrected = apply_trusted_development_evidence(
        report,
        base.model_copy(update={"completion_contract": contract}),
        evidence("unity_compile"),
    )

    assert len(corrected.criterion_checks) == 1
    assert corrected.criterion_checks[0].passed is True
    assert corrected.criterion_checks[0].evidence_bindings[0].command_id == "unity_compile"


def test_trusted_receipt_populates_compile_behavior_and_visual_dimensions() -> None:
    base = requirements("Preserve the Unity authentication path from Login to Lobby.")
    contract = CompletionContract(
        target_state="Login reaches Lobby through the preserved authentication path.",
        quality_criteria=[
            QualityCriterion(
                criterion_id="Q01",
                description="Unity compilation success",
                evaluation_mode=EvaluationMode.DETERMINISTIC,
                evidence_required="Unity compile output.",
            ),
            QualityCriterion(
                criterion_id="Q91",
                description="Preserved Login to Lobby transition behavior",
                evaluation_mode=EvaluationMode.DETERMINISTIC,
                evidence_required="PlayMode interaction evidence.",
            ),
        ],
    )
    bindings = [
        create_evidence_binding(
            criterion_id=None,
            kind=kind,
            status=EvidenceStatus.PASSED,
            summary=summary,
            artifact=(
                ArtifactReference(
                    artifact_type="unity_manifest",
                    path="development/unity/runtime-evidence.json",
                    sha256="a" * 64,
                )
                if kind in {EvidenceKind.BEHAVIOR, EvidenceKind.VISUAL}
                else None
            ),
            command_id=command,
        )
        for kind, summary, command in (
            (EvidenceKind.COMPILE, "Compilation passed.", "unity_compile"),
            (EvidenceKind.BEHAVIOR, "The shipped click path executed.", "unity_playmode"),
            (EvidenceKind.VISUAL, "Rendered PNG integrity passed.", "unity_playmode"),
        )
    ]
    payload = evidence("unity_compile", "unity_playmode")
    payload["verification_receipt"] = {
        "evidence_bindings": [item.model_dump(mode="json") for item in bindings]
    }

    corrected = apply_trusted_development_evidence(
        passing_report(),
        base.model_copy(update={"completion_contract": contract}),
        payload,
    )

    observed = {
        binding.kind
        for check in corrected.criterion_checks
        for binding in check.evidence_bindings
    }
    assert observed == {
        EvidenceKind.COMPILE,
        EvidenceKind.BEHAVIOR,
        EvidenceKind.VISUAL,
    }
