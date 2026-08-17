from __future__ import annotations

import pytest

from onebrief.product_language import (
    CANONICAL_PRODUCT_LANGUAGE,
    enforce_canonical_product_language,
)
from onebrief.agents.requirements_analyst import REQUIREMENTS_ANALYST_INSTRUCTION


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
