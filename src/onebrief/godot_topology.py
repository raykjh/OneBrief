"""Trusted Godot topology compiler for bounded greenfield game Quests."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from enum import StrEnum
from pathlib import Path, PurePosixPath
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator


class GodotRegionKind(StrEnum):
    SCREEN = "screen"
    OVERLAY = "overlay"


class GodotRegion(BaseModel):
    region_id: str = Field(pattern=r"^[a-z][a-z0-9_]{1,47}$")
    label: str = Field(min_length=1, max_length=80)
    kind: GodotRegionKind = GodotRegionKind.SCREEN
    transitions: list[str] = Field(default_factory=list, max_length=8)
    background_hex: str = Field(default="172033", pattern=r"^[A-Fa-f0-9]{6}$")


class GodotTopologyPlan(BaseModel):
    schema_version: str = "khalinos-godot-topology-plan-v1"
    project_name: str = Field(min_length=2, max_length=80)
    initial_region: str = Field(pattern=r"^[a-z][a-z0-9_]{1,47}$")
    regions: list[GodotRegion] = Field(min_length=2, max_length=16)
    viewport_width: int = Field(default=1280, ge=640, le=3840)
    viewport_height: int = Field(default=720, ge=360, le=2160)

    @model_validator(mode="after")
    def valid_connected_topology(self) -> "GodotTopologyPlan":
        identifiers = [region.region_id for region in self.regions]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("Godot topology region IDs must be unique")
        known = set(identifiers)
        if self.initial_region not in known:
            raise ValueError("Godot topology initial region is not declared")
        for region in self.regions:
            unknown = set(region.transitions) - known
            if unknown:
                raise ValueError(
                    f"Godot topology transition references unknown regions: {sorted(unknown)}"
                )
            if region.region_id in region.transitions:
                raise ValueError("Godot topology region cannot transition to itself")
        reachable: set[str] = set()
        pending = [self.initial_region]
        by_id = {region.region_id: region for region in self.regions}
        while pending:
            current = pending.pop()
            if current in reachable:
                continue
            reachable.add(current)
            pending.extend(by_id[current].transitions)
        missing = known - reachable
        if missing:
            raise ValueError(
                f"Godot topology contains unreachable regions: {sorted(missing)}"
            )
        return self


class CompiledGodotTopology(BaseModel):
    schema_version: str = "khalinos-compiled-godot-topology-v1"
    engine_family: str = "godot"
    engine_version: str = "4.7"
    plan_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    bundle_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    initial_scene: str
    region_scene_paths: dict[str, str] = Field(min_length=2, max_length=16)
    files: dict[str, str] = Field(min_length=5, max_length=24)
    verification_command: list[str] = Field(min_length=4, max_length=12)


class GodotTopologyMaterializationReceipt(BaseModel):
    schema_version: str = "khalinos-godot-topology-materialization-v1"
    destination: str
    bundle_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    written_paths: list[str] = Field(min_length=5, max_length=24)


def _canonical_sha256(value: object) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json", exclude_none=True)
    return hashlib.sha256(json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


def _gd(value: str) -> str:
    return json.dumps(value, ensure_ascii=True)


def _rgb(value: str) -> tuple[float, float, float]:
    return tuple(int(value[index:index + 2], 16) / 255 for index in (0, 2, 4))


def _safe_project_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9 _.-]", "", value).strip()
    return cleaned or "KHALINOS Project"


def _region_script() -> str:
    return """extends Control

@export var region_id: String = ""
@export var region_kind: String = "screen"
@export var transitions: PackedStringArray = []

func _ready() -> void:
    $Margin/VBox/Region.text = region_id.to_upper().replace("_", " ")
    $Margin/VBox/Kind.text = region_kind.to_upper()
    for target in transitions:
        var button := Button.new()
        button.text = "Open " + target.replace("_", " ").capitalize()
        button.custom_minimum_size = Vector2(0, 56)
        button.pressed.connect(_open.bind(target))
        $Margin/VBox/Actions.add_child(button)

func _open(target: String) -> void:
    get_tree().change_scene_to_file("res://scenes/" + target + ".tscn")
"""


def _probe_script() -> str:
    return """extends SceneTree

func _initialize() -> void:
    call_deferred("_probe")

func _argument(prefix: String) -> String:
    for argument in OS.get_cmdline_user_args():
        if argument.begins_with(prefix):
            return argument.trim_prefix(prefix)
    return ""

func _probe() -> void:
    var output := _argument("--output=")
    if output.is_empty():
        push_error("KHALINOS topology probe requires --output=<receipt.json>")
        quit(2)
        return
    var manifest_file := FileAccess.open("res://KHALINOS_TOPOLOGY.json", FileAccess.READ)
    if manifest_file == null:
        push_error("KHALINOS topology manifest is missing")
        quit(3)
        return
    var manifest = JSON.parse_string(manifest_file.get_as_text())
    var visited: Array[String] = []
    var errors: Array[String] = []
    for region in manifest.regions:
        var packed = load(region.scene_path)
        if packed == null:
            errors.append("unloadable:" + region.region_id)
            continue
        var instance = packed.instantiate()
        if instance == null or instance.get("region_id") != region.region_id:
            errors.append("identity_mismatch:" + region.region_id)
        else:
            visited.append(region.region_id)
        if instance != null:
            instance.free()
    var receipt := {
        "schema_version": "khalinos-godot-topology-probe-v1",
        "visited": visited,
        "errors": errors,
        "passed": errors.is_empty() and visited.size() == manifest.regions.size()
    }
    var receipt_file := FileAccess.open(output, FileAccess.WRITE)
    receipt_file.store_string(JSON.stringify(receipt))
    receipt_file.close()
    quit(0 if receipt.passed else 1)
"""


def _scene(region: GodotRegion) -> str:
    red, green, blue = _rgb(region.background_hex)
    transitions = "PackedStringArray(" + ", ".join(
        _gd(target) for target in region.transitions
    ) + ")"
    return """[gd_scene load_steps=2 format=3]

[ext_resource type="Script" path="res://scripts/khalinos_topology_region.gd" id="1"]

[node name="Region" type="Control"]
layout_mode = 3
anchors_preset = 15
anchor_right = 1.0
anchor_bottom = 1.0
grow_horizontal = 2
grow_vertical = 2
script = ExtResource("1")
region_id = %s
region_kind = %s
transitions = %s

[node name="Background" type="ColorRect" parent="."]
layout_mode = 1
anchors_preset = 15
anchor_right = 1.0
anchor_bottom = 1.0
grow_horizontal = 2
grow_vertical = 2
color = Color(%.6f, %.6f, %.6f, 1)

[node name="Margin" type="MarginContainer" parent="."]
layout_mode = 1
anchors_preset = 15
anchor_right = 1.0
anchor_bottom = 1.0
offset_left = 80.0
offset_top = 70.0
offset_right = -80.0
offset_bottom = -70.0
grow_horizontal = 2
grow_vertical = 2

[node name="VBox" type="VBoxContainer" parent="Margin"]
layout_mode = 2
theme_override_constants/separation = 18
alignment = 1

[node name="Kind" type="Label" parent="Margin/VBox"]
layout_mode = 2
theme_override_colors/font_color = Color(0.75, 0.82, 0.86, 1)
theme_override_font_sizes/font_size = 16
text = %s
horizontal_alignment = 1

[node name="Region" type="Label" parent="Margin/VBox"]
layout_mode = 2
theme_override_colors/font_color = Color(1, 0.88, 0.48, 1)
theme_override_font_sizes/font_size = 46
text = %s
horizontal_alignment = 1

[node name="Actions" type="VBoxContainer" parent="Margin/VBox"]
layout_mode = 2
theme_override_constants/separation = 10
""" % (
        _gd(region.region_id),
        _gd(region.kind.value),
        transitions,
        red,
        green,
        blue,
        _gd(region.kind.value.upper()),
        _gd(region.label),
    )


def compile_godot_topology(plan: GodotTopologyPlan) -> CompiledGodotTopology:
    scene_paths = {
        region.region_id: f"scenes/{region.region_id}.tscn"
        for region in plan.regions
    }
    topology_manifest = {
        "schema_version": "khalinos-godot-runtime-topology-v1",
        "initial_region": plan.initial_region,
        "regions": [
            {
                "region_id": region.region_id,
                "kind": region.kind.value,
                "scene_path": "res://" + scene_paths[region.region_id],
                "transitions": region.transitions,
            }
            for region in plan.regions
        ],
    }
    files = {
        "project.godot": """[application]
config/name=%s
run/main_scene=%s

[display]
window/size/viewport_width=%d
window/size/viewport_height=%d
window/size/window_width_override=%d
window/size/window_height_override=%d

[rendering]
renderer/rendering_method="gl_compatibility"
renderer/rendering_method.mobile="gl_compatibility"
environment/defaults/default_clear_color=Color(0.03, 0.04, 0.06, 1)
""" % (
            _gd(_safe_project_name(plan.project_name)),
            _gd("res://" + scene_paths[plan.initial_region]),
            plan.viewport_width,
            plan.viewport_height,
            plan.viewport_width,
            plan.viewport_height,
        ),
        "scripts/khalinos_topology_region.gd": _region_script(),
        "scripts/khalinos_topology_probe.gd": _probe_script(),
        "KHALINOS_TOPOLOGY.json": json.dumps(
            topology_manifest, indent=2, ensure_ascii=False
        ) + "\n",
        **{
            scene_paths[region.region_id]: _scene(region)
            for region in plan.regions
        },
    }
    files = {path: content.replace("\r\n", "\n") for path, content in files.items()}
    plan_sha = _canonical_sha256(plan)
    bundle_sha = _canonical_sha256({
        "plan_sha256": plan_sha,
        "files": files,
    })
    return CompiledGodotTopology(
        plan_sha256=plan_sha,
        bundle_sha256=bundle_sha,
        initial_scene=scene_paths[plan.initial_region],
        region_scene_paths=scene_paths,
        files=files,
        verification_command=[
            "godot",
            "--headless",
            "--path",
            ".",
            "--script",
            "res://scripts/khalinos_topology_probe.gd",
            "--",
            "--output=<authorized-receipt-path>",
        ],
    )


def materialize_godot_topology(
    bundle: CompiledGodotTopology,
    destination: Path,
) -> GodotTopologyMaterializationReceipt:
    """Write one new topology bundle without overwriting existing project bytes."""

    root = destination.resolve()
    root.mkdir(parents=True, exist_ok=True)
    normalized: dict[str, str] = {}
    for raw_path, content in bundle.files.items():
        relative = PurePosixPath(raw_path.replace("\\", "/"))
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("compiled Godot topology contains an unsafe path")
        path = relative.as_posix()
        target = (root / Path(*relative.parts)).resolve()
        if not target.is_relative_to(root):
            raise ValueError("compiled Godot topology escapes the destination")
        if target.exists() and target.read_text(encoding="utf-8") != content:
            raise FileExistsError(f"trusted topology refuses to overwrite {path}")
        normalized[path] = content
    if _canonical_sha256({
        "plan_sha256": bundle.plan_sha256,
        "files": normalized,
    }) != bundle.bundle_sha256:
        raise ValueError("compiled Godot topology bundle digest changed")

    staging = root / f".khalinos-topology-{uuid4().hex}"
    written: list[Path] = []
    try:
        for relative, content in normalized.items():
            stage_path = staging / Path(*PurePosixPath(relative).parts)
            stage_path.parent.mkdir(parents=True, exist_ok=True)
            stage_path.write_text(content, encoding="utf-8", newline="\n")
        for relative, content in normalized.items():
            target = root / Path(*PurePosixPath(relative).parts)
            if target.exists():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(
                staging / Path(*PurePosixPath(relative).parts),
                target,
            )
            written.append(target)
    except Exception:
        for target in reversed(written):
            target.unlink(missing_ok=True)
        raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return GodotTopologyMaterializationReceipt(
        destination=str(root),
        bundle_sha256=bundle.bundle_sha256,
        written_paths=sorted(normalized),
    )
