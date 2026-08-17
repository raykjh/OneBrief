import asyncio
from pathlib import Path

import pytest

from onebrief.execution_pipeline import ExecutionPipeline
from onebrief.jobs import create_job
from onebrief.producer import estimate_budget
from onebrief.requirements_gate import apply_requirements_gate, find_scoring_gap
from onebrief.runner import reinspect_requirements
from onebrief.schemas import InformationRequirement, IntakeRequest, InternalSource, RequirementsAnalysis, SourcePriority


def _intake() -> IntakeRequest:
    return IntakeRequest(
        goal="회사 기준으로 지원자를 평가하고 순위표를 작성해줘.",
        desired_output="근거가 포함된 점수표와 추천 보고서",
        budget_limit_usd=1.0,
    )


def _ready() -> RequirementsAnalysis:
    return RequirementsAnalysis(
        supported=True,
        support_reason="The model believes inputs are complete.",
        normalized_goal="Score the applicants and produce a ranked evaluation.",
        deliverables=["Ranked applicant scorecard"],
        mandatory_information=[],
        optional_information=[],
        acceptance_criteria=["Apply the approved company evaluation policy."],
        assumptions=[],
        consolidated_questions=[],
        ready_for_estimate=True,
    )


def _csv() -> InternalSource:
    return InternalSource(
        name="candidates.csv",
        priority=SourcePriority.MANDATORY,
        requirement_keys=["candidate_data"],
        content=(
            "candidate_id,operations_years,documentation_evidence,cross_functional_projects\n"
            "A1,3,strong,4\n"
            "A2,5,medium,8\n"
        ),
        media_type="text/csv",
    )


def _incomplete_rules() -> InternalSource:
    return InternalSource(
        name="criteria.md",
        priority=SourcePriority.MANDATORY,
        requirement_keys=["evaluation_rules"],
        content=(
            "Experience: 40 points. Documentation: 30 points. Collaboration: 30 points.\n"
            "Tied candidates are included together."
        ),
    )


def _complete_rules() -> InternalSource:
    return InternalSource(
        name="criteria.md",
        priority=SourcePriority.MANDATORY,
        requirement_keys=["evaluation_rules"],
        content=(
            "operations_years at least 5 years: 40 points\n"
            "operations_years less than 5 years: 20 points\n"
            "cross_functional_projects at least 8 projects: 30 points\n"
            "cross_functional_projects less than 8 projects: 15 points\n"
            "documentation_evidence strong: 30 points\n"
            "documentation_evidence medium: 20 points\n"
            "Add the three field scores. Include tied candidates together."
        ),
    )


def test_missing_conversion_table_overrides_ready_model_result() -> None:
    sources = [_csv(), _incomplete_rules()]
    intake = _intake().model_copy(update={"internal_sources": sources})

    result = apply_requirements_gate(intake, _ready())

    assert result.ready_for_estimate is False
    assert [item.key for item in result.mandatory_information] == ["scoring_conversion_rules"]
    assert "operations_years" in result.consolidated_questions[0]
    assert find_scoring_gap(intake, _ready()) is not None


def test_complete_conversion_rules_allow_estimation() -> None:
    sources = [_csv(), _complete_rules()]
    intake = _intake().model_copy(update={"internal_sources": sources})

    result = apply_requirements_gate(intake, _ready())
    estimate = estimate_budget(intake, result)

    assert result.ready_for_estimate is True
    assert result.mandatory_information == []
    assert estimate.recommended_approval_usd > 0

def test_explicit_non_scoring_policy_does_not_demand_conversion_formula() -> None:
    csv_source = InternalSource(
        name="requests.csv",
        priority=SourcePriority.MANDATORY,
        requirement_keys=["request_data"],
        content=(
            "request_id,status,impact,effort_hours\n"
            "R1,open,high,4\n"
            "R2,blocked,medium,8\n"
        ),
        media_type="text/csv",
    )
    rules = InternalSource(
        name="rules.md",
        priority=SourcePriority.MANDATORY,
        requirement_keys=["priority_rules"],
        content=(
            "Never invent a numerical score. Sort risk using the explicit order "
            "Critical, High, Medium, Normal and then by due date and request ID."
        ),
    )
    intake = IntakeRequest(
        goal="Create a workbook that assigns categorical priority to each request.",
        internal_sources=[csv_source, rules],
    )
    scoring_requirement = InformationRequirement(
        key="scoring_conversion_rules",
        request="Provide a score conversion table and tie formula.",
        reason="A numeric ranking would otherwise require invented rules.",
        acceptable_evidence=["score table"],
    )
    analysis = _ready().model_copy(update={
        "mandatory_information": [scoring_requirement],
        "consolidated_questions": [scoring_requirement.request],
        "ready_for_estimate": False,
    })

    result = apply_requirements_gate(intake, analysis)

    assert result.ready_for_estimate is True
    assert result.mandatory_information == []
    assert result.consolidated_questions == []
    assert find_scoring_gap(intake, result) is None


def test_public_research_time_window_is_optional_and_disclosed() -> None:
    intake = IntakeRequest(
        goal="광교 지역의 조건별 주거 매물 목록을 만들어줘.",
        public_research_allowed=True,
    )
    analysis = _ready().model_copy(
        update={
            "mandatory_information": [
                InformationRequirement(
                    key="analysis_period",
                    request="최근 3개월을 보여줄지 조회 기간을 정해 주세요.",
                    reason="검색 범위를 정해야 합니다.",
                    acceptable_evidence=["기간 지정"],
                )
            ],
            "consolidated_questions": ["최근 3개월을 보여드릴까요?"],
            "ready_for_estimate": False,
        }
    )

    result = apply_requirements_gate(intake, analysis)

    assert result.ready_for_estimate is True
    assert result.mandatory_information == []
    assert [item.key for item in result.optional_information] == ["analysis_period"]
    assert result.consolidated_questions == []
    assert any("실제 사용 기간" in item for item in result.assumptions)


def test_confirmed_api_indicator_and_framework_choices_cannot_loop() -> None:
    answer = InternalSource(
        name="user-supplement-abc123.md",
        priority=SourcePriority.MANDATORY,
        requirement_keys=["api", "indicators", "framework"],
        content=(
            "모의 데이터는 안됨. 무료 공개 API를 사용해주고, 표준 기술 지표를 적용해줘. "
            "특정 프론트엔드 프레임워크는 없음."
        ),
    )
    intake = IntakeRequest(
        goal="환율 추이와 매수·매도 의견을 보여주는 웹프로그램을 만들어줘.",
        internal_sources=[answer],
    )
    items = [
        InformationRequirement(
            key="api",
            request="특정 API 서비스 또는 모의 데이터 사용 여부를 알려주세요.",
            reason="데이터 제공자를 선택해야 합니다.",
            acceptable_evidence=["API 선택"],
        ),
        InformationRequirement(
            key="indicators",
            request="사용할 기술적 지표를 알려주세요.",
            reason="매수·매도 규칙이 필요합니다.",
            acceptable_evidence=["기술 지표"],
        ),
        InformationRequirement(
            key="framework",
            request="선호하는 프론트엔드 프레임워크와 차트 라이브러리를 알려주세요.",
            reason="구현 기술을 선택해야 합니다.",
            acceptable_evidence=["프레임워크 선택"],
        ),
    ]
    analysis = _ready().model_copy(
        update={
            "mandatory_information": items,
            "consolidated_questions": [item.request for item in items],
            "ready_for_estimate": False,
        }
    )

    result = apply_requirements_gate(intake, analysis)

    assert result.ready_for_estimate is True
    assert result.mandatory_information == []
    assert {item.key for item in result.optional_information} == {
        "api",
        "indicators",
        "framework",
    }
    assert result.consolidated_questions == []



def test_producer_blocks_stale_ready_file_before_budget_creation() -> None:
    sources = [_csv(), _incomplete_rules()]
    intake = _intake().model_copy(update={"internal_sources": sources})

    with pytest.raises(ValueError, match="scoring_conversion_rules|conversion table|원자료 필드"):
        estimate_budget(intake, _ready())


def test_reinspection_gate_corrects_an_incorrect_model_ready_result(monkeypatch) -> None:
    async def fake_run_requirements(_payload):
        return _ready()

    import onebrief.runner as runner_module

    monkeypatch.setattr(runner_module, "_run_requirements", fake_run_requirements)
    sources = [_csv(), _incomplete_rules()]
    intake = _intake().model_copy(update={"internal_sources": sources})

    result = asyncio.run(reinspect_requirements(intake, _ready()))

    assert result.ready_for_estimate is False
    assert result.mandatory_information[0].key == "scoring_conversion_rules"


class NeverCalledGateway:
    def __init__(self):
        self.calls = 0

    def generate_json(self, **_kwargs):
        self.calls += 1
        raise AssertionError("model must not be called before the requirements gate passes")


def test_direct_execution_is_blocked_before_any_model_call(tmp_path: Path) -> None:
    sources = [_csv(), _incomplete_rules()]
    gateway = NeverCalledGateway()

    with pytest.raises(ValueError, match="requirements gate blocked"):
        ExecutionPipeline(tmp_path / "run", gateway=gateway).run(
            intake=_intake(),
            requirements=_ready(),
            sources=sources,
            output_dir=tmp_path / "output",
        )

    assert gateway.calls == 0
    assert not (tmp_path / "output").exists()


def test_job_creation_is_blocked_before_approval_ledger(tmp_path: Path) -> None:
    complete_sources = [_csv(), _complete_rules()]
    complete_intake = _intake().model_copy(update={"internal_sources": complete_sources})
    estimate = estimate_budget(complete_intake, _ready())
    jobs_dir = tmp_path / "jobs"

    with pytest.raises(ValueError, match="requirements gate blocked"):
        create_job(
            jobs_dir=jobs_dir,
            intake=_intake(),
            requirements=_ready(),
            sources=[_csv(), _incomplete_rules()],
            estimate=estimate,
            approved_usd=estimate.recommended_approval_usd,
        )

    assert not jobs_dir.exists()
