import hashlib
import json
from pathlib import Path

from onebrief.unity_layout_diagnostics import (
    compact_unity_layout_diagnostic_context,
    install_unity_layout_diagnostic_source,
    package_unity_layout_diagnostics,
)


def _raw_report(path: Path) -> bytes:
    payload = {
        "schema_version": "onebrief-unity-layout-raw-v1",
        "scene_paths": ["Assets/Scenes/Lobby.unity"],
        "canvases": [{"hierarchy_path": "Canvas"}],
        "rect_transforms": [
            {"hierarchy_path": "Canvas/Panel"},
            {"hierarchy_path": "Canvas/Panel/Buttons"},
        ],
        "risks": [
            "Assets/Scenes/Lobby.unity|Canvas/Panel|stretched_width_positive_delta",
            "Assets/Scenes/Lobby.unity|Canvas/Panel/Buttons|fixed_rect_outside_reference_width",
            "Assets/Scenes/Lobby.unity|Canvas/Title|stretched_width_positive_delta",
        ],
        "truncated": False,
    }
    raw = json.dumps(payload).encode("utf-8")
    path.parent.mkdir(parents=True)
    path.write_bytes(raw)
    return raw


def test_layout_diagnostic_is_packaged_and_bound_to_candidate(tmp_path: Path) -> None:
    raw_path = tmp_path / "clone" / "onebrief-evidence" / "unity-layout-diagnostics.json"
    raw = _raw_report(raw_path)

    summary = package_unity_layout_diagnostics(
        raw_path,
        tmp_path / "packaged",
        source_revision="a" * 40,
        candidate_sha256="b" * 64,
    )

    assert summary.capability_pack_id == "unity-layout-diagnostics"
    assert summary.scene_count == 1
    assert summary.canvas_count == 1
    assert summary.rect_transform_count == 2
    assert summary.risk_counts == {
        "fixed_rect_outside_reference_width": 1,
        "stretched_width_positive_delta": 2,
    }
    assert summary.raw_report_sha256 == hashlib.sha256(raw).hexdigest()
    assert (tmp_path / "packaged" / "hierarchy.json").read_bytes() == raw
    context = compact_unity_layout_diagnostic_context(tmp_path / "packaged")
    assert context is not None
    assert '"candidate_sha256": "' + "b" * 64 + '"' in context


def test_fixed_diagnostic_source_is_installed_only_in_disposable_clone(tmp_path: Path) -> None:
    target = install_unity_layout_diagnostic_source(tmp_path / "clone")

    assert target == (
        tmp_path / "clone" / "Assets" / "OneBriefDiagnostics" / "Editor"
        / "LayoutDiagnostics.cs"
    )
    content = target.read_text(encoding="utf-8")
    assert "OneBriefDiagnostics.LayoutDiagnostics" not in content
    assert "public static class LayoutDiagnostics" in content
    assert "RectTransform" in content
    assert "File.WriteAllText" in content
