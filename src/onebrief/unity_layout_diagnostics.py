"""Fixed Unity Canvas/RectTransform diagnostic capability.

The injected Editor source exists only inside a disposable clone.  It observes
scene hierarchy and serialized layout values, writes bounded evidence, and is
never part of the product patch.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from collections import Counter
from pathlib import Path

from pydantic import BaseModel, Field

from onebrief.capability_packs import (
    BUILTIN_CAPABILITY_PACKS,
    CapabilityPackId,
)


UNITY_LAYOUT_DIAGNOSTIC_SOURCE = r'''#if UNITY_EDITOR
using System;
using System.Collections.Generic;
using System.IO;
using UnityEditor;
using UnityEditor.SceneManagement;
using UnityEngine;
using UnityEngine.SceneManagement;
using UnityEngine.UI;

namespace OneBriefDiagnostics
{
    [Serializable]
    public sealed class LayoutRectRecord
    {
        public string scene_path;
        public string canvas_path;
        public string hierarchy_path;
        public bool active;
        public float[] anchor_min;
        public float[] anchor_max;
        public float[] pivot;
        public float[] size_delta;
        public float[] anchored_position;
        public float[] rect_size;
        public string[] component_types;
        public string[] risk_flags;
    }

    [Serializable]
    public sealed class LayoutCanvasRecord
    {
        public string scene_path;
        public string hierarchy_path;
        public string render_mode;
        public bool has_canvas_scaler;
        public string scale_mode;
        public string screen_match_mode;
        public float[] reference_resolution;
        public string[] risk_flags;
    }

    [Serializable]
    public sealed class LayoutReport
    {
        public string schema_version = "onebrief-unity-layout-raw-v1";
        public List<string> scene_paths = new List<string>();
        public List<LayoutCanvasRecord> canvases = new List<LayoutCanvasRecord>();
        public List<LayoutRectRecord> rect_transforms = new List<LayoutRectRecord>();
        public List<string> risks = new List<string>();
        public bool truncated;
    }

    public static class LayoutDiagnostics
    {
        private const int MaxScenes = 12;
        private const int MaxRects = 800;

        public static void Export()
        {
            var report = new LayoutReport();
            var scenes = DiscoverScenes();
            foreach (var scenePath in scenes)
            {
                if (report.rect_transforms.Count >= MaxRects)
                {
                    report.truncated = true;
                    break;
                }
                var scene = EditorSceneManager.OpenScene(scenePath, OpenSceneMode.Single);
                report.scene_paths.Add(scenePath);
                foreach (var root in scene.GetRootGameObjects())
                {
                    foreach (var canvas in root.GetComponentsInChildren<Canvas>(true))
                    {
                        InspectCanvas(scenePath, canvas, report);
                        if (report.rect_transforms.Count >= MaxRects)
                        {
                            report.truncated = true;
                            break;
                        }
                    }
                    if (report.truncated) break;
                }
            }
            var evidence = Path.Combine(Directory.GetCurrentDirectory(), "onebrief-evidence");
            Directory.CreateDirectory(evidence);
            var target = Path.Combine(evidence, "unity-layout-diagnostics.json");
            File.WriteAllText(target, JsonUtility.ToJson(report, true));
            Debug.Log("OneBrief layout diagnostics wrote " + target);
        }

        private static List<string> DiscoverScenes()
        {
            var result = new List<string>();
            foreach (var scene in EditorBuildSettings.scenes)
            {
                if (scene.enabled && scene.path.StartsWith("Assets/") && !result.Contains(scene.path))
                    result.Add(scene.path);
            }
            foreach (var guid in AssetDatabase.FindAssets("t:Scene", new[] { "Assets" }))
            {
                var path = AssetDatabase.GUIDToAssetPath(guid);
                if (!String.IsNullOrEmpty(path) && !result.Contains(path)) result.Add(path);
                if (result.Count >= MaxScenes) break;
            }
            result.Sort(StringComparer.Ordinal);
            if (result.Count > MaxScenes) result.RemoveRange(MaxScenes, result.Count - MaxScenes);
            return result;
        }

        private static void InspectCanvas(string scenePath, Canvas canvas, LayoutReport report)
        {
            var canvasPath = HierarchyPath(canvas.transform);
            var scaler = canvas.GetComponent<CanvasScaler>();
            var canvasRisks = new List<string>();
            if (canvas.renderMode != RenderMode.WorldSpace && scaler == null)
                canvasRisks.Add("screen_space_canvas_missing_scaler");
            if (scaler != null && scaler.uiScaleMode == CanvasScaler.ScaleMode.ConstantPixelSize)
                canvasRisks.Add("constant_pixel_size_on_screen_space_canvas");
            if (scaler != null && scaler.uiScaleMode == CanvasScaler.ScaleMode.ScaleWithScreenSize &&
                (scaler.referenceResolution.x <= 0f || scaler.referenceResolution.y <= 0f))
                canvasRisks.Add("invalid_reference_resolution");
            var canvasRecord = new LayoutCanvasRecord
            {
                scene_path = scenePath,
                hierarchy_path = canvasPath,
                render_mode = canvas.renderMode.ToString(),
                has_canvas_scaler = scaler != null,
                scale_mode = scaler == null ? "none" : scaler.uiScaleMode.ToString(),
                screen_match_mode = scaler == null ? "none" : scaler.screenMatchMode.ToString(),
                reference_resolution = scaler == null ? Pair(0f, 0f) : Pair(scaler.referenceResolution),
                risk_flags = canvasRisks.ToArray(),
            };
            report.canvases.Add(canvasRecord);
            AddRisks(report, scenePath, canvasPath, canvasRisks);

            var reference = scaler == null ? new Vector2(0f, 0f) : scaler.referenceResolution;
            foreach (var rect in canvas.GetComponentsInChildren<RectTransform>(true))
            {
                if (report.rect_transforms.Count >= MaxRects) return;
                var risks = RectRisks(rect, reference);
                var components = rect.GetComponents<Component>();
                var componentNames = new List<string>();
                foreach (var component in components)
                {
                    if (component == null) continue;
                    componentNames.Add(component.GetType().Name);
                    if (componentNames.Count >= 12) break;
                }
                var path = HierarchyPath(rect);
                report.rect_transforms.Add(new LayoutRectRecord
                {
                    scene_path = scenePath,
                    canvas_path = canvasPath,
                    hierarchy_path = path,
                    active = rect.gameObject.activeInHierarchy,
                    anchor_min = Pair(rect.anchorMin),
                    anchor_max = Pair(rect.anchorMax),
                    pivot = Pair(rect.pivot),
                    size_delta = Pair(rect.sizeDelta),
                    anchored_position = Pair(rect.anchoredPosition),
                    rect_size = Pair(rect.rect.size),
                    component_types = componentNames.ToArray(),
                    risk_flags = risks.ToArray(),
                });
                AddRisks(report, scenePath, path, risks);
            }
        }

        private static List<string> RectRisks(RectTransform rect, Vector2 reference)
        {
            var risks = new List<string>();
            var stretchX = rect.anchorMax.x - rect.anchorMin.x > 0.001f;
            var stretchY = rect.anchorMax.y - rect.anchorMin.y > 0.001f;
            if (stretchX && rect.sizeDelta.x > 1f) risks.Add("stretched_width_positive_delta");
            if (stretchY && rect.sizeDelta.y > 1f) risks.Add("stretched_height_positive_delta");
            if (!stretchX && reference.x > 0f && rect.sizeDelta.x > reference.x)
                risks.Add("fixed_width_exceeds_reference");
            if (!stretchY && reference.y > 0f && rect.sizeDelta.y > reference.y)
                risks.Add("fixed_height_exceeds_reference");
            if (!stretchX && reference.x > 0f &&
                Mathf.Abs(rect.anchoredPosition.x) + Mathf.Abs(rect.sizeDelta.x) * 0.5f > reference.x)
                risks.Add("fixed_rect_outside_reference_width");
            if (!stretchY && reference.y > 0f &&
                Mathf.Abs(rect.anchoredPosition.y) + Mathf.Abs(rect.sizeDelta.y) * 0.5f > reference.y)
                risks.Add("fixed_rect_outside_reference_height");
            return risks;
        }

        private static void AddRisks(LayoutReport report, string scene, string path, List<string> risks)
        {
            foreach (var risk in risks)
            {
                if (report.risks.Count >= 240) return;
                report.risks.Add(scene + "|" + path + "|" + risk);
            }
        }

        private static float[] Pair(Vector2 value) { return Pair(value.x, value.y); }
        private static float[] Pair(float x, float y) { return new[] { x, y }; }

        private static string HierarchyPath(Transform current)
        {
            var names = new List<string>();
            while (current != null)
            {
                names.Add(current.name);
                current = current.parent;
            }
            names.Reverse();
            return String.Join("/", names.ToArray());
        }
    }
}
#endif
'''


class UnityLayoutDiagnosticsSummary(BaseModel):
    schema_version: str = "onebrief-unity-layout-diagnostics-v1"
    capability_pack_id: str = CapabilityPackId.UNITY_LAYOUT_DIAGNOSTICS.value
    capability_pack_version: str
    source_revision: str = Field(pattern=r"^[a-f0-9]{40}$")
    candidate_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    raw_report_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    scene_count: int = Field(ge=0, le=12)
    canvas_count: int = Field(ge=0, le=200)
    rect_transform_count: int = Field(ge=0, le=800)
    risk_counts: dict[str, int] = Field(default_factory=dict)
    risk_records: list[str] = Field(default_factory=list, max_length=80)
    truncated: bool = False


def install_unity_layout_diagnostic_source(clone: Path) -> Path:
    target = clone / "Assets" / "OneBriefDiagnostics" / "Editor" / "LayoutDiagnostics.cs"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(UNITY_LAYOUT_DIAGNOSTIC_SOURCE, encoding="utf-8", newline="\n")
    return target


def package_unity_layout_diagnostics(
    raw_path: Path,
    output_dir: Path,
    *,
    source_revision: str,
    candidate_sha256: str,
) -> UnityLayoutDiagnosticsSummary:
    if not raw_path.is_file() or raw_path.stat().st_size > 2_000_000:
        raise RuntimeError("Unity layout diagnostics did not produce a bounded report")
    raw_bytes = raw_path.read_bytes()
    try:
        payload = json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Unity layout diagnostics produced invalid JSON") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != "onebrief-unity-layout-raw-v1":
        raise RuntimeError("Unity layout diagnostics schema is unavailable or unsupported")
    scenes = payload.get("scene_paths")
    canvases = payload.get("canvases")
    rects = payload.get("rect_transforms")
    risks = payload.get("risks")
    if not all(isinstance(item, list) for item in (scenes, canvases, rects, risks)):
        raise RuntimeError("Unity layout diagnostics omitted required hierarchy collections")
    if len(scenes) > 12 or len(canvases) > 200 or len(rects) > 800 or len(risks) > 240:
        raise RuntimeError("Unity layout diagnostics exceeded the approved evidence bounds")
    normalized_risks = [str(item)[:1000] for item in risks]
    counts = Counter(
        item.rsplit("|", 1)[-1] if "|" in item else "unclassified"
        for item in normalized_risks
    )
    definition = BUILTIN_CAPABILITY_PACKS[CapabilityPackId.UNITY_LAYOUT_DIAGNOSTICS]
    summary = UnityLayoutDiagnosticsSummary(
        capability_pack_version=definition.version,
        source_revision=source_revision,
        candidate_sha256=candidate_sha256,
        raw_report_sha256=hashlib.sha256(raw_bytes).hexdigest(),
        scene_count=len(scenes),
        canvas_count=len(canvases),
        rect_transform_count=len(rects),
        risk_counts=dict(sorted(counts.items())),
        risk_records=normalized_risks[:80],
        truncated=bool(payload.get("truncated", False)),
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(raw_path, output_dir / "hierarchy.json")
    (output_dir / "summary.json").write_text(
        summary.model_dump_json(indent=2) + "\n", encoding="utf-8"
    )
    return summary


def compact_unity_layout_diagnostic_context(evidence_dir: Path) -> str | None:
    path = evidence_dir / "summary.json"
    if not path.is_file():
        return None
    try:
        summary = UnityLayoutDiagnosticsSummary.model_validate_json(
            path.read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return None
    payload = {
        "capability_pack": summary.capability_pack_id,
        "source_revision": summary.source_revision,
        "candidate_sha256": summary.candidate_sha256,
        "scene_count": summary.scene_count,
        "canvas_count": summary.canvas_count,
        "rect_transform_count": summary.rect_transform_count,
        "risk_counts": summary.risk_counts,
        "highest_priority_risks": summary.risk_records[:24],
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)
