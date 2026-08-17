from __future__ import annotations

from onebrief.godot_product_finish import GodotProductFinishPlan, compile_godot_product_finish


def test_product_finish_compiler_is_deterministic_and_complete() -> None:
    first = compile_godot_product_finish(GodotProductFinishPlan())
    second = compile_godot_product_finish(GodotProductFinishPlan())
    assert first == second
    assert set(first.replacements) == {
        "project.godot", "scripts/khalinos_topology_region.gd",
        "scripts/khalinos_gameplay.gd", "scenes/title.tscn",
        "scenes/chamber_select.tscn", "scenes/pause.tscn",
        "scenes/mission_result.tscn",
    }
    assert set(first.additions) == {
        "scripts/khalinos_session.gd", "scripts/khalinos_product_screen.gd",
        "scripts/khalinos_m04_probe.gd", "scripts/khalinos_m04_capture.gd",
        "scripts/khalinos_release_audit.gd",
        "KHALINOS_M04_PRODUCT.json",
    }
    assert "KHALINOSSession" in first.replacements["project.godot"]
    assert "KHALINOSReleaseAudit" in first.replacements["project.godot"]
    assert "--release-audit-dir=" in first.additions["scripts/khalinos_release_audit.gd"]
    assert "save_to" in first.replacements["scripts/khalinos_gameplay.gd"]
    assert "THREE" not in first.additions["scripts/khalinos_session.gd"]
    assert first.additions["scripts/khalinos_session.gd"].count("The ") >= 3
