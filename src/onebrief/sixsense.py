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
    r"(?:\uc815\uc2dd\s*(?:\uac8c\uc784\s*)?\uc11c\ube44\uc2a4\s*\ud654|\ub354\s*(?:\ubc1c\uc804|\uac1c\uc120|\uc88b\uac8c)|"
    r"\uace0\ub3c4\uc758\s*\uc804\ubb38\uc131|\uc138\ub828\ub418\uac8c|professional|official[ -]?service|"
    r"improve further|make it better|polish|modernize)",
    re.IGNORECASE,
)
_WEB_WORK = re.compile(
    r"(?:\uc6f9\s*(?:\uc0ac\uc774\ud2b8|\ud504\ub85c\uadf8\ub7a8|\uc571)|\ud648\ud398\uc774\uc9c0|homepage|website|web app)",
    re.IGNORECASE,
)


def _category(dimension: str) -> str:
    value = dimension.casefold().replace("-", "_").replace(" ", "_")
    categories = {
        "visual": ("visual", "style", "theme", "design", "tone", "\uc2dc\uac01", "\ub514\uc790\uc778"),
        "structure": ("information", "content", "navigation", "structure", "\uc815\ubcf4", "\uad6c\uc870", "\uba54\ub274"),
        "audience": ("audience", "user", "reader", "customer", "\uc0ac\uc6a9\uc790", "\ub300\uc0c1"),
        "breadth": ("local", "language", "delivery", "market", "platform", "breadth", "\uc5b8\uc5b4", "\uc2dc\uc7a5", "\uc11c\ube44\uc2a4_\ubc94\uc704"),
        "completion": ("complete", "quality", "release", "finish", "\uc644\ub8cc", "\ud488\uc9c8"),
        "scope": ("scope", "function", "behavior", "feature", "priority", "\ubc94\uc704", "\uae30\ub2a5"),
    }
    for category, tokens in categories.items():
        if any(token in value for token in tokens):
            return category
    return value


def _option(
    option_id: str, label: str, decision: str, recommended: bool = False
) -> SixSenseOption:
    return SixSenseOption(
        option_id=option_id,
        label=label,
        decision=decision,
        recommended=recommended,
    )


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


def _web_fallbacks() -> dict[str, SixSenseQuestion]:
    return {
        "scope": _question(
            "S02",
            "Functional scope",
            "How far should this improvement go?",
            "This prevents a broad upgrade from ending as a cosmetic change",
            [
                _option("full_product", "Structure and features", "Preserve current behavior while extending information structure and user-facing features to a commercial-service standard.", True),
                _option("design_focus", "Design and presentation", "Keep the current feature scope and focus on visual design and content presentation."),
                _option("quality_only", "Quality fixes only", "Keep the current structure and improve defects, readability, and responsive quality only."),
            ],
        ),
        "structure": _question(
            "S03",
            "Information structure",
            "How should visitors find the content?",
            "This determines the navigation and service sections",
            [
                _option("service_sections", "Standard service sections", "Add top navigation and separate introduction, primary features or rules, detailed content, and download sections.", True),
                _option("single_page", "Keep one-page flow", "Keep the current one-page flow and improve section navigation."),
                _option("minimal_landing", "Minimal landing page", "Use a short hero and primary action-focused landing page."),
            ],
        ),
        "audience": _question(
            "S04",
            "Primary audience",
            "Who should understand it first?",
            "This sets the explanation depth and first-screen priority",
            [
                _option("new_users", "First-time visitors", "Prioritize first-time understanding and onboarding.", True),
                _option("existing_users", "Existing users", "Prioritize updates, detailed features, and community information."),
                _option("balanced_users", "Balance both", "Balance newcomer introduction and returning-user detail."),
            ],
        ),
        "breadth": _question(
            "S05",
            "Service breadth",
            "How broad should language and delivery support be?",
            "This fixes the localization and verification scope",
            [
                _option("global_languages", "Major global languages", "Preserve current locales and expand to major service languages with verifiable switching.", True),
                _option("current_languages", "Current languages only", "Keep current locales and improve their quality and switching."),
                _option("domestic_first", "Domestic-first", "Prioritize the primary domestic locale and provide only essential information elsewhere."),
            ],
        ),
        "visual": _question(
            "S06",
            "Visual direction",
            "How much of the existing brand should remain?",
            "This resolves the subjective design direction once",
            [
                _option("brand_standard", "Brand plus commercial standard", "Keep existing colors and assets while applying conventional commercial-service polish.", True),
                _option("preserve_visual", "Preserve the current design", "Keep the visual design and improve layout and readability only."),
                _option("new_visual", "Redesign the visual system", "Preserve content and brand assets but create a new visual system."),
            ],
        ),
    }


def apply_sixsense_policy(
    intake: IntakeRequest, analysis: RequirementsAnalysis
) -> RequirementsAnalysis:
    """Guarantee useful coverage when a vague web upgrade would ask too little."""
    if not (_VAGUE_UPGRADE.search(intake.goal) and _WEB_WORK.search(intake.goal)):
        return analysis
    # KHALINOS has one canonical control-plane language. Localization belongs to
    # a separate presentation layer, so intake language never selects product copy.
    fallbacks = _web_fallbacks()
    plan = analysis.sixsense or SixSensePlan(
        standard_profile=(
            "Preserve the existing brand and behavior while applying conventional "
            "commercial-web structure, accessibility, and responsive quality."
        )
    )
    by_category: dict[str, SixSenseQuestion] = {}
    for question in plan.questions:
        by_category.setdefault(_category(question.dimension), question)
    ordered: list[SixSenseQuestion] = []
    for index, category in enumerate(
        ("scope", "structure", "audience", "breadth", "visual"), start=2
    ):
        question = by_category.get(category) or fallbacks[category]
        ordered.append(question.model_copy(update={"question_id": f"S0{index}"}))
    return analysis.model_copy(
        update={"sixsense": plan.model_copy(update={"questions": ordered})}
    )
