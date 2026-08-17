"""Trusted M04 compiler for the player-facing PUZZLE product experience."""

from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, Field

from onebrief.godot_gameplay import CompiledGodotGameplay, _canonical_sha
from onebrief.godot_gameplay_shaping import GodotGameplayShapingPlan, _runtime_script


class GodotProductFinishPlan(BaseModel):
    schema_version: Literal["khalinos-godot-product-finish-plan-v1"] = "khalinos-godot-product-finish-plan-v1"
    product_title: str = Field(default="THE THREADBOUND VAULT", min_length=3, max_length=80)
    chambers: list[str] = Field(
        default_factory=lambda: ["I. The Silent Seal", "II. The Watcher's Turn", "III. The Broken Path"],
        min_length=3, max_length=3,
    )
    settings_keys: list[str] = Field(default_factory=lambda: ["high_contrast", "reduced_motion", "master_volume"], min_length=3, max_length=3)
    capture_regions: list[str] = Field(default_factory=lambda: ["title", "chamber_select", "gameplay", "mission_result"], min_length=4, max_length=4)


def _project_file() -> str:
    return '''[application]
config/name="PUZZLE"
run/main_scene="res://scenes/title.tscn"

[autoload]
KHALINOSSession="*res://scripts/khalinos_session.gd"
KHALINOSReleaseAudit="*res://scripts/khalinos_release_audit.gd"

[display]
window/size/viewport_width=1280
window/size/viewport_height=720
window/size/window_width_override=1280
window/size/window_height_override=720
window/stretch/mode="canvas_items"

[rendering]
renderer/rendering_method="gl_compatibility"
renderer/rendering_method.mobile="gl_compatibility"
environment/defaults/default_clear_color=Color(0.03, 0.04, 0.06, 1)
'''


def _session_script(plan: GodotProductFinishPlan) -> str:
    chambers = json.dumps(plan.chambers, ensure_ascii=True)
    return '''extends Node

const CHAMBERS := %s
var selected_chamber := 1
var settings := {"high_contrast": false, "reduced_motion": false, "master_volume": 0.8}
var last_result := {"completed": false, "turns": 0, "red_thread_used": false, "chamber": 1}

func select_chamber(chamber_id: int) -> bool:
    if chamber_id < 1 or chamber_id > CHAMBERS.size(): return false
    selected_chamber = chamber_id
    return true

func chamber_title(chamber_id := selected_chamber) -> String:
    if chamber_id < 1 or chamber_id > CHAMBERS.size(): return "Unknown Chamber"
    return CHAMBERS[chamber_id - 1]

func set_setting(key: String, value) -> bool:
    if not settings.has(key): return false
    settings[key] = value
    return true

func record_completion(turns: int, red_thread_used: bool) -> void:
    last_result = {"completed": true, "turns": turns, "red_thread_used": red_thread_used, "chamber": selected_chamber}

func save_settings_to(path: String) -> bool:
    var file := FileAccess.open(path, FileAccess.WRITE)
    if file == null: return false
    file.store_string(JSON.stringify(settings))
    file.close()
    return true

func load_settings_from(path: String) -> bool:
    var file := FileAccess.open(path, FileAccess.READ)
    if file == null: return false
    var parsed = JSON.parse_string(file.get_as_text())
    file.close()
    if not parsed is Dictionary: return false
    for key in settings:
        if parsed.has(key): settings[key] = parsed[key]
    return true

func debug_contract() -> Dictionary:
    return {"selected_chamber": selected_chamber, "chambers": CHAMBERS, "settings": settings, "last_result": last_result}
''' % chambers


def _screen_script(plan: GodotProductFinishPlan) -> str:
    title = json.dumps(plan.product_title, ensure_ascii=True)
    return '''extends Control

@export var screen_id := "title"
const PRODUCT_TITLE := %s
var content: VBoxContainer

func _session() -> Node:
    return get_node("/root/KHALINOSSession")

func _ready() -> void:
    _build_background()
    _build_content()

func _label(text: String, size: int, color: Color, centered := true) -> Label:
    var label := Label.new()
    label.text = text
    label.add_theme_font_size_override("font_size", size)
    label.add_theme_color_override("font_color", color)
    label.horizontal_alignment = HORIZONTAL_ALIGNMENT_CENTER if centered else HORIZONTAL_ALIGNMENT_LEFT
    label.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
    return label

func _button(text: String, action: Callable) -> Button:
    var button := Button.new()
    button.text = text
    button.custom_minimum_size = Vector2(430, 54)
    button.add_theme_font_size_override("font_size", 17)
    button.pressed.connect(action)
    return button

func _build_background() -> void:
    var background := ColorRect.new()
    background.set_anchors_and_offsets_preset(Control.PRESET_FULL_RECT)
    background.color = Color("071018")
    add_child(background)
    var frame := ColorRect.new()
    frame.position = Vector2(54, 44)
    frame.size = Vector2(1172, 632)
    frame.color = Color("101d28")
    add_child(frame)
    var inner := ColorRect.new()
    inner.position = Vector2(66, 56)
    inner.size = Vector2(1148, 608)
    inner.color = Color("09131c")
    add_child(inner)
    for index in range(9):
        var rune := ColorRect.new()
        rune.position = Vector2(88 + index * 137, 82 if index %% 2 == 0 else 620)
        rune.size = Vector2(54, 3)
        rune.color = Color("99783d")
        add_child(rune)

func _build_content() -> void:
    var center := CenterContainer.new()
    center.set_anchors_and_offsets_preset(Control.PRESET_FULL_RECT)
    add_child(center)
    content = VBoxContainer.new()
    content.custom_minimum_size = Vector2(720, 0)
    content.alignment = BoxContainer.ALIGNMENT_CENTER
    content.add_theme_constant_override("separation", 16)
    center.add_child(content)
    if screen_id == "title": _title()
    elif screen_id == "chamber_select": _chambers()
    elif screen_id == "pause": _pause()
    else: _result()

func _title() -> void:
    content.add_child(_label("A TURN-BASED STEALTH PUZZLE", 15, Color("9bb0bd")))
    content.add_child(_label(PRODUCT_TITLE, 48, Color("e5c36d")))
    content.add_child(_label("Read the patrol. Recover the seal. Leave no trace.", 21, Color("d5e0e7")))
    var spacer := Control.new(); spacer.custom_minimum_size = Vector2(0, 22); content.add_child(spacer)
    content.add_child(_button("ENTER THE LABYRINTH", _open_chambers))
    content.add_child(_button("SETTINGS", _toggle_settings))
    content.add_child(_label("No combat. Every move is a decision.", 14, Color("718692")))

func _chambers() -> void:
    content.add_child(_label("CHOOSE A CHAMBER", 38, Color("e5c36d")))
    content.add_child(_label("Three patrol rhythms. One rule: be unseen.", 17, Color("aebfca")))
    for index in range(_session().CHAMBERS.size()):
        var chamber_id := index + 1
        var suffix: String = ["BALANCED", "SWIFT PATROL", "NO BROKEN THREAD"][index]
        content.add_child(_button(_session().CHAMBERS[index] + "   -   " + suffix, _select.bind(chamber_id)))
    content.add_child(_button("BACK", _open_title))

func _pause() -> void:
    content.add_child(_label("THREAD HELD", 42, Color("e5c36d")))
    content.add_child(_label("The chamber waits for your next decision.", 18, Color("c5d3db")))
    content.add_child(_button("RESUME", _open_gameplay))
    content.add_child(_button("SAVE AND RETURN", _open_title))

func _result() -> void:
    var result: Dictionary = _session().last_result
    var unbroken := not bool(result.get("red_thread_used", false))
    content.add_child(_label("SEAL RECOVERED", 44, Color("e5c36d")))
    content.add_child(_label("UNBROKEN THREAD" if unbroken else "THREAD RECLAIMED", 23, Color("55d4cf") if unbroken else Color("dca85c")))
    content.add_child(_label("Chamber %%d   -   %%d turns" %% [int(result.get("chamber", 1)), int(result.get("turns", 0))], 17, Color("c5d3db")))
    content.add_child(_button("NEXT CHAMBER", _open_chambers))
    content.add_child(_button("REPLAY", _open_gameplay))

func _settings_panel() -> VBoxContainer:
    var panel := VBoxContainer.new()
    panel.name = "SettingsPanel"
    panel.add_theme_constant_override("separation", 9)
    panel.add_child(_label("ACCESSIBILITY & AUDIO", 18, Color("e5c36d")))
    for key in ["high_contrast", "reduced_motion"]:
        var toggle := CheckButton.new()
        toggle.text = key.replace("_", " ").to_upper()
        toggle.button_pressed = bool(_session().settings[key])
        toggle.toggled.connect(_set_bool.bind(key))
        panel.add_child(toggle)
    var volume := HSlider.new()
    volume.min_value = 0; volume.max_value = 1; volume.step = 0.05
    volume.value = float(_session().settings.master_volume)
    volume.value_changed.connect(_set_volume)
    panel.add_child(volume)
    return panel

func _toggle_settings() -> void:
    var existing := content.get_node_or_null("SettingsPanel")
    if existing != null: existing.queue_free()
    else: content.add_child(_settings_panel())

func _set_bool(value: bool, key: String) -> void: _session().set_setting(key, value)
func _set_volume(value: float) -> void: _session().set_setting("master_volume", value)
func _select(chamber_id: int) -> void: _session().select_chamber(chamber_id); _open_gameplay()
func _open_title() -> void: get_tree().change_scene_to_file("res://scenes/title.tscn")
func _open_chambers() -> void: get_tree().change_scene_to_file("res://scenes/chamber_select.tscn")
func _open_gameplay() -> void: get_tree().change_scene_to_file("res://scenes/gameplay.tscn")

func debug_select(chamber_id: int) -> bool: return _session().select_chamber(chamber_id)
func debug_contract() -> Dictionary:
    return {"screen_id": screen_id, "product_title": PRODUCT_TITLE, "chambers": _session().CHAMBERS, "actions": {"title": ["enter", "settings"], "chamber_select": ["select_1", "select_2", "select_3", "back"], "pause": ["resume", "save_return"], "mission_result": ["next", "replay"]}.get(screen_id, [])}
''' % title


def _screen_scene(screen_id: str) -> str:
    return '''[gd_scene load_steps=2 format=3]

[ext_resource type="Script" path="res://scripts/khalinos_product_screen.gd" id="1"]

[node name="%s" type="Control"]
layout_mode = 3
anchors_preset = 15
anchor_right = 1.0
anchor_bottom = 1.0
grow_horizontal = 2
grow_vertical = 2
script = ExtResource("1")
screen_id = "%s"
''' % (screen_id.capitalize().replace("_", ""), screen_id)


def _finished_gameplay_script() -> str:
    source = _runtime_script(GodotGameplayShapingPlan())
    source = source.replace(
        "var status_label: Label\n",
        'var status_label: Label\nvar chamber_id := 1\nvar chamber_title := "I. The Silent Seal"\n',
    )
    source = source.replace(
        "func _ready() -> void:\n    _build_world()",
        'func _ready() -> void:\n    if has_node("/root/KHALINOSSession"):\n        var session := get_node("/root/KHALINOSSession")\n        chamber_id = session.selected_chamber\n        chamber_title = session.chamber_title()\n    _build_world()',
    )
    source = source.replace(
        'legend.text = "MOVE: ARROWS/WASD   WAIT: SPACE   UNDO: U   RESTART: R   RED THREAD: T"',
        'legend.text = "MOVE: ARROWS/WASD   WAIT: SPACE   UNDO: U   RESTART: R   RED THREAD: T   SAVE: F5   LOAD: F9"',
    )
    source = source.replace(
        'status_label.text = "TURN %02d   STATE: %s',
        'status_label.text = "CHAMBER %d · %s\\nTURN %02d   STATE: %s',
    ).replace(
        '% [turn, state.to_upper(), "YES" if has_seal',
        '% [chamber_id, chamber_title, turn, state.to_upper(), "YES" if has_seal',
    )
    source = source.replace(
        "guard_index = (guard_index + 1) % CONFIG.guard_patrol.size()",
        "guard_index = (guard_index + chamber_id) % CONFIG.guard_patrol.size()",
    )
    source = source.replace(
        'elif player == _point("exit_position") and has_seal:\n        state = "escaped"',
        'elif player == _point("exit_position") and has_seal:\n        state = "escaped"\n        if has_node("/root/KHALINOSSession"): get_node("/root/KHALINOSSession").record_completion(turn, red_thread_used)',
    )
    source = source.replace(
        '"guard_index": guard_index, "turn": turn, "has_seal":',
        '"guard_index": guard_index, "turn": turn, "chamber_id": chamber_id, "has_seal":',
    ).replace(
        '"vision": vision, "turn": turn, "has_seal":',
        '"vision": vision, "turn": turn, "guard_index": guard_index, "chamber_id": chamber_id, "has_seal":',
    ).replace(
        '"red_thread_used": red_thread_used, "state":',
        '"red_thread_used": red_thread_used, "red_thread_anchor": [red_thread_anchor.x, red_thread_anchor.y], "state":',
    )
    source = source.replace(
        'elif event.is_key_pressed(KEY_T): use_red_thread()',
        'elif event.is_key_pressed(KEY_T): use_red_thread()\n    elif event.is_key_pressed(KEY_F5): save_to("user://puzzle_save.json")\n    elif event.is_key_pressed(KEY_F9): load_from("user://puzzle_save.json")\n    elif event.is_key_pressed(KEY_ESCAPE): get_tree().change_scene_to_file("res://scenes/pause.tscn")',
    )
    source += '''

func save_to(path: String) -> bool:
    var file := FileAccess.open(path, FileAccess.WRITE)
    if file == null: return false
    file.store_string(JSON.stringify(debug_snapshot()))
    file.close()
    return true

func load_from(path: String) -> bool:
    var file := FileAccess.open(path, FileAccess.READ)
    if file == null: return false
    var parsed = JSON.parse_string(file.get_as_text())
    file.close()
    if not parsed is Dictionary or not parsed.has("player"): return false
    player = Vector2i(parsed.player[0], parsed.player[1])
    turn = int(parsed.get("turn", 0))
    guard_index = int(parsed.get("guard_index", 0))
    chamber_id = int(parsed.get("chamber_id", chamber_id))
    has_seal = bool(parsed.get("has_seal", false))
    has_key = bool(parsed.get("has_key", false))
    pressure_active = bool(parsed.get("pressure_active", false))
    red_thread_used = bool(parsed.get("red_thread_used", false))
    var anchor: Array = parsed.get("red_thread_anchor", [-1, -1])
    red_thread_anchor = Vector2i(anchor[0], anchor[1])
    state = str(parsed.get("state", "playing"))
    undo_stack.clear()
    _refresh_world()
    return true
'''
    return source


def _probe_script() -> str:
    return '''extends SceneTree

func _initialize() -> void: call_deferred("_probe")
func _argument(prefix: String) -> String:
    for argument in OS.get_cmdline_user_args():
        if argument.begins_with(prefix): return argument.trim_prefix(prefix)
    return ""

func _probe() -> void:
    var output := _argument("--output=")
    var errors: Array[String] = []
    var session := root.get_node_or_null("KHALINOSSession")
    if session == null:
        errors.append("session_autoload_missing")
        var failed_file := FileAccess.open(output, FileAccess.WRITE)
        if failed_file != null: failed_file.store_string(JSON.stringify({"errors": errors, "passed": false})); failed_file.close()
        quit(1)
        return
    var visited: Array[String] = []
    var screen_contracts: Dictionary = {}
    for screen_id in ["title", "chamber_select", "pause", "mission_result"]:
        var packed = load("res://scenes/" + screen_id + ".tscn")
        if packed == null:
            errors.append("unloadable:" + screen_id)
            continue
        var screen = packed.instantiate()
        root.add_child(screen)
        await process_frame
        if not screen.has_method("debug_contract"): errors.append("missing_contract:" + screen_id)
        else:
            screen_contracts[screen_id] = screen.debug_contract()
            visited.append(screen_id)
        root.remove_child(screen); screen.queue_free(); await process_frame
    if session.CHAMBERS.size() != 3: errors.append("chamber_catalog_not_three")
    session.select_chamber(2)
    var gameplay = load("res://scenes/gameplay.tscn").instantiate()
    root.add_child(gameplay); await process_frame
    var initial: Dictionary = gameplay.debug_snapshot()
    if initial.chamber_id != 2: errors.append("chamber_selection_not_bound")
    gameplay.apply_action("up")
    var saved: Dictionary = gameplay.debug_snapshot()
    var state_path := output.get_base_dir().path_join("m04_state.json")
    if not gameplay.save_to(state_path): errors.append("save_failed")
    gameplay.apply_action("up")
    if not gameplay.load_from(state_path): errors.append("load_failed")
    var loaded: Dictionary = gameplay.debug_snapshot()
    if loaded.player != saved.player or loaded.turn != saved.turn: errors.append("save_load_not_exact")
    root.remove_child(gameplay); gameplay.queue_free(); await process_frame
    var settings_path := output.get_base_dir().path_join("m04_settings.json")
    session.set_setting("high_contrast", true)
    session.set_setting("master_volume", 0.35)
    if not session.save_settings_to(settings_path): errors.append("settings_save_failed")
    session.set_setting("high_contrast", false)
    if not session.load_settings_from(settings_path): errors.append("settings_load_failed")
    var settings_ok: bool = session.settings.high_contrast and abs(float(session.settings.master_volume) - 0.35) < 0.001
    if not settings_ok: errors.append("settings_roundtrip_failed")
    var receipt := {"schema_version": "khalinos-godot-m04-probe-v1", "visited": visited, "screen_contracts": screen_contracts, "chambers": session.CHAMBERS, "selected_chamber": initial.chamber_id, "save_load": {"saved": saved, "loaded": loaded, "exact": loaded.player == saved.player and loaded.turn == saved.turn}, "settings_roundtrip": settings_ok, "errors": errors, "passed": errors.is_empty()}
    var file := FileAccess.open(output, FileAccess.WRITE)
    if file == null: quit(8); return
    file.store_string(JSON.stringify(receipt)); file.close(); quit(0 if receipt.passed else 1)
'''


def _capture_script(plan: GodotProductFinishPlan) -> str:
    regions = json.dumps(plan.capture_regions, ensure_ascii=True)
    return '''extends SceneTree

func _initialize() -> void: call_deferred("_capture")
func _argument(prefix: String) -> String:
    for argument in OS.get_cmdline_user_args():
        if argument.begins_with(prefix): return argument.trim_prefix(prefix)
    return ""

func _capture() -> void:
    var destination := _argument("--capture-dir=")
    if destination.is_empty(): quit(8); return
    DirAccess.make_dir_recursive_absolute(destination)
    var session := root.get_node_or_null("KHALINOSSession")
    if session == null: quit(7); return
    session.select_chamber(1)
    session.record_completion(12, false)
    for region in %s:
        var scene = load("res://scenes/" + region + ".tscn").instantiate()
        root.add_child(scene)
        await process_frame; await process_frame; await process_frame
        var image := root.get_texture().get_image()
        var error := image.save_png(destination.path_join(region + ".png"))
        root.remove_child(scene); scene.queue_free(); await process_frame
        if error != OK: quit(9); return
    quit(0)
''' % regions


def _release_audit_script(plan: GodotProductFinishPlan) -> str:
    regions = json.dumps(plan.capture_regions, ensure_ascii=True)
    return '''extends Node

func _ready() -> void:
    var destination := _argument("--release-audit-dir=")
    if not destination.is_empty(): call_deferred("_audit", destination)

func _argument(prefix: String) -> String:
    for argument in OS.get_cmdline_user_args():
        if argument.begins_with(prefix): return argument.trim_prefix(prefix)
    return ""

func _audit(destination: String) -> void:
    DirAccess.make_dir_recursive_absolute(destination)
    var errors: Array[String] = []
    var session := get_node_or_null("/root/KHALINOSSession")
    if session == null:
        errors.append("session_autoload_missing")
        _write_receipt(destination, {"errors": errors, "passed": false})
        get_tree().quit(1)
        return
    var visited: Array[String] = []
    var screen_contracts: Dictionary = {}
    for screen_id in ["title", "chamber_select", "pause", "mission_result"]:
        var packed = load("res://scenes/" + screen_id + ".tscn")
        if packed == null:
            errors.append("unloadable:" + screen_id)
            continue
        var screen = packed.instantiate()
        get_tree().root.add_child(screen)
        await get_tree().process_frame
        if not screen.has_method("debug_contract"): errors.append("missing_contract:" + screen_id)
        else:
            screen_contracts[screen_id] = screen.debug_contract()
            visited.append(screen_id)
        get_tree().root.remove_child(screen); screen.queue_free(); await get_tree().process_frame
    if session.CHAMBERS.size() != 3: errors.append("chamber_catalog_not_three")
    session.select_chamber(2)
    var gameplay = load("res://scenes/gameplay.tscn").instantiate()
    get_tree().root.add_child(gameplay); await get_tree().process_frame
    var initial: Dictionary = gameplay.debug_snapshot()
    if initial.chamber_id != 2: errors.append("chamber_selection_not_bound")
    gameplay.apply_action("up")
    var saved: Dictionary = gameplay.debug_snapshot()
    var state_path := destination.path_join("m04_state.json")
    if not gameplay.save_to(state_path): errors.append("save_failed")
    gameplay.apply_action("up")
    if not gameplay.load_from(state_path): errors.append("load_failed")
    var loaded: Dictionary = gameplay.debug_snapshot()
    if loaded.player != saved.player or loaded.turn != saved.turn: errors.append("save_load_not_exact")
    get_tree().root.remove_child(gameplay); gameplay.queue_free(); await get_tree().process_frame
    var settings_path := destination.path_join("m04_settings.json")
    session.set_setting("high_contrast", true)
    session.set_setting("master_volume", 0.35)
    if not session.save_settings_to(settings_path): errors.append("settings_save_failed")
    session.set_setting("high_contrast", false)
    if not session.load_settings_from(settings_path): errors.append("settings_load_failed")
    var settings_ok: bool = session.settings.high_contrast and abs(float(session.settings.master_volume) - 0.35) < 0.001
    if not settings_ok: errors.append("settings_roundtrip_failed")
    var receipt := {"schema_version": "khalinos-godot-m04-probe-v1", "visited": visited, "screen_contracts": screen_contracts, "chambers": session.CHAMBERS, "selected_chamber": initial.chamber_id, "save_load": {"saved": saved, "loaded": loaded, "exact": loaded.player == saved.player and loaded.turn == saved.turn}, "settings_roundtrip": settings_ok, "errors": errors, "passed": errors.is_empty()}
    _write_receipt(destination, receipt)
    var current := get_tree().current_scene
    if current != null:
        get_tree().root.remove_child(current); current.queue_free(); await get_tree().process_frame
    session.select_chamber(1)
    session.record_completion(12, false)
    for region in %s:
        var scene = load("res://scenes/" + region + ".tscn").instantiate()
        get_tree().root.add_child(scene)
        await get_tree().process_frame; await get_tree().process_frame; await get_tree().process_frame
        var error := get_tree().root.get_texture().get_image().save_png(destination.path_join(region + ".png"))
        get_tree().root.remove_child(scene); scene.queue_free(); await get_tree().process_frame
        if error != OK: errors.append("capture_failed:" + region)
    get_tree().quit(0 if errors.is_empty() else 1)

func _write_receipt(destination: String, receipt: Dictionary) -> void:
    var file := FileAccess.open(destination.path_join("release_probe.json"), FileAccess.WRITE)
    if file != null:
        file.store_string(JSON.stringify(receipt)); file.close()
''' % regions


def compile_godot_product_finish(plan: GodotProductFinishPlan) -> CompiledGodotGameplay:
    replacements = {
        "project.godot": _project_file(),
        "scripts/khalinos_topology_region.gd": _screen_script(plan),
        "scripts/khalinos_gameplay.gd": _finished_gameplay_script(),
        "scenes/title.tscn": _screen_scene("title"),
        "scenes/chamber_select.tscn": _screen_scene("chamber_select"),
        "scenes/pause.tscn": _screen_scene("pause"),
        "scenes/mission_result.tscn": _screen_scene("mission_result"),
    }
    additions = {
        "scripts/khalinos_session.gd": _session_script(plan),
        "scripts/khalinos_product_screen.gd": _screen_script(plan),
        "scripts/khalinos_m04_probe.gd": _probe_script(),
        "scripts/khalinos_m04_capture.gd": _capture_script(plan),
        "scripts/khalinos_release_audit.gd": _release_audit_script(plan),
        "KHALINOS_M04_PRODUCT.json": json.dumps(plan.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
    }
    replacements = {key: value.replace("\r\n", "\n") for key, value in replacements.items()}
    additions = {key: value.replace("\r\n", "\n") for key, value in additions.items()}
    plan_sha = _canonical_sha(plan)
    bundle_sha = _canonical_sha({"plan_sha256": plan_sha, "replacements": replacements, "additions": additions})
    return CompiledGodotGameplay(plan_sha256=plan_sha, bundle_sha256=bundle_sha, replacements=replacements, additions=additions)
