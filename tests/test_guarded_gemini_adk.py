from types import SimpleNamespace

from google.genai import types

from onebrief.budget_guard import RunStatus
from onebrief.guarded_gemini import BudgetedGeminiClient


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
