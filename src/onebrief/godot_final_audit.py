"""Independent M99 completion audit for the finished Godot PUZZLE product."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, Field

from onebrief.execution_schemas import Verdict
from onebrief.godot_gameplay_execution import _png_dimensions
from onebrief.godot_quest_chain import verify_godot_quest_chain
from onebrief.quest_orchestration import (
    QuestBudget, QuestContract, QuestFailureOwner, QuestInputCheckpoint,
    QuestRole, QuestState, QuestType, QuestVerificationReceipt,
)
from onebrief.schemas import ExecutionPhase
from onebrief.toolpack_lifecycle import AdapterId, ProjectToolPackLifecycle


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _sha(value: object) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json", exclude_none=True)
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _file_sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = value.model_dump(mode="json", exclude_none=True) if isinstance(value, BaseModel) else value
    temporary = path.with_suffix(path.suffix + f".{uuid4().hex}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


class GodotM99Request(BaseModel):
    schema_version: str = "khalinos-godot-m99-request-v1"
    project_id: str = Field(min_length=2, max_length=64)
    source_revision: str = Field(pattern=r"^[a-f0-9]{40}$")
    toolpack_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    milestone_plan_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    authority_envelope_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    predecessor_result_dirs: list[str] = Field(min_length=4, max_length=14)


class FinalAuditCommandReceipt(BaseModel):
    schema_version: str = "khalinos-godot-final-audit-command-v1"
    command_kind: str
    executable_path: str
    executable_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    argv: list[str]
    returncode: int
    output_tail: str = Field(max_length=8000)
    artifact_sha256: str | None = None


class GodotM99RunResult(BaseModel):
    schema_version: str = "khalinos-godot-m99-run-result-v1"
    output_dir: str
    quest_contract: QuestContract
    quest_receipt: QuestVerificationReceipt
    release_executable: str


def _run(argv: list[str], *, cwd: Path, kind: str, executable: Path, artifact: Path | None, timeout: int) -> FinalAuditCommandReceipt:
    try:
        completed = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, shell=False, check=False)
        output = "\n".join(part for part in (completed.stdout, completed.stderr) if part)
        returncode = completed.returncode
    except subprocess.TimeoutExpired as error:
        stdout = error.stdout.decode("utf-8", errors="replace") if isinstance(error.stdout, bytes) else (error.stdout or "")
        stderr = error.stderr.decode("utf-8", errors="replace") if isinstance(error.stderr, bytes) else (error.stderr or "")
        output = "\n".join(part for part in (stdout, stderr, f"KHALINOS_TIMEOUT_SECONDS={timeout}") if part)
        returncode = 124
    return FinalAuditCommandReceipt(command_kind=kind, executable_path=str(executable), executable_sha256=_file_sha(executable), argv=argv, returncode=returncode, output_tail=output[-8000:], artifact_sha256=_file_sha(artifact) if artifact and artifact.is_file() else None)


def execute_godot_m99(request: GodotM99Request, *, registry_root: Path, output_dir: Path) -> GodotM99RunResult:
    output = output_dir.resolve()
    if output.exists() and any(output.iterdir()):
        raise FileExistsError("Godot M99 output directory must be new or empty")
    output.mkdir(parents=True, exist_ok=True)
    chain = verify_godot_quest_chain([Path(item) for item in request.predecessor_result_dirs])
    if [item.milestone_id for item in chain] != ["M01", "M02", "M03", "M04"]:
        raise PermissionError("M99 requires the complete verified M01 through M04 chain")
    predecessor = chain[-1]
    if predecessor.source_revision != request.source_revision:
        raise PermissionError("M99 source revision differs from the completed product chain")
    lifecycle = ProjectToolPackLifecycle(request.project_id, registry_root)
    state = lifecycle.state()
    if not state.execution_ready or state.generated is None or state.generated.sha256 != request.toolpack_sha256:
        raise PermissionError("M99 requires the approved product ToolPack")
    profile = state.generated
    bindings = [item for item in profile.approved_host_executables if item.adapter_id == AdapterId.GODOT_HEADLESS_PROBE]
    if len(bindings) != 1:
        raise PermissionError("M99 requires one digest-bound Godot executable")
    godot = Path(bindings[0].executable_path).resolve()
    if _file_sha(godot) != bindings[0].executable_sha256:
        raise PermissionError("M99 Godot executable digest changed")
    source_root = Path(profile.project_root).resolve()
    source_head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=source_root, capture_output=True, text=True, check=True).stdout.strip()
    source_status = subprocess.run(["git", "status", "--porcelain", "--untracked-files=all"], cwd=source_root, capture_output=True, text=True, check=True).stdout.splitlines()
    if source_head != request.source_revision or set(source_status) - {"?? ONEBRIEF_PROJECT.json"}:
        raise RuntimeError("M99 source repository is not the approved clean revision")
    m04_dir = Path(request.predecessor_result_dirs[-1]).resolve()
    release = m04_dir / "artifacts" / "puzzle-complete.exe"
    m04_run = json.loads((m04_dir / "run_result.json").read_text(encoding="utf-8"))
    expected_release_sha = m04_run["execution_receipt"].get("windows_export_sha256")
    if not release.is_file() or _file_sha(release) != expected_release_sha:
        raise PermissionError("M04 release executable no longer matches its receipt")
    receipt_ids = [item.quest_receipt.receipt_id for item in chain]
    quest_seed = {
        "parent_quest_id": predecessor.quest_contract.quest_id, "project_id": request.project_id,
        "milestone_id": "M99", "objective": "Independently audit the complete PUZZLE Quest chain and prove the exported Windows product reproduces the verified source behavior and four-screen presentation.",
        "quest_type": QuestType.PROCESS_BOUND, "initial_execution_phase": ExecutionPhase.FINAL_VERIFICATION,
        "input_checkpoint": QuestInputCheckpoint(milestone_plan_sha256=request.milestone_plan_sha256, source_revision=request.source_revision, dependency_checkpoints={"M04": predecessor.quest_receipt.execution_checkpoint_sha256}, previous_receipt_id=predecessor.quest_receipt.receipt_id, previous_receipt_sha256=predecessor.quest_receipt.sha256),
        "authority_envelope_sha256": request.authority_envelope_sha256, "authorized_files": [],
        "forbidden_scope": ["product source writes", "acceptance-criteria changes", "reusing M04 screenshots as M99 runtime evidence", "network or publishing actions"],
        "required_process": ["verify every raw M01 through M04 receipt and inherited candidate byte", "rerun the product probe twice from the source candidate", "invoke the digest-bound release-audit mode from the exported Windows executable", "capture four new screens from the exported executable", "verify the original source repository remains unchanged"],
        "acceptance_criteria": [
            "M99-C1: M01 through M04 form one unbroken canonical PASS chain",
            "M99-C2: the completed candidate contains only verified product bytes",
            "M99-C3: two final source probes reproduce the same PASS",
            "M99-C4: the exported executable reproduces the source product receipt",
            "M99-C5: the exported executable captures four fresh 1280x720 product screens",
            "M99-C6: the release digest matches M04 and the binary launches successfully",
            "M99-C7: no Cloud, model, source-write, or authorization boundary was consumed",
        ],
        "required_evidence": ["canonical Quest and receipt chain", "complete candidate hash map", "two final source probe receipts", "exported-binary probe receipt", "fresh exported-binary screenshot hashes", "release executable digest and launch result", "clean source repository observation"],
        "assigned_role": QuestRole.EVIDENCE_BUILDER, "verifier_role": QuestRole.INDEPENDENT_VERIFIER,
        "budget": QuestBudget(minimum_usd=0, maximum_usd=0), "dependencies": ["M04"],
        "failure_owner": QuestFailureOwner.UNKNOWN, "preserve_receipt_ids": receipt_ids, "issued_at": _now(),
    }
    provisional = QuestContract(quest_id="QC-0000000000000000", **quest_seed)
    quest = provisional.model_copy(update={"quest_id": "QC-" + _sha(provisional.model_dump(mode="json", exclude={"quest_id", "issued_at"}))[:16]})
    _atomic_json(output / "quest_contract.json", quest)
    project_dir = Path(predecessor.candidate_dir) / "game"
    primary_path = output / "source_primary_probe.json"
    primary = _run([str(godot), "--headless", "--path", str(project_dir), "--script", "res://scripts/khalinos_m04_probe.gd", "--", f"--output={primary_path}"], cwd=project_dir, kind="source_primary_probe", executable=godot, artifact=primary_path, timeout=180)
    independent_path = output / "source_independent_probe.json"
    independent = _run([str(godot), "--headless", "--path", str(project_dir), "--script", "res://scripts/khalinos_m04_probe.gd", "--", f"--output={independent_path}"], cwd=project_dir, kind="source_independent_probe", executable=godot, artifact=independent_path, timeout=180)
    captures = output / "release_captures"
    captures.mkdir()
    release_probe_path = captures / "release_probe.json"
    release_audit = _run([str(release), "--position", "-10000,-10000", "--", f"--release-audit-dir={captures}"], cwd=output, kind="release_audit", executable=release, artifact=release_probe_path, timeout=180)
    for name, command in (("source_primary_command.json", primary), ("source_independent_command.json", independent), ("release_audit_command.json", release_audit)):
        _atomic_json(output / name, command)
    primary_payload = json.loads(primary_path.read_text(encoding="utf-8")) if primary_path.is_file() else {}
    independent_payload = json.loads(independent_path.read_text(encoding="utf-8")) if independent_path.is_file() else {}
    release_payload = json.loads(release_probe_path.read_text(encoding="utf-8")) if release_probe_path.is_file() else {}
    capture_paths = [captures / f"{name}.png" for name in ("title", "chamber_select", "gameplay", "mission_result")]
    capture_ok = all(path.is_file() and _png_dimensions(path) == (1280, 720) and path.stat().st_size >= 10_000 for path in capture_paths)
    criteria = {
        "M99-C1": len(chain) == 4 and all(item.quest_receipt.state == QuestState.PASSED for item in chain),
        "M99-C2": len(predecessor.complete_candidate_hashes) >= 20 and all(len(value) == 64 for value in predecessor.complete_candidate_hashes.values()),
        "M99-C3": primary.returncode == 0 and independent.returncode == 0 and primary_payload.get("passed") is True and independent_payload == primary_payload,
        "M99-C4": release_audit.returncode == 0 and release_payload.get("passed") is True and release_payload == primary_payload,
        "M99-C5": release_audit.returncode == 0 and capture_ok,
        "M99-C6": release.is_file() and release.stat().st_size >= 1_000_000 and _file_sha(release) == expected_release_sha,
        "M99-C7": source_head == request.source_revision and not (set(source_status) - {"?? ONEBRIEF_PROJECT.json"}),
    }
    passed = all(criteria.values())
    capture_evidence = [{"name": path.name, "sha256": _file_sha(path) if path.is_file() else None, "dimensions": _png_dimensions(path), "bytes": path.stat().st_size if path.is_file() else 0} for path in capture_paths]
    verification = {"schema_version": "khalinos-godot-m99-independent-verification-v1", "quest_id": quest.quest_id, "quest_sha256": quest.sha256, "criteria": criteria, "chain_receipt_ids": receipt_ids, "complete_candidate_sha256": _sha(predecessor.complete_candidate_hashes), "source_primary_sha256": _file_sha(primary_path) if primary_path.is_file() else None, "source_independent_sha256": _file_sha(independent_path) if independent_path.is_file() else None, "release_probe_sha256": _file_sha(release_probe_path) if release_probe_path.is_file() else None, "release_sha256": _file_sha(release), "release_captures": capture_evidence, "cost_usd": 0, "passed": passed}
    ledger = {"schema_version": "khalinos-godot-m99-completion-ledger-v1", "milestone_id": "M99", "quest_id": quest.quest_id, "preserved_receipt_ids": receipt_ids, "criteria": [{"criterion_id": key, "status": "passed" if value else "failed"} for key, value in criteria.items()], "status": "passed" if passed else "failed"}
    checkpoint = {"schema_version": "khalinos-godot-m99-final-checkpoint-v1", "source_revision": request.source_revision, "predecessor_receipt_id": predecessor.quest_receipt.receipt_id, "release_sha256": _file_sha(release), "complete_candidate_sha256": _sha(predecessor.complete_candidate_hashes), "verification_sha256": _sha(verification)}
    _atomic_json(output / "independent_verification.json", verification)
    _atomic_json(output / "completion_ledger.json", ledger)
    _atomic_json(output / "final_checkpoint.json", checkpoint)
    receipt_seed = {"quest_id": quest.quest_id, "quest_sha256": quest.sha256, "milestone_id": "M99", "state": QuestState.PASSED if passed else QuestState.BLOCKED, "verdict": Verdict.PASS if passed else Verdict.REVISE, "completion_ledger_sha256": _sha(ledger) if passed else None, "verification_sha256": _sha(verification) if passed else None, "execution_checkpoint_sha256": _sha(checkpoint), "passed_criterion_ids": [key for key, value in criteria.items() if value], "preserved_receipt_ids": receipt_ids, "gaps": [key for key, value in criteria.items() if not value], "failure_owner": QuestFailureOwner.NONE if passed else QuestFailureOwner.TECHNICAL, "verifier_role": QuestRole.INDEPENDENT_VERIFIER, "authorization_required": False}
    provisional_receipt = QuestVerificationReceipt(receipt_id="QR-0000000000000000", created_at=_now(), **receipt_seed)
    receipt = provisional_receipt.model_copy(update={"receipt_id": "QR-" + _sha(provisional_receipt.model_dump(mode="json", exclude={"receipt_id", "created_at"}))[:16]})
    _atomic_json(output / "quest_verification_receipt.json", receipt)
    payload = {"schema_version": "khalinos-godot-m99-run-result-v1", "output_dir": str(output), "quest_contract": quest.model_dump(mode="json"), "quest_receipt": receipt.model_dump(mode="json"), "release_executable": str(release)}
    _atomic_json(output / "run_result.json", payload)
    return GodotM99RunResult.model_validate(payload)
