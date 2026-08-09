"""Deterministic policy for fast, useful SixSense decision coverage."""

from __future__ import annotations

import re

from onebrief.schemas import (
    IntakeRequest,
    RequirementsAnalysis,
    SixSenseOption,
    SixSensePlan,
    SixSenseQuestion,
)


_VAGUE_UPGRADE = re.compile(
    r"(?:정식\s*(?:게임\s*)?서비스\s*수준|더\s*(?:발전|개선|좋게)|고도화|전문화|세련되게|"
    r"professional|official[ -]?service|improve further|make it better|polish|modernize)",
    re.IGNORECASE,
)
_WEB_WORK = re.compile(r"(?:웹\s*(?:사이트|프로그램|앱)|홈페이지|homepage|website|web app)", re.IGNORECASE)


def _category(dimension: str) -> str:
    value = dimension.casefold().replace("-", "_").replace(" ", "_")
    if any(token in value for token in ("visual", "style", "theme", "design", "tone", "시각", "디자인")):
        return "visual"
    if any(token in value for token in ("information", "content", "navigation", "structure", "정보", "구조", "메뉴")):
        return "structure"
    if any(token in value for token in ("audience", "user", "reader", "customer", "사용자", "대상")):
        return "audience"
    if any(token in value for token in ("local", "language", "delivery", "market", "platform", "breadth", "언어", "시장", "서비스_범위")):
        return "breadth"
    if any(token in value for token in ("complete", "quality", "release", "finish", "완료", "품질")):
        return "completion"
    if any(token in value for token in ("scope", "function", "behavior", "feature", "priority", "범위", "기능")):
        return "scope"
    return value


def _option(option_id: str, label: str, decision: str, recommended: bool = False) -> SixSenseOption:
    return SixSenseOption(option_id=option_id, label=label, decision=decision, recommended=recommended)


def _question(
    question_id: str,
    dimension: str,
    prompt: str,
    reason: str,
    options: list[SixSenseOption],
) -> SixSenseQuestion:
    return SixSenseQuestion(
        question_id=question_id,
        dimension=dimension,
        prompt=prompt,
        reason=reason,
        options=options,
    )


def _web_fallbacks(korean: bool) -> dict[str, SixSenseQuestion]:
    if korean:
        return {
            "scope": _question("S02", "기능 범위", "이번 개선은 어디까지 바꿀까요?", "디자인만 바뀌고 끝나는 일을 막습니다", [
                _option("full_product", "구조와 기능까지 전면 개선", "기존 기능은 보존하면서 정보 구조와 사용자 기능까지 상용 서비스 수준으로 확장한다.", True),
                _option("design_focus", "디자인과 표현 중심", "현재 기능 범위는 유지하고 시각 디자인과 콘텐츠 표현에 집중한다."),
                _option("quality_only", "현재 구성의 완성도만 개선", "현재 구조와 기능은 유지하고 오류, 가독성, 반응형 완성도만 개선한다."),
            ]),
            "structure": _question("S03", "정보 구조", "방문자가 내용을 찾는 방식은 어떻게 구성할까요?", "상용 홈페이지의 메뉴와 섹션 구조를 결정합니다", [
                _option("service_sections", "표준 서비스 메뉴와 섹션", "상단 메뉴를 두고 소개, 주요 기능이나 규칙, 상세 콘텐츠, 다운로드를 명확한 섹션으로 구분한다.", True),
                _option("single_page", "현재 한 페이지 흐름 유지", "현재 한 페이지 흐름을 유지하면서 섹션 구분과 탐색만 개선한다."),
                _option("minimal_landing", "핵심 소개만 간결하게", "짧은 소개와 핵심 행동 버튼 중심의 랜딩 페이지로 구성한다."),
            ]),
            "audience": _question("S04", "주요 사용자", "누가 가장 먼저 이해하도록 만들까요?", "설명의 깊이와 첫 화면의 우선순위를 정합니다", [
                _option("new_users", "처음 방문한 사용자", "처음 접하는 사용자가 서비스의 정체성과 이용 방법을 빠르게 이해하도록 구성한다.", True),
                _option("existing_users", "기존 사용자", "기존 사용자가 업데이트, 상세 기능, 커뮤니티 정보를 빠르게 찾도록 구성한다."),
                _option("balanced_users", "신규·기존 사용자 균형", "신규 소개와 기존 사용자용 상세 정보를 같은 비중으로 제공한다."),
            ]),
            "breadth": _question("S05", "서비스 범위", "언어와 서비스 범위는 어디까지 확장할까요?", "다국어 콘텐츠와 검증 범위를 미리 확정합니다", [
                _option("global_languages", "주요 글로벌 언어까지 확장", "현재 언어를 보존하고 영어, 일본어, 중국어 간체, 스페인어 등 주요 서비스 언어를 검증 가능한 방식으로 확장한다.", True),
                _option("current_languages", "현재 언어만 유지", "현재 지원 언어를 유지하고 번역 완성도와 전환 동작만 개선한다."),
                _option("domestic_first", "국내 사용자 중심", "한국어를 중심으로 구성하고 다른 언어는 핵심 정보만 제공한다."),
            ]),
            "visual": _question("S06", "시각 방향", "기존 브랜드를 얼마나 유지할까요?", "주관적인 디자인 선택을 한 번만 확인합니다", [
                _option("brand_standard", "기존 브랜드 + 상용 표준", "기존 색상과 자산을 살리면서 일반적인 상용 서비스 홈페이지 수준으로 정돈한다.", True),
                _option("preserve_visual", "현재 디자인 최대한 유지", "현재 시각 디자인을 유지하고 배치와 가독성만 개선한다."),
                _option("new_visual", "새로운 스타일로 재설계", "콘텐츠와 브랜드 자산은 보존하되 새로운 시각 체계로 재설계한다."),
            ]),
        }
    return {
        "scope": _question("S02", "Functional scope", "How far should this improvement go?", "This prevents a broad upgrade from ending as a cosmetic change", [
            _option("full_product", "Structure and features", "Preserve current behavior while extending information structure and user-facing features to a commercial-service standard.", True),
            _option("design_focus", "Design and presentation", "Keep the current feature scope and focus on visual design and content presentation."),
            _option("quality_only", "Quality fixes only", "Keep the current structure and improve defects, readability, and responsive quality only."),
        ]),
        "structure": _question("S03", "Information structure", "How should visitors find the content?", "This determines the navigation and service sections", [
            _option("service_sections", "Standard service sections", "Add top navigation and separate introduction, primary features or rules, detailed content, and download sections.", True),
            _option("single_page", "Keep one-page flow", "Keep the current one-page flow and improve section navigation."),
            _option("minimal_landing", "Minimal landing page", "Use a short hero and primary action-focused landing page."),
        ]),
        "audience": _question("S04", "Primary audience", "Who should understand it first?", "This sets the explanation depth and first-screen priority", [
            _option("new_users", "First-time visitors", "Prioritize first-time understanding and onboarding.", True),
            _option("existing_users", "Existing users", "Prioritize updates, detailed features, and community information."),
            _option("balanced_users", "Balance both", "Balance newcomer introduction and returning-user detail."),
        ]),
        "breadth": _question("S05", "Service breadth", "How broad should language and delivery support be?", "This fixes the localization and verification scope", [
            _option("global_languages", "Major global languages", "Preserve current locales and expand to major service languages with verifiable switching.", True),
            _option("current_languages", "Current languages only", "Keep current locales and improve their quality and switching."),
            _option("domestic_first", "Domestic-first", "Prioritize the primary domestic locale and provide only essential information elsewhere."),
        ]),
        "visual": _question("S06", "Visual direction", "How much of the existing brand should remain?", "This resolves the subjective design direction once", [
            _option("brand_standard", "Brand plus commercial standard", "Keep existing colors and assets while applying conventional commercial-service polish.", True),
            _option("preserve_visual", "Preserve the current design", "Keep the visual design and improve layout and readability only."),
            _option("new_visual", "Redesign the visual system", "Preserve content and brand assets but create a new visual system."),
        ]),
    }


def apply_sixsense_policy(intake: IntakeRequest, analysis: RequirementsAnalysis) -> RequirementsAnalysis:
    """Guarantee useful coverage when a vague web upgrade would otherwise ask too little."""
    if not (_VAGUE_UPGRADE.search(intake.goal) and _WEB_WORK.search(intake.goal)):
        return analysis
    korean = bool(re.search(r"[가-힣]", intake.goal))
    fallbacks = _web_fallbacks(korean)
    plan = analysis.sixsense or SixSensePlan(
        standard_profile=(
            "기존 브랜드와 기능을 보존하면서 일반적인 상용 웹서비스의 구조, 접근성, 반응형 완성도를 적용합니다."
            if korean
            else "Preserve the existing brand and behavior while applying conventional commercial-web structure, accessibility, and responsive quality."
        )
    )
    by_category: dict[str, SixSenseQuestion] = {}
    for question in plan.questions:
        by_category.setdefault(_category(question.dimension), question)
    ordered: list[SixSenseQuestion] = []
    for index, category in enumerate(("scope", "structure", "audience", "breadth", "visual"), start=2):
        question = by_category.get(category) or fallbacks[category]
        ordered.append(question.model_copy(update={"question_id": f"S0{index}"}))
    return analysis.model_copy(update={"sixsense": plan.model_copy(update={"questions": ordered})})
