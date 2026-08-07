from pathlib import Path
import hashlib
import subprocess

import pytest
from pydantic import ValidationError

from onebrief.development_toolpack import (
    CodeChangeSet,
    DevelopmentCommandResult,
    ExchangeDevelopmentToolPack,
    FileChange,
    sha256_file,
)


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def _repository(root: Path) -> Path:
    root.mkdir()
    (root / "web" / "src").mkdir(parents=True)
    (root / "package.json").write_text('{"scripts":{"test":"echo ok"}}\n', encoding="utf-8")
    (root / "web" / "package.json").write_text(
        '{"scripts":{"test":"echo ok","build":"echo ok"}}\n', encoding="utf-8"
    )
    (root / "web" / "src" / "page.tsx").write_text(
        'export default function Page(){return <main>Old</main>}\n', encoding="utf-8"
    )
    _git(root, "init")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "config", "user.name", "OneBrief Test")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "fixture")
    return root


def _runner(command_id: str, argv: list[str], cwd: Path, _timeout: int):
    assert "New" in (cwd / "web" / "src" / "page.tsx").read_text(encoding="utf-8")
    return DevelopmentCommandResult(
        command_id=command_id,
        argv=argv,
        exit_code=0,
        duration_seconds=0.01,
        output_tail="passed",
    )


def test_development_changes_only_an_isolated_clone_and_returns_verified_patch(tmp_path: Path) -> None:
    root = _repository(tmp_path / "exchange")
    original = root / "web" / "src" / "page.tsx"
    pack = ExchangeDevelopmentToolPack(root, _runner)
    inspection, sources = pack.inspect(tmp_path / "inspection")
    assert inspection.context_files
    page_record = next(item for item in inspection.context_files if item.path == "web/src/page.tsx")
    assert any(item.name.endswith("web/src/page.tsx") for item in sources)

    changes = CodeChangeSet(
        summary="Improve the visible Exchange page.",
        changes=[FileChange(
            path="web/src/page.tsx",
            base_sha256=page_record.sha256,
            content='export default function Page(){return <main>New</main>}\n',
            reason="Expose the approved improvement in the web application.",
        )],
    )
    run = pack.apply_and_verify(changes, tmp_path / "delivery" / "development")

    assert run.status == "verified"
    assert [item.command_id for item in run.commands] == [
        "repository_tests", "web_build", "web_tests", "production_http"
    ]
    assert "Old" in original.read_text(encoding="utf-8")
    changed = tmp_path / "delivery" / "development" / "changed_files" / "web" / "src" / "page.tsx"
    assert "New" in changed.read_text(encoding="utf-8")
    patch = tmp_path / "delivery" / "development" / "changes.patch"
    assert "page.tsx" in patch.read_text(encoding="utf-8")



def test_inspection_hash_matches_the_committed_git_blob(tmp_path: Path) -> None:
    root = _repository(tmp_path / "exchange")
    inspection, _ = ExchangeDevelopmentToolPack(root, _runner).inspect(tmp_path / "inspection")
    page_record = next(item for item in inspection.context_files if item.path == "web/src/page.tsx")
    committed = subprocess.run(
        ["git", "show", "HEAD:web/src/page.tsx"], cwd=root, check=True, capture_output=True,
    ).stdout

    assert page_record.sha256 == hashlib.sha256(committed).hexdigest()

def test_editable_web_sources_are_selected_before_large_evidence_files(tmp_path: Path) -> None:
    root = _repository(tmp_path / "exchange")
    docs = root / "docs"
    docs.mkdir()
    (docs / "a.md").write_text("a" * 110_000, encoding="utf-8")
    (docs / "b.md").write_text("b" * 110_000, encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "large evidence")

    inspection, sources = ExchangeDevelopmentToolPack(root, _runner).inspect(tmp_path / "inspection")

    paths = {item.path for item in inspection.context_files}
    assert "web/src/page.tsx" in paths
    assert any(item.name == "exchange-source/web/src/page.tsx" for item in sources)


def test_next_app_router_source_path_is_editable() -> None:
    change = FileChange(
        path="web/app/page.tsx",
        content="export default function Page(){return <main>Exchange</main>}\n",
        reason="Improve the approved Next.js page.",
    )
    assert change.path == "web/app/page.tsx"


def test_mature_page_replacement_has_a_realistic_but_bounded_size_limit() -> None:
    accepted = CodeChangeSet(
        summary="Preserve and improve a mature web page.",
        changes=[FileChange(
            path="web/app/page.tsx",
            content="x" * 50_000,
            reason="A mature existing page must fit without a wasteful compression retry.",
        )],
    )
    assert len(accepted.changes[0].content) == 50_000

    with pytest.raises(ValidationError):
        FileChange(
            path="web/app/page.tsx",
            content="x" * 70_000,
            reason="Unbounded replacements remain prohibited.",
        )


@pytest.mark.parametrize(
    "path",
    ["../outside.ts", ".env", "web/node_modules/pkg/index.js", "credentials/key.json", "image.png", "exchange-source/web/app/page.tsx"],
)
def test_development_blocks_unsafe_or_non_text_paths(path: str) -> None:
    with pytest.raises(ValidationError):
        FileChange(path=path, content="blocked", reason="Must be rejected by policy.")

def test_development_blocks_host_runtime_access() -> None:
    with pytest.raises(ValidationError, match="prohibited host-runtime"):
        FileChange(
            path="web/src/unsafe.ts",
            content="import { exec } from 'node:child_process';",
            reason="This must never execute on the host.",
        )


def test_development_refuses_a_dirty_source_repository(tmp_path: Path) -> None:
    root = _repository(tmp_path / "exchange")
    (root / "web" / "src" / "page.tsx").write_text("dirty\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="clean source repository"):
        ExchangeDevelopmentToolPack(root, _runner).inspect(tmp_path / "inspection")
