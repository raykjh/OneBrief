"""Deterministic completeness checks applied before estimation or execution."""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass

from onebrief.schemas import (
    InformationRequirement,
    IntakeRequest,
    InternalSource,
    OutputTarget,
    RequirementsAnalysis,
)


_SCORING_TASK = re.compile(
    r"평가|점수|채점|순위|랭킹|선발|score|scoring|rank|ranking|evaluate|evaluation|"
    r"grade|grading|rating|shortlist",
    re.IGNORECASE,
)
_SCORE_VALUE = re.compile(r"\d+(?:\.\d+)?\s*(?:점|points?)", re.IGNORECASE)
_THRESHOLD = re.compile(
    r"(?:\d+(?:\.\d+)?\s*(?:년|개|회|명|점|%|years?|items?|projects?)?\s*"
    r"(?:이상|이하|미만|초과)|(?:at\s+least|at\s+most|more\s+than|less\s+than)\s+"
    r"\d+(?:\.\d+)?|(?:>=|<=|>|<)\s*\d+(?:\.\d+)?)",
    re.IGNORECASE,
)
_FORMULA = re.compile(r"공식|계산식|정규화|formula|weighted\s+sum|normalize", re.IGNORECASE)
_HANGUL = re.compile(r"[가-힣]")

_EXPLICIT_NON_SCORING = re.compile(
    r"(?:no|never|do\s+not|must\s+not)\s+(?:invent\s+|use\s+|create\s+)?"
    r"(?:a\s+)?(?:numeric(?:al)?\s+)?(?:score|scoring)"
    r"|(?:점수|채점).{0,30}(?:금지|사용하지|만들지|없음|요구되지)",
    re.IGNORECASE,
)

_OUTPUT_TARGET_CONTRACTS = {
    OutputTarget.EXISTING_PROJECT: "기존 프로젝트의 원래 실행 형태를 유지한 개선본",
    OutputTarget.WEB_APP: "웹에서 직접 실행할 수 있는 프로그램",
    OutputTarget.UNITY_APP: "Unity 프로젝트로 실행할 수 있는 게임 또는 앱",
    OutputTarget.SPREADSHEET: "스프레드시트 파일(XLSX 또는 요청된 표 형식)",
    OutputTarget.DOCUMENT: "문서 파일(DOCX, PDF 또는 요청된 문서 형식)",
    OutputTarget.TEXT_FILE: "텍스트 파일(TXT 또는 Markdown)",
}


_CREATIVE_TASK = re.compile(
    r"각본|시나리오|영화|소설|이야기|screenplay|script|story|novel",
    re.IGNORECASE,
)
_CREATIVE_DEFAULTABLE_GAP = re.compile(
    r"줄거리|시놉시스|사건|주인공|등장인물|인물|캐릭터|장르|톤|분위기|결말|분량|러닝타임|"
    r"premise|plot|synopsis|incident|protagonist|character|cast|genre|tone|mood|ending|length|runtime",
    re.IGNORECASE,
)
_PRESERVE_EXISTING = re.compile(
    r"기존\s*(?:작품|각본|시나리오|줄거리|인물)|특정\s*(?:작품|인물)|각색|리메이크|"
    r"existing\s+(?:work|script|plot|character)|adapt|remake",
    re.IGNORECASE,
)
_PUBLIC_RESEARCH_TIME_WINDOW = re.compile(
    r"\uAE30\uAC04|\uC870\uD68C\s*\uAE30\uAC04|\uBD84\uC11D\s*\uAE30\uAC04|\uAC80\uC0C9\s*\uAE30\uAC04|\uBA87\s*\uAC1C\uC6D4|\d+\s*\uAC1C\uC6D4|"
    r"time\s*window|date\s*range|lookback|horizon|reporting\s*period|analysis\s*period",
    re.IGNORECASE,
)
_FRAMEWORK_PREFERENCE = re.compile(r"프론트엔드|프레임워크|차트\s*라이브러리|react|vue|vanilla|frontend\s*framework|chart\s*library", re.IGNORECASE)
_DATA_PROVIDER_PREFERENCE = re.compile(r"api\s*(?:서비스|제공자)?|데이터\s*(?:서비스|제공자)|data\s*provider|mock\s*data|모의\s*데이터", re.IGNORECASE)
_INDICATOR_PREFERENCE = re.compile(r"기술\s*(?:적\s*)?지표|이동평균|technical\s*indicator|moving\s*average", re.IGNORECASE)
_NO_PREFERENCE_DECISION = re.compile(r"선호.*없|특정.*없|상관없|알아서|no\s*preference|any\s*framework", re.IGNORECASE)
_FREE_PUBLIC_API_DECISION = re.compile(r"무료\s*(?:공개\s*)?api|공개\s*api|free\s*(?:public\s*)?api|public\s*api", re.IGNORECASE)
_STANDARD_INDICATOR_DECISION = re.compile(r"표준\s*(?:기술\s*)?지표|standard\s*(?:technical\s*)?indicator", re.IGNORECASE)



@dataclass(frozen=True)
class ScoringGap:
    fields: tuple[str, ...]
    reasons: tuple[str, ...]


def _is_csv(source: InternalSource) -> bool:
    return source.name.casefold().endswith(".csv") or source.media_type.casefold() == "text/csv"


def _task_text(intake: IntakeRequest, analysis: RequirementsAnalysis) -> str:
    return "\n".join(
        [
            intake.goal,
            intake.output_target.value,
            intake.desired_output or "",
            analysis.normalized_goal,
            *analysis.deliverables,
            *analysis.acceptance_criteria,
        ]
    )



def _apply_output_target(
    intake: IntakeRequest,
    analysis: RequirementsAnalysis,
) -> RequirementsAnalysis:
    """Make an explicit output selection a deterministic, non-substitutable contract."""
    contract = _OUTPUT_TARGET_CONTRACTS.get(intake.output_target)
    if contract is None:
        return analysis
    deliverables = list(analysis.deliverables)
    if not any(contract in item for item in deliverables):
        deliverables.append(contract)
    criterion = f"최종 산출물은 {contract} 자체여야 하며 보고서나 요약 파일로 대체하지 않는다."
    criteria = list(analysis.acceptance_criteria)
    if criterion not in criteria:
        criteria.append(criterion)
    return analysis.model_copy(
        update={
            "deliverables": deliverables[:10],
            "acceptance_criteria": criteria[:12],
        }
    )


def _csv_profiles(sources: list[InternalSource]) -> tuple[list[str], list[set[str]], int]:
    fields: list[str] = []
    categorical_values: list[set[str]] = []
    numeric_fields = 0
    for source in sources:
        if not _is_csv(source):
            continue
        try:
            reader = csv.DictReader(io.StringIO(source.content))
            headers = list(reader.fieldnames or [])
            rows = list(reader)
        except csv.Error:
            continue
        for field in headers[1:]:
            values = {(row.get(field) or "").strip() for row in rows if (row.get(field) or "").strip()}
            if not values:
                continue
            fields.append(field)
            if all(re.fullmatch(r"-?\d+(?:\.\d+)?", value) for value in values):
                numeric_fields += 1
            else:
                categorical_values.append(values)
    return fields, categorical_values, numeric_fields


def find_scoring_gap(
    intake: IntakeRequest,
    analysis: RequirementsAnalysis,
    sources: list[InternalSource] | None = None,
) -> ScoringGap | None:
    authoritative_sources = list(sources if sources is not None else intake.internal_sources)
    authoritative_text = "\n".join(source.content for source in authoritative_sources)
    if _EXPLICIT_NON_SCORING.search(authoritative_text):
        return None
    if not _SCORING_TASK.search(_task_text(intake, analysis)):
        return None
    fields, categorical_groups, numeric_fields = _csv_profiles(authoritative_sources)
    if not fields:
        return None

    rule_lines = [
        line.strip()
        for source in authoritative_sources
        if not _is_csv(source)
        for line in source.content.splitlines()
        if line.strip()
    ]
    threshold_rule_count = len(
        {
            line.casefold()
            for line in rule_lines
            if _THRESHOLD.search(line) and _SCORE_VALUE.search(line)
        }
    )
    has_general_formula = any(_FORMULA.search(line) for line in rule_lines)
    reasons: list[str] = []
    if numeric_fields and not has_general_formula and threshold_rule_count < numeric_fields:
        reasons.append(
            f"{numeric_fields} numeric field(s) require thresholds or an explicit formula; "
            f"only {threshold_rule_count} supported threshold rule(s) were found"
        )

    missing_categories: set[str] = set()
    for values in categorical_groups:
        for value in values:
            if not any(value.casefold() in line.casefold() and _SCORE_VALUE.search(line) for line in rule_lines):
                missing_categories.add(value)
    if missing_categories:
        reasons.append(
            "categorical score mappings are missing for: " + ", ".join(sorted(missing_categories))
        )
    if not reasons:
        return None
    return ScoringGap(fields=tuple(fields), reasons=tuple(reasons))


def _gap_requirement(gap: ScoringGap, korean: bool) -> tuple[InformationRequirement, str]:
    field_list = ", ".join(gap.fields)
    if korean:
        request = (
            f"원자료 필드({field_list})를 점수로 환산하고 합산·동점을 처리하는 "
            "승인된 기준표나 공식을 제공해 주세요."
        )
        reason = "배점만으로는 원자료 값을 점수와 순위로 변환할 수 없어 임의 규칙 생성을 막아야 합니다."
        question = request
        evidence = ["승인된 점수 환산표", "필드별 계산 공식", "동점 처리 규칙이 포함된 평가 정책"]
    else:
        request = (
            f"Provide an approved conversion table or formula for source fields ({field_list}), "
            "including aggregation and tie handling."
        )
        reason = (
            "Weights alone cannot convert raw values into scores and ranks without inventing rules."
        )
        question = request
        evidence = [
            "approved score conversion table",
            "field-level calculation formula",
            "evaluation policy with tie handling",
        ]
    return (
        InformationRequirement(
            key="scoring_conversion_rules",
            request=request,
            reason=reason,
            acceptable_evidence=evidence,
        ),
        question,
    )


def _apply_creative_defaults(
    intake: IntakeRequest,
    analysis: RequirementsAnalysis,
) -> RequirementsAnalysis:
    if (
        not intake.internal_sources
        or not _CREATIVE_TASK.search(intake.goal)
        or _PRESERVE_EXISTING.search(intake.goal)
        or not analysis.mandatory_information
    ):
        return analysis
    defaultable = [
        item
        for item in analysis.mandatory_information
        if _CREATIVE_DEFAULTABLE_GAP.search(
            " ".join([item.key, item.request, item.reason, *item.acceptable_evidence])
        )
    ]
    if len(defaultable) != len(analysis.mandatory_information):
        return analysis
    optional_by_key = {item.key: item for item in analysis.optional_information}
    optional_by_key.update({item.key: item for item in defaultable})
    assumption = (
        "줄거리, 주요 인물, 장르, 톤, 결말 방향 및 목표 분량은 제공된 정본 안에서 "
        "일관된 창작 기본값으로 선택하고 결과물에 명시한다."
    )
    assumptions = list(dict.fromkeys([*analysis.assumptions, assumption]))
    return analysis.model_copy(
        update={
            "mandatory_information": [],
            "optional_information": list(optional_by_key.values())[:10],
            "consolidated_questions": [],
            "assumptions": assumptions[:10],
            "ready_for_estimate": analysis.supported,
        }
    )


def _apply_public_research_defaults(
    intake: IntakeRequest,
    analysis: RequirementsAnalysis,
) -> RequirementsAnalysis:
    """Keep a defaultable public-search time window from blocking execution."""
    if not intake.public_research_allowed or not analysis.mandatory_information:
        return analysis
    defaultable = []
    remaining = []
    for item in analysis.mandatory_information:
        text = " ".join([item.key, item.request, item.reason, *item.acceptable_evidence])
        (defaultable if _PUBLIC_RESEARCH_TIME_WINDOW.search(text) else remaining).append(item)
    if not defaultable:
        return analysis
    optional_by_key = {item.key: item for item in analysis.optional_information}
    optional_by_key.update({item.key: item for item in defaultable})
    questions = [
        question
        for question in analysis.consolidated_questions
        if not _PUBLIC_RESEARCH_TIME_WINDOW.search(question)
    ]
    assumption = (
        "공개자료 검색 기간은 최신성과 목표 적합성을 기준으로 합리적으로 선택하고 "
        "결과물에 실제 사용 기간을 명시한다."
    )
    return analysis.model_copy(
        update={
            "mandatory_information": remaining,
            "optional_information": list(optional_by_key.values())[:10],
            "consolidated_questions": questions,
            "assumptions": list(dict.fromkeys([*analysis.assumptions, assumption]))[:10],
            "ready_for_estimate": analysis.supported and not remaining,
        }
    )


def _apply_confirmed_implementation_defaults(
    intake: IntakeRequest,
    analysis: RequirementsAnalysis,
) -> RequirementsAnalysis:
    decisions = "\n".join(
        source.content
        for source in intake.internal_sources
        if source.name.startswith("user-supplement-")
    )
    if not decisions or not analysis.mandatory_information:
        return analysis

    def resolved(text: str) -> bool:
        return bool(
            (_FRAMEWORK_PREFERENCE.search(text) and _NO_PREFERENCE_DECISION.search(decisions))
            or (_DATA_PROVIDER_PREFERENCE.search(text) and _FREE_PUBLIC_API_DECISION.search(decisions))
            or (_INDICATOR_PREFERENCE.search(text) and _STANDARD_INDICATOR_DECISION.search(decisions))
        )

    defaultable = []
    remaining = []
    for item in analysis.mandatory_information:
        text = " ".join([item.key, item.request, item.reason, *item.acceptable_evidence])
        (defaultable if resolved(text) else remaining).append(item)
    if not defaultable:
        return analysis
    optional_by_key = {item.key: item for item in analysis.optional_information}
    optional_by_key.update({item.key: item for item in defaultable})
    questions = [
        question for question in analysis.consolidated_questions if not resolved(question)
    ]
    assumption = (
        "사용자가 확정한 무료 공개 API, 표준 기술 지표 및 프레임워크 무선호 결정을 "
        "구현 기본값으로 적용하고 최종 결과물에 실제 선택을 명시한다."
    )
    return analysis.model_copy(
        update={
            "mandatory_information": remaining,
            "optional_information": list(optional_by_key.values())[:10],
            "consolidated_questions": questions,
            "assumptions": list(dict.fromkeys([*analysis.assumptions, assumption]))[:10],
            "ready_for_estimate": analysis.supported and not remaining,
        }
    )


def _apply_explicit_non_scoring_policy(
    intake: IntakeRequest,
    analysis: RequirementsAnalysis,
    sources: list[InternalSource] | None = None,
) -> RequirementsAnalysis:
    """Remove a numeric-score gap when authoritative rules forbid scoring."""
    authoritative = list(sources if sources is not None else intake.internal_sources)
    if not _EXPLICIT_NON_SCORING.search("\n".join(item.content for item in authoritative)):
        return analysis
    removed_keys = {
        item.key
        for item in analysis.mandatory_information
        if item.key == "scoring_conversion_rules"
    }
    if not removed_keys:
        return analysis
    remaining = [
        item for item in analysis.mandatory_information if item.key not in removed_keys
    ]
    questions = [
        question
        for question in analysis.consolidated_questions
        if not re.search(r"score|scoring|conversion table|점수|채점", question, re.IGNORECASE)
    ]
    assumption = (
        "The authoritative policy explicitly forbids numerical scoring; use its "
        "categorical ordering and tie rules without inventing a score."
    )
    return analysis.model_copy(
        update={
            "mandatory_information": remaining,
            "consolidated_questions": questions,
            "assumptions": list(dict.fromkeys([*analysis.assumptions, assumption]))[:10],
            "ready_for_estimate": analysis.supported and not remaining,
        }
    )


def apply_requirements_gate(
    intake: IntakeRequest,
    analysis: RequirementsAnalysis,
    sources: list[InternalSource] | None = None,
) -> RequirementsAnalysis:
    analysis = _apply_output_target(intake, analysis)
    analysis = _apply_public_research_defaults(intake, analysis)
    analysis = _apply_confirmed_implementation_defaults(intake, analysis)
    analysis = _apply_creative_defaults(intake, analysis)
    analysis = _apply_explicit_non_scoring_policy(intake, analysis, sources)
    gap = find_scoring_gap(intake, analysis, sources)
    if gap is None:
        return analysis
    requirement, question = _gap_requirement(gap, bool(_HANGUL.search(intake.goal)))
    mandatory = [
        item for item in analysis.mandatory_information if item.key != requirement.key
    ]
    mandatory.append(requirement)
    questions = list(dict.fromkeys([*analysis.consolidated_questions, question]))
    return analysis.model_copy(
        update={
            "mandatory_information": mandatory,
            "consolidated_questions": questions,
            "ready_for_estimate": False,
        }
    )


def require_ready_for_estimate(
    intake: IntakeRequest,
    analysis: RequirementsAnalysis,
    sources: list[InternalSource] | None = None,
) -> RequirementsAnalysis:
    gated = apply_requirements_gate(intake, analysis, sources)
    if not gated.ready_for_estimate:
        questions = " | ".join(gated.consolidated_questions) or "Mandatory information is missing."
        raise ValueError(f"requirements gate blocked estimation or execution: {questions}")
    return gated
