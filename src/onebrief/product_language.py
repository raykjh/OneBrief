"""Canonical language boundary for KHALINOS-authored control-plane text."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

from pydantic import BaseModel


CANONICAL_PRODUCT_LANGUAGE = "en"

_NON_ENGLISH_SCRIPT = re.compile(
    r"[\u1100-\u11ff\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af\ufffd]"
)


def _text_values(value: Any, path: str = "$") -> Iterable[tuple[str, str]]:
    if isinstance(value, BaseModel):
        yield from _text_values(value.model_dump(mode="json"), path)
    elif isinstance(value, Mapping):
        for key, item in value.items():
            yield from _text_values(item, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            yield from _text_values(item, f"{path}[{index}]")
    elif isinstance(value, str):
        yield path, value


def enforce_canonical_product_language(value: Any, *, surface: str) -> None:
    """Fail closed when KHALINOS-authored control text is not canonical English.

    Authoritative source bodies are intentionally excluded by callers. Translation
    belongs to a future presentation layer and must never mutate signed contracts,
    receipts, or evidence.
    """

    violations = [
        path for path, text in _text_values(value) if _NON_ENGLISH_SCRIPT.search(text)
    ]
    if violations:
        sample = ", ".join(violations[:5])
        raise ValueError(
            f"{surface} contains non-English product text at {sample}; "
            "KHALINOS contracts and evidence use canonical English"
        )
