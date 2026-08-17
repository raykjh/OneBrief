from __future__ import annotations

import asyncio

import pytest

from onebrief.product_language import (
    CANONICAL_PRODUCT_LANGUAGE,
    enforce_canonical_product_language,
)
from onebrief.agents.requirements_analyst import REQUIREMENTS_ANALYST_INSTRUCTION
from onebrief.runner import inspect_requirements
from tests.test_requirements_gate import _intake, _ready


def test_canonical_product_language_is_english() -> None:
    assert CANONICAL_PRODUCT_LANGUAGE == "en"
    enforce_canonical_product_language(
        {"objective": "Issue the next evidence-bound Quest."},
        surface="QuestContract",
    )


def test_requirements_contract_declares_english_canonical_language() -> None:
    assert "canonical product language" in REQUIREMENTS_ANALYST_INSTRUCTION
    assert "same language as the user's goal" not in REQUIREMENTS_ANALYST_INSTRUCTION


@pytest.mark.parametrize("text", ["다음 퀘스트", "次のクエスト", "下一任务", "broken � text"])
def test_non_english_control_plane_text_is_rejected(text: str) -> None:
    with pytest.raises(ValueError, match="canonical English"):
        enforce_canonical_product_language(
            {"objective": text},
            surface="QuestContract",
        )


def test_requirements_language_failure_gets_one_bounded_english_reissue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bad = _ready().model_copy(update={"deliverables": ["\ud55c\uae00 \uc0b0\ucd9c\ubb3c"]})
    good = _ready().model_copy(update={"deliverables": ["English deliverable"]})
    calls: list[str] = []

    async def fake_run(payload: dict[str, object]):
        calls.append(str(payload["mode"]))
        return bad if len(calls) == 1 else good

    monkeypatch.setattr("onebrief.runner._run_requirements", fake_run)

    result = asyncio.run(inspect_requirements(_intake()))

    assert result.deliverables == ["English deliverable"]
    assert calls == ["single_pass_with_sources", "canonical_english_reissue"]
