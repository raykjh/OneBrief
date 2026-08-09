import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from onebrief.project_import import ExternalProjectImporter, MANIFEST_NAME
from onebrief.safe_apply import apply_verified_project_result
from onebrief.toolpack_lifecycle import ProjectToolPackLifecycle


def _git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True,
        text=True, encoding="utf-8",
    )
    return completed.stdout.strip()


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _project(tmp_path: Path, monkeypatch) -> tuple[Path, str]:
    root = tmp_path / "project"
    registry = tmp_path / "registry"
    (root / "src" / "sample").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "docs").mkdir()
    (root / "src" / "sample" / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / "tests" / "test_value.py").write_text(
        "from sample import VALUE\n\ndef test_value():\n    assert VALUE == 2\n",
        encoding="utf-8",
    )
    (root / "docs" / "TASK.md").write_text("Set VALUE to 2.\n", encoding="utf-8")
    (root / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\npythonpath = ['src']\ntestpaths = ['tests']\n",
        encoding="utf-8",
    )
    manifest = {
        "schema_version": "onebrief-project-v1",
        "project_id": "safe-apply-python",
        "name": "Safe Apply Python",
        "project_type": "python_library",
        "project_root": str(root.resolve()),
        "canonical_goal": "Set the tested value safely.",
        "summary": "Safe apply fixture.",
        "authoritative_documents": ["docs/TASK.md"],
    }
    (root / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    _git(root, "init")
    _git(root, "config", "user.name", "OneBrief Test")
    _git(root, "config", "user.email", "onebrief@example.invalid")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "Create safe apply fixture")
    head = _git(root, "rev-parse", "HEAD")
    monkeypatch.setenv("ONEBRIEF_PROJECTS_ROOT", str(registry))
    ExternalProjectImporter(registry).import_bytes((root / MANIFEST_NAME).read_bytes())
    lifecycle = ProjectToolPackLifecycle("safe-apply-python", registry)
    state = lifecycle.generate_and_qualify()
    lifecycle.approve(state.qualification.toolpack_sha256)
    return root, head


def _result(tmp_path: Path, root: Path, head: str) -> Path:
    result = tmp_path / "result"
    artifacts = result / "artifacts"
    development = artifacts / "development"
    changed = development / "changed_files" / "src" / "sample" / "__init__.py"
    changed.parent.mkdir(parents=True)
    changed.write_bytes(b"VALUE = 2\n")
    base = _sha(root / "src" / "sample" / "__init__.py")
    (development / "change_set.json").write_text(json.dumps({
        "summary": "Set the tested value to two.",
        "changes": [{
            "path": "src/sample/__init__.py",
            "base_sha256": base,
            "content": "VALUE = 2\n",
            "reason": "Satisfy the approved test.",
        }],
    }), encoding="utf-8")
    snapshot = artifacts / "project_snapshot"
    snapshot.mkdir()
    (snapshot / "restore_evidence.json").write_text(json.dumps({
        "project_id": "safe-apply-python",
        "source_head_sha": head,
        "status": "verified_and_approved",
    }), encoding="utf-8")
    (artifacts / "final_verification.json").write_text('{"verdict":"PASS"}', encoding="utf-8")
    (artifacts / "final_approval.json").write_text('{"verdict":"PASS"}', encoding="utf-8")
    (artifacts / "completion_ledger.json").write_text(json.dumps({
        "complete": True, "required_total": 1, "required_passed": 1,
    }), encoding="utf-8")
    files = []
    for path in sorted(result.rglob("*")):
        if path.is_file() and path.name != "package_manifest.json":
            files.append({
                "path": path.relative_to(result).as_posix(),
                "size_bytes": path.stat().st_size,
                "sha256": _sha(path),
            })
    (result / "package_manifest.json").write_text(json.dumps({
        "job_id": "safe-apply-job",
        "status": "complete",
        "created_at": "2026-08-09T00:00:00+00:00",
        "files": files,
    }), encoding="utf-8")
    return result


def test_safe_apply_backs_up_applies_validates_and_is_idempotent(tmp_path: Path, monkeypatch) -> None:
    root, head = _project(tmp_path, monkeypatch)
    result = _result(tmp_path, root, head)
    backups = tmp_path / "backups"

    receipt = apply_verified_project_result("safe-apply-python", result, backups)
    repeated = apply_verified_project_result("safe-apply-python", result, backups)

    assert receipt.status == "applied"
    assert repeated.apply_id == receipt.apply_id
    assert (root / "src" / "sample" / "__init__.py").read_text(encoding="utf-8") == "VALUE = 2\n"
    assert receipt.commands[0].command_id == "python_tests"
    assert receipt.commands[0].exit_code == 0
    assert (Path(receipt.backup_path) / "original" / "src" / "sample" / "__init__.py").read_text(
        encoding="utf-8"
    ) == "VALUE = 1\n"


def test_safe_apply_restores_original_when_local_validation_fails(tmp_path: Path, monkeypatch) -> None:
    root, head = _project(tmp_path, monkeypatch)
    result = _result(tmp_path, root, head)

    def fail_runner(_command_id: str, _argv: list[str], _cwd: Path, _timeout: int):
        raise RuntimeError("forced local verification failure")

    with pytest.raises(RuntimeError, match="original files were restored"):
        apply_verified_project_result(
            "safe-apply-python", result, tmp_path / "backups", runner=fail_runner
        )

    assert (root / "src" / "sample" / "__init__.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    assert not _git(root, "status", "--porcelain")


def test_safe_apply_rejects_tampered_result_before_source_write(tmp_path: Path, monkeypatch) -> None:
    root, head = _project(tmp_path, monkeypatch)
    result = _result(tmp_path, root, head)
    changed = result / "artifacts" / "development" / "changed_files" / "src" / "sample" / "__init__.py"
    changed.write_text("VALUE = 999\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="hash mismatch"):
        apply_verified_project_result("safe-apply-python", result, tmp_path / "backups")

    assert (root / "src" / "sample" / "__init__.py").read_text(encoding="utf-8") == "VALUE = 1\n"
