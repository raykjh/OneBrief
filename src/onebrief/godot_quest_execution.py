"""End-to-end trusted Godot M01 execution in an isolated Git snapshot."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field

from onebrief.execution_schemas import Verdict
from onebrief.godot_topology import (
    GodotTopologyPlan,
    compile_godot_topology,
    materialize_godot_topology,
)
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
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


def _file_sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(value, BaseModel):
        payload = value.model_dump(mode="json", exclude_none=True)
    else:
        payload = value
    temporary = path.with_suffix(path.suffix + f".{uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


class GodotM01Request(BaseModel):
    schema_version: Literal["khalinos-godot-m01-request-v1"] = (
        "khalinos-godot-m01-request-v1"
    )
    project_id: str = Field(min_length=2, max_length=64)
    source_revision: str = Field(pattern=r"^[a-f0-9]{40}$")
    toolpack_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    milestone_plan_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    authority_envelope_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    project_prefix: str = "game"
    plan: GodotTopologyPlan


class GodotCommandReceipt(BaseModel):
    schema_version: Literal["khalinos-godot-command-receipt-v1"] = (
        "khalinos-godot-command-receipt-v1"
    )
    adapter_id: Literal[AdapterId.GODOT_HEADLESS_PROBE]
    executable_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    argv: list[str] = Field(min_length=8, max_length=16)
    returncode: int
    output_tail: str = Field(max_length=8000)
    probe_receipt_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


class GodotM01ExecutionReceipt(BaseModel):
    schema_version: Literal["khalinos-godot-m01-execution-receipt-v1"] = (
        "khalinos-godot-m01-execution-receipt-v1"
    )
    quest_id: str = Field(pattern=r"^QC-[a-f0-9]{16}$")
    quest_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_revision: str = Field(pattern=r"^[a-f0-9]{40}$")
    toolpack_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    trusted_compiler_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    plan_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    bundle_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    materialization_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    changed_files: list[str] = Field(min_length=1, max_length=64)
    command_receipt_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    passed: bool


class GodotM01RunResult(BaseModel):
    schema_version: Literal["khalinos-godot-m01-run-result-v1"] = (
        "khalinos-godot-m01-run-result-v1"
    )
    output_dir: str
    candidate_dir: str
    quest_contract: QuestContract
    execution_receipt: GodotM01ExecutionReceipt
    quest_receipt: QuestVerificationReceipt


def _safe_prefix(value: str) -> str:
    raw = value.replace("\\", "/").strip()
    relative = PurePosixPath(raw)
    if (
        not raw
        or raw.startswith("/")
        or re.match(r"^[A-Za-z]:", raw)
        or relative.is_absolute()
        or ".." in relative.parts
    ):
        raise ValueError("Godot project prefix escapes the approved workspace")
    return relative.as_posix()


def _run_probe(
    executable: Path,
    project_dir: Path,
    receipt_path: Path,
    executable_sha256: str,
) -> GodotCommandReceipt:
    argv = [
        str(executable),
        "--headless",
        "--path",
        str(project_dir),
        "--script",
        "res://scripts/khalinos_topology_probe.gd",
        "--",
        f"--output={receipt_path}",
    ]
    completed = subprocess.run(
        argv,
        cwd=project_dir,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
        shell=False,
        check=False,
    )
    output = "\n".join(part for part in (completed.stdout, completed.stderr) if part)
    return GodotCommandReceipt(
        adapter_id=AdapterId.GODOT_HEADLESS_PROBE,
        executable_sha256=executable_sha256,
        argv=argv,
        returncode=completed.returncode,
        output_tail=output[-8000:],
        probe_receipt_sha256=(
            _file_sha(receipt_path) if receipt_path.is_file() else None
        ),
    )


def execute_godot_m01(
    request: GodotM01Request,
    *,
    registry_root: Path,
    output_dir: Path,
) -> GodotM01RunResult:
    """Issue, materialize, execute, and independently verify one Godot M01."""

    output = output_dir.resolve()
    if output.exists() and any(output.iterdir()):
        raise FileExistsError("Godot M01 output directory must be new or empty")
    output.mkdir(parents=True, exist_ok=True)

    lifecycle = ProjectToolPackLifecycle(request.project_id, registry_root)
    state = lifecycle.state()
    if not state.execution_ready or state.generated is None or state.approval is None:
        raise PermissionError("Godot M01 requires an approved execution-ready ToolPack")
    profile = state.generated
    if profile.sha256 != request.toolpack_sha256:
        raise PermissionError("Godot M01 request is bound to another ToolPack digest")
    if profile.repository_head_sha != request.source_revision:
        raise PermissionError("Godot M01 request is bound to another source revision")

    adapter = next((
        item for item in profile.adapters
        if item.enabled and item.adapter_id == AdapterId.GODOT_HEADLESS_PROBE
    ), None)
    binding = next((
        item for item in profile.approved_host_executables
        if item.adapter_id == AdapterId.GODOT_HEADLESS_PROBE
    ), None)
    if adapter is None or binding is None:
        raise PermissionError("approved Godot adapter and executable binding are required")
    executable = Path(binding.executable_path).resolve()
    if not lifecycle._host_binding_matches(binding):
        raise PermissionError("approved Godot executable path, size, or digest changed")
    compiler_sha256 = profile.trusted_component_digests.get("godot_topology_compiler")
    if compiler_sha256 is None or compiler_sha256 != lifecycle._trusted_component_digests(
        profile.adapters
    ).get("godot_topology_compiler"):
        raise PermissionError("trusted Godot topology compiler digest changed")

    prefix = _safe_prefix(request.project_prefix)
    if prefix + "/" not in profile.allowed_write_prefixes:
        raise PermissionError("Godot project prefix is outside the approved write boundary")
    if str(adapter.parameter or "") != prefix:
        raise PermissionError("Godot adapter project prefix changed after approval")

    root = Path(profile.project_root).resolve()
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True,
        encoding="utf-8", errors="replace", check=True, shell=False,
    ).stdout.strip()
    dirty = subprocess.run(
        ["git", "status", "--porcelain"], cwd=root, capture_output=True, text=True,
        encoding="utf-8", errors="replace", check=True, shell=False,
    ).stdout.splitlines()
    dirty = [
        line for line in dirty
        if line[3:].replace("\\", "/") != "ONEBRIEF_PROJECT.json"
        and not line[3:].replace("\\", "/").startswith(".onebrief/")
    ]
    if head != request.source_revision or dirty:
        raise RuntimeError("Godot M01 source repository is not the approved clean revision")

    bundle = compile_godot_topology(request.plan)
    authorized_files = sorted(f"{prefix}/{path}" for path in bundle.files)
    quest_seed = {
        "project_id": request.project_id,
        "milestone_id": "M01",
        "source_revision": request.source_revision,
        "toolpack_sha256": request.toolpack_sha256,
        "trusted_compiler_sha256": compiler_sha256,
        "plan_sha256": bundle.plan_sha256,
        "bundle_sha256": bundle.bundle_sha256,
        "authorized_files": authorized_files,
    }
    quest_id = "QC-" + _sha(quest_seed)[:16]
    quest = QuestContract(
        quest_id=quest_id,
        project_id=request.project_id,
        milestone_id="M01",
        objective=(
            "Materialize the approved PUZZLE whole-product screen topology in an isolated "
            "Godot project and prove every declared region loads headlessly."
        ),
        quest_type=QuestType.PROCESS_BOUND,
        initial_execution_phase=ExecutionPhase.PRODUCT_IMPLEMENTATION,
        input_checkpoint=QuestInputCheckpoint(
            milestone_plan_sha256=request.milestone_plan_sha256,
            source_revision=request.source_revision,
        ),
        authority_envelope_sha256=request.authority_envelope_sha256,
        authorized_files=authorized_files,
        forbidden_scope=[
            "original repository writes",
            "model-authored engine commands or executable paths",
            "acceptance-criteria or verifier weakening",
        ],
        required_process=[
            "compile the declarative plan with the trusted topology compiler",
            "materialize only in an isolated local Git clone",
            "run the digest-bound Godot executable through the approved adapter",
            "independently re-hash files and rerun the headless probe",
        ],
        acceptance_criteria=[
            "M01-C1: every approved region has a materialized Godot scene",
            "M01-C2: the runtime manifest preserves the approved initial region and transitions",
            "M01-C3: the approved headless probe loads every region without errors",
            "M01-C4: independent verification reproduces the same PASS",
        ],
        required_evidence=[
            "trusted plan and bundle digests",
            "materialization receipt and exact changed-file list",
            "approved executable digest and command receipt",
            "primary and independent probe receipts",
        ],
        assigned_role=QuestRole.ACCOUNTABLE_MAKER,
        verifier_role=QuestRole.INDEPENDENT_VERIFIER,
        budget=QuestBudget(minimum_usd=0, maximum_usd=0),
        failure_owner=QuestFailureOwner.UNKNOWN,
        issued_at=_now(),
    )
    _atomic_json(output / "quest_contract.json", quest)

    candidate = output / "candidate"
    subprocess.run(
        ["git", "clone", "--local", "--no-hardlinks", str(root), str(candidate)],
        cwd=output,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
        shell=False,
    )
    project_dir = candidate / Path(*PurePosixPath(prefix).parts)
    materialization = materialize_godot_topology(bundle, project_dir)
    if sorted(materialization.written_paths) != sorted(bundle.files):
        raise RuntimeError("Godot materialization did not produce the exact compiled bundle")
    _atomic_json(output / "materialization_receipt.json", materialization)

    changed = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"], cwd=candidate, capture_output=True,
        text=True, encoding="utf-8", errors="replace", check=True, shell=False,
    ).stdout.splitlines()
    changed_files = sorted(line[3:].replace("\\", "/") for line in changed)
    unexpected = sorted(set(changed_files) - set(authorized_files))
    if unexpected:
        raise PermissionError("Godot materialization changed unauthorized files: " + ", ".join(unexpected))

    primary_probe = output / "primary_probe.json"
    primary_command = _run_probe(
        executable, project_dir, primary_probe, binding.executable_sha256
    )
    _atomic_json(output / "primary_command_receipt.json", primary_command)
    primary_payload = (
        json.loads(primary_probe.read_text(encoding="utf-8"))
        if primary_probe.is_file() else {}
    )

    independent_probe = output / "independent_probe.json"
    independent_command = _run_probe(
        executable, project_dir, independent_probe, binding.executable_sha256
    )
    _atomic_json(output / "independent_command_receipt.json", independent_command)
    independent_payload = (
        json.loads(independent_probe.read_text(encoding="utf-8"))
        if independent_probe.is_file() else {}
    )

    expected_regions = [region.region_id for region in request.plan.regions]
    file_hashes_match = all(
        _file_sha(project_dir / Path(*PurePosixPath(path).parts))
        == hashlib.sha256(content.encode("utf-8")).hexdigest()
        for path, content in bundle.files.items()
    )
    criteria = {
        "M01-C1": file_hashes_match and len(materialization.written_paths) == len(bundle.files),
        "M01-C2": (
            primary_payload.get("visited") == expected_regions
            and not primary_payload.get("errors")
        ),
        "M01-C3": primary_command.returncode == 0 and primary_payload.get("passed") is True,
        "M01-C4": (
            independent_command.returncode == 0
            and independent_payload.get("passed") is True
            and independent_payload.get("visited") == expected_regions
            and independent_payload == primary_payload
        ),
    }
    passed = all(criteria.values())
    verification = {
        "schema_version": "khalinos-godot-m01-independent-verification-v1",
        "verifier_role": QuestRole.INDEPENDENT_VERIFIER.value,
        "quest_id": quest.quest_id,
        "quest_sha256": quest.sha256,
        "criteria": criteria,
        "primary_probe_sha256": _file_sha(primary_probe) if primary_probe.is_file() else None,
        "independent_probe_sha256": (
            _file_sha(independent_probe) if independent_probe.is_file() else None
        ),
        "passed": passed,
    }
    ledger = {
        "schema_version": "khalinos-godot-m01-completion-ledger-v1",
        "milestone_id": "M01",
        "quest_id": quest.quest_id,
        "criteria": [
            {"criterion_id": key, "status": "passed" if value else "failed"}
            for key, value in criteria.items()
        ],
        "status": "passed" if passed else "failed",
    }
    checkpoint = {
        "schema_version": "khalinos-godot-m01-execution-checkpoint-v1",
        "source_revision": request.source_revision,
        "toolpack_sha256": request.toolpack_sha256,
        "trusted_compiler_sha256": compiler_sha256,
        "plan_sha256": bundle.plan_sha256,
        "bundle_sha256": bundle.bundle_sha256,
        "candidate_tree_sha256": _sha({
            path: _file_sha(project_dir / Path(*PurePosixPath(path).parts))
            for path in sorted(bundle.files)
        }),
    }
    _atomic_json(output / "independent_verification.json", verification)
    _atomic_json(output / "completion_ledger.json", ledger)
    _atomic_json(output / "execution_checkpoint.json", checkpoint)

    execution = GodotM01ExecutionReceipt(
        quest_id=quest.quest_id,
        quest_sha256=quest.sha256,
        source_revision=request.source_revision,
        toolpack_sha256=request.toolpack_sha256,
        trusted_compiler_sha256=compiler_sha256,
        plan_sha256=bundle.plan_sha256,
        bundle_sha256=bundle.bundle_sha256,
        materialization_sha256=_sha(materialization),
        changed_files=changed_files,
        command_receipt_sha256=_sha(primary_command),
        passed=passed,
    )
    _atomic_json(output / "execution_receipt.json", execution)

    receipt_seed = {
        "quest_id": quest.quest_id,
        "quest_sha256": quest.sha256,
        "milestone_id": "M01",
        "state": QuestState.PASSED if passed else QuestState.BLOCKED,
        "verdict": Verdict.PASS if passed else Verdict.FAIL,
        "completion_ledger_sha256": _sha(ledger) if passed else None,
        "verification_sha256": _sha(verification) if passed else None,
        "execution_checkpoint_sha256": _sha(checkpoint),
        "passed_criterion_ids": [key for key, value in criteria.items() if value],
        "gaps": [key for key, value in criteria.items() if not value],
        "failure_owner": (
            QuestFailureOwner.NONE if passed else QuestFailureOwner.TECHNICAL
        ),
        "verifier_role": QuestRole.INDEPENDENT_VERIFIER,
        "authorization_required": False,
    }
    provisional_receipt = QuestVerificationReceipt(
        receipt_id="QR-0000000000000000",
        created_at=_now(),
        **receipt_seed,
    )
    receipt_id = "QR-" + _sha(provisional_receipt.model_dump(
        mode="json", exclude={"receipt_id", "created_at"}
    ))[:16]
    quest_receipt = provisional_receipt.model_copy(
        update={"receipt_id": receipt_id}
    )
    _atomic_json(output / "quest_verification_receipt.json", quest_receipt)
    _atomic_json(output / "run_result.json", {
        "schema_version": "khalinos-godot-m01-run-result-v1",
        "output_dir": str(output),
        "candidate_dir": str(candidate),
        "quest_contract": quest.model_dump(mode="json"),
        "execution_receipt": execution.model_dump(mode="json"),
        "quest_receipt": quest_receipt.model_dump(mode="json"),
    })
    return GodotM01RunResult(
        output_dir=str(output),
        candidate_dir=str(candidate),
        quest_contract=quest,
        execution_receipt=execution,
        quest_receipt=quest_receipt,
    )
