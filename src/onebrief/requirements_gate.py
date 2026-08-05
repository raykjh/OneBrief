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
            intake.desired_output or "",
            analysis.normalized_goal,
            *analysis.deliverables,
            *analysis.acceptance_criteria,
        ]
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


def apply_requirements_gate(
    intake: IntakeRequest,
    analysis: RequirementsAnalysis,
    sources: list[InternalSource] | None = None,
) -> RequirementsAnalysis:
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
