import subprocess
from pathlib import Path

from onebrief.project_catalog import ProjectCatalog


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def test_catalog_exposes_only_the_approved_exchange_repository(tmp_path: Path) -> None:
    root = tmp_path / "exchange"
    (root / "web").mkdir(parents=True)
    (root / "package.json").write_text('{"name":"exchange"}', encoding="utf-8")
    (root / "web" / "package.json").write_text('{"name":"web"}', encoding="utf-8")
    _git(root, "init")
    _git(root, "config", "user.name", "OneBrief Test")
    _git(root, "config", "user.email", "onebrief@example.invalid")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "Initial Exchange application")

    project = ProjectCatalog(exchange_root=root).get("exchange")

    assert project.root_path == str(root.resolve())
    assert project.ready_for_isolated_edit is True
    assert project.worktree_status == "clean"
    assert len(project.head_sha or "") == 40
    assert project.recent_history[0].summary == "Initial Exchange application"
    assert ProjectCatalog(exchange_root=root).list("fx")


def test_catalog_ignores_onebrief_control_files(tmp_path: Path) -> None:
    root = tmp_path / "exchange"
    root.mkdir()
    (root / "package.json").write_text("{}", encoding="utf-8")
    _git(root, "init")
    _git(root, "config", "user.name", "OneBrief Test")
    _git(root, "config", "user.email", "onebrief@example.invalid")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "Baseline")
    (root / "ONEBRIEF_PROJECT.json").write_text("{}", encoding="utf-8")
    control = root / ".onebrief" / "project_state.json"
    control.parent.mkdir()
    control.write_text("{}", encoding="utf-8")

    project = ProjectCatalog(exchange_root=root).get("exchange")

    assert project.worktree_status == "clean"
    assert project.ready_for_isolated_edit is True


def test_catalog_blocks_isolated_edit_when_worktree_is_modified(tmp_path: Path) -> None:
    root = tmp_path / "exchange"
    root.mkdir()
    (root / "package.json").write_text("{}", encoding="utf-8")
    _git(root, "init")
    _git(root, "config", "user.name", "OneBrief Test")
    _git(root, "config", "user.email", "onebrief@example.invalid")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "Baseline")
    (root / "package.json").write_text('{"changed":true}', encoding="utf-8")

    project = ProjectCatalog(exchange_root=root).get("exchange")

    assert project.worktree_status == "modified"
    assert project.ready_for_isolated_edit is False
