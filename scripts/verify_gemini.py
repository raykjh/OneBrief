"""Minimal Vertex AI Gemini availability check for OneBrief.

This script uses Application Default Credentials and does not read or persist
API keys. It intentionally sends a tiny prompt and prints only the response,
model identifier, and usage metadata.
"""

from __future__ import annotations

import json
import os

from google import genai
from google.genai import types


PROJECT_ID = os.getenv("GOOGLE_CLOUD_PROJECT", "ares-agentic-cinema-20260729")
LOCATION = os.getenv("GOOGLE_CLOUD_LOCATION", "global")
MODEL_ID = os.getenv("ONEBRIEF_VERIFY_MODEL", "gemini-3.5-flash")


def _thinking_config(model_id: str) -> types.ThinkingConfig:
    # Match the production gateway's model-specific bounded reasoning policy.
    if model_id == "gemini-3.1-pro-preview":
        return types.ThinkingConfig(thinking_level="low")
    return types.ThinkingConfig(thinking_budget=0)


def main() -> None:
    client = genai.Client(
        vertexai=True,
        project=PROJECT_ID,
        location=LOCATION,
        http_options=types.HttpOptions(api_version="v1"),
    )
    response = client.models.generate_content(
        model=MODEL_ID,
        contents="Reply with exactly ONEBRIEF_OK and nothing else.",
        config=types.GenerateContentConfig(
            max_output_tokens=128,
            thinking_config=_thinking_config(MODEL_ID),
        ),
    )

    usage = response.usage_metadata
    result = {
        "ok": (response.text or "").strip() == "ONEBRIEF_OK",
        "response": (response.text or "").strip(),
        "model": MODEL_ID,
        "project": PROJECT_ID,
        "location": LOCATION,
        "usage": usage.model_dump(mode="json") if usage else None,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
