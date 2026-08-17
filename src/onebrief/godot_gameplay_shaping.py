"""Trusted M03 shaping compiler for complete puzzle-rule interactions."""

from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from onebrief.godot_gameplay import CompiledGodotGameplay, _canonical_sha


class GodotGameplayShapingPlan(BaseModel):
    schema_version: Literal["khalinos-godot-gameplay-shaping-plan-v1"] = (
        "khalinos-godot-gameplay-shaping-plan-v1"
    )
    board_width: Literal[9] = 9
    board_height: Literal[9] = 9
    player_start: tuple[int, int] = (1, 7)
    seal_position: tuple[int, int] = (1, 3)
    exit_position: tuple[int, int] = (1, 1)
    key_position: tuple[int, int] = (2, 7)
    door_position: tuple[int, int] = (3, 7)
    pressure_plate_position: tuple[int, int] = (1, 6)
    trap_position: tuple[int, int] = (2, 6)
    obstacles: list[tuple[int, int]] = Field(
        default_factory=lambda: [(4, 3), (4, 4), (4, 5)], min_length=1, max_length=20
    )
    guard_patrol: list[tuple[int, int]] = Field(
        default_factory=lambda: [(7, 1), (7, 2), (7, 3), (7, 4), (7, 5), (7, 6), (7, 7), (7, 6), (7, 5), (7, 4), (7, 3), (7, 2)],
        min_length=2, max_length=32,
    )
    solution_actions: list[Literal["up", "down", "left", "right", "wait"]] = Field(
        default_factory=lambda: ["up", "up", "up", "up", "up", "up"], min_length=1, max_length=64
    )

    @model_validator(mode="after")
    def valid_layout(self) -> "GodotGameplayShapingPlan":
        points = [
            self.player_start, self.seal_position, self.exit_position,
            self.key_position, self.door_position, self.pressure_plate_position,
            self.trap_position, *self.obstacles, *self.guard_patrol,
        ]
        if any(not (0 < x < 8 and 0 < y < 8) for x, y in points):
            raise ValueError("M03 gameplay positions must stay inside the 9x9 boundary")
        if len(set(self.obstacles)) != len(self.obstacles):
            raise ValueError("M03 obstacles must be unique")
        protected = {self.player_start, self.seal_position, self.exit_position, self.key_position, self.door_position, self.pressure_plate_position, self.trap_position}
        if protected & set(self.obstacles):
            raise ValueError("M03 obstacle overlaps an interactive cell")
        return self


def _runtime_script(plan: GodotGameplayShapingPlan) -> str:
    config = json.dumps(plan.model_dump(mode="json"), ensure_ascii=True, separators=(",", ":"))
    template = '''extends Node3D

const CONFIG := __KHALINOS_CONFIG__
var player := Vector2i(CONFIG.player_start[0], CONFIG.player_start[1])
var guard_index := 0
var turn := 0
var has_seal := false
var has_key := false
var pressure_active := false
var red_thread_used := false
var red_thread_anchor := Vector2i(-1, -1)
var state := "playing"
var undo_stack: Array[Dictionary] = []
var status_label: Label

func _point(name: String) -> Vector2i:
    var point: Array = CONFIG[name]
    return Vector2i(point[0], point[1])

func _points(name: String) -> Array[Vector2i]:
    var result: Array[Vector2i] = []
    for raw in CONFIG[name]:
        var point: Array = raw
        result.append(Vector2i(point[0], point[1]))
    return result

func _ready() -> void:
    _build_world()
    _refresh_world()
    call_deferred("_capture_if_requested")

func _material(color: Color) -> StandardMaterial3D:
    var material := StandardMaterial3D.new()
    material.albedo_color = color
    material.roughness = 0.74
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
    var world_environment := WorldEnvironment.new()
    var environment := Environment.new()
    environment.background_mode = Environment.BG_COLOR
    environment.background_color = Color("071018")
    environment.ambient_light_source = Environment.AMBIENT_SOURCE_COLOR
    environment.ambient_light_color = Color("91a9b9")
    environment.ambient_light_energy = 0.42
    world_environment.environment = environment
    add_child(world_environment)
    var light := DirectionalLight3D.new()
    light.rotation_degrees = Vector3(-58, -34, 0)
    light.light_energy = 1.3
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
    panel.position = Vector2(28, 24)
    panel.size = Vector2(420, 194)
    panel.color = Color(0.02, 0.035, 0.05, 0.94)
    canvas.add_child(panel)
    var title := Label.new()
    title.position = Vector2(24, 14)
    title.text = "PUZZLE - THE THREADBOUND VAULT"
    title.add_theme_font_size_override("font_size", 22)
    title.add_theme_color_override("font_color", Color("e3c06b"))
    panel.add_child(title)
    status_label = Label.new()
    status_label.position = Vector2(24, 50)
    status_label.size = Vector2(370, 132)
    status_label.add_theme_font_size_override("font_size", 15)
    panel.add_child(status_label)
    var legend := Label.new()
    legend.position = Vector2(28, 650)
    legend.text = "MOVE: ARROWS/WASD   WAIT: SPACE   UNDO: U   RESTART: R   RED THREAD: T"
    legend.add_theme_color_override("font_color", Color("b9c8d2"))
    canvas.add_child(legend)

func _guard() -> Vector2i:
    var point: Array = CONFIG.guard_patrol[guard_index]
    return Vector2i(point[0], point[1])

func _guard_next() -> Vector2i:
    var point: Array = CONFIG.guard_patrol[(guard_index + 1) % CONFIG.guard_patrol.size()]
    return Vector2i(point[0], point[1])

func _inside(cell: Vector2i) -> bool:
    return cell.x > 0 and cell.y > 0 and cell.x < CONFIG.board_width - 1 and cell.y < CONFIG.board_height - 1

func _closed_door() -> bool:
    return not has_key

func _blocked(cell: Vector2i) -> bool:
    return cell in _points("obstacles") or (cell == _point("door_position") and _closed_door())

func _vision_cells() -> Array[Vector2i]:
    var cells: Array[Vector2i] = []
    var current := _guard()
    var target := _guard_next()
    var difference := target - current
    var direction := Vector2i.ZERO
    if difference.x != 0: direction.x = 1 if difference.x > 0 else -1
    if difference.y != 0: direction.y = 1 if difference.y > 0 else -1
    var cursor := current + direction
    while _inside(cursor) and not _blocked(cursor):
        cells.append(cursor)
        cursor += direction
    return cells

func _clear_dynamic() -> void:
    for child in get_children():
        if child.name.begins_with("Dynamic_"):
            remove_child(child)
            child.queue_free()

func _piece(name: String, cell: Vector2i, color: Color, height := 0.7, width := 0.55) -> void:
    _box("Dynamic_" + name, Vector3(cell.x, height * 0.5 + 0.02, cell.y), Vector3(width, height, width), color)

func _refresh_world() -> void:
    _clear_dynamic()
    for obstacle in _points("obstacles"):
        _piece("Obstacle_%d_%d" % [obstacle.x, obstacle.y], obstacle, Color("536170"), 0.82, 0.82)
    _piece("Exit", _point("exit_position"), Color("47d7dd"), 0.16, 0.72)
    _piece("Pressure", _point("pressure_plate_position"), Color("55b979") if pressure_active else Color("426550"), 0.10, 0.72)
    _piece("Trap", _point("trap_position"), Color("54616b") if pressure_active else Color("c4474e"), 0.11, 0.70)
    if _closed_door(): _piece("Door", _point("door_position"), Color("8c6642"), 1.0, 0.82)
    if not has_key: _piece("Key", _point("key_position"), Color("d8a94c"), 0.22, 0.32)
    if not has_seal: _piece("Seal", _point("seal_position"), Color("efc65b"), 0.30, 0.48)
    for vision in _vision_cells():
        _piece("Vision_%d_%d" % [vision.x, vision.y], vision, Color(0.78, 0.12, 0.16, 0.28), 0.08, 0.76)
    _piece("GuardPreview", _guard_next(), Color(0.97, 0.18, 0.22, 0.62), 0.14, 0.72)
    _piece("Player", player, Color("e4edf3"), 0.82)
    _piece("Guard", _guard(), Color("b9414b"), 1.0)
    status_label.text = "TURN %02d   STATE: %s\nSEAL: %s   KEY: %s\nPLATE: %s   GUARD NEXT: (%d, %d)\nRED THREAD: %s   UNDO: %d" % [turn, state.to_upper(), "YES" if has_seal else "NO", "YES" if has_key else "NO", "ACTIVE" if pressure_active else "IDLE", _guard_next().x, _guard_next().y, "SPENT" if red_thread_used else "READY", undo_stack.size()]

func _snapshot() -> Dictionary:
    return {"player": player, "guard_index": guard_index, "turn": turn, "has_seal": has_seal, "has_key": has_key, "pressure_active": pressure_active, "red_thread_used": red_thread_used, "red_thread_anchor": red_thread_anchor, "state": state}

func _restore(snapshot: Dictionary) -> void:
    player = snapshot.player
    guard_index = snapshot.guard_index
    turn = snapshot.turn
    has_seal = snapshot.has_seal
    has_key = snapshot.has_key
    pressure_active = snapshot.pressure_active
    red_thread_used = snapshot.red_thread_used
    red_thread_anchor = snapshot.red_thread_anchor
    state = snapshot.state
    _refresh_world()

func apply_action(action: String) -> bool:
    if state != "playing": return false
    var delta := Vector2i(999, 999)
    match action:
        "up": delta = Vector2i.UP
        "down": delta = Vector2i.DOWN
        "left": delta = Vector2i.LEFT
        "right": delta = Vector2i.RIGHT
        "wait": delta = Vector2i.ZERO
    if delta == Vector2i(999, 999): return false
    var destination: Vector2i = player + delta
    if not _inside(destination) or _blocked(destination): return false
    undo_stack.append(_snapshot())
    red_thread_anchor = player
    player = destination
    turn += 1
    guard_index = (guard_index + 1) % CONFIG.guard_patrol.size()
    if player == _point("key_position"): has_key = true
    if player == _point("pressure_plate_position"): pressure_active = true
    if player == _point("seal_position"): has_seal = true
    if player == _point("trap_position") and not pressure_active:
        state = "failed_trap"
    elif player == _guard() or player in _vision_cells():
        state = "detected"
    elif player == _point("exit_position") and has_seal:
        state = "escaped"
    _refresh_world()
    return true

func undo() -> bool:
    if undo_stack.is_empty(): return false
    var snapshot: Dictionary = undo_stack.pop_back()
    _restore(snapshot)
    return true

func restart() -> void:
    player = _point("player_start")
    guard_index = 0
    turn = 0
    has_seal = false
    has_key = false
    pressure_active = false
    red_thread_used = false
    red_thread_anchor = Vector2i(-1, -1)
    state = "playing"
    undo_stack.clear()
    _refresh_world()

func use_red_thread() -> bool:
    if red_thread_used or red_thread_anchor == Vector2i(-1, -1) or state != "playing": return false
    player = red_thread_anchor
    red_thread_used = true
    _refresh_world()
    return true

func debug_snapshot() -> Dictionary:
    var vision: Array = []
    for cell in _vision_cells(): vision.append([cell.x, cell.y])
    return {"board": [9, 9], "player": [player.x, player.y], "guard": [_guard().x, _guard().y], "guard_next": [_guard_next().x, _guard_next().y], "vision": vision, "turn": turn, "has_seal": has_seal, "has_key": has_key, "pressure_active": pressure_active, "red_thread_used": red_thread_used, "state": state, "feature_counts": {"obstacles": CONFIG.obstacles.size(), "door": 1, "key": 1, "trap": 1, "pressure_plate": 1}}

func debug_run_solution() -> Dictionary:
    restart()
    for action in CONFIG.solution_actions:
        if not apply_action(action): break
    return debug_snapshot()

func _unhandled_input(event: InputEvent) -> void:
    if event.is_action_pressed("ui_up") or event.is_key_pressed(KEY_W): apply_action("up")
    elif event.is_action_pressed("ui_down") or event.is_key_pressed(KEY_S): apply_action("down")
    elif event.is_action_pressed("ui_left") or event.is_key_pressed(KEY_A): apply_action("left")
    elif event.is_action_pressed("ui_right") or event.is_key_pressed(KEY_D): apply_action("right")
    elif event.is_key_pressed(KEY_SPACE): apply_action("wait")
    elif event.is_key_pressed(KEY_U): undo()
    elif event.is_key_pressed(KEY_R): restart()
    elif event.is_key_pressed(KEY_T): use_red_thread()

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

func _initialize() -> void: call_deferred("_probe")

func _argument(prefix: String) -> String:
    for argument in OS.get_cmdline_user_args():
        if argument.begins_with(prefix): return argument.trim_prefix(prefix)
    return ""

func _instance(packed: PackedScene):
    var game = packed.instantiate()
    root.add_child(game)
    return game

func _dispose(game) -> void:
    root.remove_child(game)
    game.queue_free()

func _probe() -> void:
    var output := _argument("--output=")
    var errors: Array[String] = []
    var packed = load("res://scenes/gameplay.tscn")
    if packed == null:
        errors.append("gameplay_scene_unloadable")
    var initial: Dictionary = {}
    var interaction: Dictionary = {}
    var recovery: Dictionary = {}
    var solution: Dictionary = {}
    var rule_checks := {"trap_lethal": false, "key_door": false, "pressure_safe": false}
    if packed != null:
        var game = _instance(packed)
        await process_frame
        if not game.has_method("debug_snapshot"):
            errors.append("m03_runtime_unavailable")
        else:
            initial = game.debug_snapshot()
            if initial.feature_counts != {"obstacles": 3, "door": 1, "key": 1, "trap": 1, "pressure_plate": 1}: errors.append("feature_topology_mismatch")
            if initial.vision.is_empty(): errors.append("guard_vision_missing")
        _dispose(game)
        await process_frame

        var trap_game = _instance(packed)
        await process_frame
        trap_game.apply_action("right")
        trap_game.apply_action("up")
        rule_checks.trap_lethal = trap_game.debug_snapshot().state == "failed_trap"
        if not rule_checks.trap_lethal: errors.append("armed_trap_not_lethal")
        _dispose(trap_game)
        await process_frame

        var rules_game = _instance(packed)
        await process_frame
        rules_game.apply_action("right")
        if not rules_game.debug_snapshot().has_key: errors.append("key_not_collected")
        rule_checks.key_door = rules_game.debug_snapshot().has_key and rules_game.apply_action("right")
        if not rule_checks.key_door: errors.append("unlocked_door_not_passable")
        if not rules_game.undo() or rules_game.debug_snapshot().player != [2, 7]: errors.append("undo_not_exact")
        rules_game.apply_action("left")
        if not rules_game.use_red_thread() or rules_game.debug_snapshot().player != [2, 7] or not rules_game.debug_snapshot().red_thread_used: errors.append("red_thread_not_one_step_rewind")
        rules_game.restart()
        if rules_game.debug_snapshot().turn != 0 or rules_game.debug_snapshot().red_thread_used: errors.append("restart_not_clean")
        rules_game.apply_action("up")
        rules_game.apply_action("right")
        interaction = rules_game.debug_snapshot()
        rule_checks.pressure_safe = interaction.pressure_active and interaction.state == "playing"
        if not rule_checks.pressure_safe: errors.append("pressure_plate_did_not_disarm_trap")
        _dispose(rules_game)
        await process_frame

        var solution_game = _instance(packed)
        await process_frame
        solution = solution_game.debug_run_solution()
        if solution.state != "escaped" or not solution.has_seal: errors.append("m03_solution_not_escaped")
        recovery = {"undo": errors.find("undo_not_exact") == -1, "restart": errors.find("restart_not_clean") == -1, "red_thread": errors.find("red_thread_not_one_step_rewind") == -1}
        _dispose(solution_game)
    var receipt := {"schema_version": "khalinos-godot-m03-probe-v1", "initial": initial, "rule_checks": rule_checks, "interaction": interaction, "recovery": recovery, "solution": solution, "errors": errors, "passed": errors.is_empty()}
    var file := FileAccess.open(output, FileAccess.WRITE)
    if file == null:
        quit(8)
        return
    file.store_string(JSON.stringify(receipt))
    file.close()
    quit(0 if receipt.passed else 1)
'''


def compile_godot_gameplay_shaping(plan: GodotGameplayShapingPlan) -> CompiledGodotGameplay:
    replacements = {"scripts/khalinos_gameplay.gd": _runtime_script(plan)}
    additions = {
        "scripts/khalinos_m03_probe.gd": _probe_script(),
        "KHALINOS_M03_SHAPING.json": json.dumps(plan.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
    }
    replacements = {key: value.replace("\r\n", "\n") for key, value in replacements.items()}
    additions = {key: value.replace("\r\n", "\n") for key, value in additions.items()}
    plan_sha = _canonical_sha(plan)
    bundle_sha = _canonical_sha({"plan_sha256": plan_sha, "replacements": replacements, "additions": additions})
    return CompiledGodotGameplay(plan_sha256=plan_sha, bundle_sha256=bundle_sha, replacements=replacements, additions=additions)
