"""Collect authoritative, unnormalized execution outputs into a raw Quest receipt."""

from __future__ import annotations

import re
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel

from onebrief.completion_ledger import CompletionLedger
from onebrief.convergence_policy import RepairContract
from onebrief.execution_schemas import ExecutionCheckpoint, VerificationReport
from onebrief.phase_execution import PhaseDecision
from onebrief.quest_kernel.models import RawQuestReceipt

ModelT = TypeVar("ModelT", bound=BaseModel)


def _round_number(path: Path) -> int:
    match = re.search(r"(?:_r|_f)(\d+)", path.stem)
    return int(match.group(1)) if match else -1


def _latest_model(paths: list[Path], model: type[ModelT]) -> ModelT | None:
    for path in sorted(paths, key=lambda item: (_round_number(item), item.name), reverse=True):
        try:
            return model.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
    return None


def collect_raw_quest_receipt(
    output_dir: Path,
    checkpoint: ExecutionCheckpoint,
) -> RawQuestReceipt:
    """Read the latest typed receipts without rewriting their content or ownership."""

    verification_paths = [output_dir / "final_verification.json"]
    verification_paths.extend(output_dir.glob("verification_r*.json"))
    phase_paths = list(output_dir.glob("phase_decision_deterministic_r*.json"))
    if not phase_paths:
        phase_paths = list(output_dir.glob("phase_decision_r*.json"))
    repair_paths = list(output_dir.glob("repair_contract_f*.json"))
    if not repair_paths and (output_dir / "repair_contract.json").is_file():
        repair_paths = [output_dir / "repair_contract.json"]
    ledger = _latest_model(
        [output_dir / "completion_ledger.json"],
        CompletionLedger,
    )
    return RawQuestReceipt(
        checkpoint=checkpoint,
        verification=_latest_model(verification_paths, VerificationReport),
        phase_decision=_latest_model(phase_paths, PhaseDecision),
        repair_contract=_latest_model(repair_paths, RepairContract),
        completion_ledger_complete=bool(ledger is not None and ledger.complete),
        artifacts={
            "output_directory": output_dir.name,
        },
    )
