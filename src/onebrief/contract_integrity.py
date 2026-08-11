"""Deterministic integrity checks for model-authored completion contracts."""

from __future__ import annotations

import re

from onebrief.schemas import EvaluationMode, IntakeRequest, QualityCriterion, RequirementsAnalysis


_EXPLICIT_CONSTRAINT = re.compile(
    r"반드시|제외|하지\s*마|안\s*돼|안\s*되|없어야|까지만|"
    r"\bonly\b|\bmust\b|\bshall\b|\bexclude\b|\bwithout\b|"
    r"\bdo\s+not\b|\bnever\b|\bno\s+",
    re.IGNORECASE,
)
_PERSONA_PROFILE = re.compile(
    r"전문가|컨설턴트|전문\s*기획자|\bexpert\b|\bconsultant\b|\bspecialist\b",
    re.IGNORECASE,
)
_DELEGATED_DISCOVERY = re.compile(
    r"찾(?:아|아서|은)|조사(?:해|해서)?|비교(?:해|해서)?|추천(?:해|해서)?|"
    r"선정(?:해|해서)?|선택(?:해|해서)?|정(?:해|해서)\s*줘|제안(?:해|해서)?|"
    r"\b(?:find|research|investigate|compare|recommend|identify|select|choose|determine|propose)\b",
    re.IGNORECASE,
)
_EXPLICIT_QUESTION_REQUEST = re.compile(
    r"질문(?:해|해서|을\s*해)|물어(?:봐|보고)|의견을\s*물|"
    r"\b(?:ask\s+me|ask\s+questions?|confirm\s+with\s+me)\b",
    re.IGNORECASE,
)
_GENERIC_TOKENS = {
    "결과", "결과물", "작업", "제품", "프로그램", "자료", "내용", "기준",
    "result", "artifact", "work", "product", "program", "data", "content",
}


def _clauses(value: str) -> list[str]:
    return [
        item.strip(" -\t")
        for item in re.split(r"(?<=[.!?。])\s+|[\r\n]+", value)
        if item.strip(" -\t")
    ]


def _tokens(value: str) -> set[str]:
    return {
        token.casefold()
        for token in re.findall(r"[가-힣]{2,}|[a-zA-Z]{3,}", value)
        if token.casefold() not in _GENERIC_TOKENS
    }


def _covered(clause: str, contract_text: str) -> bool:
    if clause.casefold() in contract_text.casefold():
        return True
    tokens = _tokens(clause)
    if not tokens:
        return False
    return len(tokens & _tokens(contract_text)) >= min(2, len(tokens))


def _sanitize_standard_profile(
    intake: IntakeRequest, requirements: RequirementsAnalysis
) -> RequirementsAnalysis:
    plan = requirements.sixsense
    if plan is None or not _PERSONA_PROFILE.search(plan.standard_profile):
        return requirements
    korean = bool(re.search(r"[가-힣]", intake.goal))
    profile = (
        "목표 분야에 적용되는 공식 기준과 일반적인 전문 실무 형식을 사용하고, 명시되지 않은 세부사항에는 보수적인 기본값을 적용합니다."
        if korean else
        "Use applicable official standards and conventional professional practice, with conservative defaults for unspecified details."
    )
    return requirements.model_copy(update={
        "sixsense": plan.model_copy(update={"standard_profile": profile})
    })


def _respect_delegated_discovery(
    intake: IntakeRequest, requirements: RequirementsAnalysis
) -> RequirementsAnalysis:
    """Keep delegated discovery with OneBrief instead of returning it to the user.

    Mandatory information gaps remain authoritative and can still be surfaced by the
    requirements gate. This only removes optional SixSense choices when the user has
    explicitly delegated finding, comparing, recommending, or selecting the answer.
    """

    plan = requirements.sixsense
    source_text = "\n".join(filter(None, [intake.goal, intake.desired_output or ""]))
    if (
        plan is None
        or not plan.questions
        or not _DELEGATED_DISCOVERY.search(source_text)
        or _EXPLICIT_QUESTION_REQUEST.search(source_text)
    ):
        return requirements
    return requirements.model_copy(update={
        "sixsense": plan.model_copy(update={"questions": []})
    })


def enforce_contract_integrity(
    intake: IntakeRequest, requirements: RequirementsAnalysis
) -> RequirementsAnalysis:
    """Copy missing explicit constraints into the contract without designing a solution."""

    requirements = _respect_delegated_discovery(
        intake, _sanitize_standard_profile(intake, requirements)
    )
    contract = requirements.completion_contract
    if contract is None:
        return requirements
    source_text = "\n".join(filter(None, [intake.goal, intake.desired_output or ""]))
    explicit = [item for item in _clauses(source_text) if _EXPLICIT_CONSTRAINT.search(item)]
    contract_text = "\n".join([
        contract.target_state,
        *requirements.deliverables,
        *requirements.acceptance_criteria,
        *(item.description for item in contract.quality_criteria),
    ])
    missing = [item for item in explicit if not _covered(item, contract_text)]
    if not missing:
        return requirements
    korean = bool(re.search(r"[가-힣]", intake.goal))
    evidence = (
        "최종 산출물과 근거에서 이 사용자 제약이 충족되었음을 독립적으로 확인한다."
        if korean else
        "Independent evidence maps the final artifact to this exact user constraint."
    )
    criteria = list(contract.quality_criteria)
    acceptance = list(requirements.acceptance_criteria)
    for clause in missing:
        description = (
            f"사용자가 명시한 제약을 충족한다: {clause}"
            if korean else f"Satisfy the explicit user constraint: {clause}"
        )[:300]
        item = QualityCriterion(
            criterion_id="Q01",
            description=description,
            evaluation_mode=EvaluationMode.INDEPENDENT_REVIEW,
            evidence_required=evidence,
        )
        if len(criteria) < 12:
            criteria.append(item)
        else:
            criteria[-1] = item
        acceptance.append(description)
    criteria = [
        item.model_copy(update={"criterion_id": f"Q{index:02d}"})
        for index, item in enumerate(criteria[:12], start=1)
    ]
    return requirements.model_copy(update={
        "completion_contract": contract.model_copy(update={"quality_criteria": criteria}),
        "acceptance_criteria": list(dict.fromkeys(acceptance))[:12],
    })
