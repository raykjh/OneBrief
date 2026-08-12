"""Deterministic memory for code deltas that already failed trusted verification."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel


REGISTER_NAME = "development_rejected_change_fingerprints.json"
HISTORY_NAME = "development_rejected_change_history.json"


def development_change_fingerprint(candidate: BaseModel | dict[str, Any]) -> str:
    """Hash executable file results while ignoring summaries and explanations."""

    payload = (
        candidate.model_dump(mode="json")
        if isinstance(candidate, BaseModel)
        else candidate
    )
    changes = sorted(
        (
            str(item.get("path", "")).replace("\\", "/").casefold(),
            item.get("base_sha256"),
            str(item.get("content", "")),
        )
        for item in payload.get("changes", [])
        if isinstance(item, dict)
    )
    canonical = json.dumps(changes, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def write_rejected_change_fingerprints(work: Path, fingerprints: set[str]) -> Path:
    target = work / REGISTER_NAME
    target.write_text(
        json.dumps({
            "schema_version": "onebrief-rejected-change-fingerprints-v1",
            "fingerprints": sorted(fingerprints),
        }, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return target


def discover_rejected_change_fingerprints(work: Path) -> set[str]:
    """Load the durable register and recover older failed round/delta pairs."""

    fingerprints: set[str] = set()
    register = work / REGISTER_NAME
    if register.is_file():
        try:
            payload = json.loads(register.read_text(encoding="utf-8"))
            fingerprints.update(
                str(item) for item in payload.get("fingerprints", [])
                if isinstance(item, str) and len(item) == 64
            )
        except (OSError, json.JSONDecodeError, AttributeError):
            pass
    for delta_path in work.glob("code_change_set_delta_r*.json"):
        suffix = delta_path.stem.removeprefix("code_change_set_delta_r")
        if not suffix.isdigit():
            continue
        if not (work / f"development_verification_failure_r{suffix}.txt").is_file():
            continue
        try:
            fingerprints.add(development_change_fingerprint(
                json.loads(delta_path.read_text(encoding="utf-8"))
            ))
        except (OSError, json.JSONDecodeError, AttributeError, TypeError):
            continue
    return fingerprints


def discover_rejected_change_history(work: Path) -> list[dict[str, Any]]:
    """Return compact, model-readable facts about already rejected deltas."""

    accepted = discover_rejected_change_fingerprints(work)
    records: dict[str, dict[str, Any]] = {}
    history = work / HISTORY_NAME
    if history.is_file():
        try:
            payload = json.loads(history.read_text(encoding="utf-8"))
            for item in payload.get("rejected_changes", []):
                if isinstance(item, dict) and item.get("fingerprint") in accepted:
                    records[str(item["fingerprint"])] = item
        except (OSError, json.JSONDecodeError, AttributeError):
            pass
    for delta_path in sorted(work.glob("code_change_set_delta_r*.json")):
        try:
            payload = json.loads(delta_path.read_text(encoding="utf-8"))
            fingerprint = development_change_fingerprint(payload)
        except (OSError, json.JSONDecodeError, AttributeError, TypeError):
            continue
        if fingerprint not in accepted:
            continue
        changes = [item for item in payload.get("changes", []) if isinstance(item, dict)]
        records[fingerprint] = {
            "fingerprint": fingerprint,
            "summary": str(payload.get("summary", ""))[:500],
            "changed_paths": sorted({str(item.get("path", "")) for item in changes}),
            "reasons": [str(item.get("reason", ""))[:300] for item in changes],
        }
    return [records[key] for key in sorted(records)]


def write_rejected_change_history(work: Path, records: list[dict[str, Any]]) -> Path:
    target = work / HISTORY_NAME
    target.write_text(
        json.dumps({
            "schema_version": "onebrief-rejected-change-history-v1",
            "rejected_changes": records,
        }, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return target
