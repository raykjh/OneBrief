"""Trusted incremental Godot compiler for the PUZZLE M02 gameplay slice."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path, PurePosixPath
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator


def _canonical_sha(value: object) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json", exclude_none=True)
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")).hexdigest()


def _file_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class GodotGameplayPlan(BaseModel):
    schema_version: Literal["khalinos-godot-gameplay-plan-v1"] = (
        "khalinos-godot-gameplay-plan-v1"
    )
    project_name: str = Field(default="PUZZLE", min_length=2, max_length=80)
    board_width: int = Field(default=9, ge=5, le=15)
    board_height: int = Field(default=9, ge=5, le=15)
    player_start: tuple[int, int] = (1, 7)
    seal_position: tuple[int, int] = (1, 3)
    exit_position: tuple[int, int] = (1, 1)
    guard_patrol: list[tuple[int, int]] = Field(
        default_factory=lambda: [(7, 1), (7, 2), (7, 3), (7, 4), (7, 5), (7, 6), (7, 7), (7, 6), (7, 5), (7, 4), (7, 3), (7, 2)],
        min_length=2,
        max_length=32,
    )
    solution_actions: list[Literal["up", "down", "left", "right", "wait"]] = Field(
        default_factory=lambda: ["up", "up", "up", "up", "up", "up"],
        min_length=1,
        max_length=64,
    )

    @model_validator(mode="after")
    def positions_are_inside(self) -> "GodotGameplayPlan":
        points = [self.player_start, self.seal_position, self.exit_position, *self.guard_patrol]
        if any(not (0 < x < self.board_width - 1 and 0 < y < self.board_height - 1) for x, y in points):
            raise ValueError("gameplay positions must stay inside the boundary wall")
        if len({self.player_start, self.seal_position, self.exit_position}) != 3:
            raise ValueError("player, seal, and exit positions must be distinct")
        return self


class CompiledGodotGameplay(BaseModel):
    schema_version: Literal["khalinos-compiled-godot-gameplay-v1"] = (
        "khalinos-compiled-godot-gameplay-v1"
    )
    plan_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    bundle_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    replacements: dict[str, str] = Field(min_length=1, max_length=8)
    additions: dict[str, str] = Field(min_length=3, max_length=12)


class GodotGameplayMaterializationReceipt(BaseModel):
    schema_version: Literal["khalinos-godot-gameplay-materialization-v1"] = (
        "khalinos-godot-gameplay-materialization-v1"
    )
    destination: str
    bundle_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    previous_hashes: dict[str, str]
    written_paths: list[str]


def _gameplay_scene() -> str:
    return """[gd_scene load_steps=2 format=3]

[ext_resource type="Script" path="res://scripts/khalinos_gameplay.gd" id="1"]

[node name="Gameplay" type="Node3D"]
script = ExtResource("1")
"""


def _gameplay_script(plan: GodotGameplayPlan) -> str:
    config = json.dumps(plan.model_dump(mode="json"), ensure_ascii=True, separators=(",", ":"))
    template = '''extends Node3D

const CONFIG := __KHALINOS_CONFIG__
var player := Vector2i(CONFIG.player_start[0], CONFIG.player_start[1])
var guard_index := 0
var turn := 0
var has_seal := false
var state := "playing"
var status_label: Label

func _ready() -> void:
    _build_world()
    _refresh_world()
    call_deferred("_capture_if_requested")

func _material(color: Color) -> StandardMaterial3D:
    var material := StandardMaterial3D.new()
    material.albedo_color = color
    material.roughness = 0.76
    return material

func _box(name: String, position: Vector3, size: Vector3, color: Color) -> MeshInstance3D:
    var node := MeshInstance3D.new()
    node.name = name
    var mesh := BoxMesh.new()
    mesh.size = size
    mesh.material = _material(color)
    node.mesh = mesh
    node.position = position
    add_child(node)
    return node

func _build_world() -> void:
    var environment := WorldEnvironment.new()
    var env := Environment.new()
    env.background_mode = Environment.BG_COLOR
    env.background_color = Color("091018")
    env.ambient_light_source = Environment.AMBIENT_SOURCE_COLOR
    env.ambient_light_color = Color("8ca6b8")
    env.ambient_light_energy = 0.42
    environment.environment = env
    add_child(environment)
    var light := DirectionalLight3D.new()
    light.rotation_degrees = Vector3(-58, -34, 0)
    light.light_energy = 1.25
    light.shadow_enabled = true
    add_child(light)
    var camera := Camera3D.new()
    camera.position = Vector3(4, 11.5, 12.5)
    camera.rotation_degrees = Vector3(-43, 0, 0)
    camera.fov = 47
    add_child(camera)
    for y in range(CONFIG.board_height):
        for x in range(CONFIG.board_width):
            var boundary := x == 0 or y == 0 or x == CONFIG.board_width - 1 or y == CONFIG.board_height - 1
            if boundary:
                _box("Wall_%d_%d" % [x, y], Vector3(x, 0.55, y), Vector3(0.94, 1.1, 0.94), Color("263746"))
            else:
                var color := Color("25313c") if (x + y) % 2 == 0 else Color("202a34")
                _box("Tile_%d_%d" % [x, y], Vector3(x, -0.08, y), Vector3(0.92, 0.14, 0.92), color)
    var canvas := CanvasLayer.new()
    add_child(canvas)
    var panel := ColorRect.new()
    panel.position = Vector2(28, 26)
    panel.size = Vector2(390, 154)
    panel.color = Color(0.025, 0.04, 0.06, 0.92)
    canvas.add_child(panel)
    var title := Label.new()
    title.position = Vector2(24, 16)
    title.text = "PUZZLE - THE THREADBOUND VAULT"
    title.add_theme_font_size_override("font_size", 22)
    title.add_theme_color_override("font_color", Color("e3c06b"))
    panel.add_child(title)
    status_label = Label.new()
    status_label.position = Vector2(24, 52)
    status_label.size = Vector2(340, 82)
    status_label.add_theme_font_size_override("font_size", 16)
    panel.add_child(status_label)
    var legend := Label.new()
    legend.position = Vector2(28, 650)
    legend.text = "ARROWS / WASD: MOVE   SPACE: WAIT   GOLD: SEAL   CYAN: EXIT   RED: GUARD PREVIEW"
    legend.add_theme_color_override("font_color", Color("b9c8d2"))
    canvas.add_child(legend)

func _guard() -> Vector2i:
    var point: Array = CONFIG.guard_patrol[guard_index]
    return Vector2i(point[0], point[1])

func _guard_next() -> Vector2i:
    var point: Array = CONFIG.guard_patrol[(guard_index + 1) % CONFIG.guard_patrol.size()]
    return Vector2i(point[0], point[1])

func _clear_dynamic() -> void:
    for child in get_children():
        if child.name.begins_with("Dynamic_"):
            child.queue_free()

func _piece(name: String, cell: Vector2i, color: Color, height := 0.7) -> void:
    _box("Dynamic_" + name, Vector3(cell.x, height * 0.5 + 0.02, cell.y), Vector3(0.55, height, 0.55), color)

func _refresh_world() -> void:
    _clear_dynamic()
    _piece("Exit", Vector2i(CONFIG.exit_position[0], CONFIG.exit_position[1]), Color("47d7dd"), 0.18)
    if not has_seal:
        _piece("Seal", Vector2i(CONFIG.seal_position[0], CONFIG.seal_position[1]), Color("efc65b"), 0.28)
    _piece("Player", player, Color("d9e5ef"), 0.82)
    _piece("Guard", _guard(), Color("b9414b"), 1.0)
    _piece("GuardPreview", _guard_next(), Color(0.93, 0.16, 0.2, 0.48), 0.12)
    status_label.text = "TURN %02d\nSEAL: %s\nGUARD NEXT: (%d, %d)\nSTATE: %s" % [turn, "RECOVERED" if has_seal else "UNRECOVERED", _guard_next().x, _guard_next().y, state.to_upper()]

func _inside(cell: Vector2i) -> bool:
    return cell.x > 0 and cell.y > 0 and cell.x < CONFIG.board_width - 1 and cell.y < CONFIG.board_height - 1

func apply_action(action: String) -> bool:
    if state != "playing":
        return false
    var delta := Vector2i(999, 999)
    match action:
        "up": delta = Vector2i.UP
        "down": delta = Vector2i.DOWN
        "left": delta = Vector2i.LEFT
        "right": delta = Vector2i.RIGHT
        "wait": delta = Vector2i.ZERO
    if delta == Vector2i(999, 999):
        return false
    var destination: Vector2i = player + delta
    if not _inside(destination):
        return false
    player = destination
    turn += 1
    guard_index = (guard_index + 1) % CONFIG.guard_patrol.size()
    if player == Vector2i(CONFIG.seal_position[0], CONFIG.seal_position[1]):
        has_seal = true
    if player == _guard():
        state = "detected"
    elif player == Vector2i(CONFIG.exit_position[0], CONFIG.exit_position[1]) and has_seal:
        state = "escaped"
    _refresh_world()
    return true

func debug_snapshot() -> Dictionary:
    return {"board": [CONFIG.board_width, CONFIG.board_height], "player": [player.x, player.y], "guard": [_guard().x, _guard().y], "guard_next": [_guard_next().x, _guard_next().y], "turn": turn, "has_seal": has_seal, "state": state}

func debug_run_solution() -> Dictionary:
    for action in CONFIG.solution_actions:
        if not apply_action(action):
            break
    return debug_snapshot()

func _unhandled_input(event: InputEvent) -> void:
    if event.is_action_pressed("ui_up") or event.is_key_pressed(KEY_W): apply_action("up")
    elif event.is_action_pressed("ui_down") or event.is_key_pressed(KEY_S): apply_action("down")
    elif event.is_action_pressed("ui_left") or event.is_key_pressed(KEY_A): apply_action("left")
    elif event.is_action_pressed("ui_right") or event.is_key_pressed(KEY_D): apply_action("right")
    elif event.is_key_pressed(KEY_SPACE): apply_action("wait")

func _argument(prefix: String) -> String:
    for argument in OS.get_cmdline_user_args():
        if argument.begins_with(prefix): return argument.trim_prefix(prefix)
    return ""

func _capture_if_requested() -> void:
    var output := _argument("--capture=")
    if output.is_empty(): return
    await get_tree().process_frame
    await get_tree().process_frame
    await get_tree().process_frame
    var image := get_viewport().get_texture().get_image()
    var error := image.save_png(output)
    get_tree().quit(0 if error == OK else 9)
'''
    return template.replace("__KHALINOS_CONFIG__", config)


def _probe_script() -> str:
    return '''extends SceneTree

func _initialize() -> void:
    call_deferred("_probe")

func _argument(prefix: String) -> String:
    for argument in OS.get_cmdline_user_args():
        if argument.begins_with(prefix): return argument.trim_prefix(prefix)
    return ""

func _probe() -> void:
    var output := _argument("--output=")
    var errors: Array[String] = []
    var packed = load("res://scenes/gameplay.tscn")
    if packed == null:
        errors.append("gameplay_scene_unloadable")
    var game = packed.instantiate() if packed != null else null
    if game != null and game.has_method("debug_snapshot"):
        root.add_child(game)
        await process_frame
        var initial: Dictionary = game.debug_snapshot()
        if initial.board != [9, 9]: errors.append("board_not_9x9")
        if initial.guard == initial.guard_next: errors.append("guard_preview_not_distinct")
        if not game.apply_action("up"): errors.append("player_action_rejected")
        var advanced: Dictionary = game.debug_snapshot()
        if advanced.turn != 1: errors.append("turn_not_consumed")
        game.queue_free()
        await process_frame
    elif game != null:
        errors.append("gameplay_script_unavailable")
        game.queue_free()
    var solution_game = packed.instantiate() if packed != null else null
    var solved: Dictionary = {}
    if solution_game != null and solution_game.has_method("debug_run_solution"):
        root.add_child(solution_game)
        await process_frame
        solved = solution_game.debug_run_solution()
        if solved.state != "escaped" or not solved.has_seal: errors.append("coarse_loop_not_solved")
        solution_game.queue_free()
    elif solution_game != null:
        errors.append("gameplay_solution_unavailable")
        solution_game.queue_free()
    var receipt := {"schema_version": "khalinos-godot-gameplay-probe-v1", "board": [9, 9], "turn_advanced": errors.find("turn_not_consumed") == -1, "guard_preview": errors.find("guard_preview_not_distinct") == -1, "solution": solved, "errors": errors, "passed": errors.is_empty()}
    var file := FileAccess.open(output, FileAccess.WRITE)
    if file == null:
        quit(8)
        return
    file.store_string(JSON.stringify(receipt))
    file.close()
    quit(0 if receipt.passed else 1)
'''


def _export_preset() -> str:
    return '''[preset.0]

name="Windows Desktop"
platform="Windows Desktop"
runnable=true
advanced_options=false
dedicated_server=false
custom_features=""
export_filter="all_resources"
include_filter=""
exclude_filter=""
export_path="build/puzzle-m02.exe"
script_export_mode=2

[preset.0.options]

binary_format/architecture="x86_64"
binary_format/embed_pck=true
texture_format/s3tc_bptc=true
texture_format/etc2_astc=false
'''


def compile_godot_gameplay(plan: GodotGameplayPlan) -> CompiledGodotGameplay:
    replacements = {"scenes/gameplay.tscn": _gameplay_scene()}
    additions = {
        "scripts/khalinos_gameplay.gd": _gameplay_script(plan),
        "scripts/khalinos_gameplay_probe.gd": _probe_script(),
        "export_presets.cfg": _export_preset(),
        "KHALINOS_M02_GAMEPLAY.json": json.dumps(plan.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
    }
    replacements = {key: value.replace("\r\n", "\n") for key, value in replacements.items()}
    additions = {key: value.replace("\r\n", "\n") for key, value in additions.items()}
    plan_sha = _canonical_sha(plan)
    bundle_sha = _canonical_sha({"plan_sha256": plan_sha, "replacements": replacements, "additions": additions})
    return CompiledGodotGameplay(plan_sha256=plan_sha, bundle_sha256=bundle_sha, replacements=replacements, additions=additions)


def materialize_godot_gameplay(bundle: CompiledGodotGameplay, destination: Path, *, expected_previous_hashes: dict[str, str]) -> GodotGameplayMaterializationReceipt:
    root = destination.resolve()
    all_files = {**bundle.replacements, **bundle.additions}
    normalized: dict[str, str] = {}
    for raw, content in all_files.items():
        relative = PurePosixPath(raw.replace("\\", "/"))
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("compiled gameplay bundle contains an unsafe path")
        path = relative.as_posix()
        target = (root / Path(*relative.parts)).resolve()
        if not target.is_relative_to(root):
            raise ValueError("compiled gameplay bundle escapes the destination")
        if path in bundle.replacements:
            expected = expected_previous_hashes.get(path)
            if not expected or not target.is_file() or _file_sha(target) != expected:
                raise PermissionError(f"gameplay replacement is not bound to the verified predecessor: {path}")
        elif target.exists() and target.read_text(encoding="utf-8") != content:
            raise FileExistsError(f"trusted gameplay refuses to overwrite {path}")
        normalized[path] = content
    if _canonical_sha({"plan_sha256": bundle.plan_sha256, "replacements": bundle.replacements, "additions": bundle.additions}) != bundle.bundle_sha256:
        raise ValueError("compiled gameplay bundle digest changed")
    staging = root / f".khalinos-gameplay-{uuid4().hex}"
    previous = {path: _file_sha(root / Path(*PurePosixPath(path).parts)) for path in bundle.replacements}
    backups: dict[Path, bytes] = {}
    written: list[Path] = []
    try:
        for relative, content in normalized.items():
            staged = staging / Path(*PurePosixPath(relative).parts)
            staged.parent.mkdir(parents=True, exist_ok=True)
            staged.write_text(content, encoding="utf-8", newline="\n")
        for relative in normalized:
            target = root / Path(*PurePosixPath(relative).parts)
            if target.exists(): backups[target] = target.read_bytes()
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staging / Path(*PurePosixPath(relative).parts), target)
            written.append(target)
    except Exception:
        for target in reversed(written):
            if target in backups: target.write_bytes(backups[target])
            else: target.unlink(missing_ok=True)
        raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return GodotGameplayMaterializationReceipt(destination=str(root), bundle_sha256=bundle.bundle_sha256, previous_hashes=previous, written_paths=sorted(normalized))
