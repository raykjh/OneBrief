"""Execute a common receipt-gated Godot successor for the PUZZLE M03 shaping layer."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field

from onebrief.execution_schemas import Verdict
from onebrief.godot_gameplay import materialize_godot_gameplay
from onebrief.godot_gameplay_execution import _png_dimensions
from onebrief.godot_gameplay_shaping import (
    GodotGameplayShapingPlan,
    compile_godot_gameplay_shaping,
)
from onebrief.godot_quest_chain import verify_godot_quest_chain
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


class GodotM03Request(BaseModel):
    schema_version: Literal["khalinos-godot-m03-request-v1"] = "khalinos-godot-m03-request-v1"
    project_id: str = Field(min_length=2, max_length=64)
    source_revision: str = Field(pattern=r"^[a-f0-9]{40}$")
    toolpack_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    milestone_plan_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    authority_envelope_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    predecessor_result_dirs: list[str] = Field(min_length=2, max_length=14)
    project_prefix: Literal["game"] = "game"
    plan: GodotGameplayShapingPlan = Field(default_factory=GodotGameplayShapingPlan)


class GodotM03CommandReceipt(BaseModel):
    schema_version: Literal["khalinos-godot-m03-command-receipt-v1"] = "khalinos-godot-m03-command-receipt-v1"
    command_kind: Literal["shaping_probe", "runtime_capture", "windows_export"]
    adapter_id: Literal[AdapterId.GODOT_HEADLESS_PROBE] = AdapterId.GODOT_HEADLESS_PROBE
    executable_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    argv: list[str] = Field(min_length=4, max_length=18)
    returncode: int
    output_tail: str = Field(max_length=8000)
    artifact_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


class GodotM03ExecutionReceipt(BaseModel):
    schema_version: Literal["khalinos-godot-m03-execution-receipt-v1"] = "khalinos-godot-m03-execution-receipt-v1"
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


class GodotM03RunResult(BaseModel):
    schema_version: Literal["khalinos-godot-m03-run-result-v1"] = "khalinos-godot-m03-run-result-v1"
    output_dir: str
    candidate_dir: str
    quest_contract: QuestContract
    execution_receipt: GodotM03ExecutionReceipt
    quest_receipt: QuestVerificationReceipt


def _run(argv: list[str], *, cwd: Path, kind: str, executable_sha256: str, artifact: Path | None, timeout: int) -> GodotM03CommandReceipt:
    try:
        completed = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, shell=False, check=False)
        output = "\n".join(part for part in (completed.stdout, completed.stderr) if part)
        returncode = completed.returncode
    except subprocess.TimeoutExpired as error:
        stdout = error.stdout.decode("utf-8", errors="replace") if isinstance(error.stdout, bytes) else (error.stdout or "")
        stderr = error.stderr.decode("utf-8", errors="replace") if isinstance(error.stderr, bytes) else (error.stderr or "")
        output = "\n".join(part for part in (stdout, stderr, f"KHALINOS_TIMEOUT_SECONDS={timeout}") if part)
        returncode = 124
    return GodotM03CommandReceipt(command_kind=kind, executable_sha256=executable_sha256, argv=argv, returncode=returncode, output_tail=output[-8000:], artifact_sha256=_file_sha(artifact) if artifact and artifact.is_file() else None)


def execute_godot_m03(request: GodotM03Request, *, registry_root: Path, output_dir: Path) -> GodotM03RunResult:
    output = output_dir.resolve()
    if output.exists() and any(output.iterdir()):
        raise FileExistsError("Godot M03 output directory must be new or empty")
    output.mkdir(parents=True, exist_ok=True)
    chain = verify_godot_quest_chain([Path(item) for item in request.predecessor_result_dirs], project_prefix=request.project_prefix)
    predecessor = chain[-1]
    if predecessor.milestone_id != "M02" or predecessor.source_revision != request.source_revision:
        raise PermissionError("M03 requires the exact verified M02 checkpoint")

    lifecycle = ProjectToolPackLifecycle(request.project_id, registry_root)
    state = lifecycle.state()
    if not state.execution_ready or state.generated is None:
        raise PermissionError("Godot M03 requires an approved execution-ready ToolPack")
    profile = state.generated
    if profile.sha256 != request.toolpack_sha256 or profile.repository_head_sha != request.source_revision or profile.allowed_write_prefixes != ["game/"]:
        raise PermissionError("Godot M03 request differs from the approved ToolPack checkpoint")
    bindings = [item for item in profile.approved_host_executables if item.adapter_id == AdapterId.GODOT_HEADLESS_PROBE]
    if len(bindings) != 1:
        raise PermissionError("Godot M03 requires one digest-bound executable")
    binding = bindings[0]
    executable = Path(binding.executable_path).resolve()
    if not executable.is_file() or executable.stat().st_size != binding.executable_size or _file_sha(executable) != binding.executable_sha256:
        raise PermissionError("approved Godot executable binding changed")
    compiler_sha = profile.trusted_component_digests.get("godot_gameplay_shaping_compiler")
    if compiler_sha != _file_sha(Path(__file__).with_name("godot_gameplay_shaping.py")):
        raise PermissionError("trusted M03 shaping compiler digest is not approved")
    source_root = Path(profile.project_root).resolve()
    current_head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=source_root, capture_output=True, text=True, check=True).stdout.strip()
    status = subprocess.run(["git", "status", "--porcelain", "--untracked-files=all"], cwd=source_root, capture_output=True, text=True, check=True).stdout.splitlines()
    if current_head != request.source_revision or set(status) - {"?? ONEBRIEF_PROJECT.json"}:
        raise RuntimeError("Godot M03 source repository is not the approved clean revision")

    bundle = compile_godot_gameplay_shaping(request.plan)
    authorized_files = sorted(f"game/{path}" for path in [*bundle.replacements, *bundle.additions])
    receipt_ids = [item.quest_receipt.receipt_id for item in chain]
    quest_seed = {
        "parent_quest_id": predecessor.quest_contract.quest_id,
        "project_id": request.project_id,
        "milestone_id": "M03",
        "objective": "Shape the verified M02 chamber into a rule-complete stealth puzzle layer with guard vision, obstacles, key and door, trap and pressure plate, Undo, Restart, and one-use Red Thread recovery.",
        "quest_type": QuestType.PROCESS_BOUND,
        "initial_execution_phase": ExecutionPhase.PRODUCT_IMPLEMENTATION,
        "input_checkpoint": QuestInputCheckpoint(milestone_plan_sha256=request.milestone_plan_sha256, source_revision=request.source_revision, dependency_checkpoints={"M02": predecessor.quest_receipt.execution_checkpoint_sha256}, previous_receipt_id=predecessor.quest_receipt.receipt_id, previous_receipt_sha256=predecessor.quest_receipt.sha256),
        "authority_envelope_sha256": request.authority_envelope_sha256,
        "authorized_files": authorized_files,
        "forbidden_scope": ["writes outside game/", "server or network integration", "model-authored engine argv", "weakening or rewriting M01/M02 receipts"],
        "required_process": ["verify the complete raw M01 to M02 receipt chain", "restore every inherited candidate byte into an isolated clone", "replace only the exact verified M02 gameplay runtime", "run primary and independent rule probes", "capture a fresh feature-visible runtime PNG and export a fresh Windows binary"],
        "acceptance_criteria": [
            "M03-C1: obstacles, key, door, trap, and pressure plate are present in the 9x9 chamber",
            "M03-C2: deterministic guard vision cells and next position are exposed before action",
            "M03-C3: key opens the door, an armed trap fails, and the pressure plate disarms it",
            "M03-C4: Undo, Restart, and one-use Red Thread restore their exact contracted states",
            "M03-C5: the approved route still recovers the seal and escapes",
            "M03-C6: primary and independent probes reproduce the same PASS",
            "M03-C7: a fresh 1280x720 runtime screenshot shows the shaped chamber",
            "M03-C8: a fresh non-empty Windows Desktop executable is exported",
        ],
        "required_evidence": ["verified M01 to M02 raw receipt chain", "trusted M03 plan, compiler, and bundle digests", "exact inherited and delta file hashes", "primary and independent rule receipts", "fresh PNG dimensions and SHA-256", "fresh Windows executable size and SHA-256"],
        "assigned_role": QuestRole.ACCOUNTABLE_MAKER, "verifier_role": QuestRole.INDEPENDENT_VERIFIER,
        "budget": QuestBudget(minimum_usd=0, maximum_usd=0), "dependencies": ["M02"],
        "failure_owner": QuestFailureOwner.UNKNOWN, "preserve_receipt_ids": receipt_ids, "issued_at": _now(),
    }
    provisional = QuestContract(quest_id="QC-0000000000000000", **quest_seed)
    quest = provisional.model_copy(update={"quest_id": "QC-" + _sha(provisional.model_dump(mode="json", exclude={"quest_id", "issued_at"}))[:16]})
    _atomic_json(output / "quest_contract.json", quest)

    candidate = output / "candidate"
    subprocess.run(["git", "clone", "--local", "--no-hardlinks", str(source_root), str(candidate)], cwd=output, capture_output=True, text=True, check=True, shell=False)
    previous_candidate = Path(predecessor.candidate_dir)
    for relative, digest in predecessor.complete_candidate_hashes.items():
        source = previous_candidate / Path(*PurePosixPath(relative).parts)
        target = candidate / Path(*PurePosixPath(relative).parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        if _file_sha(target) != digest:
            raise RuntimeError("verified predecessor byte changed during M03 restoration")
    project_dir = candidate / "game"
    materialization = materialize_godot_gameplay(bundle, project_dir, expected_previous_hashes={path: predecessor.complete_candidate_hashes[f"game/{path}"] for path in bundle.replacements})
    _atomic_json(output / "materialization_receipt.json", materialization)
    observed = sorted(line[3:].replace("\\", "/") for line in subprocess.run(["git", "status", "--porcelain", "--untracked-files=all"], cwd=candidate, capture_output=True, text=True, check=True).stdout.splitlines())
    if observed != sorted(set(predecessor.complete_candidate_hashes) | set(authorized_files)):
        raise PermissionError("M03 candidate contains files outside its verified chain and authorized delta")

    artifacts = output / "artifacts"
    artifacts.mkdir()
    primary_path = output / "primary_m03_probe.json"
    probe_argv = [str(executable), "--headless", "--path", str(project_dir), "--script", "res://scripts/khalinos_m03_probe.gd", "--", f"--output={primary_path}"]
    primary = _run(probe_argv, cwd=project_dir, kind="shaping_probe", executable_sha256=binding.executable_sha256, artifact=primary_path, timeout=180)
    primary_payload = json.loads(primary_path.read_text(encoding="utf-8")) if primary_path.is_file() else {}
    independent_path = output / "independent_m03_probe.json"
    independent_argv = [*probe_argv[:-1], f"--output={independent_path}"]
    independent = _run(independent_argv, cwd=project_dir, kind="shaping_probe", executable_sha256=binding.executable_sha256, artifact=independent_path, timeout=180)
    independent_payload = json.loads(independent_path.read_text(encoding="utf-8")) if independent_path.is_file() else {}
    screenshot = artifacts / "puzzle-m03-shaping.png"
    capture = _run([str(executable), "--path", str(project_dir), "res://scenes/gameplay.tscn", "--position", "-10000,-10000", "--", f"--capture={screenshot}"], cwd=project_dir, kind="runtime_capture", executable_sha256=binding.executable_sha256, artifact=screenshot, timeout=180)
    windows_exe = artifacts / "puzzle-m03.exe"
    export = _run([str(executable), "--headless", "--path", str(project_dir), "--export-release", "Windows Desktop", str(windows_exe)], cwd=project_dir, kind="windows_export", executable_sha256=binding.executable_sha256, artifact=windows_exe, timeout=300)
    for name, command in (("primary_command_receipt.json", primary), ("independent_command_receipt.json", independent), ("capture_command_receipt.json", capture), ("export_command_receipt.json", export)):
        _atomic_json(output / name, command)

    exact_delta = all(_file_sha(project_dir / Path(*PurePosixPath(path).parts)) == hashlib.sha256(content.encode("utf-8")).hexdigest() for path, content in {**bundle.replacements, **bundle.additions}.items())
    rule_checks = primary_payload.get("rule_checks", {})
    recovery = primary_payload.get("recovery", {})
    solution = primary_payload.get("solution", {})
    dimensions = _png_dimensions(screenshot)
    criteria = {
        "M03-C1": exact_delta and primary_payload.get("initial", {}).get("feature_counts") == {"obstacles": 3, "door": 1, "key": 1, "trap": 1, "pressure_plate": 1},
        "M03-C2": bool(primary_payload.get("initial", {}).get("vision")) and primary_payload.get("initial", {}).get("guard_next") is not None,
        "M03-C3": rule_checks == {"trap_lethal": True, "key_door": True, "pressure_safe": True},
        "M03-C4": recovery == {"undo": True, "restart": True, "red_thread": True},
        "M03-C5": solution.get("state") == "escaped" and solution.get("has_seal") is True,
        "M03-C6": primary.returncode == 0 and independent.returncode == 0 and primary_payload.get("passed") is True and independent_payload == primary_payload,
        "M03-C7": (capture.returncode == 0 and dimensions == (1280, 720) and screenshot.stat().st_size >= 10_000) if screenshot.is_file() else False,
        "M03-C8": export.returncode == 0 and windows_exe.is_file() and windows_exe.stat().st_size >= 1_000_000,
    }
    passed = all(criteria.values())
    verification = {"schema_version": "khalinos-godot-m03-independent-verification-v1", "verifier_role": QuestRole.INDEPENDENT_VERIFIER.value, "quest_id": quest.quest_id, "quest_sha256": quest.sha256, "criteria": criteria, "primary_probe_sha256": _file_sha(primary_path) if primary_path.is_file() else None, "independent_probe_sha256": _file_sha(independent_path) if independent_path.is_file() else None, "screenshot": {"sha256": _file_sha(screenshot) if screenshot.is_file() else None, "dimensions": dimensions, "bytes": screenshot.stat().st_size if screenshot.is_file() else 0}, "windows_export": {"sha256": _file_sha(windows_exe) if windows_exe.is_file() else None, "bytes": windows_exe.stat().st_size if windows_exe.is_file() else 0}, "passed": passed}
    ledger = {"schema_version": "khalinos-godot-m03-completion-ledger-v1", "milestone_id": "M03", "quest_id": quest.quest_id, "preserved_receipt_ids": receipt_ids, "criteria": [{"criterion_id": key, "status": "passed" if value else "failed"} for key, value in criteria.items()], "status": "passed" if passed else "failed"}
    checkpoint = {"schema_version": "khalinos-godot-m03-execution-checkpoint-v1", "source_revision": request.source_revision, "predecessor_receipt_id": predecessor.quest_receipt.receipt_id, "predecessor_checkpoint_sha256": _sha(predecessor.execution_checkpoint), "toolpack_sha256": request.toolpack_sha256, "trusted_compiler_sha256": compiler_sha, "plan_sha256": bundle.plan_sha256, "bundle_sha256": bundle.bundle_sha256, "candidate_delta_sha256": _sha({path: _file_sha(project_dir / Path(*PurePosixPath(path).parts)) for path in [*bundle.replacements, *bundle.additions]})}
    _atomic_json(output / "independent_verification.json", verification)
    _atomic_json(output / "completion_ledger.json", ledger)
    _atomic_json(output / "execution_checkpoint.json", checkpoint)
    execution = GodotM03ExecutionReceipt(quest_id=quest.quest_id, quest_sha256=quest.sha256, predecessor_receipt_id=predecessor.quest_receipt.receipt_id, predecessor_receipt_sha256=predecessor.quest_receipt.sha256, source_revision=request.source_revision, toolpack_sha256=request.toolpack_sha256, trusted_compiler_sha256=compiler_sha, plan_sha256=bundle.plan_sha256, bundle_sha256=bundle.bundle_sha256, changed_files=authorized_files, screenshot_sha256=_file_sha(screenshot) if screenshot.is_file() else None, windows_export_sha256=_file_sha(windows_exe) if windows_exe.is_file() else None, passed=passed)
    _atomic_json(output / "execution_receipt.json", execution)
    receipt_seed = {"quest_id": quest.quest_id, "quest_sha256": quest.sha256, "milestone_id": "M03", "state": QuestState.PASSED if passed else QuestState.BLOCKED, "verdict": Verdict.PASS if passed else Verdict.FAIL, "completion_ledger_sha256": _sha(ledger) if passed else None, "verification_sha256": _sha(verification) if passed else None, "execution_checkpoint_sha256": _sha(checkpoint), "passed_criterion_ids": [key for key, value in criteria.items() if value], "preserved_receipt_ids": receipt_ids, "gaps": [key for key, value in criteria.items() if not value], "failure_owner": QuestFailureOwner.NONE if passed else QuestFailureOwner.TECHNICAL, "verifier_role": QuestRole.INDEPENDENT_VERIFIER, "authorization_required": False}
    provisional_receipt = QuestVerificationReceipt(receipt_id="QR-0000000000000000", created_at=_now(), **receipt_seed)
    quest_receipt = provisional_receipt.model_copy(update={"receipt_id": "QR-" + _sha(provisional_receipt.model_dump(mode="json", exclude={"receipt_id", "created_at"}))[:16]})
    _atomic_json(output / "quest_verification_receipt.json", quest_receipt)
    payload = {"schema_version": "khalinos-godot-m03-run-result-v1", "output_dir": str(output), "candidate_dir": str(candidate), "quest_contract": quest.model_dump(mode="json"), "execution_receipt": execution.model_dump(mode="json"), "quest_receipt": quest_receipt.model_dump(mode="json")}
    _atomic_json(output / "run_result.json", payload)
    return GodotM03RunResult.model_validate(payload)
