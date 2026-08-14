"""Trusted Unity PlayMode screenshot helper installed only in isolated clones."""

from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4


HELPER_FILENAME = "OneBriefAtomicScreenshot.cs"
HELPER_MARKER = "OneBriefAtomicScreenshot.Capture"
SCENARIO_HELPER_MARKER = "OneBriefAtomicScreenshot.CaptureScenario"


ATOMIC_SCREENSHOT_SOURCE = r'''using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Security.Cryptography;
using UnityEngine;
using UnityEngine.UI;

namespace OneBrief.Visual
{
    // Trusted evidence-only helper installed by OneBrief in the disposable clone.
    public static class OneBriefAtomicScreenshot
    {
        [Serializable]
        public sealed class CaptureReceipt
        {
            public string screenshot_path;
            public int viewport_width;
            public int viewport_height;
            public string sha256;
        }

        [Serializable]
        public sealed class ScenarioReceipt
        {
            public string scenario_id;
            public string observed_state;
            public string interaction;
            public int assertion_count;
            public string screenshot_path;
            public int viewport_width;
            public int viewport_height;
            public string sha256;
        }

        [Serializable]
        private sealed class ScenarioManifest
        {
            public string schema_version = "onebrief-unity-visual-evidence-v1";
            public ScenarioReceipt[] scenarios;
        }

        private sealed class CanvasState
        {
            public Canvas canvas;
            public RenderMode renderMode;
            public Camera worldCamera;
            public float planeDistance;
        }

        private static void RequireDeterministicVisualSignal(Texture2D texture)
        {
            var colors = new HashSet<uint>();
            float minimumLuminance = 1f;
            float maximumLuminance = 0f;
            int visibleSamples = 0;
            int columns = Math.Min(32, texture.width);
            int rows = Math.Min(18, texture.height);
            for (int y = 0; y < rows; y++)
            {
                int sampleY = Math.Min(texture.height - 1, (y * texture.height) / rows);
                for (int x = 0; x < columns; x++)
                {
                    int sampleX = Math.Min(texture.width - 1, (x * texture.width) / columns);
                    Color32 pixel = texture.GetPixel(sampleX, sampleY);
                    if (pixel.a < 8) continue;
                    visibleSamples++;
                    uint key = ((uint)pixel.r << 24) | ((uint)pixel.g << 16)
                        | ((uint)pixel.b << 8) | pixel.a;
                    colors.Add(key);
                    float luminance = (0.2126f * pixel.r + 0.7152f * pixel.g + 0.0722f * pixel.b) / 255f;
                    minimumLuminance = Math.Min(minimumLuminance, luminance);
                    maximumLuminance = Math.Max(maximumLuminance, luminance);
                }
            }
            if (visibleSamples < 4 || (colors.Count < 4 && maximumLuminance - minimumLuminance < 0.08f))
                throw new InvalidOperationException(
                    "Rendered PNG is blank or has no deterministic visual signal.");
        }

        public static CaptureReceipt Capture(
            string relativePath,
            Camera sceneCamera,
            int width,
            int height,
            IEnumerable<Canvas> measuredCanvases)
        {
            if (sceneCamera == null) throw new ArgumentNullException(nameof(sceneCamera));
            if (width < 64 || height < 64) throw new ArgumentOutOfRangeException("viewport");
            string root = Path.GetFullPath(Path.Combine(Application.dataPath, "..", "onebrief-evidence"));
            Directory.CreateDirectory(root);
            string finalPath = Path.GetFullPath(Path.Combine(root, relativePath ?? string.Empty));
            string boundary = root.TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar)
                + Path.DirectorySeparatorChar;
            if (!finalPath.StartsWith(boundary, StringComparison.OrdinalIgnoreCase))
                throw new InvalidOperationException("Screenshot path escaped onebrief-evidence.");
            if (!string.Equals(Path.GetExtension(finalPath), ".png", StringComparison.OrdinalIgnoreCase))
                throw new InvalidOperationException("Screenshot path must end in .png.");
            Directory.CreateDirectory(Path.GetDirectoryName(finalPath));

            var canvases = (measuredCanvases ?? Enumerable.Empty<Canvas>())
                .Where(item => item != null && item.isActiveAndEnabled && item.gameObject.activeInHierarchy)
                .Distinct()
                .ToArray();
            var states = canvases.Select(item => new CanvasState {
                canvas = item,
                renderMode = item.renderMode,
                worldCamera = item.worldCamera,
                planeDistance = item.planeDistance,
            }).ToArray();
            RenderTexture priorActive = RenderTexture.active;
            RenderTexture priorTarget = sceneCamera.targetTexture;
            RenderTexture target = null;
            Texture2D texture = null;
            string temporaryPath = finalPath + "." + Guid.NewGuid().ToString("N") + ".tmp";
            try
            {
                foreach (Canvas canvas in canvases)
                {
                    if (canvas.renderMode == RenderMode.ScreenSpaceOverlay)
                    {
                        canvas.renderMode = RenderMode.ScreenSpaceCamera;
                        canvas.worldCamera = sceneCamera;
                        canvas.planeDistance = Math.Max(sceneCamera.nearClipPlane + 0.1f, 1f);
                    }
                }
                Canvas.ForceUpdateCanvases();
                target = RenderTexture.GetTemporary(width, height, 24, RenderTextureFormat.ARGB32);
                texture = new Texture2D(width, height, TextureFormat.RGBA32, false);
                sceneCamera.targetTexture = target;
                RenderTexture.active = target;
                sceneCamera.Render();
                texture.ReadPixels(new Rect(0, 0, width, height), 0, 0, false);
                texture.Apply(false, false);
                RequireDeterministicVisualSignal(texture);
                byte[] png = ImageConversion.EncodeToPNG(texture);
                if (png == null || png.Length < 1024)
                    throw new InvalidOperationException("Rendered PNG is empty or too small.");
                using (var stream = new FileStream(
                    temporaryPath, FileMode.CreateNew, FileAccess.Write, FileShare.None,
                    4096, FileOptions.WriteThrough))
                {
                    stream.Write(png, 0, png.Length);
                    stream.Flush(true);
                }
                byte[] durable = File.ReadAllBytes(temporaryPath);
                var decoded = new Texture2D(2, 2, TextureFormat.RGBA32, false);
                try
                {
                    if (!ImageConversion.LoadImage(decoded, durable, true)
                        || decoded.width != width || decoded.height != height)
                        throw new InvalidOperationException("Durable PNG dimensions do not match the capture.");
                }
                finally { UnityEngine.Object.DestroyImmediate(decoded); }
                if (File.Exists(finalPath)) File.Replace(temporaryPath, finalPath, null);
                else File.Move(temporaryPath, finalPath);
                using (var sha = SHA256.Create())
                {
                    return new CaptureReceipt {
                        screenshot_path = relativePath.Replace('\\', '/'),
                        viewport_width = width,
                        viewport_height = height,
                        sha256 = BitConverter.ToString(sha.ComputeHash(durable)).Replace("-", "").ToLowerInvariant(),
                    };
                }
            }
            finally
            {
                if (File.Exists(temporaryPath)) File.Delete(temporaryPath);
                sceneCamera.targetTexture = priorTarget;
                RenderTexture.active = priorActive;
                foreach (CanvasState state in states)
                {
                    state.canvas.renderMode = state.renderMode;
                    state.canvas.worldCamera = state.worldCamera;
                    state.canvas.planeDistance = state.planeDistance;
                }
                if (texture != null) UnityEngine.Object.DestroyImmediate(texture);
                if (target != null) RenderTexture.ReleaseTemporary(target);
                Canvas.ForceUpdateCanvases();
            }
        }

        public static ScenarioReceipt CaptureScenario(
            string scenarioId,
            string observedState,
            string interaction,
            int assertionCount,
            string relativePath,
            Camera sceneCamera,
            int width,
            int height,
            IEnumerable<Canvas> measuredCanvases)
        {
            if (string.IsNullOrWhiteSpace(scenarioId))
                throw new ArgumentException("Scenario ID is empty.");
            if (string.IsNullOrWhiteSpace(observedState))
                throw new ArgumentException("Observed state is empty.");
            if (string.IsNullOrWhiteSpace(interaction))
                throw new ArgumentException("Interaction is empty.");
            if (assertionCount < 1)
                throw new ArgumentOutOfRangeException(nameof(assertionCount));
            CaptureReceipt capture = Capture(
                relativePath, sceneCamera, width, height, measuredCanvases);
            return new ScenarioReceipt {
                scenario_id = scenarioId,
                observed_state = observedState,
                interaction = interaction,
                assertion_count = assertionCount,
                screenshot_path = capture.screenshot_path,
                viewport_width = capture.viewport_width,
                viewport_height = capture.viewport_height,
                sha256 = capture.sha256,
            };
        }

        public static void WriteManifestAtomically(params ScenarioReceipt[] scenarios)
        {
            if (scenarios == null || scenarios.Length == 0)
                throw new ArgumentException("No scenarios supplied.");
            var captures = scenarios.Select(scenario => {
                if (scenario == null) throw new ArgumentException("Scenario is null.");
                if (string.IsNullOrWhiteSpace(scenario.scenario_id)
                    || string.IsNullOrWhiteSpace(scenario.observed_state)
                    || string.IsNullOrWhiteSpace(scenario.interaction)
                    || scenario.assertion_count < 1)
                    throw new InvalidOperationException("Scenario evidence fields are incomplete.");
                return new CaptureReceipt {
                    screenshot_path = scenario.screenshot_path,
                    viewport_width = scenario.viewport_width,
                    viewport_height = scenario.viewport_height,
                    sha256 = scenario.sha256,
                };
            }).ToArray();
            string json = JsonUtility.ToJson(new ScenarioManifest { scenarios = scenarios });
            WriteManifestAtomically(json, captures);
        }

        public static void WriteManifestAtomically(string json, params CaptureReceipt[] captures)
        {
            if (string.IsNullOrWhiteSpace(json)) throw new ArgumentException("Manifest JSON is empty.");
            if (captures == null || captures.Length == 0) throw new ArgumentException("No captures supplied.");
            string root = Path.GetFullPath(Path.Combine(Application.dataPath, "..", "onebrief-evidence"));
            foreach (CaptureReceipt capture in captures)
            {
                string path = Path.GetFullPath(Path.Combine(root, capture.screenshot_path));
                if (!File.Exists(path) || new FileInfo(path).Length < 1024)
                    throw new InvalidOperationException("Manifest referenced a screenshot that was not materialized.");
            }
            string finalPath = Path.Combine(root, "runtime-evidence.json");
            string temporaryPath = finalPath + "." + Guid.NewGuid().ToString("N") + ".tmp";
            byte[] bytes = System.Text.Encoding.UTF8.GetBytes(json);
            using (var stream = new FileStream(
                temporaryPath, FileMode.CreateNew, FileAccess.Write, FileShare.None,
                4096, FileOptions.WriteThrough))
            {
                stream.Write(bytes, 0, bytes.Length);
                stream.Flush(true);
            }
            if (File.Exists(finalPath)) File.Replace(temporaryPath, finalPath, null);
            else File.Move(temporaryPath, finalPath);
        }
    }
}
'''


def install_atomic_unity_evidence_helper(clone: Path) -> list[Path]:
    """Install one trusted helper beside each active OneBrief.Visual test asmdef."""

    installed: list[Path] = []
    assets = clone / "Assets"
    if not assets.is_dir():
        return installed
    parents: set[Path] = set()
    for source in assets.rglob("*.cs"):
        if not source.is_file() or source.is_symlink():
            continue
        text = source.read_text(encoding="utf-8", errors="replace").casefold()
        if "onebrief.visual" not in text or "[unitytest]" not in text:
            continue
        if any(path.suffix.casefold() == ".asmdef" for path in source.parent.iterdir()):
            parents.add(source.parent)
    for parent in sorted(parents):
        destination = parent / HELPER_FILENAME
        temporary = destination.with_suffix(f".{uuid4().hex}.tmp")
        temporary.write_text(ATOMIC_SCREENSHOT_SOURCE, encoding="utf-8", newline="\n")
        os.replace(temporary, destination)
        installed.append(destination)
    return installed
