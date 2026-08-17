"""Deterministic Unity/Godot topology materialization bake-off.

The script intentionally generates disposable projects outside the repository. It uses
no model calls and records engine output without manually normalizing failures.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass
class Step:
    name: str
    ok: bool
    seconds: float
    command: list[str]
    returncode: int | None
    output_tail: str


def _run(name: str, command: list[str], *, cwd: Path, timeout: int) -> Step:
    started = time.perf_counter()
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,
        )
        output = result.stdout or ""
        return Step(
            name=name,
            ok=result.returncode == 0,
            seconds=round(time.perf_counter() - started, 3),
            command=command,
            returncode=result.returncode,
            output_tail=output[-2000:],
        )
    except subprocess.TimeoutExpired as error:
        output = error.stdout or ""
        if isinstance(output, bytes):
            output = output.decode("utf-8", errors="replace")
        return Step(
            name=name,
            ok=False,
            seconds=round(time.perf_counter() - started, 3),
            command=command,
            returncode=None,
            output_tail=(output + "\nTIMEOUT")[-2000:],
        )


def _write(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value.replace("\r\n", "\n"), encoding="utf-8", newline="\n")


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _tree_bytes(path: Path) -> int:
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def _gd_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=True)


def generate_godot(root: Path, plan: dict[str, object]) -> dict[str, object]:
    regions = plan["regions"]
    assert isinstance(regions, list)
    _write(
        root / "project.godot",
        """[application]
config/name="KHALINOS Engine Bakeoff - Godot"
run/main_scene="res://scenes/title.tscn"

[display]
window/size/viewport_width=960
window/size/viewport_height=540
window/size/window_width_override=960
window/size/window_height_override=540

[rendering]
renderer/rendering_method="gl_compatibility"
renderer/rendering_method.mobile="gl_compatibility"
environment/defaults/default_clear_color=Color(0.03, 0.04, 0.06, 1)
""",
    )
    route = [str(region["id"]) for region in regions]
    _write(
        root / "scripts" / "region.gd",
        """extends Control

@export var region_id: String = ""
@export var next_scene: String = ""

func _ready() -> void:
    $Margin/VBox/Region.text = region_id.to_upper().replace("_", " ")
    $Margin/VBox/Engine.text = "GODOT · TRUSTED TOPOLOGY"
    $Margin/VBox/Continue.disabled = next_scene.is_empty()
    $Margin/VBox/Continue.pressed.connect(_continue)
    call_deferred("_automate")

func _continue() -> void:
    if not next_scene.is_empty():
        get_tree().change_scene_to_file("res://scenes/" + next_scene + ".tscn")

func _argument(prefix: String) -> String:
    for argument in OS.get_cmdline_user_args():
        if argument.begins_with(prefix):
            return argument.trim_prefix(prefix)
    return ""

func _automate() -> void:
    var output := _argument("--bakeoff-output=")
    if output.is_empty():
        return
    var route := %s
    var index := route.find(region_id)
    await get_tree().process_frame
    await get_tree().process_frame
    var capture := _argument("--capture-dir=")
    if not capture.is_empty():
        DirAccess.make_dir_recursive_absolute(capture)
        var image := get_viewport().get_texture().get_image()
        image.save_png(capture.path_join(region_id + ".png"))
    var state := {"engine": "godot", "visited": route.slice(0, index + 1), "current": region_id}
    var file := FileAccess.open(output, FileAccess.WRITE)
    file.store_string(JSON.stringify(state))
    file.close()
    if index + 1 < route.size():
        get_tree().change_scene_to_file("res://scenes/" + route[index + 1] + ".tscn")
    else:
        get_tree().quit(0)
""" % json.dumps(route),
    )
    for region in regions:
        region_id = str(region["id"])
        next_scene = str(region["next"] or "")
        color = str(region["color"])
        rgb = tuple(int(color[index:index + 2], 16) / 255 for index in (0, 2, 4))
        _write(
            root / "scenes" / f"{region_id}.tscn",
            """[gd_scene load_steps=2 format=3]

[ext_resource type="Script" path="res://scripts/region.gd" id="1"]

[node name="Region" type="Control"]
layout_mode = 3
anchors_preset = 15
anchor_right = 1.0
anchor_bottom = 1.0
grow_horizontal = 2
grow_vertical = 2
script = ExtResource("1")
region_id = %s
next_scene = %s

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
offset_top = 80.0
offset_right = -80.0
offset_bottom = -80.0
grow_horizontal = 2
grow_vertical = 2

[node name="VBox" type="VBoxContainer" parent="Margin"]
layout_mode = 2
theme_override_constants/separation = 22
alignment = 1

[node name="Engine" type="Label" parent="Margin/VBox"]
unique_name_in_owner = true
layout_mode = 2
theme_override_colors/font_color = Color(0.75, 0.82, 0.86, 1)
theme_override_font_sizes/font_size = 18
text = "ENGINE"
horizontal_alignment = 1

[node name="Region" type="Label" parent="Margin/VBox"]
unique_name_in_owner = true
layout_mode = 2
theme_override_colors/font_color = Color(1, 0.88, 0.48, 1)
theme_override_font_sizes/font_size = 52
text = "REGION"
horizontal_alignment = 1

[node name="Continue" type="Button" parent="Margin/VBox"]
unique_name_in_owner = true
custom_minimum_size = Vector2(0, 64)
layout_mode = 2
text = "Continue"
""" % (
                _gd_string(region_id),
                _gd_string(next_scene),
                *rgb,
            ),
        )
    _write(
        root / "export_presets.cfg",
        """[preset.0]

name="Windows Desktop"
platform="Windows Desktop"
runnable=true
advanced_options=false
dedicated_server=false
custom_features=""
export_filter="all_resources"
include_filter=""
exclude_filter=""
export_path="build/godot-bakeoff.exe"
script_export_mode=2

[preset.0.options]

binary_format/architecture="x86_64"
binary_format/embed_pck=true
texture_format/s3tc_bptc=true
texture_format/etc2_astc=false
""",
    )
    (root / "build").mkdir(parents=True, exist_ok=True)
    files = [item for item in root.rglob("*") if item.is_file()]
    return {"source_files": len(files), "source_bytes": sum(item.stat().st_size for item in files)}


def generate_unity(root: Path, plan: dict[str, object]) -> dict[str, object]:
    regions = plan["regions"]
    assert isinstance(regions, list)
    ids = [str(region["id"]) for region in regions]
    labels = [str(region["label"]) for region in regions]
    colors = [str(region["color"]) for region in regions]
    _write(root / "ProjectSettings" / "ProjectVersion.txt", "m_EditorVersion: 6000.3.11f1\n")
    capability_modules = {
        "frame_capture": [
            "com.unity.modules.imageconversion",
            "com.unity.modules.screencapture",
        ],
        "immediate_ui": ["com.unity.modules.imgui"],
        "runtime_receipt": ["com.unity.modules.jsonserialize"],
    }
    required_modules = sorted({
        module
        for modules in capability_modules.values()
        for module in modules
    })
    _write(
        root / "Packages" / "manifest.json",
        json.dumps({
            "dependencies": {module: "1.0.0" for module in required_modules},
        }, indent=2) + "\n",
    )
    _write(
        root / "KHALINOS_CAPABILITIES.json",
        json.dumps(capability_modules, indent=2) + "\n",
    )
    _write(
        root / "Assets" / "Scripts" / "RegionController.cs",
        """using System;
using System.Collections;
using System.Collections.Generic;
using System.IO;
using UnityEngine;
using UnityEngine.SceneManagement;

public sealed class RegionController : MonoBehaviour
{
    public string RegionId = "";
    public string NextScene = "";
    private static readonly List<string> Visited = new List<string>();

    private void Start()
    {
        if (!Visited.Contains(RegionId)) Visited.Add(RegionId);
        if (Argument("--bakeoff-output=") is string output && output.Length > 0)
            StartCoroutine(Automate(output));
    }

    private static string Argument(string prefix)
    {
        foreach (var value in Environment.GetCommandLineArgs())
            if (value.StartsWith(prefix, StringComparison.Ordinal)) return value.Substring(prefix.Length);
        return "";
    }

    private IEnumerator Automate(string output)
    {
        yield return null;
        yield return new WaitForEndOfFrame();
        var capture = Argument("--capture-dir=");
        if (capture.Length > 0)
        {
            Directory.CreateDirectory(capture);
            ScreenCapture.CaptureScreenshot(Path.Combine(capture, RegionId + ".png"));
            yield return new WaitForEndOfFrame();
            yield return new WaitForSeconds(0.15f);
        }
        File.WriteAllText(output, JsonUtility.ToJson(new Receipt {
            engine = "unity", current = RegionId, visited = Visited.ToArray()
        }));
        if (NextScene.Length > 0) SceneManager.LoadScene(NextScene);
        else Application.Quit(0);
    }

    private void OnGUI()
    {
        var title = new GUIStyle(GUI.skin.label) { fontSize = 44, alignment = TextAnchor.MiddleCenter };
        title.normal.textColor = new Color(1f, 0.88f, 0.48f);
        GUI.Label(new Rect(80, 150, Screen.width - 160, 100), RegionId.ToUpperInvariant().Replace('_', ' '), title);
        var engine = new GUIStyle(GUI.skin.label) { fontSize = 18, alignment = TextAnchor.MiddleCenter };
        engine.normal.textColor = new Color(0.75f, 0.82f, 0.86f);
        GUI.Label(new Rect(80, 110, Screen.width - 160, 40), "UNITY · TRUSTED TOPOLOGY", engine);
        GUI.enabled = NextScene.Length > 0;
        if (GUI.Button(new Rect(Screen.width / 2 - 130, 280, 260, 64), "Continue"))
            SceneManager.LoadScene(NextScene);
        GUI.enabled = true;
    }

    [Serializable]
    private sealed class Receipt { public string engine; public string current; public string[] visited; }
}
""",
    )
    _write(
        root / "Assets" / "Editor" / "BakeoffBuilder.cs",
        """using System;
using System.IO;
using UnityEditor;
using UnityEditor.Build.Reporting;
using UnityEditor.SceneManagement;
using UnityEngine;

public static class BakeoffBuilder
{
    private static readonly string[] Ids = %s;
    private static readonly string[] Labels = %s;
    private static readonly string[] Colors = %s;

    public static void BuildAll()
    {
        Directory.CreateDirectory("Assets/Scenes");
        var paths = new string[Ids.Length];
        for (var index = 0; index < Ids.Length; index++)
        {
            var scene = EditorSceneManager.NewScene(NewSceneSetup.EmptyScene, NewSceneMode.Single);
            var cameraObject = new GameObject("Main Camera");
            var camera = cameraObject.AddComponent<Camera>();
            camera.clearFlags = CameraClearFlags.SolidColor;
            ColorUtility.TryParseHtmlString("#" + Colors[index], out var background);
            camera.backgroundColor = background;
            cameraObject.tag = "MainCamera";
            var root = new GameObject("RegionRoot_" + Labels[index].Replace(" ", ""));
            var controller = root.AddComponent<RegionController>();
            controller.RegionId = Ids[index];
            controller.NextScene = index + 1 < Ids.Length ? Ids[index + 1] : "";
            paths[index] = "Assets/Scenes/" + Ids[index] + ".unity";
            EditorSceneManager.SaveScene(scene, paths[index]);
        }
        var output = Environment.GetEnvironmentVariable("KHALINOS_BAKEOFF_BUILD");
        if (string.IsNullOrWhiteSpace(output)) output = Path.GetFullPath("build/unity-bakeoff.exe");
        Directory.CreateDirectory(Path.GetDirectoryName(output));
        var report = BuildPipeline.BuildPlayer(paths, output, BuildTarget.StandaloneWindows64, BuildOptions.None);
        if (report.summary.result != BuildResult.Succeeded)
            throw new InvalidOperationException("Unity build failed: " + report.summary.result);
        File.WriteAllText(Path.Combine(Path.GetDirectoryName(output), "build-summary.json"),
            "{\\\"engine\\\":\\\"unity\\\",\\\"scenes\\\":" + paths.Length + ",\\\"bytes\\\":" + report.summary.totalSize + "}");
    }
}
""" % (
            "new[] { " + ", ".join(json.dumps(value) for value in ids) + " }",
            "new[] { " + ", ".join(json.dumps(value) for value in labels) + " }",
            "new[] { " + ", ".join(json.dumps(value) for value in colors) + " }",
        ),
    )
    files = [item for item in root.rglob("*") if item.is_file()]
    return {"source_files": len(files), "source_bytes": sum(item.stat().st_size for item in files)}


def _receipt_ok(path: Path, expected: list[str]) -> bool:
    if not path.is_file():
        return False
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return value.get("visited") == expected and value.get("current") == expected[-1]


def _pngs_ok(root: Path, expected: list[str]) -> bool:
    return all((root / f"{region}.png").stat().st_size > 100 for region in expected)


def _markdown(report: dict[str, object]) -> str:
    lines = [
        "# KHALINOS Engine Bake-off Result",
        "",
        f"Input SHA-256: `{report['input_sha256']}`",
        "",
        "| Engine | Materialized scenes | Runtime route | PNG capture | Windows export | Build bytes | Total measured seconds |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for engine in ("unity", "godot"):
        value = report[engine]
        lines.append(
            f"| {engine.title()} | {'PASS' if value['scene_count_ok'] else 'FAIL'} | "
            f"{'PASS' if value['route_ok'] else 'FAIL'} | {'PASS' if value['capture_ok'] else 'FAIL'} | "
            f"{'PASS' if value['export_ok'] else 'FAIL'} | {value['export_bytes']} | "
            f"{value['total_seconds']:.3f} |"
        )
    lines.extend([
        "",
        "## Raw steps",
        "",
        "The JSON report beside this file preserves exact commands, return codes, durations, and output tails.",
        "",
    ])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--godot", type=Path, required=True)
    parser.add_argument("--unity", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--plan", type=Path, default=Path(__file__).with_name("topology_plan.json"))
    parser.add_argument("--report", type=Path, default=Path(__file__).with_name("result.json"))
    args = parser.parse_args()
    plan = json.loads(args.plan.read_text(encoding="utf-8"))
    route = [str(region["id"]) for region in plan["regions"]]
    workspace = args.workspace.resolve()
    if "khalinos-engine-bakeoff" not in {part.casefold() for part in workspace.parts}:
        raise ValueError("workspace must remain inside a khalinos-engine-bakeoff directory")
    if workspace.parent == workspace or len(workspace.parts) < 4:
        raise ValueError("workspace path is too broad for disposable generation")
    if workspace.exists():
        shutil.rmtree(workspace)
    workspace.mkdir(parents=True)
    args.workspace = workspace
    godot_root = args.workspace / "godot"
    unity_root = args.workspace / "unity"
    generated_godot = generate_godot(godot_root, plan)
    generated_unity = generate_unity(unity_root, plan)

    godot_receipt = args.workspace / "godot-receipt.json"
    godot_capture = args.workspace / "godot-captures"
    godot_steps = [
        _run("import_and_route", [str(args.godot), "--language", "en", "--headless", "--path", str(godot_root), "--", f"--bakeoff-output={godot_receipt}"], cwd=godot_root, timeout=120),
        _run("capture", [str(args.godot), "--language", "en", "--path", str(godot_root), "--position", "-10000,-10000", "--", f"--bakeoff-output={godot_receipt}", f"--capture-dir={godot_capture}"], cwd=godot_root, timeout=120),
        _run("export", [str(args.godot), "--language", "en", "--headless", "--path", str(godot_root), "--export-release", "Windows Desktop"], cwd=godot_root, timeout=300),
    ]

    unity_build = unity_root / "build" / "unity-bakeoff.exe"
    unity_log = args.workspace / "unity-editor.log"
    unity_env = dict(**__import__("os").environ)
    unity_env["KHALINOS_BAKEOFF_BUILD"] = str(unity_build)
    started = time.perf_counter()
    try:
        result = subprocess.run(
            [str(args.unity), "-batchmode", "-quit", "-projectPath", str(unity_root), "-executeMethod", "BakeoffBuilder.BuildAll", "-logFile", str(unity_log)],
            cwd=unity_root,
            env=unity_env,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=600,
            check=False,
        )
        unity_build_step = Step("materialize_and_export", result.returncode == 0, round(time.perf_counter() - started, 3), [str(args.unity), "-batchmode", "-quit", "-projectPath", str(unity_root), "-executeMethod", "BakeoffBuilder.BuildAll"], result.returncode, ((result.stdout or "") + (unity_log.read_text(encoding="utf-8", errors="replace") if unity_log.is_file() else ""))[-2000:])
    except subprocess.TimeoutExpired as error:
        unity_build_step = Step("materialize_and_export", False, round(time.perf_counter() - started, 3), [str(args.unity), "-batchmode", "-quit"], None, "TIMEOUT")
    unity_receipt = args.workspace / "unity-receipt.json"
    unity_capture = args.workspace / "unity-captures"
    unity_steps = [unity_build_step]
    if unity_build.is_file():
        unity_steps.extend([
            _run("route", [str(unity_build), "-batchmode", "-nographics", f"--bakeoff-output={unity_receipt}"], cwd=unity_build.parent, timeout=120),
            _run("capture", [str(unity_build), "-screen-width", "960", "-screen-height", "540", "-popupwindow", f"--bakeoff-output={unity_receipt}", f"--capture-dir={unity_capture}"], cwd=unity_build.parent, timeout=120),
        ])

    godot_exe = godot_root / "build" / "godot-bakeoff.exe"
    report = {
        "schema_version": "khalinos-engine-bakeoff-result-v1",
        "input_sha256": _digest(args.plan),
        "route": route,
        "unity": {
            **generated_unity,
            "scene_count_ok": len(list((unity_root / "Assets" / "Scenes").glob("*.unity"))) == len(route),
            "route_ok": _receipt_ok(unity_receipt, route),
            "capture_ok": _pngs_ok(unity_capture, route) if unity_capture.is_dir() else False,
            "export_ok": unity_build.is_file() and unity_build.stat().st_size > 0,
            "export_bytes": _tree_bytes(unity_build.parent) if unity_build.is_file() else 0,
            "total_seconds": round(sum(step.seconds for step in unity_steps), 3),
            "steps": [asdict(step) for step in unity_steps],
        },
        "godot": {
            **generated_godot,
            "scene_count_ok": len(list((godot_root / "scenes").glob("*.tscn"))) == len(route),
            "route_ok": _receipt_ok(godot_receipt, route),
            "capture_ok": _pngs_ok(godot_capture, route) if godot_capture.is_dir() else False,
            "export_ok": godot_exe.is_file() and godot_exe.stat().st_size > 0,
            "export_bytes": _tree_bytes(godot_exe.parent) if godot_exe.is_file() else 0,
            "total_seconds": round(sum(step.seconds for step in godot_steps), 3),
            "steps": [asdict(step) for step in godot_steps],
        },
    }
    args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    args.report.with_suffix(".md").write_text(_markdown(report), encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
