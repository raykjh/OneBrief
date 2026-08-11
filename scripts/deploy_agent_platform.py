"""Deploy or inspect OneBrief's managed ADK project-owner agent."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import agentplatform

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Agent objects are serialized locally before the runtime configuration is
# applied. Pin the construction-time model project so a developer's gcloud/ADC
# default project can never leak into the deployed agent.
os.environ["ONEBRIEF_PROJECT"] = "onebrief-agent-20260805"
os.environ["ONEBRIEF_MODEL_LOCATION"] = "global"
os.environ["GOOGLE_CLOUD_PROJECT"] = "onebrief-agent-20260805"
os.environ["GOOGLE_CLOUD_LOCATION"] = "global"

from agent_runtime.onebrief_runtime import build_agent_engine_app


PROJECT = "onebrief-agent-20260805"
LOCATION = "asia-northeast3"
BUCKET = "gs://onebrief-agent-20260805-jobs"
SERVICE_ACCOUNT = (
    "onebrief-agent-runtime@onebrief-agent-20260805.iam.gserviceaccount.com"
)


def _config() -> dict[str, object]:
    return {
        "staging_bucket": BUCKET,
        "display_name": "OneBrief Project Owner",
        "description": (
            "ADK project owner that starts immutable budget-approved OneBrief "
            "work orders on the fixed Cloud Run executor."
        ),
        "requirements": [
            "google-adk==2.6.2",
            "google-cloud-aiplatform[agent_engines]>=1.112,<2",
            "google-cloud-run==0.16.1",
            "cloudpickle>=3.1,<4",
            "pydantic>=2.13,<3",
        ],
        # Keep the archive path relative so Agent Platform extracts this
        # self-contained package at its runtime import root.
        "extra_packages": ["agent_runtime"],
        "env_vars": {
            "GOOGLE_GENAI_USE_VERTEXAI": "TRUE",
            "ONEBRIEF_PROJECT": PROJECT,
            "ONEBRIEF_REGION": LOCATION,
            "ONEBRIEF_JOB_BUCKET": "onebrief-agent-20260805-jobs",
            "ONEBRIEF_CLOUD_RUN_JOB": "onebrief-worker",
            "ONEBRIEF_AGENT_PLATFORM_MODEL": "gemini-3.5-flash",
            "ONEBRIEF_MODEL_LOCATION": "global",
            "GOOGLE_CLOUD_AGENT_ENGINE_ENABLE_TELEMETRY": "true",
        },
        "service_account": SERVICE_ACCOUNT,
        # OneBrief's product flow completes one approved project at a time.
        # Parallel campaigns are an internal regression tool, not a reason to
        # expand the production project-owner runtime concurrently.
        "min_instances": 1,
        "max_instances": 1,
        "resource_limits": {"cpu": "1", "memory": "1Gi"},
        "labels": {"app": "onebrief", "component": "project-owner"},
        "python_version": "3.12",
        "agent_framework": "google-adk",
    }


def deploy(resource_name: str | None = None) -> dict[str, object]:
    client = agentplatform.Client(project=PROJECT, location=LOCATION)
    if resource_name:
        engine = client.agent_engines.update(
            name=resource_name,
            agent=build_agent_engine_app(),
            config=_config(),
        )
    else:
        engine = client.agent_engines.create(
            agent=build_agent_engine_app(),
            config=_config(),
        )
    resource = engine.api_resource
    return {
        "name": resource.name,
        "display_name": resource.display_name,
        "location": LOCATION,
        "service_account": SERVICE_ACCOUNT,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("deploy", "update"))
    parser.add_argument("--resource-name")
    args = parser.parse_args()
    if args.command == "deploy":
        print(json.dumps(deploy(), indent=2))
    else:
        if not args.resource_name:
            parser.error("update requires --resource-name")
        print(json.dumps(deploy(args.resource_name), indent=2))


if __name__ == "__main__":
    main()
