"""The only allowed Gemini gateway for approved long-form execution."""

from __future__ import annotations

import json
import os
import time
from math import ceil
from pathlib import Path
from typing import Any, TypeVar

from google import genai
from google.genai import types
from pydantic import BaseModel

from onebrief.budget_guard import BudgetGuardError, BudgetStore, RunStatus
from onebrief.gemini_schema import gemini_compatible_model, restore_nullable_values
from onebrief.producer import approximate_tokens

T = TypeVar("T", bound=BaseModel)


class BudgetedGeminiClient:
    def __init__(self, run_dir: Path):
        self.store = BudgetStore(run_dir)
        self.client = genai.Client(
            vertexai=True,
            project=os.getenv("GOOGLE_CLOUD_PROJECT", "onebrief-agent-20260805"),
            location=os.getenv("GOOGLE_CLOUD_LOCATION", "global"),
        )

    @staticmethod
    def _transient_provider_error(exc: Exception) -> bool:
        text = f"{type(exc).__name__}: {exc}".casefold()
        return any(marker in text for marker in (
            "429", "resource_exhausted", "resource exhausted",
            "503", "service_unavailable", "temporarily unavailable",
        ))

    def _invoke_with_transient_retry(self, operation):
        """Retry only capacity failures under the existing hard cost reservation."""
        delays = (5, 15, 30)
        for attempt in range(len(delays) + 1):
            try:
                return operation()
            except Exception as exc:
                if attempt == len(delays) or not self._transient_provider_error(exc):
                    raise
                time.sleep(delays[attempt])

    @staticmethod
    def _thinking_config(model: str) -> types.ThinkingConfig:
        """Return a model-compatible bounded reasoning setting.

        Gemini 3.1 Pro uses thinking levels and rejects the Flash-oriented
        MINIMAL/zero-budget configuration. LOW keeps the critical decision call
        bounded; reasoning tokens remain inside max_output_tokens and are settled
        against the same hard budget ledger.
        """
        if model == "gemini-3.1-pro-preview":
            return types.ThinkingConfig(thinking_level="low")
        return types.ThinkingConfig(thinking_budget=0)

    def _generate(
        self,
        *,
        stage: str,
        model: str,
        contents: Any,
        max_output_tokens: int,
        system_instruction: str | None,
        temperature: float,
        response_schema: type[BaseModel] | None,
    ) -> Any:
        status = self.store.read().status
        if status not in {RunStatus.APPROVED, RunStatus.RUNNING}:
            raise BudgetGuardError(f"run cannot start a call while {status.value}")

        # Use the least model-compatible reasoning setting. max_output_tokens remains
        # the enforceable cap for response plus reasoning tokens.
        thinking = self._thinking_config(model)
        provider_schema = gemini_compatible_model(response_schema) if response_schema else None
        generation = types.GenerationConfig(
            max_output_tokens=max_output_tokens,
            temperature=temperature,
            response_mime_type="application/json" if response_schema else None,
            response_schema=provider_schema,
            thinking_config=thinking,
        )
        count = self.client.models.count_tokens(
            model=model,
            contents=contents,
            config=types.CountTokensConfig(
                system_instruction=system_instruction,
                generation_config=generation,
            ),
        )
        # Vertex countTokens does not include response-schema tokens. Add schema and
        # system text locally, then keep 15% plus 256 tokens of conservative headroom.
        schema_text = (
            json.dumps(provider_schema.model_json_schema(), ensure_ascii=False, sort_keys=True)
            if provider_schema
            else ""
        )
        observed = int(count.total_tokens or 0)
        local_overhead = approximate_tokens(schema_text) + approximate_tokens(system_instruction or "")
        input_cap = ceil((observed + local_overhead) * 1.15) + 256
        reservation = self.store.reserve_call(
            stage=stage,
            model=model,
            input_token_cap=input_cap,
            output_token_cap=max_output_tokens,
        )
        try:
            response = self._invoke_with_transient_retry(lambda: self.client.models.generate_content(
                model=model,
                contents=contents,
                config=types.GenerateContentConfig(
                    system_instruction=system_instruction,
                    max_output_tokens=max_output_tokens,
                    temperature=temperature,
                    response_mime_type="application/json" if response_schema else None,
                    response_schema=provider_schema,
                    thinking_config=thinking,
                ),
            ))
        except Exception as exc:
            self.store.release_call(reservation.call_id, f"provider call failed: {type(exc).__name__}")
            raise

        usage: Any = response.usage_metadata
        actual_input = int(getattr(usage, "prompt_token_count", 0) or observed)
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
        return response

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
        response = self._generate(
            stage=stage,
            model=model,
            contents=contents,
            max_output_tokens=max_output_tokens,
            system_instruction=system_instruction,
            temperature=temperature,
            response_schema=None,
        )
        return response.text or ""

    def generate_json(
        self,
        *,
        stage: str,
        model: str,
        contents: str,
        schema: type[T],
        max_output_tokens: int,
        system_instruction: str,
        temperature: float = 0.1,
    ) -> T:
        response = self._generate(
            stage=stage,
            model=model,
            contents=contents,
            max_output_tokens=max_output_tokens,
            system_instruction=system_instruction,
            temperature=temperature,
            response_schema=schema,
        )
        if response.parsed is not None:
            parsed = restore_nullable_values(schema, response.parsed)
            return schema.model_validate(parsed)
        if not response.text:
            raise ValueError(f"{stage} returned no structured response")
        return schema.model_validate_json(response.text)

    def generate_json_with_images(
        self,
        *,
        stage: str,
        model: str,
        contents: str,
        image_paths: list[Path],
        schema: type[T],
        max_output_tokens: int,
        system_instruction: str,
        temperature: float = 0.0,
    ) -> T:
        """Run a budgeted structured multimodal observation.

        Images are accepted only from explicit local paths supplied by the
        pipeline. The same immutable reservation and settlement path used by
        every text call remains authoritative.
        """

        if not image_paths:
            raise ValueError("multimodal observation requires at least one image")
        if len(image_paths) > 6:
            raise ValueError("multimodal observation accepts at most six images")
        parts: list[types.Part] = [types.Part.from_text(text=contents)]
        for path in image_paths:
            resolved = path.resolve()
            if not resolved.is_file() or resolved.suffix.casefold() != ".png":
                raise ValueError(f"multimodal observation requires a PNG file: {path}")
            parts.append(types.Part.from_bytes(data=resolved.read_bytes(), mime_type="image/png"))
        response = self._generate(
            stage=stage,
            model=model,
            contents=parts,
            max_output_tokens=max_output_tokens,
            system_instruction=system_instruction,
            temperature=temperature,
            response_schema=schema,
        )
        if response.parsed is not None:
            parsed = restore_nullable_values(schema, response.parsed)
            return schema.model_validate(parsed)
        if not response.text:
            raise ValueError(f"{stage} returned no structured multimodal response")
        return schema.model_validate_json(response.text)

    def generate_adk_response(
        self,
        *,
        stage: str,
        model: str,
        contents: list[types.Content],
        config: types.GenerateContentConfig | None,
    ) -> Any:
        """Execute an ADK model turn through the immutable OneBrief budget ledger.

        ADK remains responsible for agent lifecycle, state, structured output and
        events. This method is deliberately the only provider boundary so an ADK
        agent cannot bypass the approved token and cost ceiling.
        """

        status = self.store.read().status
        if status not in {RunStatus.APPROVED, RunStatus.RUNNING}:
            raise BudgetGuardError(f"run cannot start a call while {status.value}")
        generation = config.model_copy(deep=True) if config is not None else types.GenerateContentConfig()
        generation.thinking_config = self._thinking_config(model)
        max_output_tokens = int(generation.max_output_tokens or 4096)
        count_generation = types.GenerationConfig(
            max_output_tokens=max_output_tokens,
            temperature=generation.temperature,
            response_mime_type=generation.response_mime_type,
            response_schema=generation.response_schema,
            thinking_config=generation.thinking_config,
        )
        count = self.client.models.count_tokens(
            model=model,
            contents=contents,
            config=types.CountTokensConfig(
                system_instruction=generation.system_instruction,
                generation_config=count_generation,
            ),
        )
        schema_text = str(generation.response_schema or "")
        system_text = str(generation.system_instruction or "")
        observed = int(count.total_tokens or 0)
        local_overhead = approximate_tokens(schema_text) + approximate_tokens(system_text)
        input_cap = ceil((observed + local_overhead) * 1.15) + 256
        reservation = self.store.reserve_call(
            stage=stage,
            model=model,
            input_token_cap=input_cap,
            output_token_cap=max_output_tokens,
        )
        try:
            response = self._invoke_with_transient_retry(lambda: self.client.models.generate_content(
                model=model,
                contents=contents,
                config=generation,
            ))
        except Exception as exc:
            self.store.release_call(
                reservation.call_id, f"provider call failed: {type(exc).__name__}"
            )
            raise
        usage: Any = response.usage_metadata
        actual_input = int(getattr(usage, "prompt_token_count", 0) or observed)
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
        return response

