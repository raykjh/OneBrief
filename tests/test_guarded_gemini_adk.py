from types import SimpleNamespace

from google.genai import types

from onebrief.budget_guard import RunStatus
from onebrief.guarded_gemini import BudgetedGeminiClient


def test_thinking_policy_is_compatible_with_pro_and_bounded_for_flash() -> None:
    pro = BudgetedGeminiClient._thinking_config("gemini-3.1-pro-preview")
    flash = BudgetedGeminiClient._thinking_config("gemini-3.5-flash")

    assert pro.thinking_level == types.ThinkingLevel.LOW
    assert pro.thinking_budget is None
    assert flash.thinking_level is None
    assert flash.thinking_budget == 0


def test_provider_timeout_is_bounded() -> None:
    assert BudgetedGeminiClient.PROVIDER_TIMEOUT_MS == 360_000


def test_adk_count_tokens_uses_generation_config_not_generate_content_config() -> None:
    captured = {}

    class Store:
        def read(self):
            return SimpleNamespace(status=RunStatus.APPROVED)

        def reserve_call(self, **_kwargs):
            return SimpleNamespace(call_id="call-1")

        def settle_call(self, *_args, **_kwargs):
            return None

    class Models:
        def count_tokens(self, **kwargs):
            captured["config"] = kwargs["config"]
            return SimpleNamespace(total_tokens=10)

        def generate_content(self, **_kwargs):
            return SimpleNamespace(usage_metadata=SimpleNamespace(
                prompt_token_count=10,
                candidates_token_count=2,
                thoughts_token_count=0,
                total_token_count=12,
            ))

    client = BudgetedGeminiClient.__new__(BudgetedGeminiClient)
    client.store = Store()
    client.client = SimpleNamespace(models=Models())

    client.generate_adk_response(
        stage="test_adk",
        model="gemini-3.5-flash",
        contents=[types.Content(role="user", parts=[types.Part(text="hello")])],
        config=types.GenerateContentConfig(max_output_tokens=32),
    )

    assert isinstance(
        captured["config"].generation_config,
        types.GenerationConfig,
    )
