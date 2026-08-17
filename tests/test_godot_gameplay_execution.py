from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

from onebrief.godot_gameplay_execution import (
    GodotM02CommandReceipt,
    GodotM02Request,
    _sha,
    execute_godot_m02,
)
from onebrief.godot_quest_execution import (
    GodotCommandReceipt,
    GodotM01Request,
    execute_godot_m01,
)
from onebrief.godot_topology import GodotRegion, GodotTopologyPlan
from onebrief.project_import import ExternalProjectImporter, MANIFEST_NAME
from onebrief.toolpack_lifecycle import AdapterId, ProjectToolPackLifecycle


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True).stdout.strip()


def _setup(tmp_path: Path, monkeypatch):
    root = tmp_path / "puzzle"
    registry = tmp_path / "registry"
    (root / "game").mkdir(parents=True)
    (root / "game" / ".gitkeep").write_text("\n", encoding="utf-8")
    (root / "docs").mkdir()
    (root / "docs" / "outcome.md").write_text("Approved PUZZLE outcome\n", encoding="utf-8")
    _git(root, "init")
    _git(root, "config", "user.name", "KHALINOS Test")
    _git(root, "config", "user.email", "khalinos@example.invalid")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "Create envelope")
    manifest = {
        "schema_version": "onebrief-project-v1", "project_id": "puzzle-m02",
        "name": "PUZZLE", "project_type": "godot_project", "project_root": str(root),
        "canonical_goal": "Build the approved stealth puzzle.", "summary": "M02 test.",
        "authoritative_documents": ["docs/outcome.md"],
    }
    payload = json.dumps(manifest).encode()
    (root / MANIFEST_NAME).write_bytes(payload)
    ExternalProjectImporter(registry).import_bytes(payload)
    monkeypatch.setenv("KHALINOS_GODOT_EXECUTABLE", sys.executable)
    lifecycle = ProjectToolPackLifecycle("puzzle-m02", registry)
    state = lifecycle.generate_and_qualify()
    lifecycle.approve(state.generated.sha256)
    return root, registry, lifecycle


def test_m02_consumes_raw_m01_receipt_and_preserves_lineage(tmp_path: Path, monkeypatch) -> None:
    root, registry, lifecycle = _setup(tmp_path, monkeypatch)
    profile = lifecycle.state().generated
    plan = GodotTopologyPlan(
        project_name="PUZZLE", initial_region="title",
        regions=[
            GodotRegion(region_id="title", label="Title", transitions=["gameplay"]),
            GodotRegion(region_id="gameplay", label="Gameplay", transitions=[]),
        ],
    )
    expected = ["title", "gameplay"]

    def fake_m01_probe(executable, project_dir, receipt_path, executable_sha256):
        payload = {"schema_version": "khalinos-godot-topology-probe-v1", "visited": expected, "errors": [], "passed": True}
        receipt_path.write_text(json.dumps(payload), encoding="utf-8")
        return GodotCommandReceipt(
            adapter_id=AdapterId.GODOT_HEADLESS_PROBE, executable_sha256=executable_sha256,
            argv=[str(executable), "--headless", "--path", str(project_dir), "--script", "probe", "--", f"--output={receipt_path}"],
            returncode=0, output_tail="PASS", probe_receipt_sha256=hashlib.sha256(receipt_path.read_bytes()).hexdigest(),
        )

    monkeypatch.setattr("onebrief.godot_quest_execution._run_probe", fake_m01_probe)
    revision = _git(root, "rev-parse", "HEAD")
    m01 = execute_godot_m01(
        GodotM01Request(
            project_id="puzzle-m02", source_revision=revision, toolpack_sha256=profile.sha256,
            milestone_plan_sha256="a" * 64, authority_envelope_sha256="b" * 64, plan=plan,
        ), registry_root=registry, output_dir=tmp_path / "m01",
    )

    probe_payload = {
        "schema_version": "khalinos-godot-gameplay-probe-v1", "board": [9, 9],
        "turn_advanced": True, "guard_preview": True,
        "solution": {"state": "escaped", "has_seal": True}, "errors": [], "passed": True,
    }

    def fake_run(argv, *, cwd, kind, executable_sha256, artifact, timeout):
        if kind == "gameplay_probe": artifact.write_text(json.dumps(probe_payload), encoding="utf-8")
        elif kind == "runtime_capture":
            artifact.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + (1280).to_bytes(4, "big") + (720).to_bytes(4, "big") + b"\x00" * 10_000)
        else:
            artifact.write_bytes(b"MZ" + b"\x00" * 1_000_000)
        return GodotM02CommandReceipt(
            command_kind=kind, executable_sha256=executable_sha256, argv=argv,
            returncode=0, output_tail="PASS", artifact_sha256=hashlib.sha256(artifact.read_bytes()).hexdigest(),
        )

    monkeypatch.setattr("onebrief.godot_gameplay_execution._run", fake_run)
    result = execute_godot_m02(
        GodotM02Request(
            project_id="puzzle-m02", source_revision=revision, toolpack_sha256=profile.sha256,
            milestone_plan_sha256="c" * 64, authority_envelope_sha256="d" * 64,
            previous_result_dir=m01.output_dir,
        ), registry_root=registry, output_dir=tmp_path / "m02",
    )

    assert result.quest_contract.parent_quest_id == m01.quest_contract.quest_id
    assert result.quest_contract.input_checkpoint.previous_receipt_id == m01.quest_receipt.receipt_id
    assert result.quest_receipt.preserved_receipt_ids == [m01.quest_receipt.receipt_id]
    assert result.quest_receipt.state.value == "passed"
    assert result.quest_receipt.passed_criterion_ids == [f"M02-C{index}" for index in range(1, 7)]
    assert _git(root, "status", "--porcelain") in {"", "?? ONEBRIEF_PROJECT.json"}
