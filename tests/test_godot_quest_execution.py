from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from onebrief.godot_quest_execution import (
    GodotCommandReceipt,
    GodotM01Request,
    _sha,
    execute_godot_m01,
)
from onebrief.godot_topology import GodotRegion, GodotRegionKind, GodotTopologyPlan
from onebrief.project_import import ExternalProjectImporter, MANIFEST_NAME
from onebrief.toolpack_lifecycle import AdapterId, ProjectToolPackLifecycle


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()


def _project(tmp_path: Path, monkeypatch) -> tuple[Path, Path, ProjectToolPackLifecycle]:
    root = tmp_path / "puzzle"
    registry = tmp_path / "registry"
    (root / "game").mkdir(parents=True)
    (root / "docs").mkdir()
    (root / "game" / ".gitkeep").write_text("", encoding="utf-8")
    (root / "docs" / "outcome.md").write_text("Approved PUZZLE outcome\n", encoding="utf-8")
    _git(root, "init")
    _git(root, "config", "user.name", "KHALINOS Test")
    _git(root, "config", "user.email", "khalinos@example.invalid")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "Create greenfield envelope")
    manifest = {
        "schema_version": "onebrief-project-v1",
        "project_id": "puzzle",
        "name": "PUZZLE",
        "project_type": "godot_project",
        "project_root": str(root.resolve()),
        "canonical_goal": "Build the approved turn-based stealth puzzle.",
        "summary": "Serverless Godot validation project.",
        "authoritative_documents": ["docs/outcome.md"],
    }
    payload = json.dumps(manifest).encode("utf-8")
    (root / MANIFEST_NAME).write_bytes(payload)
    ExternalProjectImporter(registry).import_bytes(payload)
    monkeypatch.setenv("KHALINOS_GODOT_EXECUTABLE", sys.executable)
    lifecycle = ProjectToolPackLifecycle("puzzle", registry)
    generated = lifecycle.generate_and_qualify()
    lifecycle.approve(generated.generated.sha256)
    return root, registry, lifecycle


def _plan() -> GodotTopologyPlan:
    return GodotTopologyPlan(
        project_name="PUZZLE",
        initial_region="title",
        regions=[
            GodotRegion(region_id="title", kind=GodotRegionKind.SCREEN, label="Title", transitions=["chamber_select"]),
            GodotRegion(region_id="chamber_select", kind=GodotRegionKind.SCREEN, label="Chambers", transitions=["gameplay"]),
            GodotRegion(region_id="gameplay", kind=GodotRegionKind.SCREEN, label="Labyrinth", transitions=["pause", "mission_result"]),
            GodotRegion(region_id="pause", kind=GodotRegionKind.OVERLAY, label="Pause", transitions=["gameplay"]),
            GodotRegion(region_id="mission_result", kind=GodotRegionKind.OVERLAY, label="Result", transitions=["chamber_select"]),
        ],
    )


def test_godot_m01_vertical_path_issues_executes_and_independently_verifies(
    tmp_path: Path, monkeypatch
) -> None:
    root, registry, lifecycle = _project(tmp_path, monkeypatch)
    profile = lifecycle.state().generated
    expected = [region.region_id for region in _plan().regions]

    def fake_probe(executable, project_dir, receipt_path, executable_sha256):
        receipt_path.write_text(json.dumps({
            "schema_version": "khalinos-godot-topology-probe-v1",
            "visited": expected,
            "errors": [],
            "passed": True,
        }), encoding="utf-8")
        return GodotCommandReceipt(
            adapter_id=AdapterId.GODOT_HEADLESS_PROBE,
            executable_sha256=executable_sha256,
            argv=[str(executable), "--headless", "--path", str(project_dir), "--script", "res://scripts/khalinos_topology_probe.gd", "--", f"--output={receipt_path}"],
            returncode=0,
            output_tail="PASS",
            probe_receipt_sha256=_sha(json.loads(receipt_path.read_text(encoding="utf-8"))),
        )

    monkeypatch.setattr("onebrief.godot_quest_execution._run_probe", fake_probe)
    request = GodotM01Request(
        project_id="puzzle",
        source_revision=_git(root, "rev-parse", "HEAD"),
        toolpack_sha256=profile.sha256,
        milestone_plan_sha256="a" * 64,
        authority_envelope_sha256="b" * 64,
        plan=_plan(),
    )

    result = execute_godot_m01(
        request, registry_root=registry, output_dir=tmp_path / "m01"
    )

    assert result.quest_contract.milestone_id == "M01"
    assert result.execution_receipt.passed is True
    assert result.quest_receipt.state.value == "passed"
    assert result.quest_receipt.passed_criterion_ids == [
        "M01-C1", "M01-C2", "M01-C3", "M01-C4"
    ]
    assert (Path(result.candidate_dir) / "game" / "project.godot").is_file()
    assert _git(root, "status", "--porcelain") in {"", "?? ONEBRIEF_PROJECT.json"}
    integrity = _sha(result.quest_receipt.model_dump(
        mode="json", exclude={"receipt_id", "created_at"}
    ))
    assert result.quest_receipt.receipt_id == "QR-" + integrity[:16]


@pytest.mark.parametrize("prefix", ["../game", "/tmp/game", "C:\\tmp\\game"])
def test_godot_m01_rejects_project_prefix_outside_workspace(
    tmp_path: Path, monkeypatch, prefix: str
) -> None:
    root, registry, lifecycle = _project(tmp_path, monkeypatch)
    profile = lifecycle.state().generated
    request = GodotM01Request(
        project_id="puzzle",
        source_revision=_git(root, "rev-parse", "HEAD"),
        toolpack_sha256=profile.sha256,
        milestone_plan_sha256="a" * 64,
        authority_envelope_sha256="b" * 64,
        project_prefix=prefix,
        plan=_plan(),
    )

    with pytest.raises(ValueError, match="escapes the approved workspace"):
        execute_godot_m01(
            request, registry_root=registry, output_dir=tmp_path / "m01"
        )
