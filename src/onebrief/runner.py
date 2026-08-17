"""Programmatic runners for intake analysis and reinspection."""

from __future__ import annotations

import json
import os
from typing import Any
import re
from uuid import uuid4

from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from onebrief.agents import requirements_analyst
from onebrief.requirements_gate import apply_requirements_gate
from onebrief.schemas import IntakeRequest, RequirementsAnalysis
from onebrief.sixsense import apply_sixsense_policy
from onebrief.assurance import apply_assurance_policy
from onebrief.delivery_policy import apply_standard_first_delivery_policy
from onebrief.contract_integrity import enforce_contract_integrity
from onebrief.product_language import enforce_canonical_product_language

APP_NAME = "onebrief"


def _finalize_requirements(
    intake: IntakeRequest, requirements: RequirementsAnalysis
) -> RequirementsAnalysis:
    result = apply_standard_first_delivery_policy(
        intake, enforce_contract_integrity(intake, requirements)
    )
    enforce_canonical_product_language(result, surface="RequirementsAnalysis")
    return result


def _normalize_requirements_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Repair harmless provider formatting before strict domain validation."""

    def text(value: Any, limit: int, fallback: str) -> str:
        normalized = str(value or fallback).strip()
        return normalized[:limit] or fallback

    def strings(value: Any, limit: int, item_limit: int) -> list[str]:
        items = value if isinstance(value, list) else []
        return [text(item, item_limit, "Not specified.") for item in items[:limit]]

    def key(value: Any, index: int) -> str:
        normalized = re.sub(r"[^a-z0-9_]+", "_", str(value or "").casefold()).strip("_")
        if not normalized or not normalized[0].isalpha():
            normalized = f"requirement_{index:02d}_{normalized}".rstrip("_")
        return normalized[:64]

    payload["supported"] = bool(payload.get("supported", True))
    payload["support_reason"] = text(payload.get("support_reason"), 500, "The requested work is supported.")
    payload["normalized_goal"] = text(payload.get("normalized_goal"), 1000, "Complete the requested work.")
    payload["deliverables"] = strings(payload.get("deliverables"), 10, 300) or ["Requested artifact"]
    payload["acceptance_criteria"] = (
        strings(payload.get("acceptance_criteria"), 12, 300)
        or ["The requested artifact is complete and usable."]
    )
    payload["assumptions"] = strings(payload.get("assumptions"), 10, 300)
    payload["consolidated_questions"] = strings(payload.get("consolidated_questions"), 10, 500)

    for field_name in ("mandatory_information", "optional_information"):
        normalized_information: list[dict[str, Any]] = []
        raw_information = payload.get(field_name)
        for index, item in enumerate(raw_information if isinstance(raw_information, list) else [], start=1):
            if not isinstance(item, dict):
                continue
            normalized_information.append({
                "key": key(item.get("key"), index),
                "request": text(item.get("request"), 500, "Please provide the required information."),
                "reason": text(item.get("reason"), 500, "This information is needed to complete the work."),
                "acceptable_evidence": (
                    strings(item.get("acceptable_evidence"), 5, 200)
                    or ["A relevant authoritative source."]
                ),
            })
        payload[field_name] = normalized_information[:10]

    contract = payload.get("completion_contract")
    if not isinstance(contract, dict):
        contract = {}
        payload["completion_contract"] = contract
    contract["target_state"] = text(
        contract.get("target_state"), 1000, payload["normalized_goal"]
    )
    raw_criteria = contract.get("quality_criteria")
    normalized_criteria: list[dict[str, Any]] = []
    for index, criterion in enumerate(raw_criteria if isinstance(raw_criteria, list) else [], start=1):
        if not isinstance(criterion, dict):
            continue
        mode = str(criterion.get("evaluation_mode", "independent_review"))
        if mode not in {"deterministic", "independent_review"}:
            mode = "independent_review"
        normalized_criteria.append({
            "criterion_id": f"Q{index:02d}",
            "description": text(
                criterion.get("description"), 300, payload["acceptance_criteria"][0]
            ),
            "evaluation_mode": mode,
            "evidence_required": text(
                criterion.get("evidence_required"), 300, "Evidence that the criterion is satisfied."
            ),
            "required": bool(criterion.get("required", True)),
        })
    if not normalized_criteria:
        normalized_criteria = [{
            "criterion_id": f"Q{index:02d}",
            "description": criterion,
            "evaluation_mode": "independent_review",
            "evidence_required": "Evidence that the criterion is satisfied.",
            "required": True,
        } for index, criterion in enumerate(payload["acceptance_criteria"], start=1)]
    contract["quality_criteria"] = normalized_criteria[:12]
    contract["pass_condition"] = text(
        contract.get("pass_condition"), 300, "All required criteria pass."
    )

    sixsense = payload.get("sixsense")
    if not isinstance(sixsense, dict):
        sixsense = {}
    normalized_questions: list[dict[str, Any]] = []
    raw_questions = sixsense.get("questions")
    for question_index, question in enumerate(
        raw_questions if isinstance(raw_questions, list) else [], start=2
    ):
        if question_index > 6 or not isinstance(question, dict):
            break
        normalized_options: list[dict[str, Any]] = []
        raw_options = question.get("options")
        for option_index, option in enumerate(
            raw_options if isinstance(raw_options, list) else [], start=1
        ):
            if option_index > 4 or not isinstance(option, dict):
                break
            option_id = key(option.get("option_id"), option_index)[:32]
            normalized_options.append({
                "option_id": option_id,
                "label": text(option.get("label"), 120, f"Option {option_index}"),
                "decision": text(
                    option.get("decision"), 300, option.get("label") or f"Option {option_index}"
                ),
                "recommended": bool(option.get("recommended", option_index == 1)),
            })
        if len(normalized_options) < 2:
            continue
        recommended = next(
            (index for index, option in enumerate(normalized_options) if option["recommended"]),
            0,
        )
        for index, option in enumerate(normalized_options):
            option["recommended"] = index == recommended
        normalized_questions.append({
            "question_id": f"S0{question_index}",
            "dimension": text(question.get("dimension"), 80, "work preference"),
            "prompt": text(question.get("prompt"), 300, "Which direction should OneBrief use?"),
            "reason": text(
                question.get("reason"), 300, "This choice materially changes the result."
            ),
            "options": normalized_options,
            "allow_custom": bool(question.get("allow_custom", True)),
        })
    profile = text(
        sixsense.get("standard_profile"),
        800,
        "Use a coherent, widely accepted professional standard for unspecified details.",
    )
    payload["sixsense"] = {
        "standard_profile": profile,
        "questions": normalized_questions,
        "interaction_target_seconds": 30,
    }

    if payload["mandatory_information"]:
        payload["ready_for_estimate"] = False
        if not payload["consolidated_questions"]:
            payload["consolidated_questions"] = [
                item["request"] for item in payload["mandatory_information"]
            ][:10]
    else:
        payload["ready_for_estimate"] = bool(payload.get("ready_for_estimate", True))
    if not payload["supported"]:
        payload["ready_for_estimate"] = False
    return payload




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
    return RequirementsAnalysis.model_validate(_normalize_requirements_payload(json.loads(final_text)))


async def inspect_requirements(intake: IntakeRequest) -> RequirementsAnalysis:
    """Inspect a goal and its supplied sources in one bounded preflight call."""
    result = await _run_requirements(
        {
            "mode": "single_pass_with_sources",
            "intake_with_uploaded_sources": intake.model_dump(mode="json"),
            "instruction": (
                "Build the work contract from the goal and authoritative uploads in one pass. "
                "Inspect source contents, not only names or requirement keys. Return every "
                "remaining mandatory question together; do not start the requested work. "
                "For existing_project, first restore the project contract and work status from "
                "the onebrief-project-continuation source. Resume pending or failed work and ask "
                "only when the restored state cannot resolve a material ambiguity."
            ),
        }
    )
    result = apply_assurance_policy(
        intake, apply_sixsense_policy(intake, apply_requirements_gate(intake, result))
    )
    return _finalize_requirements(intake, result)


async def analyze_requirements(intake: IntakeRequest) -> RequirementsAnalysis:
    result = await _run_requirements(
        {"mode": "initial_analysis", "intake": intake.model_dump(mode="json")}
    )
    result = apply_assurance_policy(
        intake, apply_sixsense_policy(intake, apply_requirements_gate(intake, result))
    )
    return _finalize_requirements(intake, result)


async def reinspect_requirements(
    intake: IntakeRequest,
    previous: RequirementsAnalysis,
    *,
    sixsense_completed: bool = False,
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
    confirmed_decisions = [
        source.content
        for source in intake.internal_sources
        if source.name.startswith("user-supplement-")
    ]
    result = await _run_requirements(
        {
            "mode": "reinspection_after_upload",
            "previous_gaps_only": previous_gaps,
            "intake_with_uploaded_sources": intake.model_dump(mode="json"),
            "user_confirmed_decisions": confirmed_decisions,
            "sixsense_completed": sixsense_completed,
            "instruction": (
                "Rebuild the contract from the original intake and authoritative uploads. "
                "Re-evaluate every previous gap against actual uploaded content. A matching "
                "requirement key is only a routing hint, not proof of sufficiency. Keep any "
                "gap whose content is absent, incomplete, contradictory, or unusable. "
                "Treat user_confirmed_decisions as direct authoritative answers. A choice such as "
                "free public API, standard technical indicators, or no framework preference resolves "
                "that implementation-choice gap. Do not repeat a resolved question. Ordinary framework, "
                "library, provider, display, and analysis defaults are optional rather than mandatory."
                + (
                    " The user completed the SixSense preference sequence. Do not create another "
                    "SixSense interview; keep its questions empty and ask only for genuinely missing "
                    "authoritative information that cannot be inferred or defaulted."
                    if sixsense_completed else ""
                )
            ),
        }
    )
    result = apply_requirements_gate(intake, result)
    if not sixsense_completed:
        result = apply_sixsense_policy(intake, result)
    if sixsense_completed and result.sixsense is not None:
        result = result.model_copy(
            update={"sixsense": result.sixsense.model_copy(update={"questions": []})}
        )
    result = apply_assurance_policy(intake, result)
    return _finalize_requirements(intake, result)

