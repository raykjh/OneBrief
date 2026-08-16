"""Durable shadow and authoritative transition recording for the Quest Kernel."""

from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import uuid4

from onebrief.quest_kernel.models import (
    QuestTransitionDecision,
    RawQuestReceipt,
    canonical_sha256,
)
from onebrief.quest_kernel.receipt_interpreter import interpret_raw_receipt
from onebrief.quest_kernel.transition_guard import validate_transition


def _atomic_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + f".{uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


class QuestKernelEngine:
    """Interpret and persist a digest-bound transition without widening authority."""

    def __init__(self, root: Path):
        self.root = root
        self.decisions = root / "transition_decisions"
        self.current = root / "current_transition.json"

    def record(self, raw: RawQuestReceipt) -> QuestTransitionDecision:
        decision = validate_transition(raw, interpret_raw_receipt(raw))
        stable = {
            "raw_receipt": raw.model_dump(mode="json", exclude_none=True),
            "decision": decision.model_dump(mode="json", exclude_none=True),
        }
        decision_id = "QD-" + canonical_sha256(stable)[:16]
        payload = {
            "schema_version": "khalinos-recorded-quest-transition-v1",
            "decision_id": decision_id,
            **stable,
        }
        path = self.decisions / f"{decision_id}.json"
        if path.is_file():
            existing = json.loads(path.read_text(encoding="utf-8"))
            if canonical_sha256(existing) != canonical_sha256(payload):
                raise RuntimeError("existing Quest transition ID has different content")
        else:
            _atomic_json(path, payload)
        _atomic_json(self.current, {
            "schema_version": "khalinos-current-transition-pointer-v1",
            "decision_id": decision_id,
            "decision_sha256": canonical_sha256(payload),
            "path": f"transition_decisions/{decision_id}.json",
        })
        return decision
