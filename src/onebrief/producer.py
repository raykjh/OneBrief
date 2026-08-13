"""Deterministic Producer: estimate token, cost, time, and approval bounds."""

from __future__ import annotations

from dataclasses import dataclass
from math import ceil

from onebrief.execution_limits import (
    ANALYST_OUTPUT_CAP,
    DEVELOPER_OUTPUT_CAP,
    PUBLIC_RESEARCH_OUTPUT_CAP,
    REVISION_OUTPUT_CAP,
    VERIFIER_OUTPUT_CAP,
    WRITER_OUTPUT_CAP,
)
from onebrief.requirements_gate import require_ready_for_estimate
from onebrief.schemas import (
    BudgetEnvelope,
    BudgetStatus,
    ExecutionPhase,
    IntakeRequest,
    PhaseBudgetEstimate,
    RequirementsAnalysis,
    StageEstimate,
    ToolPackId,
)
from onebrief.team_planning import TEAM_PLANNING_OUTPUT_CAP

PRICE_CARD_VERSION = "google-agent-platform-global-standard-search-2026-08-12"
PRICE_SOURCE_URL = "https://cloud.google.com/gemini-enterprise-agent-platform/generative-ai/pricing"


@dataclass(frozen=True)
class ModelPrice:
    input_per_million: float
    output_per_million: float


PRICES = {
    "gemini-3.1-pro-preview": ModelPrice(2.00, 12.00),
    "gemini-3.5-flash": ModelPrice(1.50, 9.00),
    "gemini-3.5-flash-lite": ModelPrice(0.30, 2.50),
}


def approximate_tokens(text: str) -> int:
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
    fixed_cost_usd: float = 0.0,
) -> StageEstimate:
    per_call = _call_cost(model, input_tokens, output_tokens) + fixed_cost_usd
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
        fixed_cost_usd_per_call=fixed_cost_usd,
    )


def estimate_budget(intake: IntakeRequest, analysis: RequirementsAnalysis) -> BudgetEnvelope:
    analysis = require_ready_for_estimate(intake, analysis)

    source_tokens = sum(approximate_tokens(source.content or source.summary) for source in intake.internal_sources)
    # Executable ToolPack evidence is generated after approval, so reserve its bounded
    # model-input footprint even though it is not present in the intake descriptor yet.
    if intake.toolpack_ids:
        source_tokens += 60_000 * len(intake.toolpack_ids)
    contract_text = "\n".join(
        [
            intake.goal,
            intake.desired_output or "",
            analysis.normalized_goal,
            *analysis.deliverables,
            *analysis.acceptance_criteria,
        ]
    )
    # Covers role prompts and response schemas omitted by Vertex countTokens.
    contract_tokens = approximate_tokens(contract_text) + 2200
    anticipated_research_tokens = PUBLIC_RESEARCH_OUTPUT_CAP if intake.public_research_allowed else 0
    base = source_tokens + contract_tokens + anticipated_research_tokens
    revisions = intake.max_revision_rounds
    recommended_revisions = min(2, revisions)

    stages = [
        _stage(
            "team_planning", "gemini-3.1-pro-preview",
            contract_tokens + 4500, TEAM_PLANNING_OUTPUT_CAP,
            (1, 1, 2), 2,
        )
    ]
    if intake.public_research_allowed:
        stages.append(
            _stage(
                "public_research", "gemini-3.5-flash",
                contract_tokens + 800, PUBLIC_RESEARCH_OUTPUT_CAP,
                (1, 1, 1), 4, fixed_cost_usd=0.035,
            )
        )
    for optional_stage in (
        "project_architecture",
        "creative_direction",
        "artifact_integration",
        "policy_guard",
    ):
        optional_model = (
            "gemini-3.1-pro-preview"
            if optional_stage == "policy_guard"
            else "gemini-3.5-flash"
        )
        stages.append(
            _stage(
                optional_stage, optional_model,
                base + 1800, 1800, (0, 1, 1), 2,
            )
        )
    development = any(
        item in intake.toolpack_ids
        for item in (
            ToolPackId.EXCHANGE_DEVELOPMENT,
            ToolPackId.PROJECT_DEVELOPMENT,
            ToolPackId.GREENFIELD_WEB_DEVELOPMENT,
        )
    )
    draft_output_cap = DEVELOPER_OUTPUT_CAP if development else WRITER_OUTPUT_CAP
    stages.extend([
        _stage("evidence_analysis", "gemini-3.5-flash", base, ANALYST_OUTPUT_CAP, (1, 1, 1), 4),
        _stage(
            "long_form_draft",
            "gemini-3.5-flash",
            base + ANALYST_OUTPUT_CAP,
            draft_output_cap,
            (1, 3, 3) if development else (1, 1, 1),
            12 if development else 6,
        ),
        _stage(
            "independent_verification",
            "gemini-3.1-pro-preview",
            base + ANALYST_OUTPUT_CAP + draft_output_cap,
            VERIFIER_OUTPUT_CAP,
            (1, 1 + recommended_revisions, 1 + revisions),
            3,
        ),
        _stage(
            "revision",
            "gemini-3.5-flash",
            base + ANALYST_OUTPUT_CAP + draft_output_cap + VERIFIER_OUTPUT_CAP,
            REVISION_OUTPUT_CAP,
            (0, recommended_revisions, revisions),
            5,
        ),
        _stage(
            "final_approval",
            "gemini-3.1-pro-preview",
            base + ANALYST_OUTPUT_CAP + draft_output_cap + VERIFIER_OUTPUT_CAP,
            1200,
            (1, 1, 1),
            2,
        ),
    ])
    milestone_execution_count = 1
    milestone_implementation_count = 1
    if (
        intake.existing_project_id
        and ToolPackId.PROJECT_DEVELOPMENT in intake.toolpack_ids
        and analysis.completion_contract is not None
        and len(analysis.completion_contract.quality_criteria) >= 4
    ):
        # The durable runtime executes every independently verifiable slice and
        # one final clean-baseline integration pass.  Quote those calls before
        # approval instead of letting a legacy whole-project estimate starve a
        # later milestone.  The preview uses no source access and cannot widen
        # the already-approved completion contract.
        from onebrief.milestones import MilestoneKind, build_milestone_plan

        preview = build_milestone_plan(
            project_id=intake.existing_project_id,
            goal=intake.goal,
            requirements=analysis,
            source_revision="approval-pending",
            minimum_cost_usd=0.0,
            maximum_cost_usd=0.0,
        )
        milestone_implementation_count = sum(
            item.kind == MilestoneKind.IMPLEMENTATION for item in preview.milestones
        )
        milestone_execution_count = sum(
            item.kind != MilestoneKind.BASELINE for item in preview.milestones
        )

        scaled_stages: list[StageEstimate] = []
        for stage in stages:
            if stage.stage == "team_planning":
                scaled_stages.append(stage)
                continue
            # The final integration normally reuses the accumulated candidate,
            # so it needs analysis/verification but not a fresh initial draft.
            factor = (
                milestone_implementation_count
                if stage.stage == "long_form_draft"
                else milestone_execution_count
            )
            scaled_stages.append(_stage(
                stage.stage,
                stage.model,
                stage.input_tokens_per_call,
                stage.output_tokens_per_call,
                (
                    stage.minimum_calls * factor,
                    stage.recommended_calls * factor,
                    stage.maximum_calls * factor,
                ),
                stage.estimated_minutes_per_call,
                fixed_cost_usd=stage.fixed_cost_usd_per_call,
            ))
        stages = scaled_stages
    raw_minimum = sum(stage.minimum_cost_usd for stage in stages)
    raw_recommended = sum(stage.recommended_cost_usd for stage in stages)
    raw_maximum = sum(stage.maximum_cost_usd for stage in stages)
    # Preserve enough approval headroom for the last same-maker repair and the
    # independent verifier. Earlier calls may be cheaper than their caps, but a
    # late structured software response often carries the largest input context.
    # This reserve is approval headroom rather than a fictitious provider call.
    finalization_reserve = 0.0
    if development:
        by_name = {stage.stage: stage for stage in stages}
        finalization_reserve = (
            by_name["revision"].recommended_cost_usd
            + by_name["independent_verification"].minimum_cost_usd
        )
    minimum = round(raw_minimum * 1.10, 4)
    recommended = round(raw_recommended * 1.20 + finalization_reserve, 4)
    maximum = round(raw_maximum * 1.25, 4)
    approval = max(0.01, recommended)

    phase_budgets: list[PhaseBudgetEstimate] = []
    if development:
        by_name = {stage.stage: stage for stage in stages}

        def phase_costs(call_cost: str) -> dict[ExecutionPhase, float]:
            draft_cost = float(getattr(by_name["long_form_draft"], call_cost))
            revision_cost = float(getattr(by_name["revision"], call_cost))
            verification_cost = sum(
                float(getattr(by_name[name], call_cost))
                for name in ("independent_verification", "final_approval")
            )
            shared_cost = sum(
                float(getattr(stage, call_cost))
                for stage in stages
                if stage.stage not in {
                    "long_form_draft", "revision",
                    "independent_verification", "final_approval",
                }
            )
            return {
                ExecutionPhase.SHARED_CONTEXT: shared_cost,
                ExecutionPhase.PRODUCT_IMPLEMENTATION: draft_cost + revision_cost * 0.55,
                ExecutionPhase.EVIDENCE_CONSTRUCTION: revision_cost * 0.45,
                ExecutionPhase.FINAL_VERIFICATION: verification_cost,
            }

        minimum_phase = phase_costs("minimum_cost_usd")
        recommended_phase = phase_costs("recommended_cost_usd")
        maximum_phase = phase_costs("maximum_cost_usd")
        for phase in tuple(minimum_phase):
            minimum_phase[phase] *= 1.10
            recommended_phase[phase] *= 1.20
            maximum_phase[phase] *= 1.25
        phase_budgets = [
            PhaseBudgetEstimate(
                phase=phase,
                minimum_cost_usd=round(minimum_phase[phase], 6),
                recommended_cost_usd=round(recommended_phase[phase], 6),
                maximum_cost_usd=round(maximum_phase[phase], 6),
                max_ai_repair_calls=(
                    min(24, revisions * milestone_execution_count)
                    if phase in {
                        ExecutionPhase.PRODUCT_IMPLEMENTATION,
                        ExecutionPhase.EVIDENCE_CONSTRUCTION,
                    }
                    else 0
                ),
                max_deterministic_attempts=(
                    min(100, (2 + revisions) * milestone_execution_count)
                    if phase in {
                        ExecutionPhase.PRODUCT_IMPLEMENTATION,
                        ExecutionPhase.EVIDENCE_CONSTRUCTION,
                        ExecutionPhase.FINAL_VERIFICATION,
                    }
                    else 1
                ),
                editable_scope=(
                    ["product source; excludes tests, evidence, screenshots, and reports"]
                    if phase == ExecutionPhase.PRODUCT_IMPLEMENTATION
                    else (
                        ["tests and executable evidence harness; excludes product behavior"]
                        if phase == ExecutionPhase.EVIDENCE_CONSTRUCTION else []
                    )
                ),
            )
            for phase in (
                ExecutionPhase.SHARED_CONTEXT,
                ExecutionPhase.PRODUCT_IMPLEMENTATION,
                ExecutionPhase.EVIDENCE_CONSTRUCTION,
                ExecutionPhase.FINAL_VERIFICATION,
            )
        ]
        # Finalization headroom is an explicit, non-borrowable wallet. It can
        # only be spent by a future policy-authorized reopen stage.
        phase_budgets.append(PhaseBudgetEstimate(
            phase=ExecutionPhase.RESERVE,
            minimum_cost_usd=0.0,
            recommended_cost_usd=round(finalization_reserve, 6),
            maximum_cost_usd=round(finalization_reserve, 6),
            max_ai_repair_calls=1 if revisions else 0,
            max_deterministic_attempts=1,
            editable_scope=[],
        ))

    if intake.budget_limit_usd is None:
        status = BudgetStatus.AWAITING_APPROVAL
    elif intake.budget_limit_usd < minimum:
        status = BudgetStatus.NEEDS_BUDGET
    elif intake.budget_limit_usd < recommended:
        status = BudgetStatus.LIMITED_BUDGET
    else:
        status = BudgetStatus.WITHIN_BUDGET

    def total_minutes(call_field: str) -> int:
        return sum(getattr(stage, call_field) * stage.estimated_minutes_per_call for stage in stages)

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
            "Executable ToolPack evidence reserves 60,000 input tokens per selected pack.",
            "The Project Owner team-planning and final-approval calls are included.",
            "Decision-critical planning, independent verification, policy review, and final approval may use the approved Gemini 3.1 Pro Preview binding; routine creation remains on Flash tiers.",
            "Optional role nodes are conservatively reserved and removed after TeamPlan selection.",
            "Each revision round includes a new independent verification call.",
            "Development approval preserves explicit headroom for one final repair and verifier turn.",
            "Role prompts and response-schema input overhead are included conservatively.",
            "Recommended and maximum totals include 20% and 25% contingency respectively.",
            "Public research reserves one Gemini 3.5 grounded prompt with a $0.035 Google Search fee cap.",
            "Actual execution records provider-reported usage and stops at the approved limit.",
            "Software runs use separate non-borrowable product, evidence, verification, and reserve wallets inside the total approval.",
            (
                f"Milestone execution reserves {milestone_implementation_count} vertical-slice passes "
                f"and {milestone_execution_count - milestone_implementation_count} clean-baseline integration pass."
                if milestone_execution_count > 1
                else "This run uses one whole-contract execution pass."
            ),
        ],
        phase_budgets=phase_budgets,
    )

