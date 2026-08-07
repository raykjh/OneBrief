"""Approved local project registry for existing-project improvement work."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
import json

from pydantic import BaseModel, Field

from onebrief.schemas import ToolPackId

from onebrief.project_import import (
    ImportedProjectRecord,
    default_project_registry_root,
    inspect_project,
)
from onebrief.toolpack_lifecycle import ProjectToolPackLifecycle

class ProjectHistoryEntry(BaseModel):
    commit: str = Field(pattern=r"^[a-f0-9]{7,40}$")
    date: str
    summary: str


class RegisteredProject(BaseModel):
    project_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,63}$")
    name: str
    canonical_goal: str | None = None
    summary: str
    root_path: str
    project_type: str
    branch: str | None = None
    head_sha: str | None = Field(default=None, pattern=r"^[a-f0-9]{40}$")
    worktree_status: str
    ready_for_isolated_edit: bool
    toolpack_id: ToolPackId | None
    toolpack_status: str = "approved"
    toolpack_blockers: list[str] = Field(default_factory=list)
    origin: str = "built_in"
    workspace_path: str | None = None
    recent_history: list[ProjectHistoryEntry] = Field(default_factory=list, max_length=8)
    search_terms: list[str] = Field(default_factory=list, exclude=True)


def _git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=20, shell=False, check=False,
    )
    if completed.returncode != 0:
        return ""
    return completed.stdout.strip()


class ProjectCatalog:
    """Expose only explicitly approved projects; never scan arbitrary user paths."""

    def __init__(
        self,
        exchange_root: Path | None = None,
        registry_root: Path | None = None,
    ):
        self.exchange_root = (
            exchange_root or Path(os.environ.get("ONEBRIEF_EXCHANGE_ROOT", r"C:\exchange"))
        ).resolve()
        self.registry_root = (registry_root or default_project_registry_root()).resolve()

    def _exchange(self) -> RegisteredProject | None:
        root = self.exchange_root
        if not root.is_dir() or not (root / ".git").is_dir():
            return None
        head = _git(root, "rev-parse", "HEAD")
        branch = _git(root, "branch", "--show-current") or None
        porcelain = "\n".join(
            line for line in _git(root, "status", "--porcelain").splitlines()
            if not line[3:].replace("\\", "/").startswith(".onebrief/")
        )
        status = "modified" if porcelain else "clean"
        history: list[ProjectHistoryEntry] = []
        raw = _git(root, "log", "-5", "--date=short", "--pretty=format:%h%x1f%ad%x1f%s")
        for line in raw.splitlines():
            parts = line.split("\x1f", 2)
            if len(parts) == 3:
                history.append(ProjectHistoryEntry(
                    commit=parts[0], date=parts[1], summary=parts[2][:300]
                ))
        return RegisteredProject(
            project_id="exchange",
            name="Exchange Flow",
            summary=(
                "실시간 환율·금 시세와 기술 지표, 통화 비교 차트를 제공하는 "
                "기존 웹 애플리케이션입니다."
            ),
            root_path=str(root),
            project_type="Next.js/Vinext web application",
            branch=branch,
            head_sha=head if len(head) == 40 else None,
            worktree_status=status,
            ready_for_isolated_edit=status == "clean" and len(head) == 40,
            toolpack_id=ToolPackId.EXCHANGE_DEVELOPMENT,
            toolpack_status="approved",
            origin="built_in",
            recent_history=history,
            search_terms=["exchange", "fx", "환율", "금", "통화", "web", str(root)],
        )

    def _imported(self) -> list[RegisteredProject]:
        if not self.registry_root.is_dir():
            return []
        projects: list[RegisteredProject] = []
        for record_path in sorted(self.registry_root.glob("*/project.json")):
            try:
                record = ImportedProjectRecord.model_validate_json(
                    record_path.read_text(encoding="utf-8")
                )
                manifest = record.manifest
                if manifest.project_id == "exchange":
                    continue
                inventory = inspect_project(manifest)
                lifecycle = ProjectToolPackLifecycle(
                    manifest.project_id, self.registry_root
                ).state()
                summary = manifest.summary.strip() or manifest.canonical_goal[:500]
                projects.append(RegisteredProject(
                    project_id=manifest.project_id,
                    name=manifest.name,
                    summary=summary,
                    canonical_goal=manifest.canonical_goal,
                    root_path=inventory.root_path,
                    project_type=manifest.project_type,
                    branch=inventory.branch,
                    head_sha=inventory.head_sha,
                    worktree_status=inventory.worktree_status,
                    ready_for_isolated_edit=lifecycle.execution_ready,
                    toolpack_id=None,
                    toolpack_status=lifecycle.status,
                    toolpack_blockers=lifecycle.execution_blockers,
                    origin="imported",
                    workspace_path=str(record_path.parent),
                    recent_history=[],
                    search_terms=[
                        manifest.project_id,
                        manifest.name,
                        manifest.project_type,
                        manifest.canonical_goal,
                        inventory.root_path,
                        *inventory.detected_ecosystems,
                    ],
                ))
            except (OSError, ValueError, json.JSONDecodeError):
                continue
        return projects

    def list(self, query: str = "") -> list[RegisteredProject]:
        projects = [item for item in [self._exchange()] if item is not None]
        projects.extend(self._imported())
        needle = " ".join(query.casefold().split())
        if not needle:
            return projects
        return [
            project for project in projects
            if needle in " ".join([
                project.name, project.summary, project.project_type, *project.search_terms
            ]).casefold()
        ]

    def get(self, project_id: str) -> RegisteredProject:
        for project in self.list():
            if project.project_id == project_id:
                return project
        raise KeyError(f"approved project is unavailable: {project_id}")
