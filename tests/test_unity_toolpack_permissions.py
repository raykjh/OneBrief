from pathlib import Path
from types import SimpleNamespace

from onebrief.generic_development_toolpack import ApprovedProjectDevelopmentToolPack
from onebrief.toolpack_lifecycle import AdapterId, ToolAdapter


def pack_with_visual_adapter(tmp_path: Path) -> tuple[ApprovedProjectDevelopmentToolPack, object]:
    editor = tmp_path / "Unity.exe"
    editor.write_bytes(b"trusted editor binding")
    pack = object.__new__(ApprovedProjectDevelopmentToolPack)
    pack.root = tmp_path
    pack.lifecycle = SimpleNamespace(_unity_editor=lambda _root: editor.resolve())
    profile = SimpleNamespace(adapters=[
        ToolAdapter(
            adapter_id=AdapterId.UNITY_PLAYMODE_VISUAL_TESTS,
            label="Run bounded visual PlayMode tests",
            evidence=str(editor.resolve()),
        )
    ])
    return pack, profile


def test_visual_runtime_permission_activates_only_for_matching_goal(tmp_path: Path) -> None:
    pack, profile = pack_with_visual_adapter(tmp_path)

    visual = pack._commands(profile, tmp_path, "Add a multilingual language dropdown screen.")
    non_visual = pack._commands(profile, tmp_path, "Improve a numeric parser.")

    assert [item[0] for item in visual] == ["unity_playmode_visual_tests"]
    assert "-testPlatform" in visual[0][1]
    assert "PlayMode" in visual[0][1]
    assert "OneBrief.Visual" in visual[0][1]
    assert "-quit" not in visual[0][1]
    assert non_visual == []
