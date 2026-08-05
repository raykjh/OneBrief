"""The only allowed Gemini gateway for approved long-form execution."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from google import genai
from google.genai import types

from onebrief.budget_guard import BudgetGuardError, BudgetStore, RunStatus


class BudgetedGeminiClient:
    def __init__(self, run_dir: Path):
        self.store = BudgetStore(run_dir)
        self.client = genai.Client(
            vertexai=True,
            project=os.getenv("GOOGLE_CLOUD_PROJECT", "onebrief-agent-20260805"),
            location=os.getenv("GOOGLE_CLOUD_LOCATION", "global"),
        )

    def generate_text(
        self,
        *,
        stage: str,
        model: str,
        contents: str,
        max_output_tokens: int,
        system_instruction: str | None = None,
        temperature: float = 0.1,
    ) -> str:
        # Reject a closed/blocked run before even making the unbilled countTokens call.
        status = self.store.read().status
        if status not in {RunStatus.APPROVED, RunStatus.RUNNING}:
            raise BudgetGuardError(f"run cannot start a call while {status.value}")

        generation = types.GenerationConfig(
            max_output_tokens=max_output_tokens,
            temperature=temperature,
        )
        count = self.client.models.count_tokens(
            model=model,
            contents=contents,
            config=types.CountTokensConfig(
                system_instruction=system_instruction,
                generation_config=generation,
            ),
        )
        input_cap = int(count.total_tokens or 0)
        reservation = self.store.reserve_call(
            stage=stage,
            model=model,
            input_token_cap=input_cap,
            output_token_cap=max_output_tokens,
        )
        try:
            response = self.client.models.generate_content(
                model=model,
                contents=contents,
                config=types.GenerateContentConfig(
                    system_instruction=system_instruction,
                    max_output_tokens=max_output_tokens,
                    temperature=temperature,
                ),
            )
        except Exception as exc:
            self.store.release_call(reservation.call_id, f"provider call failed: {type(exc).__name__}")
            raise

        usage: Any = response.usage_metadata
        actual_input = int(getattr(usage, "prompt_token_count", 0) or input_cap)
        candidate_tokens = int(getattr(usage, "candidates_token_count", 0) or 0)
        thought_tokens = int(getattr(usage, "thoughts_token_count", 0) or 0)
        actual_output = candidate_tokens + thought_tokens
        if actual_output == 0:
            total = int(getattr(usage, "total_token_count", 0) or actual_input)
            actual_output = max(0, total - actual_input)
        self.store.settle_call(
            reservation.call_id,
            input_tokens=actual_input,
            output_tokens=actual_output,
        )
        return response.text or ""

