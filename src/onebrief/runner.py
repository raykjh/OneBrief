"""Programmatic single-turn runner for the Requirements Analyst."""

from __future__ import annotations

import json
import os
from uuid import uuid4

from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from onebrief.agents import requirements_analyst
from onebrief.schemas import IntakeRequest, RequirementsAnalysis

APP_NAME = "onebrief"


async def analyze_requirements(intake: IntakeRequest) -> RequirementsAnalysis:
    """Run one bounded analysis turn and validate the structured response."""

    os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "TRUE")
    os.environ.setdefault("GOOGLE_CLOUD_PROJECT", "onebrief-agent-20260805")
    os.environ.setdefault("GOOGLE_CLOUD_LOCATION", "global")

    user_id = "local-user"
    session_id = str(uuid4())
    session_service = InMemorySessionService()
    await session_service.create_session(
        app_name=APP_NAME,
        user_id=user_id,
        session_id=session_id,
    )
    runner = Runner(
        agent=requirements_analyst,
        app_name=APP_NAME,
        session_service=session_service,
    )
    message = types.Content(
        role="user",
        parts=[types.Part(text=intake.model_dump_json(indent=2))],
    )
    final_text: str | None = None
    async for event in runner.run_async(
        user_id=user_id,
        session_id=session_id,
        new_message=message,
    ):
        if event.is_final_response() and event.content and event.content.parts:
            final_text = "".join(part.text or "" for part in event.content.parts)

    if not final_text:
        raise RuntimeError("Requirements Analyst returned no final response")
    return RequirementsAnalysis.model_validate(json.loads(final_text))

