"""Reusable receipt-gated execution engine for Godot shaping successors."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Callable
from uuid import uuid4

from pydantic import BaseModel, Field

from onebrief.execution_schemas import Verdict
from onebrief.godot_gameplay import CompiledGodotGameplay, materialize_godot_gameplay
from onebrief.godot_gameplay_execution import _png_dimensions
from onebrief.godot_quest_chain import VerifiedGodotQuestCheckpoint, verify_godot_quest_chain
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


class GodotSuccessorAuthority(BaseModel):
    project_id: str = Field(min_length=2, max_length=64)
    source_revision: str = Field(pattern=r"^[a-f0-9]{40}$")
    toolpack_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    milestone_plan_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    authority_envelope_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    predecessor_result_dirs: list[str] = Field(min_length=1, max_length=14)
    project_prefix: str = "game"


class GodotSuccessorCommandReceipt(BaseModel):
    schema_version: str = "khalinos-godot-successor-command-receipt-v1"
    command_kind: str
    adapter_id: str = AdapterId.GODOT_HEADLESS_PROBE.value
    executable_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    argv: list[str] = Field(min_length=4, max_length=20)
    returncode: int
    output_tail: str = Field(max_length=8000)
    artifact_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


class GodotSuccessorExecutionReceipt(BaseModel):
    schema_version: str = "khalinos-godot-successor-execution-receipt-v1"
    quest_id: str = Field(pattern=r"^QC-[a-f0-9]{16}$")
    quest_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    predecessor_receipt_id: str = Field(pattern=r"^QR-[a-f0-9]{16}$")
    predecessor_receipt_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_revision: str = Field(pattern=r"^[a-f0-9]{40}$")
    toolpack_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    trusted_compiler_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    plan_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    bundle_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    changed_files: list[str] = Field(min_length=1, max_length=64)
    screenshot_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    windows_export_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    passed: bool


class GodotSuccessorRunResult(BaseModel):
    schema_version: str = "khalinos-godot-successor-run-result-v1"
    output_dir: str
    candidate_dir: str
    quest_contract: QuestContract
    execution_receipt: GodotSuccessorExecutionReceipt
    quest_receipt: QuestVerificationReceipt


@dataclass(frozen=True)
class GodotSuccessorSpec:
    milestone_id: str
    parent_milestone_id: str
    objective: str
    compiler_digest_key: str
    compiler_filename: str
    probe_script: str
    acceptance_criteria: list[str]
    required_process: list[str]
    required_evidence: list[str]
    screenshot_names: list[str]
    export_name: str
    capture_scene: str | None = None
    capture_script: str | None = None


Evaluation = Callable[[dict[str, object], bool, list[Path], Path], dict[str, bool]]


def _run(argv: list[str], *, cwd: Path, kind: str, executable_sha256: str, artifact: Path | None, timeout: int) -> GodotSuccessorCommandReceipt:
    try:
        completed = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, shell=False, check=False)
        output = "\n".join(part for part in (completed.stdout, completed.stderr) if part)
        returncode = completed.returncode
    except subprocess.TimeoutExpired as error:
        stdout = error.stdout.decode("utf-8", errors="replace") if isinstance(error.stdout, bytes) else (error.stdout or "")
        stderr = error.stderr.decode("utf-8", errors="replace") if isinstance(error.stderr, bytes) else (error.stderr or "")
        output = "\n".join(part for part in (stdout, stderr, f"KHALINOS_TIMEOUT_SECONDS={timeout}") if part)
        returncode = 124
    return GodotSuccessorCommandReceipt(command_kind=kind, executable_sha256=executable_sha256, argv=argv, returncode=returncode, output_tail=output[-8000:], artifact_sha256=_file_sha(artifact) if artifact and artifact.is_file() else None)


def execute_godot_successor(
    authority: GodotSuccessorAuthority,
    *,
    registry_root: Path,
    output_dir: Path,
    bundle: CompiledGodotGameplay,
    spec: GodotSuccessorSpec,
    evaluate: Evaluation,
) -> GodotSuccessorRunResult:
    output = output_dir.resolve()
    if output.exists() and any(output.iterdir()):
        raise FileExistsError("Godot successor output directory must be new or empty")
    output.mkdir(parents=True, exist_ok=True)
    chain = verify_godot_quest_chain([Path(item) for item in authority.predecessor_result_dirs], project_prefix=authority.project_prefix)
    predecessor: VerifiedGodotQuestCheckpoint = chain[-1]
    if predecessor.milestone_id != spec.parent_milestone_id or predecessor.source_revision != authority.source_revision:
        raise PermissionError("Godot successor requires its exact verified parent milestone")
    lifecycle = ProjectToolPackLifecycle(authority.project_id, registry_root)
    state = lifecycle.state()
    if not state.execution_ready or state.generated is None:
        raise PermissionError("Godot successor requires an approved execution-ready ToolPack")
    profile = state.generated
    prefix = authority.project_prefix.strip("/")
    if profile.sha256 != authority.toolpack_sha256 or profile.repository_head_sha != authority.source_revision or profile.allowed_write_prefixes != [prefix + "/"]:
        raise PermissionError("Godot successor differs from the approved ToolPack checkpoint")
    bindings = [item for item in profile.approved_host_executables if item.adapter_id == AdapterId.GODOT_HEADLESS_PROBE]
    if len(bindings) != 1:
        raise PermissionError("Godot successor requires one digest-bound executable")
    binding = bindings[0]
    executable = Path(binding.executable_path).resolve()
    if not executable.is_file() or executable.stat().st_size != binding.executable_size or _file_sha(executable) != binding.executable_sha256:
        raise PermissionError("approved Godot executable binding changed")
    compiler_sha = profile.trusted_component_digests.get(spec.compiler_digest_key)
    if compiler_sha != _file_sha(Path(__file__).with_name(spec.compiler_filename)):
        raise PermissionError("trusted successor compiler digest is not approved")
    source_root = Path(profile.project_root).resolve()
    current_head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=source_root, capture_output=True, text=True, check=True).stdout.strip()
    source_status = subprocess.run(["git", "status", "--porcelain", "--untracked-files=all"], cwd=source_root, capture_output=True, text=True, check=True).stdout.splitlines()
    if current_head != authority.source_revision or set(source_status) - {"?? ONEBRIEF_PROJECT.json"}:
        raise RuntimeError("Godot successor source repository is not the approved clean revision")
    authorized_files = sorted(f"{prefix}/{path}" for path in [*bundle.replacements, *bundle.additions])
    receipt_ids = [item.quest_receipt.receipt_id for item in chain]
    quest_seed = {
        "parent_quest_id": predecessor.quest_contract.quest_id,
        "project_id": authority.project_id, "milestone_id": spec.milestone_id,
        "objective": spec.objective, "quest_type": QuestType.PROCESS_BOUND,
        "initial_execution_phase": ExecutionPhase.PRODUCT_IMPLEMENTATION,
        "input_checkpoint": QuestInputCheckpoint(milestone_plan_sha256=authority.milestone_plan_sha256, source_revision=authority.source_revision, dependency_checkpoints={spec.parent_milestone_id: predecessor.quest_receipt.execution_checkpoint_sha256}, previous_receipt_id=predecessor.quest_receipt.receipt_id, previous_receipt_sha256=predecessor.quest_receipt.sha256),
        "authority_envelope_sha256": authority.authority_envelope_sha256,
        "authorized_files": authorized_files,
        "forbidden_scope": [f"writes outside {prefix}/", "server or network integration", "model-authored engine argv", "weakening or rewriting preserved Quest receipts"],
        "required_process": spec.required_process, "acceptance_criteria": spec.acceptance_criteria,
        "required_evidence": spec.required_evidence,
        "assigned_role": QuestRole.ACCOUNTABLE_MAKER, "verifier_role": QuestRole.INDEPENDENT_VERIFIER,
        "budget": QuestBudget(minimum_usd=0, maximum_usd=0), "dependencies": [spec.parent_milestone_id],
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
            raise RuntimeError("verified predecessor byte changed during successor restoration")
    project_dir = candidate / prefix
    materialization = materialize_godot_gameplay(bundle, project_dir, expected_previous_hashes={path: predecessor.complete_candidate_hashes[f"{prefix}/{path}"] for path in bundle.replacements})
    _atomic_json(output / "materialization_receipt.json", materialization)
    observed = sorted(line[3:].replace("\\", "/") for line in subprocess.run(["git", "status", "--porcelain", "--untracked-files=all"], cwd=candidate, capture_output=True, text=True, check=True).stdout.splitlines())
    if observed != sorted(set(predecessor.complete_candidate_hashes) | set(authorized_files)):
        raise PermissionError("Godot successor candidate contains files outside its verified chain and authorized delta")
    artifacts = output / "artifacts"
    artifacts.mkdir()
    primary_path = output / "primary_probe.json"
    probe_argv = [str(executable), "--headless", "--path", str(project_dir), "--script", spec.probe_script, "--", f"--output={primary_path}"]
    primary = _run(probe_argv, cwd=project_dir, kind="successor_probe", executable_sha256=binding.executable_sha256, artifact=primary_path, timeout=180)
    primary_payload = json.loads(primary_path.read_text(encoding="utf-8")) if primary_path.is_file() else {}
    independent_path = output / "independent_probe.json"
    independent_argv = [*probe_argv[:-1], f"--output={independent_path}"]
    independent = _run(independent_argv, cwd=project_dir, kind="successor_probe", executable_sha256=binding.executable_sha256, artifact=independent_path, timeout=180)
    independent_payload = json.loads(independent_path.read_text(encoding="utf-8")) if independent_path.is_file() else {}
    screenshots = [artifacts / name for name in spec.screenshot_names]
    if spec.capture_script:
        capture_argv = [str(executable), "--path", str(project_dir), "--script", spec.capture_script, "--position", "-10000,-10000", "--", f"--capture-dir={artifacts}"]
    elif spec.capture_scene and len(screenshots) == 1:
        capture_argv = [str(executable), "--path", str(project_dir), spec.capture_scene, "--position", "-10000,-10000", "--", f"--capture={screenshots[0]}"]
    else:
        raise ValueError("successor capture contract is incomplete")
    capture = _run(capture_argv, cwd=project_dir, kind="runtime_capture", executable_sha256=binding.executable_sha256, artifact=screenshots[0] if len(screenshots) == 1 else None, timeout=180)
    windows_exe = artifacts / spec.export_name
    export = _run([str(executable), "--headless", "--path", str(project_dir), "--export-release", "Windows Desktop", str(windows_exe)], cwd=project_dir, kind="windows_export", executable_sha256=binding.executable_sha256, artifact=windows_exe, timeout=300)
    for name, command in (("primary_command_receipt.json", primary), ("independent_command_receipt.json", independent), ("capture_command_receipt.json", capture), ("export_command_receipt.json", export)):
        _atomic_json(output / name, command)
    exact_delta = all(_file_sha(project_dir / Path(*PurePosixPath(path).parts)) == hashlib.sha256(content.encode("utf-8")).hexdigest() for path, content in {**bundle.replacements, **bundle.additions}.items())
    criteria = evaluate(primary_payload, exact_delta, screenshots, windows_exe)
    expected_ids = [item.split(":", 1)[0] for item in spec.acceptance_criteria]
    if list(criteria) != expected_ids:
        raise ValueError("successor evaluator criteria do not match the issued contract")
    criteria[expected_ids[-2]] = criteria[expected_ids[-2]] and primary.returncode == 0 and independent.returncode == 0 and primary_payload.get("passed") is True and independent_payload == primary_payload and capture.returncode == 0
    criteria[expected_ids[-1]] = criteria[expected_ids[-1]] and export.returncode == 0
    passed = all(criteria.values())
    screenshot_evidence = [{"name": path.name, "sha256": _file_sha(path) if path.is_file() else None, "dimensions": _png_dimensions(path), "bytes": path.stat().st_size if path.is_file() else 0} for path in screenshots]
    verification = {"schema_version": "khalinos-godot-successor-independent-verification-v1", "verifier_role": QuestRole.INDEPENDENT_VERIFIER.value, "quest_id": quest.quest_id, "quest_sha256": quest.sha256, "criteria": criteria, "primary_probe_sha256": _file_sha(primary_path) if primary_path.is_file() else None, "independent_probe_sha256": _file_sha(independent_path) if independent_path.is_file() else None, "screenshots": screenshot_evidence, "windows_export": {"sha256": _file_sha(windows_exe) if windows_exe.is_file() else None, "bytes": windows_exe.stat().st_size if windows_exe.is_file() else 0}, "passed": passed}
    ledger = {"schema_version": "khalinos-godot-successor-completion-ledger-v1", "milestone_id": spec.milestone_id, "quest_id": quest.quest_id, "preserved_receipt_ids": receipt_ids, "criteria": [{"criterion_id": key, "status": "passed" if value else "failed"} for key, value in criteria.items()], "status": "passed" if passed else "failed"}
    checkpoint = {"schema_version": "khalinos-godot-successor-execution-checkpoint-v1", "source_revision": authority.source_revision, "predecessor_receipt_id": predecessor.quest_receipt.receipt_id, "predecessor_checkpoint_sha256": _sha(predecessor.execution_checkpoint), "toolpack_sha256": authority.toolpack_sha256, "trusted_compiler_sha256": compiler_sha, "plan_sha256": bundle.plan_sha256, "bundle_sha256": bundle.bundle_sha256, "candidate_delta_sha256": _sha({path: _file_sha(project_dir / Path(*PurePosixPath(path).parts)) for path in [*bundle.replacements, *bundle.additions]})}
    _atomic_json(output / "independent_verification.json", verification)
    _atomic_json(output / "completion_ledger.json", ledger)
    _atomic_json(output / "execution_checkpoint.json", checkpoint)
    execution = GodotSuccessorExecutionReceipt(quest_id=quest.quest_id, quest_sha256=quest.sha256, predecessor_receipt_id=predecessor.quest_receipt.receipt_id, predecessor_receipt_sha256=predecessor.quest_receipt.sha256, source_revision=authority.source_revision, toolpack_sha256=authority.toolpack_sha256, trusted_compiler_sha256=compiler_sha, plan_sha256=bundle.plan_sha256, bundle_sha256=bundle.bundle_sha256, changed_files=authorized_files, screenshot_sha256=_sha(screenshot_evidence) if all(item["sha256"] for item in screenshot_evidence) else None, windows_export_sha256=_file_sha(windows_exe) if windows_exe.is_file() else None, passed=passed)
    _atomic_json(output / "execution_receipt.json", execution)
    receipt_seed = {"quest_id": quest.quest_id, "quest_sha256": quest.sha256, "milestone_id": spec.milestone_id, "state": QuestState.PASSED if passed else QuestState.BLOCKED, "verdict": Verdict.PASS if passed else Verdict.REVISE, "completion_ledger_sha256": _sha(ledger) if passed else None, "verification_sha256": _sha(verification) if passed else None, "execution_checkpoint_sha256": _sha(checkpoint), "passed_criterion_ids": [key for key, value in criteria.items() if value], "preserved_receipt_ids": receipt_ids, "gaps": [key for key, value in criteria.items() if not value], "failure_owner": QuestFailureOwner.NONE if passed else QuestFailureOwner.TECHNICAL, "verifier_role": QuestRole.INDEPENDENT_VERIFIER, "authorization_required": False}
    provisional_receipt = QuestVerificationReceipt(receipt_id="QR-0000000000000000", created_at=_now(), **receipt_seed)
    quest_receipt = provisional_receipt.model_copy(update={"receipt_id": "QR-" + _sha(provisional_receipt.model_dump(mode="json", exclude={"receipt_id", "created_at"}))[:16]})
    _atomic_json(output / "quest_verification_receipt.json", quest_receipt)
    payload = {"schema_version": "khalinos-godot-successor-run-result-v1", "output_dir": str(output), "candidate_dir": str(candidate), "quest_contract": quest.model_dump(mode="json"), "execution_receipt": execution.model_dump(mode="json"), "quest_receipt": quest_receipt.model_dump(mode="json")}
    _atomic_json(output / "run_result.json", payload)
    return GodotSuccessorRunResult.model_validate(payload)
