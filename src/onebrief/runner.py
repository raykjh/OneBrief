"""Programmatic runners for intake analysis and reinspection."""

from __future__ import annotations

import json
import os
from typing import Any
from uuid import uuid4

from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from onebrief.agents import requirements_analyst
from onebrief.requirements_gate import apply_requirements_gate
from onebrief.schemas import IntakeRequest, RequirementsAnalysis

APP_NAME = "onebrief"


async def _run_requirements(payload: dict[str, Any]) -> RequirementsAnalysis:
    os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "TRUE")
    os.environ.setdefault("GOOGLE_CLOUD_PROJECT", "onebrief-agent-20260805")
    os.environ.setdefault("GOOGLE_CLOUD_LOCATION", "global")
    user_id = "local-user"
    session_id = str(uuid4())
    session_service = InMemorySessionService()
    await session_service.create_session(app_name=APP_NAME, user_id=user_id, session_id=session_id)
    runner = Runner(agent=requirements_analyst, app_name=APP_NAME, session_service=session_service)
    message = types.Content(
        role="user",
        parts=[types.Part(text=json.dumps(payload, ensure_ascii=False, indent=2))],
    )
    final_text: str | None = None
    async for event in runner.run_async(user_id=user_id, session_id=session_id, new_message=message):
        if event.is_final_response() and event.content and event.content.parts:
            final_text = "".join(part.text or "" for part in event.content.parts)
    if not final_text:
        raise RuntimeError("Requirements Analyst returned no final response")
    return RequirementsAnalysis.model_validate(json.loads(final_text))


async def inspect_requirements(intake: IntakeRequest) -> RequirementsAnalysis:
    """Inspect a goal and its supplied sources in one bounded preflight call."""
    result = await _run_requirements(
        {
            "mode": "single_pass_with_sources",
            "intake_with_uploaded_sources": intake.model_dump(mode="json"),
            "instruction": (
                "Build the work contract from the goal and authoritative uploads in one pass. "
                "Inspect source contents, not only names or requirement keys. Return every "
                "remaining mandatory question together; do not start the requested work."
            ),
        }
    )
    return apply_requirements_gate(intake, result)


async def analyze_requirements(intake: IntakeRequest) -> RequirementsAnalysis:
    result = await _run_requirements(
        {"mode": "initial_analysis", "intake": intake.model_dump(mode="json")}
    )
    return apply_requirements_gate(intake, result)


async def reinspect_requirements(
    intake: IntakeRequest,
    previous: RequirementsAnalysis,
) -> RequirementsAnalysis:
    # Only prior gaps are carried forward. Previous deliverables and assumptions are
    # intentionally excluded so an early hallucination cannot become an authority.
    previous_gaps = {
        "mandatory_information": [
            item.model_dump(mode="json") for item in previous.mandatory_information
        ],
        "optional_information": [
            item.model_dump(mode="json") for item in previous.optional_information
        ],
    }
    result = await _run_requirements(
        {
            "mode": "reinspection_after_upload",
            "previous_gaps_only": previous_gaps,
            "intake_with_uploaded_sources": intake.model_dump(mode="json"),
            "instruction": (
                "Rebuild the contract from the original intake and authoritative uploads. "
                "Re-evaluate every previous gap against actual uploaded content. A matching "
                "requirement key is only a routing hint, not proof of sufficiency. Keep any "
                "gap whose content is absent, incomplete, contradictory, or unusable."
            ),
        }
    )
    return apply_requirements_gate(intake, result)

