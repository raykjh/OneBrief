import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from onebrief.generic_development_toolpack import (
    ApprovedProjectDevelopmentToolPack,
    CompactProposedProjectCodeChangeSet,
    ProjectCodeChangeSet,
    ProjectFileChange,
    ProposedProjectCodeChangeSet,
)
from onebrief.development_toolpack import DevelopmentCommandResult
from onebrief.project_import import ExternalProjectImporter, MANIFEST_NAME
from onebrief.schemas import ToolPackId
from onebrief.toolpack_lifecycle import AdapterId, ProjectToolPackLifecycle
from onebrief.toolpacks import execute_toolpacks
from onebrief.execution_agents import DeveloperAgent


def _git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True,
        text=True, encoding="utf-8",
    )
    return completed.stdout.strip()


def test_proposed_exact_edit_accepts_bounded_component_replacement() -> None:
    proposal = ProposedProjectCodeChangeSet.model_validate({
        "summary": "Replace one bounded component.",
        "changes": [{
            "path": "web/app/page.tsx",
            "base_sha256": "a" * 64,
            "search": "export default function Page() {}",
            "replace": "x" * 12_000,
            "reason": "Implement the approved screen in one exact edit.",
        }],
    })
    assert len(proposal.changes[0].replace or "") == 12_000


def test_proposed_exact_edit_supports_mature_single_file_components() -> None:
    proposal = ProposedProjectCodeChangeSet.model_validate({
        "summary": "Update a mature component without rewriting the repository.",
        "changes": [{
            "path": "web/app/page.tsx",
            "base_sha256": "a" * 64,
            "search": "old component",
            "replace": "x" * 40_000,
            "reason": "The resulting file remains within the aggregate text-size limit.",
        }],
    })

    assert len(proposal.changes[0].replace or "") == 40_000


def test_compact_proposal_prefers_catalog_edit_over_redundant_content() -> None:
    proposal = CompactProposedProjectCodeChangeSet.model_validate({
        "summary": "Repair one verified source window.",
        "changes": [{
            "path": "Assets/JULPAE/Tests/PlayMode/OneBriefVisualTests.cs",
            "base_sha256": "a" * 64,
            "content": "unsafe whole-file echo",
            "anchor_id": "A123456789abc",
            "replace": "bounded replacement",
            "reason": "Repair the failed PlayMode assertion.",
        }],
    })

    change = proposal.changes[0]
    assert change.content is None
    assert change.anchor_id == "A123456789abc"
    assert change.replace == "bounded replacement"


def test_compact_proposal_bounds_explanatory_reason_without_changing_edit() -> None:
    proposal = CompactProposedProjectCodeChangeSet.model_validate({
        "summary": "Repair one verified source window.",
        "changes": [{
            "path": "Assets/JULPAE/Tests/PlayMode/OneBriefVisualTests.cs",
            "base_sha256": "a" * 64,
            "search": "old assertion",
            "replace": "new assertion",
            "reason": "explanation " * 100,
        }],
    })

    change = proposal.changes[0]
    assert len(change.reason) == 500
    assert change.search == "old assertion"
    assert change.replace == "new assertion"


def test_structural_anchors_rediscover_a_changed_existing_region() -> None:
    developer = DeveloperAgent(
        SimpleNamespace(),
        change_set_schema=ProjectCodeChangeSet,
        source_prefix="project-source/",
        path_approver=lambda path: path if path == "web/app/page.tsx" else None,
    )
    source = "function Page() {\n  const value = 1;\n  return <main>Old</main>;\n}\n"
    proposed = ProposedProjectCodeChangeSet.model_validate({
        "summary": "Replace one component body by structural boundaries.",
        "changes": [{
            "path": "web/app/page.tsx",
            "base_sha256": hashlib.sha256(source.encode()).hexdigest(),
            "start_anchor": "function Page() {",
            "end_anchor": "}",
            "replace": "function Page() {\n  return <main>New</main>;\n}",
            "reason": "The prior exact snippet was reformatted; rediscover its component boundary.",
        }],
    })
    promoted = developer.promote_candidate(proposed, [{
        "repository_path": "web/app/page.tsx",
        "content": source,
        "sha256": hashlib.sha256(source.encode()).hexdigest(),
    }])
    assert promoted.changes[0].content.endswith("<main>New</main>;\n}\n")


def _approved_node_project(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "node-project"
    registry = tmp_path / "registry"
    (root / "src").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "src" / "app.js").write_text("export const answer = 40;\n", encoding="utf-8")
    (root / "src" / "localization-manager.js").write_text(
        "export const language = 'en';\n", encoding="utf-8"
    )

    (root / "tests" / "app.test.js").write_text(
        "import test from 'node:test';\n"
        "import assert from 'node:assert/strict';\n"
        "import { answer } from '../src/app.js';\n"
        "test('answer remains valid', () => assert.ok(answer >= 40));\n",
        encoding="utf-8",
    )
    (root / "package.json").write_text(json.dumps({
        "name": "isolated-fixture",
        "version": "1.0.0",
        "type": "module",
        "scripts": {"test": "node --test", "build": "node --check src/app.js"},
    }), encoding="utf-8")
    _git(root, "init")
    _git(root, "config", "user.name", "OneBrief Test")
    _git(root, "config", "user.email", "onebrief@example.invalid")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "Create fixture")
    manifest = {
        "schema_version": "onebrief-project-v1",
        "project_id": "generic-node",
        "name": "Generic Node",
        "project_type": "node_app",
        "project_root": str(root.resolve()),
        "canonical_goal": "Safely improve this Node application.",
        "summary": "Generic isolated development fixture.",
        "authoritative_documents": ["package.json"],
    }
    payload = json.dumps(manifest, indent=2).encode("utf-8")
    (root / MANIFEST_NAME).write_bytes(payload)
    ExternalProjectImporter(registry).import_bytes(payload)
    lifecycle = ProjectToolPackLifecycle("generic-node", registry)
    state = lifecycle.generate_and_qualify()
    assert state.qualification is not None
    approved = lifecycle.approve(state.qualification.toolpack_sha256)
    assert approved.execution_ready
    return root, registry


@pytest.mark.skipif(shutil.which("npm.cmd" if __import__("os").name == "nt" else "npm") is None, reason="npm unavailable")
def test_approved_generic_runner_edits_only_clone_and_returns_verified_patch(tmp_path: Path) -> None:
    root, registry = _approved_node_project(tmp_path)
    original = (root / "src" / "app.js").read_bytes()
    committed = subprocess.run(
        ["git", "show", "HEAD:src/app.js"], cwd=root, check=True, capture_output=True
    ).stdout
    base_sha = hashlib.sha256(committed).hexdigest()
    change_set = ProjectCodeChangeSet(
        summary="Update the tested answer.",
        changes=[{
            "path": "src/app.js",
            "base_sha256": base_sha,
            "content": "export const answer = 42;\n",
            "reason": "Implement the approved fixture change.",
        }],
    )
    output = tmp_path / "result" / "development"

    run = ApprovedProjectDevelopmentToolPack(
        "generic-node", registry
    ).apply_and_verify(change_set, output)

    assert run.status == "verified"
    assert {item.command_id for item in run.commands} == {"node_test", "node_build"}
    assert (root / "src" / "app.js").read_bytes() == original
    assert "answer = 42" in (output / "changed_files" / "src" / "app.js").read_text(encoding="utf-8")
    assert "src/app.js" in (output / "changes.patch").read_text(encoding="utf-8")


def test_approved_generic_runner_ignores_onebrief_control_metadata(tmp_path: Path) -> None:
    root, registry = _approved_node_project(tmp_path)
    control = root / ".onebrief" / "project_state.json"
    control.parent.mkdir()
    control.write_text('{"status":"incomplete"}\n', encoding="utf-8")

    _profile, head = ApprovedProjectDevelopmentToolPack(
        "generic-node", registry
    )._validate_root()

    assert head == subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, check=True,
        capture_output=True, text=True,
    ).stdout.strip()


def test_node_dependency_bootstrap_is_lockfile_bound_and_disables_scripts(tmp_path: Path) -> None:
    project = tmp_path / "project"
    web = project / "web"
    web.mkdir(parents=True)
    (web / "package.json").write_text(
        '{"devDependencies":{"eslint":"1.0.0"}}', encoding="utf-8"
    )
    (web / "package-lock.json").write_text('{"lockfileVersion":3}', encoding="utf-8")
    profile = SimpleNamespace(adapters=[SimpleNamespace(
        enabled=True, adapter_id=AdapterId.NODE_SCRIPT, parameter="web::lint"
    )])
    pack = object.__new__(ApprovedProjectDevelopmentToolPack)

    commands = pack._dependency_commands(profile, project)

    assert len(commands) == 1
    command_id, argv, timeout = commands[0]
    assert command_id == "node_web_dependencies"
    assert argv[1:] == [
        "--prefix", "web", "ci", "--ignore-scripts", "--no-audit", "--no-fund"
    ]
    assert timeout == 600


def test_node_validation_orders_lint_then_build_then_test(tmp_path: Path) -> None:
    pack = object.__new__(ApprovedProjectDevelopmentToolPack)
    profile = SimpleNamespace(adapters=[
        SimpleNamespace(enabled=True, adapter_id=AdapterId.NODE_SCRIPT, parameter="web::test"),
        SimpleNamespace(enabled=True, adapter_id=AdapterId.NODE_SCRIPT, parameter="web::build"),
        SimpleNamespace(enabled=True, adapter_id=AdapterId.NODE_SCRIPT, parameter="web::lint"),
    ])
    commands = pack._commands(profile, tmp_path, "Complete the web application")
    assert [item[0] for item in commands] == [
        "node_web_lint", "node_web_build", "node_web_test"
    ]


def test_node_dependency_restore_repairs_lock_then_retries_clean_install(tmp_path: Path) -> None:
    project = tmp_path / "project"
    web = project / "web"
    web.mkdir(parents=True)
    (web / "package.json").write_text(
        '{"devDependencies":{"eslint":"1.0.0"}}', encoding="utf-8"
    )
    (web / "package-lock.json").write_text('{"lockfileVersion":3}', encoding="utf-8")
    profile = SimpleNamespace(adapters=[SimpleNamespace(
        enabled=True, adapter_id=AdapterId.NODE_SCRIPT, parameter="web::lint"
    )])
    calls: list[tuple[str, list[str]]] = []

    def runner(command_id: str, argv: list[str], cwd: Path, timeout: int):
        calls.append((command_id, argv))
        if len(calls) == 1:
            raise RuntimeError(
                "development verification failed: node_web_dependencies "
                "Missing package from lock file; Clean install a project; Usage: npm ci"
            )
        return DevelopmentCommandResult(
            command_id=command_id, argv=argv, exit_code=0,
            duration_seconds=0.01, output_tail="ok",
        )

    pack = object.__new__(ApprovedProjectDevelopmentToolPack)
    pack.runner = runner

    results, repaired = pack._restore_node_dependencies(profile, project)

    assert repaired == ["web/package-lock.json"]
    assert [item[0] for item in calls] == [
        "node_web_dependencies", "node_web_dependencies_lock_repair", "node_web_dependencies"
    ]
    assert "--package-lock-only" in calls[1][1]
    assert len(results) == 2


def test_node_dependency_restore_retries_one_transient_runtime_failure(tmp_path: Path) -> None:
    project = tmp_path / "project"
    web = project / "web"
    web.mkdir(parents=True)
    (web / "package.json").write_text(
        '{"devDependencies":{"eslint":"1.0.0"}}', encoding="utf-8"
    )
    (web / "package-lock.json").write_text('{"lockfileVersion":3}', encoding="utf-8")
    profile = SimpleNamespace(adapters=[SimpleNamespace(
        enabled=True, adapter_id=AdapterId.NODE_SCRIPT, parameter="web::lint"
    )])
    calls = []

    def runner(command_id: str, argv: list[str], cwd: Path, timeout: int):
        calls.append(command_id)
        if len(calls) == 1:
            raise RuntimeError(
                "development verification failed: node_web_dependencies (exit_code=137)"
            )
        return DevelopmentCommandResult(
            command_id=command_id, argv=argv, exit_code=0,
            duration_seconds=0.01, output_tail="ok",
        )

    pack = object.__new__(ApprovedProjectDevelopmentToolPack)
    pack.runner = runner
    results, repaired = pack._restore_node_dependencies(profile, project)

    assert calls == ["node_web_dependencies", "node_web_dependencies_retry"]
    assert [item.command_id for item in results] == ["node_web_dependencies_retry"]
    assert repaired == []


def test_generic_runner_rejects_unapproved_path_and_stale_base(tmp_path: Path) -> None:
    root, registry = _approved_node_project(tmp_path)
    pack = ApprovedProjectDevelopmentToolPack("generic-node", registry)
    with pytest.raises(PermissionError, match="outside the approved"):
        pack.apply_and_verify(ProjectCodeChangeSet(
            summary="Unsafe edit.",
            changes=[{
                "path": "package.json", "base_sha256": None,
                "content": "{}", "reason": "Attempt policy bypass.",
            }],
        ), tmp_path / "unsafe")
    with pytest.raises(RuntimeError, match="stale or missing base hash"):
        pack.apply_and_verify(ProjectCodeChangeSet(
            summary="Stale edit.",
            changes=[{
                "path": "src/app.js", "base_sha256": "0" * 64,
                "content": "export const answer = 42;\n", "reason": "Use stale source.",
            }],
        ), tmp_path / "stale")
    assert (root / "src" / "app.js").read_text(encoding="utf-8") == "export const answer = 40;\n"

def test_generic_toolpack_execution_packages_context_and_resumes_with_editable_names(tmp_path: Path, monkeypatch) -> None:
    _root, registry = _approved_node_project(tmp_path)
    monkeypatch.setenv("ONEBRIEF_PROJECTS_ROOT", str(registry))
    output = tmp_path / "toolpacks"

    first_runs, first_sources = execute_toolpacks(
        [ToolPackId.PROJECT_DEVELOPMENT], output, "generic-node"
    )
    resumed_runs, resumed_sources = execute_toolpacks(
        [ToolPackId.PROJECT_DEVELOPMENT], output, "generic-node"
    )

    assert first_runs[0].status == "ready"
    assert resumed_runs[0].toolpack_id == ToolPackId.PROJECT_DEVELOPMENT
    assert any(item.name == "project-source/src/app.js" for item in first_sources)
    assert any(item.name == "project-source/src/app.js" for item in resumed_sources)


def test_generic_inspection_prioritizes_goal_relevant_context(tmp_path: Path) -> None:
    _root, registry = _approved_node_project(tmp_path)
    inspection, sources = ApprovedProjectDevelopmentToolPack(
        "generic-node", registry
    ).inspect(tmp_path / "focused", "Add multilingual localization and language switching")

    assert inspection.context_files
    assert "localization" in inspection.context_files[0].path
    assert sources[0].name.endswith("localization-manager.js")


def test_generic_inspection_understands_korean_localization_goal(tmp_path: Path) -> None:
    root, registry = _approved_node_project(tmp_path)
    localization = root / "src" / "Localization"
    localization.mkdir(parents=True)
    settings = localization / "JulpaeLanguageSettings.cs"
    settings.write_text("public class JulpaeLanguageSettings {}\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-m", "add localization"], cwd=root, check=True)

    ExternalProjectImporter(registry).import_bytes((root / MANIFEST_NAME).read_bytes())
    lifecycle = ProjectToolPackLifecycle("generic-node", registry)
    state = lifecycle.generate_and_qualify()
    lifecycle.approve(state.qualification.toolpack_sha256)
    inspection, _sources = ApprovedProjectDevelopmentToolPack(
        "generic-node", registry
    ).inspect(tmp_path / "korean-focused", "중국어 일본어 스페인어 다국어 언어 선택")

    assert any(
        item.path == "src/Localization/JulpaeLanguageSettings.cs"
        for item in inspection.context_files
    )


def test_generic_inspection_retargets_context_for_a_new_multiarea_ui_goal(tmp_path: Path) -> None:
    root, registry = _approved_node_project(tmp_path)
    additions = {
        "src/Login/LoginSceneController.cs": "public class LoginSceneController {}\n",
        "src/Lobby/LobbyResponsiveLayout.cs": "public class LobbyResponsiveLayout {}\n",
        "src/Lobby/LobbySettingsPanel.cs": "public class LobbySettingsPanel {}\n",
        "src/Shared/AudioVolumeController.cs": "public class AudioVolumeController {}\n",
    }
    for relative, content in additions.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-m", "add ui surfaces"], cwd=root, check=True)

    ExternalProjectImporter(registry).import_bytes((root / MANIFEST_NAME).read_bytes())
    lifecycle = ProjectToolPackLifecycle("generic-node", registry)
    state = lifecycle.generate_and_qualify()
    lifecycle.approve(state.qualification.toolpack_sha256)
    inspection, _sources = ApprovedProjectDevelopmentToolPack(
        "generic-node", registry
    ).inspect(
        tmp_path / "ui-focused",
        "로그인 로비 설정 화면을 현대화하고 음량과 화면 이동을 검증한다",
    )

    first_paths = [item.path for item in inspection.context_files[:8]]
    assert "src/Login/LoginSceneController.cs" in first_paths
    assert "src/Lobby/LobbyResponsiveLayout.cs" in first_paths
    assert "src/Lobby/LobbySettingsPanel.cs" in first_paths
    assert "src/Shared/AudioVolumeController.cs" in first_paths
    assert first_paths.index("src/localization-manager.js") > 3


def test_unity_visual_preflight_requires_discoverable_test_and_evidence(tmp_path: Path) -> None:
    _root, registry = _approved_node_project(tmp_path)
    pack = ApprovedProjectDevelopmentToolPack("generic-node", registry)
    clone = tmp_path / "unity-clone"
    tests = clone / "Assets" / "Tests" / "PlayMode"
    tests.mkdir(parents=True)
    profile = SimpleNamespace(adapters=[SimpleNamespace(
        enabled=True,
        adapter_id=AdapterId.UNITY_PLAYMODE_VISUAL_TESTS,
    )])

    missing = pack._unity_visual_contract_issues(
        profile, clone, "다국어 언어 선택 화면을 PlayMode에서 검증"
    )
    assert len(missing) == 2

    (tests / "OneBriefVisualTests.cs").write_text(
        "namespace OneBrief.Visual { [UnityTest] public void SwitchLanguage() { "
        "var json = ${\"runtime-evidence.json\"}; var png = \"ja.png\"; } }\n",
        encoding="utf-8",
    )
    (tests / "OneBrief.Visual.PlayMode.asmdef").write_text(
        '{"optionalUnityReferences":["TestAssemblies"]}\n', encoding="utf-8"
    )
    invalid_csharp = pack._unity_visual_contract_issues(
        profile, clone, "다국어 언어 선택 화면을 PlayMode에서 검증"
    )
    assert any("valid C# interpolation" in issue for issue in invalid_csharp)

    (tests / "OneBriefVisualTests.cs").write_text(
        "namespace OneBrief.Visual { [UnityTest] public void SwitchLanguage() { "
        "UnityEngine.SceneManagement.SceneManager.LoadScene(\"Lobby\"); "
        "UnityEngine.GameObject.Find(\"LanguageDropdown\"); "
        "var glyphOk = font.HasCharacter('A'); "
        "var path = \"onebrief-evidence/runtime-evidence.json\"; "
        "var png = \"onebrief-evidence/ja.png\"; } }\n",
        encoding="utf-8",
    )

    assert pack._unity_visual_contract_issues(
        profile, clone, "다국어 언어 선택 화면을 PlayMode에서 검증"
    ) == []

    unsafe = clone / "Assets" / "Scripts" / "Localization" / "OneBrief.Visual.asmdef"
    unsafe.parent.mkdir(parents=True)
    unsafe.write_text(
        '{"optionalUnityReferences":["TestAssemblies"]}\n', encoding="utf-8"
    )
    unsafe_issues = pack._unity_visual_contract_issues(
        profile, clone, "다국어 언어 선택 화면을 PlayMode에서 검증"
    )
    assert any("never be placed above production scripts" in issue for issue in unsafe_issues)


def test_unity_visual_preflight_allows_temporary_capture_camera_but_rejects_synthetic_canvas(tmp_path: Path) -> None:
    _root, registry = _approved_node_project(tmp_path)
    pack = ApprovedProjectDevelopmentToolPack("generic-node", registry)
    clone = tmp_path / "unity-capture-camera-clone"
    tests = clone / "Assets" / "Tests" / "PlayMode"
    tests.mkdir(parents=True)
    asmdef = tests / "OneBrief.Visual.Tests.asmdef"
    asmdef.write_text('{"optionalUnityReferences":["TestAssemblies"]}\n', encoding="utf-8")
    profile = SimpleNamespace(adapters=[SimpleNamespace(
        enabled=True, adapter_id=AdapterId.UNITY_PLAYMODE_VISUAL_TESTS,
    )])
    common = (
        'UnityEngine.SceneManagement.SceneManager.LoadScene("Lobby"); '
        'UnityEngine.GameObject.Find("LanguageDropdown"); '
        'var evidence = "onebrief-evidence/runtime-evidence.json"; '
        'var screenshot = "onebrief-evidence/ja.png"; '
    )

    (tests / "OneBriefVisualTests.cs").write_text(
        "namespace OneBrief.Visual { [UnityTest] public void SwitchLanguage() { "
        + common
        + 'new UnityEngine.GameObject("CaptureCamera").AddComponent<UnityEngine.Camera>(); } }\n',
        encoding="utf-8",
    )
    camera_issues = pack._unity_visual_contract_issues(profile, clone, "Unity localization UI")
    assert not any("synthetic UI" in issue for issue in camera_issues)

    (tests / "OneBriefVisualTests.cs").write_text(
        "namespace OneBrief.Visual { [UnityTest] public void SwitchLanguage() { "
        + common
        + 'new UnityEngine.GameObject("FakeCanvas").AddComponent<UnityEngine.Canvas>(); } }\n',
        encoding="utf-8",
    )
    canvas_issues = pack._unity_visual_contract_issues(profile, clone, "Unity localization UI")
    assert any("synthetic UI" in issue for issue in canvas_issues)


def test_unity_visual_preflight_rejects_direct_assembly_csharp_type_references(tmp_path: Path) -> None:
    _root, registry = _approved_node_project(tmp_path)
    pack = ApprovedProjectDevelopmentToolPack("generic-node", registry)
    clone = tmp_path / "unity-direct-type-clone"
    production = clone / "Assets" / "JULPAE" / "Scripts" / "Localization"
    tests = clone / "Assets" / "JULPAE" / "Tests" / "PlayMode"
    production.mkdir(parents=True)
    tests.mkdir(parents=True)
    (production / "JulpaeLocalization.cs").write_text(
        "public static class JulpaeLocalization { public static void SetLanguage(string value) {} }\n",
        encoding="utf-8",
    )
    (tests / "OneBriefVisualTests.cs").write_text(
        "namespace OneBrief.Visual { [UnityTest] public void SwitchLanguage() { "
        "UnityEngine.SceneManagement.SceneManager.LoadScene(\"Lobby\"); "
        "UnityEngine.GameObject.Find(\"LanguageDropdown\"); "
        "JulpaeLocalization.SetLanguage(\"ja\"); var evidence = \"runtime-evidence.json\"; "
        "var screenshot = \"ja.png\"; } }\n",
        encoding="utf-8",
    )
    (tests / "OneBrief.Visual.Tests.asmdef").write_text(
        '{"optionalUnityReferences":["TestAssemblies"]}\n', encoding="utf-8"
    )
    profile = SimpleNamespace(adapters=[SimpleNamespace(
        enabled=True,
        adapter_id=AdapterId.UNITY_PLAYMODE_VISUAL_TESTS,
    )])

    issues = pack._unity_visual_contract_issues(profile, clone, "Unity localization UI")

    assert any("cannot directly reference production types outside its assembly" in issue for issue in issues)
    assert any("JulpaeLocalization" in issue for issue in issues)


def test_unity_visual_preflight_allows_production_type_name_used_only_for_reflection(tmp_path: Path) -> None:
    _root, registry = _approved_node_project(tmp_path)
    pack = ApprovedProjectDevelopmentToolPack("generic-node", registry)
    clone = tmp_path / "unity-reflection-clone"
    production = clone / "Assets" / "JULPAE" / "Scripts" / "Localization"
    tests = clone / "Assets" / "JULPAE" / "Tests" / "PlayMode"
    production.mkdir(parents=True)
    tests.mkdir(parents=True)
    (production / "JulpaeLocalization.cs").write_text(
        "public static class JulpaeLocalization {}\n", encoding="utf-8"
    )
    (tests / "OneBriefVisualTests.cs").write_text(
        "namespace OneBrief.Visual { [UnityTest] public void SwitchLanguage() { "
        "UnityEngine.SceneManagement.SceneManager.LoadScene(\"Lobby\"); "
        "UnityEngine.GameObject.Find(\"LanguageDropdown\"); "
        "var typeName = \"JulpaeLocalization\"; // JulpaeLocalization via reflection\n"
        "var evidence = \"runtime-evidence.json\"; var screenshot = \"ja.png\"; } }\n",
        encoding="utf-8",
    )
    (tests / "OneBrief.Visual.Tests.asmdef").write_text(
        '{"optionalUnityReferences":["TestAssemblies"]}\n', encoding="utf-8"
    )
    profile = SimpleNamespace(adapters=[SimpleNamespace(
        enabled=True, adapter_id=AdapterId.UNITY_PLAYMODE_VISUAL_TESTS,
    )])

    issues = pack._unity_visual_contract_issues(profile, clone, "Unity localization UI")

    assert not any("JulpaeLocalization (Assembly-CSharp)" in issue for issue in issues)


def test_unity_visual_preflight_rejects_async_batchmode_screenshot(tmp_path: Path) -> None:
    _root, registry = _approved_node_project(tmp_path)
    pack = ApprovedProjectDevelopmentToolPack("generic-node", registry)
    tests = tmp_path / "unity-screenshot-clone" / "Assets" / "Tests" / "PlayMode"
    tests.mkdir(parents=True)
    (tests / "OneBriefVisualTests.cs").write_text(
        "namespace OneBrief.Visual { [UnityTest] public void SwitchLanguage() { "
        "UnityEngine.SceneManagement.SceneManager.LoadScene(\"Lobby\"); "
        "UnityEngine.GameObject.Find(\"LanguageDropdown\"); "
        "UnityEngine.ScreenCapture.CaptureScreenshot(\"onebrief-evidence/ko.png\"); "
        "var evidence = \"runtime-evidence.json\"; } }\n",
        encoding="utf-8",
    )
    (tests / "OneBrief.Visual.Tests.asmdef").write_text(
        '{"optionalUnityReferences":["TestAssemblies"]}\n', encoding="utf-8"
    )
    profile = SimpleNamespace(adapters=[SimpleNamespace(
        enabled=True, adapter_id=AdapterId.UNITY_PLAYMODE_VISUAL_TESTS,
    )])

    issues = pack._unity_visual_contract_issues(
        profile, tests.parents[2], "Unity localization UI"
    )

    assert any("must not rely on asynchronous ScreenCapture" in issue for issue in issues)


def test_unity_visual_preflight_requires_exact_real_settings_scene(tmp_path: Path) -> None:
    _root, registry = _approved_node_project(tmp_path)
    pack = ApprovedProjectDevelopmentToolPack("generic-node", registry)
    clone = tmp_path / "unity-settings-scene-clone"
    tests = clone / "Assets" / "Tests" / "PlayMode"
    tests.mkdir(parents=True)
    (clone / "Assets" / "LobbyScene_All.unity").write_text(
        "--- !u!1 &1\nGameObject:\n  m_Name: LanguageDropdown\n", encoding="utf-8"
    )
    (tests / "OneBriefVisualTests.cs").write_text(
        "namespace OneBrief.Visual { [UnityTest] public void SwitchLanguage() { "
        "UnityEngine.SceneManagement.SceneManager.LoadScene(\"LoginScene\"); "
        "UnityEngine.GameObject.Find(\"LanguageDropdown\"); "
        "var evidence = \"runtime-evidence.json\"; var screenshot = \"ja.png\"; } }\n",
        encoding="utf-8",
    )
    (tests / "OneBrief.Visual.Tests.asmdef").write_text(
        '{"optionalUnityReferences":["TestAssemblies"]}\n', encoding="utf-8"
    )
    profile = SimpleNamespace(adapters=[SimpleNamespace(
        enabled=True, adapter_id=AdapterId.UNITY_PLAYMODE_VISUAL_TESTS,
    )])

    issues = pack._unity_visual_contract_issues(profile, clone, "Unity localization UI")

    assert any("scene that does not exist" in issue for issue in issues)
    assert any("LobbyScene_All" in issue for issue in issues)
    assert any("scene containing the real LanguageDropdown" in issue for issue in issues)


def test_change_set_hashes_are_bound_to_trusted_inspection(tmp_path: Path) -> None:
    _root, registry = _approved_node_project(tmp_path)
    pack = ApprovedProjectDevelopmentToolPack("generic-node", registry)
    inspection, _sources = pack.inspect(tmp_path / "inspection", "localization language")
    proposed = ProjectCodeChangeSet(
        summary="Update inspected localization and add a locale file.",
        changes=[
            {
                "path": "src/localization-manager.js",
                "base_sha256": "0" * 64,
                "content": "export const language = 'ja';\n",
                "reason": "Add a tested locale default.",
            },
            {
                "path": "src/locales.js",
                "base_sha256": "f" * 64,
                "content": "export const locales = ['en', 'ja'];\n",
                "reason": "Add supported locale metadata.",
            },
        ],
    )

    bound = pack.bind_change_set_to_inspection(proposed, inspection)

    expected = next(
        item.sha256 for item in inspection.context_files
        if item.path == "src/localization-manager.js"
    )
    assert bound.changes[0].base_sha256 == expected
    assert bound.changes[1].base_sha256 is None
    assert proposed.changes[0].base_sha256 == "0" * 64


def test_project_file_change_discards_invalid_model_hash_before_trusted_binding() -> None:
    change = ProjectFileChange(
        path="Assets/JULPAE/Scripts/Localization/JulpaeLocalization.cs",
        base_sha256="0060572e-9ec0-4fe4-aa6f-d7f9a01e6b00",
        content="public static class JulpaeLocalization {}\n",
        reason="Repair localization safely.",
    )

    assert change.base_sha256 is None


def test_change_set_cannot_edit_existing_file_omitted_from_context(tmp_path: Path) -> None:
    _root, registry = _approved_node_project(tmp_path)
    pack = ApprovedProjectDevelopmentToolPack("generic-node", registry)
    inspection, _sources = pack.inspect(tmp_path / "inspection", "localization language")
    inspection = inspection.model_copy(update={
        "context_files": [
            item for item in inspection.context_files if item.path != "src/app.js"
        ]
    })
    proposed = ProjectCodeChangeSet(
        summary="Attempt an unseen edit.",
        changes=[{
            "path": "src/app.js",
            "base_sha256": None,
            "content": "export const answer = 42;\n",
            "reason": "Edit a file outside model context.",
        }],
    )

    with pytest.raises(PermissionError, match="not included in approved model context"):
        pack.bind_change_set_to_inspection(proposed, inspection)


def test_patch_includes_new_files_and_excludes_validator_side_effects(tmp_path: Path) -> None:
    root, registry = _approved_node_project(tmp_path)
    committed = subprocess.run(
        ["git", "show", "HEAD:src/app.js"], cwd=root, check=True, capture_output=True
    ).stdout

    def side_effect_runner(command_id: str, argv: list[str], cwd: Path, timeout: int):
        (cwd / "package.json").write_text('{"validator":"side-effect"}\n', encoding="utf-8")
        return DevelopmentCommandResult(
            command_id=command_id, argv=argv, exit_code=0,
            duration_seconds=0.01, output_tail="ok",
        )

    change_set = ProjectCodeChangeSet(
        summary="Update one file and add another.",
        changes=[
            {
                "path": "src/app.js",
                "base_sha256": hashlib.sha256(committed).hexdigest(),
                "content": "export const answer = 42;\n",
                "reason": "Update approved source.",
            },
            {
                "path": "src/new-locales.js",
                "base_sha256": None,
                "content": "export const locales = ['ja'];\n",
                "reason": "Add approved locale source.",
            },
        ],
    )

    output = tmp_path / "result" / "development"
    ApprovedProjectDevelopmentToolPack(
        "generic-node", registry, runner=side_effect_runner
    ).apply_and_verify(change_set, output)
    patch = (output / "changes.patch").read_text(encoding="utf-8")
    assert "src/new-locales.js" in patch
    assert "package.json" not in patch


def test_generic_runner_normalizes_nonsemantic_text_whitespace_before_validation(tmp_path: Path) -> None:
    root, registry = _approved_node_project(tmp_path)
    committed = subprocess.run(
        ["git", "show", "HEAD:src/app.js"], cwd=root, check=True, capture_output=True
    ).stdout
    validation_called = False

    def runner(command_id: str, argv: list[str], cwd: Path, timeout: int) -> DevelopmentCommandResult:
        nonlocal validation_called
        validation_called = True
        return DevelopmentCommandResult(
            command_id=command_id, argv=argv, exit_code=0,
            duration_seconds=0.01, output_tail="ok",
        )

    change_set = ProjectCodeChangeSet(
        summary="Introduce an invalid whitespace-only code line.",
        changes=[{
            "path": "src/app.js",
            "base_sha256": hashlib.sha256(committed).hexdigest(),
            "content": "export const answer = 42;  \n",
            "reason": "Exercise deterministic patch hygiene.",
        }],
    )

    output = tmp_path / "whitespace"
    ApprovedProjectDevelopmentToolPack(
        "generic-node", registry, runner=runner
    ).apply_and_verify(change_set, output)
    assert validation_called is True
    assert (output / "changed_files" / "src" / "app.js").read_text(
        encoding="utf-8"
    ) == "export const answer = 42;\n"


def test_safe_csharp_normalization_trims_line_end_whitespace_without_touching_literals() -> None:
    content = "using System;   \nvar text = @\"meaningful   \";   \n"

    normalized = ApprovedProjectDevelopmentToolPack._normalize_safe_generated_text(
        "Assets/Tests/Visual.cs", content
    )

    assert normalized == "using System;\nvar text = @\"meaningful   \";\n"


def test_safe_web_normalization_converts_crlf_and_removes_trailing_space() -> None:
    content = '<select id="langSelect">   \r\n<option>한국어</option>\r\n'

    normalized = ApprovedProjectDevelopmentToolPack._normalize_safe_generated_text(
        "src/index.html", content
    )

    assert normalized == '<select id="langSelect">\n<option>한국어</option>\n'


def test_safe_csharp_normalization_trims_generated_candidate_whitespace_for_all_csharp() -> None:
    content = "using System;\n    \nvar text = \"test\";   \n"

    generated = ApprovedProjectDevelopmentToolPack._normalize_safe_generated_text(
        "Assets/JULPAE/Tests/PlayMode/OneBriefVisualTests.cs", content
    )
    production = ApprovedProjectDevelopmentToolPack._normalize_safe_generated_text(
        "Assets/JULPAE/Scripts/Localization/LocalizedText.cs", content
    )

    assert generated == "using System;\n\nvar text = \"test\";\n"
    assert production == "using System;\n\nvar text = \"test\";\n"


def test_safe_csharp_normalization_repairs_javascript_style_interpolation_only_in_playmode_tests() -> None:
    content = 'string json = ${"{{\\"value\\":{value}}}";\n'

    generated = ApprovedProjectDevelopmentToolPack._normalize_safe_generated_text(
        "Assets/JULPAE/Tests/PlayMode/OneBriefVisualTests.cs", content
    )
    production = ApprovedProjectDevelopmentToolPack._normalize_safe_generated_text(
        "Assets/JULPAE/Scripts/Localization/LocalizedText.cs", content
    )

    assert generated == 'string json = $"{{\\"value\\":{value}}}";\n'
    assert production == content


def test_safe_unity_test_asmdef_normalization_adds_test_assembly_marker() -> None:
    content = (
        '{"name":"OneBrief.Visual.Tests","references":['
        '"UnityEngine.TestRunner","UnityEditor.TestRunner","Unity.TextMeshPro"]}\n'
    )

    normalized = ApprovedProjectDevelopmentToolPack._normalize_safe_generated_text(
        "Assets/JULPAE/Tests/PlayMode/OneBrief.Visual.Tests.asmdef", content
    )

    payload = json.loads(normalized)
    assert payload["name"] == "OneBrief.Visual.Tests"
    assert payload["references"] == ["Unity.TextMeshPro"]
    assert payload["optionalUnityReferences"] == ["TestAssemblies"]


def test_asmdef_normalization_does_not_touch_production_or_invalid_json() -> None:
    production = '{"name":"JULPAE.Localization"}\n'
    invalid = '{"name":'

    assert ApprovedProjectDevelopmentToolPack._normalize_safe_generated_text(
        "Assets/JULPAE/Scripts/Localization/JULPAE.Localization.asmdef", production
    ) == production
    assert ApprovedProjectDevelopmentToolPack._normalize_safe_generated_text(
        "Assets/JULPAE/Tests/PlayMode/Broken.asmdef", invalid
    ) == invalid
