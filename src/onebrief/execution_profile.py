"""Deterministic execution profiles that reduce waste without weakening gates."""

from __future__ import annotations

import re

from onebrief.schemas import IntakeRequest, OutputTarget, RequirementsAnalysis


_FULL_DATA_PRESERVATION = re.compile(
    r"(?:all|every)\s+(?:csv\s+)?(?:row|record|field)|"
    r"(?:모든|전체)\s*(?:csv\s*)?(?:행|레코드|기록|필드)",
    re.IGNORECASE,
)


def requires_full_csv_preservation(requirements: RequirementsAnalysis) -> bool:
    """Activate row-for-row comparison only when the completion contract asks for it."""
    parts = [
        requirements.normalized_goal,
        *requirements.deliverables,
        *requirements.acceptance_criteria,
        requirements.completion_contract.target_state if requirements.completion_contract else "",
    ]
    if requirements.completion_contract:
        parts.extend(
            criterion.description
            for criterion in requirements.completion_contract.quality_criteria
        )
    text = "\n".join(parts)
    return bool(_FULL_DATA_PRESERVATION.search(text))


def is_small_document_task(
    intake: IntakeRequest, requirements: RequirementsAnalysis
) -> bool:
    """Identify bounded artifacts that should converge in one maker turn plus two repairs."""
    return (
        intake.output_target in {OutputTarget.DOCUMENT, OutputTarget.TEXT_FILE}
        and not intake.public_research_allowed
        and not intake.toolpack_ids
        and len(requirements.deliverables) <= 4
        and len(requirements.acceptance_criteria) <= 10
        and sum(source.size_bytes for source in intake.internal_sources) <= 250_000
    )


def effective_revision_rounds(
    intake: IntakeRequest, requirements: RequirementsAnalysis
) -> int:
    """Do not spend six identical turns on a small bounded document failure."""
    return min(intake.max_revision_rounds, 2) if is_small_document_task(intake, requirements) else intake.max_revision_rounds


def compact_work_contract(contract: dict[str, object], *, enabled: bool) -> dict[str, object]:
    """Remove duplicated prose while retaining the authoritative completion contract."""
    if not enabled:
        return contract
    return {
        "goal": contract.get("goal"),
        "output_target": contract.get("output_target"),
        "completion_contract": contract.get("completion_contract"),
        "assumptions": contract.get("assumptions", []),
    }
