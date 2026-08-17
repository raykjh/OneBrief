"""Receipt-gated M02 execution over the verified Godot M01 candidate."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field

from onebrief.execution_schemas import Verdict
from onebrief.godot_gameplay import (
    GodotGameplayPlan,
    compile_godot_gameplay,
    materialize_godot_gameplay,
)
from onebrief.godot_quest_execution import GodotM01RunResult
from onebrief.quest_orchestration import (
    QuestBudget,
    QuestContract,
    QuestFailureOwner,
    QuestInputCheckpoint,
    QuestRole,
    QuestState,
    QuestType,
    QuestVerificationReceipt,
)
from onebrief.schemas import ExecutionPhase
from onebrief.toolpack_lifecycle import AdapterId, ProjectToolPackLifecycle


def _now() -> str:
    return datetime.now(UTC).isoformat()


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


def _atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = value.model_dump(mode="json", exclude_none=True) if isinstance(value, BaseModel) else value
    temporary = path.with_suffix(path.suffix + f".{uuid4().hex}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


class GodotM02Request(BaseModel):
    schema_version: Literal["khalinos-godot-m02-request-v1"] = "khalinos-godot-m02-request-v1"
    project_id: str = Field(min_length=2, max_length=64)
    source_revision: str = Field(pattern=r"^[a-f0-9]{40}$")
    toolpack_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    milestone_plan_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    authority_envelope_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    previous_result_dir: str
    project_prefix: str = "game"
    plan: GodotGameplayPlan = Field(default_factory=GodotGameplayPlan)


class GodotM02CommandReceipt(BaseModel):
    schema_version: Literal["khalinos-godot-m02-command-receipt-v1"] = "khalinos-godot-m02-command-receipt-v1"
    command_kind: Literal["gameplay_probe", "runtime_capture", "windows_export"]
    adapter_id: Literal[AdapterId.GODOT_HEADLESS_PROBE] = AdapterId.GODOT_HEADLESS_PROBE
    executable_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    argv: list[str] = Field(min_length=4, max_length=18)
    returncode: int
    output_tail: str = Field(max_length=8000)
    artifact_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


class GodotM02ExecutionReceipt(BaseModel):
    schema_version: Literal["khalinos-godot-m02-execution-receipt-v1"] = "khalinos-godot-m02-execution-receipt-v1"
    quest_id: str = Field(pattern=r"^QC-[a-f0-9]{16}$")
    quest_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    predecessor_receipt_id: str = Field(pattern=r"^QR-[a-f0-9]{16}$")
    predecessor_receipt_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_revision: str = Field(pattern=r"^[a-f0-9]{40}$")
    toolpack_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    trusted_compiler_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    plan_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    bundle_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    changed_files: list[str] = Field(min_length=1, max_length=32)
    screenshot_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    windows_export_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    passed: bool


class GodotM02RunResult(BaseModel):
    schema_version: Literal["khalinos-godot-m02-run-result-v1"] = "khalinos-godot-m02-run-result-v1"
    output_dir: str
    candidate_dir: str
    quest_contract: QuestContract
    execution_receipt: GodotM02ExecutionReceipt
    quest_receipt: QuestVerificationReceipt


def _safe_prefix(value: str) -> str:
    raw = value.replace("\\", "/").strip()
    relative = PurePosixPath(raw)
    if not raw or raw.startswith("/") or re.match(r"^[A-Za-z]:", raw) or relative.is_absolute() or ".." in relative.parts:
        raise ValueError("Godot project prefix escapes the approved workspace")
    return relative.as_posix()


def _run(argv: list[str], *, cwd: Path, kind: str, executable_sha256: str, artifact: Path | None, timeout: int) -> GodotM02CommandReceipt:
    try:
        completed = subprocess.run(
            argv, cwd=cwd, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout, shell=False, check=False,
        )
        output = "\n".join(part for part in (completed.stdout, completed.stderr) if part)
        returncode = completed.returncode
    except subprocess.TimeoutExpired as error:
        stdout = error.stdout.decode("utf-8", errors="replace") if isinstance(error.stdout, bytes) else (error.stdout or "")
        stderr = error.stderr.decode("utf-8", errors="replace") if isinstance(error.stderr, bytes) else (error.stderr or "")
        output = "\n".join(part for part in (stdout, stderr, f"KHALINOS_TIMEOUT_SECONDS={timeout}") if part)
        returncode = 124
    return GodotM02CommandReceipt(
        command_kind=kind,
        executable_sha256=executable_sha256,
        argv=argv,
        returncode=returncode,
        output_tail=output[-8000:],
        artifact_sha256=_file_sha(artifact) if artifact and artifact.is_file() else None,
    )


def _png_dimensions(path: Path) -> tuple[int, int] | None:
    if not path.is_file() or path.stat().st_size < 33:
        return None
    data = path.read_bytes()[:24]
    if data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
        return None
    return struct.unpack(">II", data[16:24])


def _load_verified_predecessor(result_dir: Path, prefix: str) -> tuple[GodotM01RunResult, dict[str, object], dict[str, str]]:
    run_path = result_dir / "run_result.json"
    if not run_path.is_file():
        raise FileNotFoundError("M02 requires the raw M01 run result")
    previous = GodotM01RunResult.model_validate_json(run_path.read_text(encoding="utf-8"))
    receipt = previous.quest_receipt
    if receipt.state != QuestState.PASSED or receipt.verdict != Verdict.PASS or not previous.execution_receipt.passed:
        raise PermissionError("M02 predecessor is not a verified PASS")
    if receipt.quest_id != previous.quest_contract.quest_id or receipt.quest_sha256 != previous.quest_contract.sha256:
        raise PermissionError("M01 Quest lineage does not match its receipt")
    receipt_integrity = _sha(receipt.model_dump(mode="json", exclude={"receipt_id", "created_at"}))
    if receipt.receipt_id != "QR-" + receipt_integrity[:16]:
        raise PermissionError("M01 receipt identity is not canonical")
    ledger = json.loads((result_dir / "completion_ledger.json").read_text(encoding="utf-8"))
    verification = json.loads((result_dir / "independent_verification.json").read_text(encoding="utf-8"))
    checkpoint = json.loads((result_dir / "execution_checkpoint.json").read_text(encoding="utf-8"))
    if receipt.completion_ledger_sha256 != _sha(ledger) or receipt.verification_sha256 != _sha(verification) or receipt.execution_checkpoint_sha256 != _sha(checkpoint):
        raise PermissionError("M01 receipt evidence digests do not match the raw evidence")
    candidate = Path(previous.candidate_dir).resolve()
    project = candidate / Path(*PurePosixPath(prefix).parts)
    hashes: dict[str, str] = {}
    for changed in previous.execution_receipt.changed_files:
        normalized = PurePosixPath(changed.replace("\\", "/"))
        parts = normalized.parts
        if not parts or parts[0] != prefix:
            raise PermissionError("M01 changed file escaped the approved project prefix")
        relative = PurePosixPath(*parts[1:]).as_posix()
        source = project / Path(*PurePosixPath(relative).parts)
        if not source.is_file():
            raise FileNotFoundError(f"verified M01 candidate file is missing: {relative}")
        hashes[relative] = _file_sha(source)
    if _sha(dict(sorted(hashes.items()))) != checkpoint.get("candidate_tree_sha256"):
        raise PermissionError("M01 candidate bytes no longer match the verified checkpoint")
    return previous, checkpoint, hashes


def execute_godot_m02(request: GodotM02Request, *, registry_root: Path, output_dir: Path) -> GodotM02RunResult:
    output = output_dir.resolve()
    if output.exists() and any(output.iterdir()):
        raise FileExistsError("Godot M02 output directory must be new or empty")
    output.mkdir(parents=True, exist_ok=True)
    prefix = _safe_prefix(request.project_prefix)
    lifecycle = ProjectToolPackLifecycle(request.project_id, registry_root)
    state = lifecycle.state()
    if not state.execution_ready or state.generated is None or state.approval is None:
        raise PermissionError("Godot M02 requires an approved execution-ready ToolPack")
    profile = state.generated
    if profile.sha256 != request.toolpack_sha256 or profile.repository_head_sha != request.source_revision:
        raise PermissionError("Godot M02 request does not match the approved ToolPack checkpoint")
    if profile.allowed_write_prefixes != [prefix + "/"]:
        raise PermissionError("Godot M02 requires the sole approved game write prefix")
    bindings = [item for item in profile.approved_host_executables if item.adapter_id == AdapterId.GODOT_HEADLESS_PROBE]
    if len(bindings) != 1:
        raise PermissionError("Godot M02 requires one digest-bound executable")
    binding = bindings[0]
    executable = Path(binding.executable_path).resolve()
    if not executable.is_file() or executable.stat().st_size != binding.executable_size or _file_sha(executable) != binding.executable_sha256:
        raise PermissionError("approved Godot executable binding changed")
    compiler_sha = profile.trusted_component_digests.get("godot_gameplay_compiler")
    actual_compiler_sha = _file_sha(Path(__file__).with_name("godot_gameplay.py"))
    if compiler_sha != actual_compiler_sha:
        raise PermissionError("trusted gameplay compiler digest is not approved")

    previous_dir = Path(request.previous_result_dir).resolve()
    previous, previous_checkpoint, previous_hashes = _load_verified_predecessor(previous_dir, prefix)
    if previous.execution_receipt.source_revision != request.source_revision:
        raise PermissionError("M02 source revision differs from the verified M01 lineage")
    source_root = Path(profile.project_root).resolve()
    current_head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=source_root, capture_output=True, text=True, check=True).stdout.strip()
    current_status = subprocess.run(["git", "status", "--porcelain", "--untracked-files=all"], cwd=source_root, capture_output=True, text=True, check=True).stdout.splitlines()
    allowed_sidecar = {"?? ONEBRIEF_PROJECT.json"}
    if current_head != request.source_revision or set(current_status) - allowed_sidecar:
        raise RuntimeError("Godot M02 source repository is not the approved clean revision")

    bundle = compile_godot_gameplay(request.plan)
    authorized_files = sorted(f"{prefix}/{path}" for path in [*bundle.replacements, *bundle.additions])
    quest_seed = {
        "parent_quest_id": previous.quest_contract.quest_id,
        "project_id": request.project_id,
        "milestone_id": "M02",
        "objective": "Increment the verified M01 topology into one coarse 3D turn-based stealth chamber with a 9x9 board, deterministic guard preview, seal recovery, and escape loop.",
        "quest_type": QuestType.PROCESS_BOUND,
        "initial_execution_phase": ExecutionPhase.PRODUCT_IMPLEMENTATION,
        "input_checkpoint": QuestInputCheckpoint(
            milestone_plan_sha256=request.milestone_plan_sha256,
            source_revision=request.source_revision,
            dependency_checkpoints={"M01": previous.quest_receipt.execution_checkpoint_sha256},
            previous_receipt_id=previous.quest_receipt.receipt_id,
            previous_receipt_sha256=previous.quest_receipt.sha256,
        ),
        "authority_envelope_sha256": request.authority_envelope_sha256,
        "authorized_files": authorized_files,
        "forbidden_scope": ["writes outside game/", "server or network integration", "model-authored engine argv", "weakening the verified M01 receipt"],
        "required_process": ["verify raw M01 receipt and candidate bytes", "clone the approved source and restore only verified M01 bytes", "apply the digest-bound trusted gameplay compiler", "run primary and independent headless gameplay probes", "capture a fresh runtime PNG and export a fresh Windows binary"],
        "acceptance_criteria": [
            "M02-C1: a real 9x9 3D board is materialized over the verified M01 gameplay region",
            "M02-C2: one player action consumes one turn and advances a deterministic guard with visible next-position preview",
            "M02-C3: the approved solution recovers the seal and reaches the exit without detection",
            "M02-C4: primary and independent headless probes reproduce the same PASS",
            "M02-C5: a quest-unique runtime screenshot is captured at the approved viewport",
            "M02-C6: a non-empty Windows Desktop executable is exported",
        ],
        "required_evidence": ["raw M01 receipt and checkpoint digests", "trusted M02 plan, compiler, and bundle digests", "exact predecessor and changed-file hashes", "primary and independent gameplay probe receipts", "fresh PNG dimensions and SHA-256", "fresh Windows executable size and SHA-256"],
        "assigned_role": QuestRole.ACCOUNTABLE_MAKER,
        "verifier_role": QuestRole.INDEPENDENT_VERIFIER,
        "budget": QuestBudget(minimum_usd=0, maximum_usd=0),
        "dependencies": ["M01"],
        "failure_owner": QuestFailureOwner.UNKNOWN,
        "preserve_receipt_ids": [previous.quest_receipt.receipt_id],
        "issued_at": _now(),
    }
    provisional = QuestContract(quest_id="QC-0000000000000000", **quest_seed)
    quest = provisional.model_copy(update={"quest_id": "QC-" + _sha(provisional.model_dump(mode="json", exclude={"quest_id", "issued_at"}))[:16]})
    _atomic_json(output / "quest_contract.json", quest)

    candidate = output / "candidate"
    subprocess.run(["git", "clone", "--local", "--no-hardlinks", str(source_root), str(candidate)], cwd=output, capture_output=True, text=True, check=True, shell=False)
    project_dir = candidate / Path(*PurePosixPath(prefix).parts)
    previous_project = Path(previous.candidate_dir).resolve() / Path(*PurePosixPath(prefix).parts)
    for relative, digest in previous_hashes.items():
        source = previous_project / Path(*PurePosixPath(relative).parts)
        target = project_dir / Path(*PurePosixPath(relative).parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        if _file_sha(target) != digest:
            raise RuntimeError("M01 verified bytes changed during isolated restoration")
    materialization = materialize_godot_gameplay(bundle, project_dir, expected_previous_hashes={path: previous_hashes[path] for path in bundle.replacements})
    _atomic_json(output / "materialization_receipt.json", materialization)

    changed = subprocess.run(["git", "status", "--porcelain", "--untracked-files=all"], cwd=candidate, capture_output=True, text=True, check=True).stdout.splitlines()
    changed_files = sorted(line[3:].replace("\\", "/") for line in changed)
    expected_all = sorted(set(previous.execution_receipt.changed_files) | set(authorized_files))
    if changed_files != expected_all:
        raise PermissionError("M02 candidate contains files outside the verified predecessor and authorized bundle")

    artifacts = output / "artifacts"
    artifacts.mkdir()
    primary_path = output / "primary_gameplay_probe.json"
    probe_argv = [str(executable), "--headless", "--path", str(project_dir), "--script", "res://scripts/khalinos_gameplay_probe.gd", "--", f"--output={primary_path}"]
    primary = _run(probe_argv, cwd=project_dir, kind="gameplay_probe", executable_sha256=binding.executable_sha256, artifact=primary_path, timeout=180)
    primary_payload = json.loads(primary_path.read_text(encoding="utf-8")) if primary_path.is_file() else {}
    independent_path = output / "independent_gameplay_probe.json"
    independent_argv = [*probe_argv[:-1], f"--output={independent_path}"]
    independent = _run(independent_argv, cwd=project_dir, kind="gameplay_probe", executable_sha256=binding.executable_sha256, artifact=independent_path, timeout=180)
    independent_payload = json.loads(independent_path.read_text(encoding="utf-8")) if independent_path.is_file() else {}
    screenshot = artifacts / "puzzle-m02-gameplay.png"
    capture_argv = [str(executable), "--path", str(project_dir), "res://scenes/gameplay.tscn", "--position", "-10000,-10000", "--", f"--capture={screenshot}"]
    capture = _run(capture_argv, cwd=project_dir, kind="runtime_capture", executable_sha256=binding.executable_sha256, artifact=screenshot, timeout=180)
    windows_exe = artifacts / "puzzle-m02.exe"
    export_argv = [str(executable), "--headless", "--path", str(project_dir), "--export-release", "Windows Desktop", str(windows_exe)]
    export = _run(export_argv, cwd=project_dir, kind="windows_export", executable_sha256=binding.executable_sha256, artifact=windows_exe, timeout=300)
    for name, receipt in (("primary_command_receipt.json", primary), ("independent_command_receipt.json", independent), ("capture_command_receipt.json", capture), ("export_command_receipt.json", export)):
        _atomic_json(output / name, receipt)

    dimensions = _png_dimensions(screenshot)
    criteria = {
        "M02-C1": primary_payload.get("board") == [9, 9] and all(_file_sha(project_dir / Path(*PurePosixPath(path).parts)) == hashlib.sha256(content.encode("utf-8")).hexdigest() for path, content in {**bundle.replacements, **bundle.additions}.items()),
        "M02-C2": primary_payload.get("turn_advanced") is True and primary_payload.get("guard_preview") is True,
        "M02-C3": primary_payload.get("solution", {}).get("state") == "escaped" and primary_payload.get("solution", {}).get("has_seal") is True,
        "M02-C4": primary.returncode == 0 and independent.returncode == 0 and primary_payload.get("passed") is True and independent_payload == primary_payload,
        "M02-C5": (capture.returncode == 0 and dimensions == (1280, 720) and screenshot.stat().st_size >= 10_000) if screenshot.is_file() else False,
        "M02-C6": export.returncode == 0 and windows_exe.is_file() and windows_exe.stat().st_size >= 1_000_000,
    }
    passed = all(criteria.values())
    verification = {"schema_version": "khalinos-godot-m02-independent-verification-v1", "verifier_role": QuestRole.INDEPENDENT_VERIFIER.value, "quest_id": quest.quest_id, "quest_sha256": quest.sha256, "criteria": criteria, "primary_probe_sha256": _file_sha(primary_path) if primary_path.is_file() else None, "independent_probe_sha256": _file_sha(independent_path) if independent_path.is_file() else None, "screenshot": {"sha256": _file_sha(screenshot) if screenshot.is_file() else None, "dimensions": dimensions, "bytes": screenshot.stat().st_size if screenshot.is_file() else 0}, "windows_export": {"sha256": _file_sha(windows_exe) if windows_exe.is_file() else None, "bytes": windows_exe.stat().st_size if windows_exe.is_file() else 0}, "passed": passed}
    ledger = {"schema_version": "khalinos-godot-m02-completion-ledger-v1", "milestone_id": "M02", "quest_id": quest.quest_id, "preserved_receipt_ids": [previous.quest_receipt.receipt_id], "criteria": [{"criterion_id": key, "status": "passed" if value else "failed"} for key, value in criteria.items()], "status": "passed" if passed else "failed"}
    checkpoint = {"schema_version": "khalinos-godot-m02-execution-checkpoint-v1", "source_revision": request.source_revision, "predecessor_receipt_id": previous.quest_receipt.receipt_id, "predecessor_checkpoint_sha256": _sha(previous_checkpoint), "toolpack_sha256": request.toolpack_sha256, "trusted_compiler_sha256": compiler_sha, "plan_sha256": bundle.plan_sha256, "bundle_sha256": bundle.bundle_sha256, "candidate_delta_sha256": _sha({path: _file_sha(project_dir / Path(*PurePosixPath(path).parts)) for path in [*bundle.replacements, *bundle.additions]})}
    _atomic_json(output / "independent_verification.json", verification)
    _atomic_json(output / "completion_ledger.json", ledger)
    _atomic_json(output / "execution_checkpoint.json", checkpoint)
    execution = GodotM02ExecutionReceipt(quest_id=quest.quest_id, quest_sha256=quest.sha256, predecessor_receipt_id=previous.quest_receipt.receipt_id, predecessor_receipt_sha256=previous.quest_receipt.sha256, source_revision=request.source_revision, toolpack_sha256=request.toolpack_sha256, trusted_compiler_sha256=compiler_sha, plan_sha256=bundle.plan_sha256, bundle_sha256=bundle.bundle_sha256, changed_files=authorized_files, screenshot_sha256=_file_sha(screenshot) if screenshot.is_file() else None, windows_export_sha256=_file_sha(windows_exe) if windows_exe.is_file() else None, passed=passed)
    _atomic_json(output / "execution_receipt.json", execution)
    receipt_seed = {"quest_id": quest.quest_id, "quest_sha256": quest.sha256, "milestone_id": "M02", "state": QuestState.PASSED if passed else QuestState.BLOCKED, "verdict": Verdict.PASS if passed else Verdict.FAIL, "completion_ledger_sha256": _sha(ledger) if passed else None, "verification_sha256": _sha(verification) if passed else None, "execution_checkpoint_sha256": _sha(checkpoint), "passed_criterion_ids": [key for key, value in criteria.items() if value], "preserved_receipt_ids": [previous.quest_receipt.receipt_id], "gaps": [key for key, value in criteria.items() if not value], "failure_owner": QuestFailureOwner.NONE if passed else QuestFailureOwner.TECHNICAL, "verifier_role": QuestRole.INDEPENDENT_VERIFIER, "authorization_required": False}
    provisional_receipt = QuestVerificationReceipt(receipt_id="QR-0000000000000000", created_at=_now(), **receipt_seed)
    quest_receipt = provisional_receipt.model_copy(update={"receipt_id": "QR-" + _sha(provisional_receipt.model_dump(mode="json", exclude={"receipt_id", "created_at"}))[:16]})
    _atomic_json(output / "quest_verification_receipt.json", quest_receipt)
    result_payload = {"schema_version": "khalinos-godot-m02-run-result-v1", "output_dir": str(output), "candidate_dir": str(candidate), "quest_contract": quest.model_dump(mode="json"), "execution_receipt": execution.model_dump(mode="json"), "quest_receipt": quest_receipt.model_dump(mode="json")}
    _atomic_json(output / "run_result.json", result_payload)
    return GodotM02RunResult.model_validate(result_payload)
