from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from onebrief.godot_gameplay import (
    GodotGameplayPlan,
    compile_godot_gameplay,
    materialize_godot_gameplay,
)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_gameplay_compiler_is_deterministic_and_bounded() -> None:
    first = compile_godot_gameplay(GodotGameplayPlan())
    second = compile_godot_gameplay(GodotGameplayPlan())

    assert first == second
    assert first.replacements.keys() == {"scenes/gameplay.tscn"}
    assert set(first.additions) == {
        "scripts/khalinos_gameplay.gd",
        "scripts/khalinos_gameplay_probe.gd",
        "export_presets.cfg",
        "KHALINOS_M02_GAMEPLAY.json",
    }
    assert "Node3D" in first.replacements["scenes/gameplay.tscn"]
    assert "guard_next" in first.additions["scripts/khalinos_gameplay.gd"]


def test_gameplay_materialization_requires_exact_verified_predecessor(tmp_path: Path) -> None:
    gameplay = tmp_path / "scenes" / "gameplay.tscn"
    gameplay.parent.mkdir(parents=True)
    gameplay.write_text("verified M01\n", encoding="utf-8")
    bundle = compile_godot_gameplay(GodotGameplayPlan())

    receipt = materialize_godot_gameplay(
        bundle,
        tmp_path,
        expected_previous_hashes={"scenes/gameplay.tscn": _sha(gameplay)},
    )

    assert receipt.written_paths == sorted([*bundle.replacements, *bundle.additions])
    assert gameplay.read_text(encoding="utf-8") == bundle.replacements["scenes/gameplay.tscn"]


def test_gameplay_materialization_refuses_wrong_predecessor_hash(tmp_path: Path) -> None:
    gameplay = tmp_path / "scenes" / "gameplay.tscn"
    gameplay.parent.mkdir(parents=True)
    gameplay.write_text("not approved\n", encoding="utf-8")

    with pytest.raises(PermissionError, match="verified predecessor"):
        materialize_godot_gameplay(
            compile_godot_gameplay(GodotGameplayPlan()),
            tmp_path,
            expected_previous_hashes={"scenes/gameplay.tscn": "0" * 64},
        )
