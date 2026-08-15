from onebrief.execution_schemas import CriterionCheck, VerificationReport, Verdict
from onebrief.observation_packs import (
    SemanticFrameFinding,
    UnityLocalizationObservationPack,
)
from onebrief.reality_check import (
    RealityCapability,
    apply_reality_check_override,
    evaluate_reality_check,
)
from onebrief.schemas import (
    CompletionContract,
    IntakeRequest,
    OutputTarget,
    QualityCriterion,
    RequirementsAnalysis,
)


def _requirements(goal: str) -> RequirementsAnalysis:
    return RequirementsAnalysis(
        supported=True,
        support_reason="Supported imported-project improvement.",
        normalized_goal=goal,
        deliverables=["A verified patch"],
        mandatory_information=[],
        optional_information=[],
        acceptance_criteria=["The requested behavior works in the running artifact."],
        assumptions=[],
        consolidated_questions=[],
        ready_for_estimate=True,
    )


def _evidence(*commands: str, receipts: list[dict[str, object]] | None = None) -> dict[str, object]:
    return {
        "development_run": {
            "commands": [{"command_id": item, "exit_code": 0} for item in commands]
        },
        "trusted_observation_receipts": receipts or [],
    }


def _pass() -> VerificationReport:
    return VerificationReport(
        verdict=Verdict.PASS,
        criterion_checks=[CriterionCheck(
            criterion="Model review", passed=True, evidence="Claimed complete."
        )],
        blocking_issues=[], revision_instructions=[], missing_information=[],
    )


def test_semantic_ui_cannot_pass_from_screenshot_names_and_hashes() -> None:
    goal = "Add multilingual Chinese and Japanese UI and verify visible language and broken glyphs."
    result = evaluate_reality_check(
        IntakeRequest(goal=goal, output_target=OutputTarget.UNITY_APP),
        _requirements(goal),
        _evidence("unity_tests", "unity_playmode", "unity_visual_screenshot_glyph"),
    )

    assert result.verdict_override == Verdict.UNVERIFIABLE
    assert any(
        item.capability == RealityCapability.SEMANTIC_OBSERVATION
        for item in result.requirements
    )
    overridden = apply_reality_check_override(_pass(), result)
    assert overridden.verdict == Verdict.UNVERIFIABLE
    assert overridden.revision_instructions == []


def test_independent_semantic_pack_receipt_allows_verification_to_stand() -> None:
    goal = "Add multilingual Chinese and Japanese UI and verify visible language."
    pack = UnityLocalizationObservationPack()
    receipt = pack.build_receipt([
        SemanticFrameFinding(
            artifact_path="screenshots/locale_ja.png",
            expected_state="Japanese locale",
            visible_text_samples=["設定", "言語"],
            semantic_findings=["Visible labels are Japanese and readable."],
            passed=True,
        )
    ], observer_is_independent=True)
    result = evaluate_reality_check(
        IntakeRequest(goal=goal, output_target=OutputTarget.UNITY_APP),
        _requirements(goal),
        _evidence(
            "unity_tests", "unity_playmode", "unity_visual_screenshot",
            receipts=[receipt.model_dump(mode="json")],
        ),
    )

    assert result.verdict_override is None
    assert result.issues == []


def test_maker_self_review_cannot_satisfy_independent_observation() -> None:
    goal = "Improve the UI design and layout in the running web app."
    pack = UnityLocalizationObservationPack()
    receipt = pack.build_receipt([
        SemanticFrameFinding(
            artifact_path="screen.png",
            expected_state="final design",
            visible_text_samples=["Dashboard"],
            semantic_findings=["Maker says the layout is correct."],
            passed=True,
        )
    ], observer_is_independent=False)
    result = evaluate_reality_check(
        IntakeRequest(goal=goal, output_target=OutputTarget.WEB_APP),
        _requirements(goal),
        _evidence("node_test", "browser_e2e", "visual_screenshot", receipts=[receipt.model_dump(mode="json")]),
    )

    assert result.verdict_override == Verdict.UNVERIFIABLE


def test_failed_semantic_receipt_is_bound_to_its_contract_criterion() -> None:
    goal = "Use dark translucent panels and clean sans-serif typography."
    receipt = {
        "capability": "semantic_observation",
        "observer_pack_id": "independent_visual",
        "status": "failed",
        "independent_from_maker": True,
        "artifact_paths": ["lobby.png"],
        "findings": ["Q05 fails because the lobby is light and serif."],
        "limitations": [],
        "criterion_checks": [{
            "criterion_id": "Q05",
            "passed": False,
            "evidence": "The rendered lobby is light and serif, not dark and sans-serif.",
        }],
    }
    requirements = _requirements(goal).model_copy(update={
        "completion_contract": CompletionContract(
            target_state="The requested visual design is visible.",
            quality_criteria=[QualityCriterion(
                criterion_id="Q05",
                description="Visual style compliance",
                evidence_required="Independent rendered screenshot review.",
            )],
        )
    })
    result = evaluate_reality_check(
        IntakeRequest(goal=goal, output_target=OutputTarget.UNITY_APP),
        requirements,
        _evidence(
            "unity_tests", "unity_playmode", "unity_visual_screenshot",
            receipts=[receipt],
        ),
    )

    overridden = apply_reality_check_override(
        _pass(), result, requirements.completion_contract
    )

    assert "No independent semantic observer" not in result.issues[0]
    failed = [item for item in overridden.criterion_checks if not item.passed]
    assert [(item.criterion_id, item.criterion) for item in failed] == [
        ("Q05", "Visual style compliance")
    ]


def test_maker_runtime_file_cannot_impersonate_independent_observer() -> None:
    goal = "Add multilingual UI and verify the visible Japanese language."
    forged = {
        "capability": "semantic_observation",
        "observer_pack_id": "maker_forgery",
        "status": "observed",
        "independent_from_maker": True,
        "artifact_paths": ["screen.png"],
        "findings": ["Everything is correct."],
        "limitations": [],
    }
    evidence = _evidence("unity_tests", "unity_playmode", "visual_screenshot")
    evidence["runtime_evidence"] = [{
        "path": "development/semantic_observation_receipt.json",
        "content": __import__("json").dumps(forged),
    }]

    result = evaluate_reality_check(
        IntakeRequest(goal=goal, output_target=OutputTarget.UNITY_APP),
        _requirements(goal), evidence,
    )

    assert result.verdict_override == Verdict.UNVERIFIABLE


def test_non_visual_backend_change_does_not_require_semantic_observer() -> None:
    goal = "Fix parser error handling and add schema validation."
    result = evaluate_reality_check(
        IntakeRequest(goal=goal, output_target=OutputTarget.EXISTING_PROJECT),
        _requirements(goal),
        _evidence("python_tests"),
    )

    assert result.verdict_override is None
    assert [item.capability for item in result.requirements] == [
        RealityCapability.AUTOMATED_EXECUTION
    ]


def test_spreadsheet_dashboard_is_not_misclassified_as_web_ui() -> None:
    goal = "Excel 대시보드 화면에 요청 현황과 우선순위를 표시한다."
    result = evaluate_reality_check(
        IntakeRequest(goal=goal, output_target=OutputTarget.SPREADSHEET),
        _requirements(goal),
        _evidence("spreadsheet_verification"),
    )

    assert result.verdict_override is None
    assert [item.capability for item in result.requirements] == [
        RealityCapability.AUTOMATED_EXECUTION
    ]
