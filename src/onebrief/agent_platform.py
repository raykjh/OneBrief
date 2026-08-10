"""Gemini Enterprise Agent Platform entry point for OneBrief.

The managed ADK agent owns orchestration only.  Approved work is already
snapshotted in Cloud Storage by the web tier; the only mutating tool available
to the agent validates that immutable work order and starts the fixed Cloud Run
Job.  It cannot choose another project, bucket, region, job, or budget.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any

from google.adk.agents import LlmAgent

from onebrief.cloud_jobs import execute_cloud_run_job, parse_gcs_job_uri


_JOB_PREFIX = re.compile(r"^jobs/[0-9a-fA-F-]{36}$")


@dataclass(frozen=True)
class AgentPlatformSettings:
    project: str
    region: str
    bucket: str
    cloud_run_job: str
    model: str

    @classmethod
    def from_env(cls) -> "AgentPlatformSettings":
        return cls(
            project=os.environ.get("ONEBRIEF_PROJECT", "onebrief-agent-20260805"),
            region=os.environ.get("ONEBRIEF_REGION", "asia-northeast3"),
            bucket=os.environ.get(
                "ONEBRIEF_JOB_BUCKET", "onebrief-agent-20260805-jobs"
            ),
            cloud_run_job=os.environ.get("ONEBRIEF_CLOUD_RUN_JOB", "onebrief-worker"),
            model=os.environ.get(
                "ONEBRIEF_AGENT_PLATFORM_MODEL", "gemini-3.5-flash"
            ),
        )


def validate_approved_job_uri(job_uri: str, settings: AgentPlatformSettings) -> str:
    """Validate routing only; the worker remains the approval authority.

    The Agent Platform service identity intentionally has no Storage permission.
    Only the web tier can create a work order in this bucket, and the fixed Cloud
    Run worker re-validates its immutable approval, hashes, and hard budget before
    any model or development tool executes.
    """

    location = parse_gcs_job_uri(job_uri)
    if location.bucket != settings.bucket:
        raise PermissionError("job URI is outside the approved OneBrief bucket")
    if not _JOB_PREFIX.fullmatch(location.prefix):
        raise PermissionError("job URI is outside the approved OneBrief jobs namespace")
    return location.uri


def start_approved_onebrief_job(job_uri: str) -> dict[str, Any]:
    """Start exactly one already-approved OneBrief work order.

    Args:
        job_uri: Immutable ``gs://`` work-order URI returned by OneBrief stage 1.

    Returns:
        A Cloud Run execution receipt.  This tool cannot create approvals,
        increase budgets, edit work orders, or select a different executor.
    """

    settings = AgentPlatformSettings.from_env()
    validate_approved_job_uri(job_uri, settings)
    receipt = execute_cloud_run_job(
        job_uri,
        project=settings.project,
        region=settings.region,
        cloud_run_job=settings.cloud_run_job,
    )
    return receipt.model_dump(mode="json")


def inspect_onebrief_job(job_uri: str) -> dict[str, Any]:
    """Confirm that a work-order URI is within the managed OneBrief namespace."""

    settings = AgentPlatformSettings.from_env()
    return {"job_uri": validate_approved_job_uri(job_uri, settings), "managed": True}


def build_project_owner_agent() -> LlmAgent:
    """Build the managed project-owner agent with a deliberately tiny tool surface."""

    settings = AgentPlatformSettings.from_env()
    return LlmAgent(
        name="onebrief_project_owner",
        description=(
            "Starts and observes immutable, budget-approved OneBrief work orders and "
            "never performs unapproved project mutations."
        ),
        model=settings.model,
        instruction=(
            "You are the OneBrief project owner running on Gemini Enterprise Agent Platform. "
            "Accept only an exact approved gs:// job URI. Call start_approved_onebrief_job "
            "once, then report its execution receipt. Never invent a URI, change a budget, "
            "select another executor, or retry a non-queued job. Use inspect_onebrief_job only "
            "for status requests. The Cloud Run worker owns the maker-verifier-revision loop."
        ),
        tools=[start_approved_onebrief_job, inspect_onebrief_job],
    )


root_agent = build_project_owner_agent()


def build_agent_engine_app():
    """Return a serializable ADK application for Agent Platform deployment."""

    from agentplatform.agent_engines import AdkApp

    return AdkApp(agent=root_agent, enable_tracing=True)
