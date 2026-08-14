"""Gemini-compatible transport schemas for constrained domain models.

Gemini structured output accepts a useful subset of JSON Schema. Domain models
keep their strict Pydantic constraints, but those constraints must not be sent
directly because unsupported keywords make the whole call fail.
"""

from __future__ import annotations

import types
from functools import lru_cache
from typing import Annotated, Any, Union, get_args, get_origin

from pydantic import BaseModel, create_model


def _transport_annotation(annotation: Any) -> Any:
    origin = get_origin(annotation)
    if origin is Annotated:
        return _transport_annotation(get_args(annotation)[0])
    if origin in {Union, types.UnionType}:
        members = [item for item in get_args(annotation) if item is not type(None)]
        if len(members) == 1:
            return _transport_annotation(members[0])
        return Union[tuple(_transport_annotation(item) for item in members)]
    if origin is list:
        return list[_transport_annotation(get_args(annotation)[0])]
    if origin is dict:
        key_type, value_type = get_args(annotation)
        return dict[_transport_annotation(key_type), _transport_annotation(value_type)]
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return gemini_compatible_model(annotation)
    return annotation

def _strip_annotated(annotation: Any) -> Any:
    while get_origin(annotation) is Annotated:
        annotation = get_args(annotation)[0]
    return annotation


def restore_nullable_values(model: type[BaseModel], payload: Any) -> Any:
    """Restore JSON nulls that a non-null transport schema emitted as strings."""

    if isinstance(payload, BaseModel):
        payload = payload.model_dump(mode="json")
    if not isinstance(payload, dict):
        return payload
    restored = dict(payload)
    for name, field in model.model_fields.items():
        if name not in restored:
            continue
        annotation = _strip_annotated(field.annotation)
        origin = get_origin(annotation)
        args = get_args(annotation)
        nullable = origin in {Union, types.UnionType} and type(None) in args
        value = restored[name]
        if nullable and isinstance(value, str) and value.strip().casefold() in {"null", "none"}:
            restored[name] = None
            continue
        candidates = [item for item in args if item is not type(None)] if nullable else [annotation]
        nested = candidates[0] if len(candidates) == 1 else annotation
        nested = _strip_annotated(nested)
        nested_origin = get_origin(nested)
        if isinstance(nested, type) and issubclass(nested, BaseModel):
            restored[name] = restore_nullable_values(nested, value)
        elif nested_origin is list and isinstance(value, list):
            item_type = _strip_annotated(get_args(nested)[0])
            if isinstance(item_type, type) and issubclass(item_type, BaseModel):
                restored[name] = [restore_nullable_values(item_type, item) for item in value]
    return restored



@lru_cache(maxsize=None)
def gemini_compatible_model(model: type[BaseModel]) -> type[BaseModel]:
    """Return an unconstrained, all-required transport version of model."""

    fields = {
        name: (_transport_annotation(field.annotation), ...)
        for name, field in model.model_fields.items()
    }
    return create_model(f"{model.__name__}GeminiTransport", **fields)
