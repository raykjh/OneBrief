from __future__ import annotations

import hashlib
from pathlib import Path

from onebrief.godot_gameplay import materialize_godot_gameplay
from onebrief.godot_gameplay_shaping import (
    GodotGameplayShapingPlan,
    compile_godot_gameplay_shaping,
)


def test_m03_shaping_compiler_is_deterministic_and_feature_complete() -> None:
    first = compile_godot_gameplay_shaping(GodotGameplayShapingPlan())
    second = compile_godot_gameplay_shaping(GodotGameplayShapingPlan())

    assert first == second
    assert first.replacements.keys() == {"scripts/khalinos_gameplay.gd"}
    assert set(first.additions) == {
        "scripts/khalinos_m03_probe.gd", "KHALINOS_M03_SHAPING.json"
    }
    runtime = first.replacements["scripts/khalinos_gameplay.gd"]
    for contract in ("_vision_cells", "undo", "restart", "use_red_thread", "pressure_active", "has_key"):
        assert contract in runtime
    assert "event is InputEventKey" in runtime
    assert "var keycode: int = int(" in runtime
    assert "event.physical_keycode" in runtime
    assert "event.is_key_pressed" not in runtime
    assert "physical_keyboard_input_not_consumed" in first.additions["scripts/khalinos_m03_probe.gd"]
    assert "keyboard_input_handler_unavailable" in first.additions["scripts/khalinos_m03_probe.gd"]


def test_m03_shaping_replaces_only_exact_m02_runtime(tmp_path: Path) -> None:
    script = tmp_path / "scripts" / "khalinos_gameplay.gd"
    script.parent.mkdir(parents=True)
    script.write_text("verified M02 runtime\n", encoding="utf-8")
    previous = hashlib.sha256(script.read_bytes()).hexdigest()
    bundle = compile_godot_gameplay_shaping(GodotGameplayShapingPlan())

    receipt = materialize_godot_gameplay(
        bundle, tmp_path,
        expected_previous_hashes={"scripts/khalinos_gameplay.gd": previous},
    )

    assert receipt.previous_hashes == {"scripts/khalinos_gameplay.gd": previous}
    assert script.read_text(encoding="utf-8") == bundle.replacements["scripts/khalinos_gameplay.gd"]
