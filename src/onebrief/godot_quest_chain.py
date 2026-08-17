"""Common verification of raw, receipt-gated Godot Quest successor chains."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path, PurePosixPath

from pydantic import BaseModel, Field

from onebrief.execution_schemas import Verdict
from onebrief.quest_orchestration import (
    QuestContract,
    QuestState,
    QuestVerificationReceipt,
)


def _sha(value: object) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json", exclude_none=True)
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")).hexdigest()


def _file_sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class VerifiedGodotQuestCheckpoint(BaseModel):
    result_dir: str
    candidate_dir: str
    milestone_id: str = Field(pattern=r"^M[0-9]{2}$")
    source_revision: str = Field(pattern=r"^[a-f0-9]{40}$")
    quest_contract: QuestContract
    quest_receipt: QuestVerificationReceipt
    execution_checkpoint: dict[str, object]
    delta_hashes: dict[str, str]
    complete_candidate_hashes: dict[str, str]


def _relative_under_prefix(path: str, prefix: str) -> str:
    relative = PurePosixPath(path.replace("\\", "/"))
    if not relative.parts or relative.parts[0] != prefix or ".." in relative.parts:
        raise PermissionError("Godot Quest changed file escaped the approved prefix")
    return PurePosixPath(*relative.parts[1:]).as_posix()


def _is_godot_uid_sidecar(candidate: Path, path: str, expected: dict[str, str]) -> bool:
    """Classify only engine-authored script UID sidecars as ephemeral evidence noise."""

    if not path.endswith(".gd.uid") or path[:-4] not in expected:
        return False
    target = candidate / Path(*PurePosixPath(path).parts)
    if not target.is_file() or target.stat().st_size > 96:
        return False
    return re.fullmatch(r"uid://[a-z0-9]+\r?\n?", target.read_text(encoding="utf-8")) is not None


def verify_godot_quest_chain(
    result_dirs: list[Path],
    *,
    project_prefix: str = "game",
) -> list[VerifiedGodotQuestCheckpoint]:
    """Verify every raw receipt and every inherited candidate byte in order."""

    if not result_dirs:
        raise ValueError("Godot Quest chain cannot be empty")
    verified: list[VerifiedGodotQuestCheckpoint] = []
    expected_complete: dict[str, str] = {}
    source_revision: str | None = None
    for index, raw_dir in enumerate(result_dirs):
        result_dir = raw_dir.resolve()
        run = json.loads((result_dir / "run_result.json").read_text(encoding="utf-8"))
        contract = QuestContract.model_validate(run["quest_contract"])
        receipt = QuestVerificationReceipt.model_validate(run["quest_receipt"])
        execution = dict(run["execution_receipt"])
        if receipt.state != QuestState.PASSED or receipt.verdict != Verdict.PASS or execution.get("passed") is not True:
            raise PermissionError("Godot successor requires a verified PASS chain")
        if receipt.quest_id != contract.quest_id or receipt.quest_sha256 != contract.sha256:
            raise PermissionError("Godot Quest contract and receipt lineage differ")
        identity = _sha(receipt.model_dump(mode="json", exclude={"receipt_id", "created_at"}))
        if receipt.receipt_id != "QR-" + identity[:16]:
            raise PermissionError("Godot Quest receipt identity is not canonical")
        ledger = json.loads((result_dir / "completion_ledger.json").read_text(encoding="utf-8"))
        verification = json.loads((result_dir / "independent_verification.json").read_text(encoding="utf-8"))
        checkpoint = json.loads((result_dir / "execution_checkpoint.json").read_text(encoding="utf-8"))
        if receipt.completion_ledger_sha256 != _sha(ledger) or receipt.verification_sha256 != _sha(verification) or receipt.execution_checkpoint_sha256 != _sha(checkpoint):
            raise PermissionError("Godot Quest raw evidence digests do not match its receipt")
        current_source = str(execution.get("source_revision", ""))
        if source_revision is None:
            source_revision = current_source
        if current_source != source_revision or checkpoint.get("source_revision") != source_revision:
            raise PermissionError("Godot Quest chain source revision changed")
        if index:
            parent = verified[-1]
            if contract.parent_quest_id != parent.quest_contract.quest_id:
                raise PermissionError("Godot successor parent Quest is not the verified predecessor")
            if contract.input_checkpoint.previous_receipt_id != parent.quest_receipt.receipt_id:
                raise PermissionError("Godot successor previous receipt is not the verified predecessor")
            if contract.input_checkpoint.previous_receipt_sha256 != parent.quest_receipt.sha256:
                raise PermissionError("Godot successor previous receipt digest changed")
            if parent.quest_receipt.receipt_id not in receipt.preserved_receipt_ids:
                raise PermissionError("Godot successor did not preserve the predecessor receipt")
        candidate = Path(run["candidate_dir"]).resolve()
        project = candidate / project_prefix
        changed_files = execution.get("changed_files")
        if not isinstance(changed_files, list) or not changed_files:
            raise PermissionError("Godot Quest execution receipt has no bounded delta")
        delta_hashes: dict[str, str] = {}
        inner_hashes: dict[str, str] = {}
        for changed in changed_files:
            relative = _relative_under_prefix(str(changed), project_prefix)
            target = project / Path(*PurePosixPath(relative).parts)
            if not target.is_file():
                raise FileNotFoundError(f"verified Godot candidate file is missing: {changed}")
            digest = _file_sha(target)
            full_path = f"{project_prefix}/{relative}"
            delta_hashes[full_path] = digest
            inner_hashes[relative] = digest
        checkpoint_digest = checkpoint.get("candidate_tree_sha256") if index == 0 else checkpoint.get("candidate_delta_sha256")
        if checkpoint_digest != _sha(dict(sorted(inner_hashes.items()))):
            raise PermissionError("Godot candidate delta no longer matches its execution checkpoint")
        for inherited, digest in expected_complete.items():
            if inherited in delta_hashes:
                continue
            target = candidate / Path(*PurePosixPath(inherited).parts)
            if not target.is_file() or _file_sha(target) != digest:
                raise PermissionError("Godot successor changed an inherited verified file outside its delta")
        expected_complete.update(delta_hashes)
        status = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all"], cwd=candidate,
            capture_output=True, text=True, encoding="utf-8", errors="replace", check=True,
        ).stdout.splitlines()
        observed = sorted(
            path for line in status
            if not _is_godot_uid_sidecar(
                candidate, path := line[3:].replace("\\", "/"), expected_complete
            )
        )
        if observed != sorted(expected_complete):
            raise PermissionError("Godot candidate contains files outside the verified Quest chain")
        verified.append(VerifiedGodotQuestCheckpoint(
            result_dir=str(result_dir), candidate_dir=str(candidate),
            milestone_id=contract.milestone_id, source_revision=source_revision,
            quest_contract=contract, quest_receipt=receipt,
            execution_checkpoint=checkpoint, delta_hashes=delta_hashes,
            complete_candidate_hashes=dict(sorted(expected_complete.items())),
        ))
    return verified
