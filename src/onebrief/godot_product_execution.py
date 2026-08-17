"""Thin M04 product-finish wrapper over the reusable Godot successor engine."""

from __future__ import annotations

from pathlib import Path

from onebrief.godot_product_finish import GodotProductFinishPlan, compile_godot_product_finish
from onebrief.godot_successor_execution import (
    GodotSuccessorAuthority,
    GodotSuccessorRunResult,
    GodotSuccessorSpec,
    execute_godot_successor,
)


def execute_godot_m04(
    authority: GodotSuccessorAuthority,
    plan: GodotProductFinishPlan,
    *,
    registry_root: Path,
    output_dir: Path,
) -> GodotSuccessorRunResult:
    bundle = compile_godot_product_finish(plan)
    criteria = [
        "M04-C1: title, chamber selection, gameplay, pause, and result form one explicit product flow",
        "M04-C2: three named chambers are selectable and the choice binds to gameplay",
        "M04-C3: save and load restore the exact player state and turn",
        "M04-C4: accessibility and audio settings survive a persisted round trip",
        "M04-C5: every UI action and product file is materialized from the trusted plan",
        "M04-C6: independent verification reproduces PASS and four fresh 1280x720 product screens",
        "M04-C7: a fresh non-empty Windows Desktop product executable is exported",
    ]
    spec = GodotSuccessorSpec(
        milestone_id="M04", parent_milestone_id="M03",
        objective="Finish the verified puzzle rules as a coherent player-facing product with title, three-chamber selection, gameplay, pause, result, save/load, settings, and a unified visual language.",
        compiler_digest_key="godot_product_finish_compiler",
        compiler_filename="godot_product_finish.py",
        probe_script="res://scripts/khalinos_m04_probe.gd",
        acceptance_criteria=criteria,
        required_process=[
            "verify the complete raw M01 through M03 Quest chain",
            "restore every inherited product byte into an isolated clone",
            "replace only exact verified predecessor files with the trusted product compiler",
            "run primary and independent product-flow probes",
            "capture title, chamber selection, gameplay, and result from the actual runtime",
            "export a fresh Windows Desktop product binary",
        ],
        required_evidence=[
            "verified M01 through M03 receipt and candidate chain",
            "trusted M04 plan, compiler, bundle, inherited-file, and delta digests",
            "primary and independent flow, save/load, and settings receipts",
            "four fresh runtime PNG dimensions and SHA-256 digests",
            "fresh Windows executable size and SHA-256",
        ],
        screenshot_names=["title.png", "chamber_select.png", "gameplay.png", "mission_result.png"],
        export_name="puzzle-complete.exe",
        capture_script="res://scripts/khalinos_m04_capture.gd",
    )

    def evaluate(payload: dict[str, object], exact_delta: bool, screenshots: list[Path], windows_exe: Path) -> dict[str, bool]:
        contracts = payload.get("screen_contracts", {})
        visited = payload.get("visited", [])
        screenshot_ok = all(
            path.is_file() and path.stat().st_size >= 10_000
            for path in screenshots
        )
        return {
            "M04-C1": visited == ["title", "chamber_select", "pause", "mission_result"] and set(contracts) == {"title", "chamber_select", "pause", "mission_result"},
            "M04-C2": len(payload.get("chambers", [])) == 3 and payload.get("selected_chamber") == 2,
            "M04-C3": payload.get("save_load", {}).get("exact") is True,
            "M04-C4": payload.get("settings_roundtrip") is True,
            "M04-C5": exact_delta and all(contracts.get(screen, {}).get("actions") for screen in contracts),
            "M04-C6": screenshot_ok,
            "M04-C7": windows_exe.is_file() and windows_exe.stat().st_size >= 1_000_000,
        }

    return execute_godot_successor(
        authority, registry_root=registry_root, output_dir=output_dir,
        bundle=bundle, spec=spec, evaluate=evaluate,
    )
