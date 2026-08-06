"""Version-oriented agent-library and project-workspace materialization."""

from __future__ import annotations

import hashlib
import json
import os
from enum import StrEnum
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator

from onebrief.agent_registry import AgentCard, AgentInstance, AgentRegistry


def _safe_id(value: str, field: str) -> str:
    cleaned = value.strip()
    if not cleaned or cleaned in {".", ".."} or any(part in cleaned for part in ("/", "\\")):
        raise ValueError(f"{field} must be one safe path segment")
    return cleaned


def _stable_json(value: BaseModel | dict) -> str:
    payload = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _write_new_or_equal(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_text(encoding="utf-8") != content:
            raise FileExistsError(f"refusing to overwrite changed workspace file: {path}")
        return
    temp = path.with_suffix(path.suffix + f".{uuid4().hex}.tmp")
    temp.write_text(content, encoding="utf-8")
    os.replace(temp, path)


class RevisionStatus(StrEnum):
    DRAFT = "draft"
    IN_REVIEW = "in_review"
    ACCEPTED = "accepted"
    REJECTED = "rejected"


class WorkspaceRevision(BaseModel):
    schema_version: str = "onebrief-workspace-revision-v1"
    revision_id: str
    parent_revision_id: str | None = None
    author_instance_id: str
    artifact_path: str
    artifact_sha256: str
    change_reason: str
    status: RevisionStatus = RevisionStatus.DRAFT

    @model_validator(mode="after")
    def validate_revision(self) -> "WorkspaceRevision":
        _safe_id(self.revision_id, "revision_id")
        _safe_id(self.author_instance_id, "author_instance_id")
        if self.parent_revision_id is not None:
            _safe_id(self.parent_revision_id, "parent_revision_id")
        if Path(self.artifact_path).is_absolute() or ".." in Path(self.artifact_path).parts:
            raise ValueError("artifact_path must stay inside the project workspace")
        if len(self.artifact_sha256) != 64 or any(c not in "0123456789abcdef" for c in self.artifact_sha256):
            raise ValueError("artifact_sha256 must be a lowercase SHA-256 digest")
        if not self.change_reason.strip():
            raise ValueError("workspace revisions require a change reason")
        return self


class ProjectWorkspaceManifest(BaseModel):
    schema_version: str = "onebrief-project-workspace-v1"
    project_id: str
    goal_summary: str
    agent_instances: list[str] = Field(default_factory=list)


AGENT_SPACE_DIRECTORIES = ("packs", "examples", "evaluations", "versions")
PROJECT_DIRECTORIES = (
    "00_contract",
    "01_sources/immutable",
    "01_sources/knowledge_candidates",
    "01_sources/approved_knowledge",
    "02_plan_and_teams",
    "03_team_workspaces",
    "04_review_requests",
    "05_accepted_artifacts",
    "06_decisions_and_evidence",
    "07_budget_and_usage",
    "08_execution_log",
)


class WorkspaceManager:
    def __init__(self, root: Path, registry: AgentRegistry | None = None):
        self.root = root.resolve()
        self.registry = registry or AgentRegistry()

    def initialize_agent_library(self) -> Path:
        library = self.root / "agent_library"
        library.mkdir(parents=True, exist_ok=True)
        registry_payload = {
            "schema_version": "onebrief-agent-registry-v1",
            "agent_types": [card.agent_type.value for card in self.registry.list()],
        }
        _write_new_or_equal(library / "registry.json", _stable_json(registry_payload))
        for card in self.registry.list():
            space = library / card.agent_type.value
            for name in AGENT_SPACE_DIRECTORIES:
                (space / name).mkdir(parents=True, exist_ok=True)
            _write_new_or_equal(space / "card.json", _stable_json(card))
        return library

    def create_project(self, project_id: str, goal_summary: str) -> Path:
        project_id = _safe_id(project_id, "project_id")
        if not goal_summary.strip():
            raise ValueError("goal_summary is required")
        project = self.root / "projects" / project_id
        for name in PROJECT_DIRECTORIES:
            (project / name).mkdir(parents=True, exist_ok=True)
        manifest_path = project / "project.json"
        if manifest_path.exists():
            existing = ProjectWorkspaceManifest.model_validate_json(
                manifest_path.read_text(encoding="utf-8")
            )
            if existing.project_id != project_id or existing.goal_summary != goal_summary:
                raise FileExistsError(
                    f"refusing to change an existing project identity: {manifest_path}"
                )
            return project
        manifest = ProjectWorkspaceManifest(project_id=project_id, goal_summary=goal_summary)
        _write_new_or_equal(manifest_path, _stable_json(manifest))
        return project

    def add_agent_instance(self, instance: AgentInstance) -> Path:
        project_id = _safe_id(instance.project_id, "project_id")
        team_id = _safe_id(instance.team_id, "team_id")
        instance_id = _safe_id(instance.instance_id, "instance_id")
        project = self.root / "projects" / project_id
        manifest_path = project / "project.json"
        if not manifest_path.exists():
            raise FileNotFoundError(f"project workspace is not initialized: {project_id}")
        self.registry.get(instance.agent_type)
        agent_space = project / "03_team_workspaces" / team_id / "agents" / instance_id
        for name in ("private_working", "submissions", "handoffs"):
            (agent_space / name).mkdir(parents=True, exist_ok=True)
        _write_new_or_equal(agent_space / "instance.json", _stable_json(instance))

        manifest = ProjectWorkspaceManifest.model_validate_json(
            manifest_path.read_text(encoding="utf-8")
        )
        if instance_id not in manifest.agent_instances:
            manifest.agent_instances.append(instance_id)
            content = _stable_json(manifest)
            temp = manifest_path.with_suffix(f".{uuid4().hex}.tmp")
            temp.write_text(content, encoding="utf-8")
            os.replace(temp, manifest_path)
        return agent_space

    def record_revision(self, project_id: str, revision: WorkspaceRevision) -> Path:
        project_id = _safe_id(project_id, "project_id")
        project = self.root / "projects" / project_id
        if not (project / "project.json").exists():
            raise FileNotFoundError(f"project workspace is not initialized: {project_id}")
        revision_path = project / "08_execution_log" / "revisions" / f"{revision.revision_id}.json"
        _write_new_or_equal(revision_path, _stable_json(revision))
        return revision_path


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()
