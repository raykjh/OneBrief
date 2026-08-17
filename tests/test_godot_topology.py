from __future__ import annotations

import json

import pytest

from onebrief.godot_topology import (
    GodotRegion,
    GodotRegionKind,
    GodotTopologyPlan,
    compile_godot_topology,
    materialize_godot_topology,
)


def _plan() -> GodotTopologyPlan:
    return GodotTopologyPlan(
        project_name="Holdout Observatory",
        initial_region="arrival",
        regions=[
            GodotRegion(
                region_id="arrival",
                label="ARRIVAL",
                transitions=["catalog"],
            ),
            GodotRegion(
                region_id="catalog",
                label="CATALOG",
                transitions=["workspace", "settings"],
            ),
            GodotRegion(
                region_id="workspace",
                label="WORKSPACE",
                transitions=["summary"],
            ),
            GodotRegion(
                region_id="settings",
                label="SETTINGS",
                kind=GodotRegionKind.OVERLAY,
                transitions=["workspace"],
            ),
            GodotRegion(
                region_id="summary",
                label="SUMMARY",
            ),
        ],
    )


def test_compiler_emits_native_scene_for_every_holdout_region() -> None:
    plan = _plan()
    bundle = compile_godot_topology(plan)

    assert bundle.initial_scene == "scenes/arrival.tscn"
    assert set(bundle.region_scene_paths) == {
        "arrival", "catalog", "workspace", "settings", "summary",
    }
    assert all(path in bundle.files for path in bundle.region_scene_paths.values())
    assert "puzzle" not in json.dumps(bundle.files).casefold()
    assert "unity" not in json.dumps(bundle.files).casefold()
    assert bundle.verification_command[0:3] == ["godot", "--headless", "--path"]


def test_compilation_is_digest_stable() -> None:
    first = compile_godot_topology(_plan())
    second = compile_godot_topology(_plan())

    assert first.plan_sha256 == second.plan_sha256
    assert first.bundle_sha256 == second.bundle_sha256
    assert first.files == second.files


def test_materialization_writes_atomic_new_bundle_and_is_idempotent(tmp_path) -> None:
    bundle = compile_godot_topology(_plan())
    destination = tmp_path / "greenfield"

    first = materialize_godot_topology(bundle, destination)
    second = materialize_godot_topology(bundle, destination)

    assert first.bundle_sha256 == second.bundle_sha256
    assert (destination / "project.godot").is_file()
    assert len(list((destination / "scenes").glob("*.tscn"))) == 5
    manifest = json.loads(
        (destination / "KHALINOS_TOPOLOGY.json").read_text(encoding="utf-8")
    )
    assert len(manifest["regions"]) == 5
    assert not list(destination.glob(".khalinos-topology-*"))


def test_materialization_refuses_to_overwrite_existing_project_file(tmp_path) -> None:
    bundle = compile_godot_topology(_plan())
    destination = tmp_path / "existing"
    destination.mkdir()
    (destination / "project.godot").write_text("user-owned\n", encoding="utf-8")

    with pytest.raises(FileExistsError, match="refuses to overwrite"):
        materialize_godot_topology(bundle, destination)

    assert (destination / "project.godot").read_text(encoding="utf-8") == "user-owned\n"
    assert not (destination / "scenes").exists()


def test_plan_rejects_unknown_and_unreachable_regions() -> None:
    with pytest.raises(ValueError, match="unknown regions"):
        GodotTopologyPlan(
            project_name="Broken Route",
            initial_region="start",
            regions=[
                GodotRegion(region_id="start", label="START", transitions=["missing"]),
                GodotRegion(region_id="finish", label="FINISH"),
            ],
        )
    with pytest.raises(ValueError, match="unreachable"):
        GodotTopologyPlan(
            project_name="Disconnected Route",
            initial_region="start",
            regions=[
                GodotRegion(region_id="start", label="START"),
                GodotRegion(region_id="finish", label="FINISH"),
            ],
        )
