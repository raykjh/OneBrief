"""Client-side dispatch from OneBrief web tier to Agent Platform Runtime."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from uuid import uuid4


@dataclass(frozen=True)
class AgentPlatformDispatchReceipt:
    agent_engine_resource: str
    agent_engine_session: str
    cloud_run_operation: str
    event_count: int


def _find_value(value: Any, key: str) -> str | None:
    if isinstance(value, dict):
        candidate = value.get(key)
        if isinstance(candidate, str) and candidate:
            return candidate
        for child in value.values():
            found = _find_value(child, key)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_value(child, key)
            if found:
                return found
    return None


def dispatch_approved_job_via_agent_platform(
    *,
    resource_name: str,
    job_uri: str,
    user_id: str,
    project: str,
    location: str,
    client: Any | None = None,
) -> AgentPlatformDispatchReceipt:
    """Ask the managed project-owner agent to start one approved Cloud Run job."""

    if client is None:
        import agentplatform

        client = agentplatform.Client(project=project, location=location)
    engine = client.agent_engines.get(name=resource_name)
    # The project-owner dispatcher is a stateless one-shot Agent Platform app.
    # Keeping URI + receipt in OneBrief's durable execution link is safer than
    # granting this tiny routing agent access to managed conversation state.
    dispatch_id = f"ephemeral-{uuid4()}"
    message = json.dumps(
        {
            "action": "start_approved_onebrief_job",
            "job_uri": job_uri,
            "instruction": "Start this exact approved work order once and return the receipt.",
        },
        ensure_ascii=False,
    )
    operation = None
    returned_uri = None
    event_count = 0
    # Tool receipts can arrive before the model's final prose. Return as soon as
    # the exact immutable job URI and Cloud Run operation are observed instead
    # of waiting indefinitely for the managed stream to close.
    for event in engine.stream_query(user_id=user_id, message=message):
        event_count += 1
        operation = operation or _find_value(event, "operation_name")
        returned_uri = returned_uri or _find_value(event, "job_uri")
        if operation and returned_uri == job_uri:
            break
    if not operation or returned_uri != job_uri:
        raise RuntimeError("Agent Platform returned no matching Cloud Run execution receipt")
    return AgentPlatformDispatchReceipt(
        agent_engine_resource=resource_name,
        agent_engine_session=dispatch_id,
        cloud_run_operation=operation,
        event_count=event_count,
    )
