"""Self-contained ADK runtime for Gemini Enterprise Agent Platform.

This deliberately small package is shipped as a top-level Agent Platform
dependency.  It does not import the full OneBrief web/worker application: the
managed project owner can only validate an immutable work-order URI and start
the fixed Cloud Run Job.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any

from google.adk.agents import LlmAgent
from google.cloud import run_v2


_JOB_URI = re.compile(
    r"^gs://(?P<bucket>[a-z0-9][a-z0-9._-]*)/"
    r"(?P<prefix>jobs/[0-9a-fA-F-]{36})/?$"
)

# ADK model clients are lazy and otherwise inherit the deployer's local ADC
# quota project. Pin model inference to the OneBrief project while leaving the
# Agent Platform runtime region in its platform-managed environment variable.
os.environ["GOOGLE_CLOUD_PROJECT"] = os.environ.get(
    "ONEBRIEF_PROJECT", "onebrief-agent-20260805"
)
os.environ["GOOGLE_CLOUD_LOCATION"] = os.environ.get(
    "ONEBRIEF_MODEL_LOCATION", "global"
)


@dataclass(frozen=True)
class RuntimeSettings:
    project: str
    region: str
    bucket: str
    cloud_run_job: str
    model: str

    @classmethod
    def from_env(cls) -> "RuntimeSettings":
        return cls(
            project=os.environ.get("ONEBRIEF_PROJECT", "onebrief-agent-20260805"),
            region=os.environ.get("ONEBRIEF_REGION", "asia-northeast3"),
            bucket=os.environ.get(
                "ONEBRIEF_JOB_BUCKET", "onebrief-agent-20260805-jobs"
            ),
            cloud_run_job=os.environ.get("ONEBRIEF_CLOUD_RUN_JOB", "onebrief-worker"),
            model=os.environ.get("ONEBRIEF_AGENT_PLATFORM_MODEL", "gemini-3.5-flash"),
        )


def validate_job_uri(job_uri: str, settings: RuntimeSettings) -> str:
    match = _JOB_URI.fullmatch(job_uri)
    if not match or match.group("bucket") != settings.bucket:
        raise PermissionError("job URI is outside the approved OneBrief namespace")
    return f"gs://{settings.bucket}/{match.group('prefix')}"


def start_approved_onebrief_job(job_uri: str) -> dict[str, Any]:
    """Start one immutable, budget-approved OneBrief work order."""

    settings = RuntimeSettings.from_env()
    job_uri = validate_job_uri(job_uri, settings)
    name = (
        f"projects/{settings.project}/locations/{settings.region}/"
        f"jobs/{settings.cloud_run_job}"
    )
    request = run_v2.RunJobRequest(
        name=name,
        overrides={
            "container_overrides": [
                {"env": [{"name": "ONEBRIEF_JOB_URI", "value": job_uri}]}
            ],
            "task_count": 1,
        },
    )
    operation = run_v2.JobsClient().run_job(request=request)
    operation_name = getattr(getattr(operation, "operation", None), "name", "")
    return {
        "job_uri": job_uri,
        "cloud_run_job": name,
        "operation_name": operation_name,
        "asynchronous": True,
    }


def inspect_onebrief_job(job_uri: str) -> dict[str, Any]:
    """Validate the work-order namespace without reading protected job data."""

    settings = RuntimeSettings.from_env()
    return {"job_uri": validate_job_uri(job_uri, settings), "managed": True}


def return_tool_receipt_without_summarization(
    *, tool: Any, args: dict[str, Any], tool_context: Any, tool_response: Any
) -> None:
    """End routing turns on the exact tool receipt instead of a second model call."""

    del args, tool_response
    if tool.name == "start_approved_onebrief_job":
        tool_context.actions.skip_summarization = True


def build_agent_engine_app():
    from agentplatform.agent_engines import AdkApp
    from google.adk.sessions import InMemorySessionService

    settings = RuntimeSettings.from_env()
    root_agent = LlmAgent(
        name="onebrief_project_owner",
        description=(
            "Starts immutable budget-approved OneBrief work orders on the fixed "
            "Cloud Run executor."
        ),
        model=settings.model,
        instruction=(
            "You are the OneBrief project owner on Gemini Enterprise Agent Platform. "
            "Accept only an exact approved gs:// job URI. For an execution request, "
            "call start_approved_onebrief_job exactly once and report the receipt. "
            "For an inspection request, call inspect_onebrief_job. Never invent a URI, "
            "change a budget, select an executor, or retry a work order."
        ),
        tools=[start_approved_onebrief_job, inspect_onebrief_job],
        after_tool_callback=return_tool_receipt_without_summarization,
    )
    # This dispatcher is deliberately one-shot and stores no conversation data.
    # A custom ephemeral session also avoids granting the runtime permission to
    # create or read managed Agent Platform Sessions for immutable work orders.
    return AdkApp(
        agent=root_agent,
        enable_tracing=True,
        session_service_builder=InMemorySessionService,
    )
