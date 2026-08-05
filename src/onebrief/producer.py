"""Deterministic Producer: estimate token, cost, time, and approval bounds."""

from __future__ import annotations

from dataclasses import dataclass
from math import ceil

from onebrief.schemas import (
    BudgetEnvelope,
    BudgetStatus,
    IntakeRequest,
    RequirementsAnalysis,
    StageEstimate,
)

PRICE_CARD_VERSION = "google-agent-platform-global-standard-2026-08-05"
PRICE_SOURCE_URL = "https://cloud.google.com/gemini-enterprise-agent-platform/generative-ai/pricing"


@dataclass(frozen=True)
class ModelPrice:
    input_per_million: float
    output_per_million: float


PRICES = {
    "gemini-3.5-flash": ModelPrice(1.50, 9.00),
    "gemini-3.5-flash-lite": ModelPrice(0.30, 2.50),
}


def approximate_tokens(text: str) -> int:
    """Conservative multilingual approximation used before execution."""

    if not text:
        return 0
    return max(1, ceil(max(len(text) / 2.0, len(text.encode("utf-8")) / 4.0)))


def _call_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    price = PRICES[model]
    return (input_tokens * price.input_per_million + output_tokens * price.output_per_million) / 1_000_000


def _stage(
    name: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    calls: tuple[int, int, int],
    minutes: int,
) -> StageEstimate:
    per_call = _call_cost(model, input_tokens, output_tokens)
    return StageEstimate(
        stage=name,
        model=model,
        input_tokens_per_call=input_tokens,
        output_tokens_per_call=output_tokens,
        minimum_calls=calls[0],
        recommended_calls=calls[1],
        maximum_calls=calls[2],
        minimum_cost_usd=round(per_call * calls[0], 6),
        recommended_cost_usd=round(per_call * calls[1], 6),
        maximum_cost_usd=round(per_call * calls[2], 6),
        estimated_minutes_per_call=minutes,
    )


def estimate_budget(intake: IntakeRequest, analysis: RequirementsAnalysis) -> BudgetEnvelope:
    if not analysis.ready_for_estimate:
        raise ValueError("requirements must pass reinspection before budget estimation")

    source_tokens = sum(approximate_tokens(source.content or source.summary) for source in intake.internal_sources)
    contract_text = "\n".join(
        [
            intake.goal,
            intake.desired_output or "",
            analysis.normalized_goal,
            *analysis.deliverables,
            *analysis.acceptance_criteria,
        ]
    )
    contract_tokens = approximate_tokens(contract_text) + 1200
    base = source_tokens + contract_tokens
    analysis_output = min(6000, max(1500, ceil(base * 0.22)))
    draft_output = min(14000, max(3000, ceil(base * 0.45)))
    revision_calls = intake.max_revision_rounds

    stages = [
        _stage("evidence_analysis", "gemini-3.5-flash", base, analysis_output, (1, 1, 1), 4),
        _stage(
            "long_form_draft",
            "gemini-3.5-flash",
            base + analysis_output,
            draft_output,
            (1, 1, 1),
            6,
        ),
        _stage(
            "standards_review",
            "gemini-3.5-flash-lite",
            base + draft_output,
            1600,
            (1, 1, 1),
            2,
        ),
        _stage(
            "independent_verification",
            "gemini-3.5-flash",
            base + draft_output,
            2200,
            (1, 1, 1),
            3,
        ),
        _stage(
            "revision_reserve",
            "gemini-3.5-flash",
            base + draft_output + 3800,
            ceil(draft_output * 0.7),
            (0, min(1, revision_calls), revision_calls),
            5,
        ),
    ]
    raw_minimum = sum(stage.minimum_cost_usd for stage in stages)
    raw_recommended = sum(stage.recommended_cost_usd for stage in stages)
    raw_maximum = sum(stage.maximum_cost_usd for stage in stages)
    minimum = round(raw_minimum * 1.10, 4)
    recommended = round(raw_recommended * 1.20, 4)
    maximum = round(raw_maximum * 1.25, 4)
    approval = max(0.01, recommended)

    if intake.budget_limit_usd is None:
        status = BudgetStatus.AWAITING_APPROVAL
    elif intake.budget_limit_usd < minimum:
        status = BudgetStatus.NEEDS_BUDGET
    elif intake.budget_limit_usd < recommended:
        status = BudgetStatus.LIMITED_BUDGET
    else:
        status = BudgetStatus.WITHIN_BUDGET

    def total_minutes(call_field: str) -> int:
        return sum(
            getattr(stage, call_field) * stage.estimated_minutes_per_call for stage in stages
        )

    return BudgetEnvelope(
        price_card_version=PRICE_CARD_VERSION,
        price_source_url=PRICE_SOURCE_URL,
        endpoint="global-standard",
        estimated_source_tokens=source_tokens,
        estimated_contract_tokens=contract_tokens,
        stages=stages,
        minimum_cost_usd=minimum,
        recommended_cost_usd=recommended,
        maximum_cost_usd=maximum,
        recommended_approval_usd=round(approval, 4),
        budget_limit_usd=intake.budget_limit_usd,
        status=status,
        estimated_minutes_minimum=total_minutes("minimum_calls"),
        estimated_minutes_recommended=total_minutes("recommended_calls"),
        estimated_minutes_maximum=total_minutes("maximum_calls"),
        notes=[
            "Estimate covers work after requirements reinspection; intake calls already made are excluded.",
            "Recommended and maximum totals include 20% and 25% contingency respectively.",
            "Actual execution must record provider-reported token usage and stop at the approved limit.",
            "Google Cloud prices may change; refresh the versioned price card before production use.",
        ],
    )

