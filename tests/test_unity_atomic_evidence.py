from pathlib import Path

from onebrief.unity_atomic_evidence import (
    ATOMIC_SCREENSHOT_SOURCE,
    HELPER_FILENAME,
    install_atomic_unity_evidence_helper,
)


def test_atomic_helper_is_installed_beside_visual_test_assembly(tmp_path: Path) -> None:
    parent = tmp_path / "Assets" / "Tests" / "PlayMode"
    parent.mkdir(parents=True)
    (parent / "OneBrief.Visual.Tests.asmdef").write_text("{}", encoding="utf-8")
    (parent / "LoginVisualTest.cs").write_text(
        "namespace OneBrief.Visual { [UnityTest] public IEnumerator Flow() { yield break; } }",
        encoding="utf-8",
    )

    installed = install_atomic_unity_evidence_helper(tmp_path)

    assert installed == [parent / HELPER_FILENAME]
    source = installed[0].read_text(encoding="utf-8")
    assert source == ATOMIC_SCREENSHOT_SOURCE
    assert "FileOptions.WriteThrough" in source
    assert "stream.Flush(true)" in source
    assert "File.Move(temporaryPath, finalPath)" in source
    assert "WriteManifestAtomically" in source
    assert "ScenarioReceipt" in source
    assert "CaptureScenario" in source
    assert 'schema_version = "onebrief-unity-visual-evidence-v1"' in source
    assert "JsonUtility.ToJson" in source
    assert "RequireDeterministicVisualSignal(texture)" in source
    assert "blank or has no deterministic visual signal" in source
    assert "ScreenCapture.CaptureScreenshot" not in source


def test_atomic_helper_is_not_installed_without_visual_test(tmp_path: Path) -> None:
    (tmp_path / "Assets").mkdir()
    assert install_atomic_unity_evidence_helper(tmp_path) == []
