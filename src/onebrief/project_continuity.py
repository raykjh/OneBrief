"""Persistent continuation context for approved existing projects."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field

from onebrief.project_catalog import RegisteredProject
from onebrief.schemas import IntakeRequest, InternalSource, SourcePriority, ToolPackId


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=20,
        shell=False,
        check=False,
    )
    return completed.stdout.strip() if completed.returncode == 0 else ""


class PreviousRun(BaseModel):
    job_id: str
    status: str
    updated_at: str
    stage: str
    message: str = ""
    result_package: str | None = None
    completed_stages: list[str] = Field(default_factory=list)
    incomplete_stages: list[str] = Field(default_factory=list)
    failed_stages: list[str] = Field(default_factory=list)


class ProjectContinuationState(BaseModel):
    schema_version: str = "onebrief-project-continuation-v1"
    project_id: str
    canonical_goal: str
    desired_output: str | None = None
    acceptance_criteria: list[str] = Field(default_factory=list)
    decisions: list[str] = Field(default_factory=list)
    completed_work: list[str] = Field(default_factory=list)
    pending_work: list[str] = Field(default_factory=list)
    failed_work: list[str] = Field(default_factory=list)
    last_run_id: str | None = None
    last_result_package: str | None = None
    repository_head_sha: str | None = None
    updated_at: str


class ProjectContinuationContext(BaseModel):
    project: RegisteredProject
    state: ProjectContinuationState
    previous_runs: list[PreviousRun] = Field(default_factory=list)
    tracked_documents: list[str] = Field(default_factory=list)
    tracked_code: list[str] = Field(default_factory=list)
    document_excerpts: dict[str, str] = Field(default_factory=dict)

    def as_internal_source(self) -> InternalSource:
        payload = self.model_dump(mode="json")
        content = json.dumps(payload, ensure_ascii=False, indent=2)
        return InternalSource(
            name=f"onebrief-project-continuation-{self.project.project_id}.json",
            priority=SourcePriority.MANDATORY,
            requirement_keys=["existing_project_continuation"],
            summary=(
                "Authoritative continuation context reconstructed from the selected repository, "
                "its Git history, durable OneBrief state, and previous execution records."
            ),
            content=content,
            media_type="application/json",
            size_bytes=len(content.encode("utf-8")),
            sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
        )


class ProjectContinuityStore:
    """Keep project memory beside the repository without treating it as product source."""

    def __init__(self, project: RegisteredProject, jobs_root: Path):
        self.project = project
        self.root = Path(project.root_path).resolve()
        self.jobs_root = jobs_root.resolve()
        self.state_dir = (
            Path(project.workspace_path).resolve() / "memory"
            if project.workspace_path
            else self.root / ".onebrief"
        )
        self.state_path = self.state_dir / "project_state.json"

    @staticmethod
    def _matches_project(intake: IntakeRequest, project_id: str) -> bool:
        if intake.existing_project_id:
            return intake.existing_project_id == project_id
        return (
            project_id == "exchange"
            and ToolPackId.EXCHANGE_DEVELOPMENT in intake.toolpack_ids
        )

    @staticmethod
    def _failure_tail(value: object, limit: int = 1000) -> str:
        text = str(value or "")
        if len(text) <= limit:
            return text
        return "..." + text[-(limit - 3):]

    def _run_summary(self, job_dir: Path) -> PreviousRun | None:
        try:
            record = json.loads((job_dir / "job.json").read_text(encoding="utf-8"))
            intake = IntakeRequest.model_validate_json(
                (job_dir / "inputs" / "intake.json").read_text(encoding="utf-8")
            )
        except (OSError, ValueError, json.JSONDecodeError):
            return None
        if not self._matches_project(intake, self.project.project_id):
            return None
        completed: list[str] = []
        incomplete: list[str] = []
        failed: list[str] = []
        graph_path = job_dir / "work" / "execution_graph_state.json"
        if graph_path.is_file():
            try:
                nodes = json.loads(graph_path.read_text(encoding="utf-8")).get("nodes", {})
                for stage, value in nodes.items():
                    status = str(value.get("status", "pending"))
                    if status == "complete":
                        completed.append(stage)
                    elif status in {"failed", "blocked"}:
                        failed.append(stage)
                    else:
                        incomplete.append(stage)
            except (OSError, json.JSONDecodeError, AttributeError):
                pass
        return PreviousRun(
            job_id=str(record.get("job_id", job_dir.name)),
            status=str(record.get("status", "unknown")),
            updated_at=str(record.get("updated_at", "")),
            stage=str(record.get("current_stage", "")),
            message=self._failure_tail(record.get("message", "")),
            result_package=record.get("result_package"),
            completed_stages=sorted(completed),
            incomplete_stages=sorted(incomplete),
            failed_stages=sorted(failed),
        )

    def previous_runs(self, limit: int = 8) -> list[PreviousRun]:
        if not self.jobs_root.is_dir():
            return []
        runs = [
            item
            for path in self.jobs_root.iterdir()
            if path.is_dir() and (item := self._run_summary(path)) is not None
        ]
        return sorted(runs, key=lambda item: item.updated_at, reverse=True)[:limit]

    def _latest_contract(self, runs: list[PreviousRun]) -> tuple[IntakeRequest | None, dict]:
        for run in runs:
            job_dir = self.jobs_root / run.job_id
            try:
                intake = IntakeRequest.model_validate_json(
                    (job_dir / "inputs" / "intake.json").read_text(encoding="utf-8")
                )
                requirements = json.loads(
                    (job_dir / "inputs" / "requirements.json").read_text(encoding="utf-8")
                )
                return intake, requirements
            except (OSError, ValueError, json.JSONDecodeError):
                continue
        return None, {}

    def _derived_state(self, runs: list[PreviousRun]) -> ProjectContinuationState:
        intake, requirements = self._latest_contract(runs)
        latest = runs[0] if runs else None
        completed: list[str] = []
        pending: list[str] = []
        failed: list[str] = []
        if latest:
            completed = latest.completed_stages
            pending = latest.incomplete_stages
            failed = latest.failed_stages
            if latest.status == "complete" and latest.result_package:
                completed = [*completed, "verified result package produced"]
                pending = [*pending, "review and apply the verified result to the source repository"]
            elif latest.status not in {"complete", "partial"}:
                failed = [
                    *failed,
                    f"{latest.stage}: {self._failure_tail(latest.message, 450)}",
                ]
        goal = str((intake.goal if intake else None) or requirements.get("normalized_goal") or self.project.canonical_goal or self.project.summary)
        return ProjectContinuationState(
            project_id=self.project.project_id,
            canonical_goal=goal,
            desired_output=intake.desired_output if intake else None,
            acceptance_criteria=list(requirements.get("acceptance_criteria", []))[:12],
            completed_work=list(dict.fromkeys(completed)),
            pending_work=list(dict.fromkeys(pending)),
            failed_work=list(dict.fromkeys(failed)),
            last_run_id=latest.job_id if latest else None,
            last_result_package=latest.result_package if latest else None,
            repository_head_sha=self.project.head_sha,
            updated_at=_now(),
        )

    def load_or_bootstrap(self) -> ProjectContinuationState:
        runs = self.previous_runs()
        if self.state_path.is_file():
            try:
                state = ProjectContinuationState.model_validate_json(
                    self.state_path.read_text(encoding="utf-8")
                )
                if state.repository_head_sha == self.project.head_sha:
                    return state
            except (OSError, ValueError):
                pass
        state = self._derived_state(runs)
        self._write(state)
        return state

    def _write(self, state: ProjectContinuationState) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix(f".{uuid4().hex}.tmp")
        temporary.write_text(state.model_dump_json(indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, self.state_path)

    def _repository_inventory(self) -> tuple[list[str], list[str], dict[str, str]]:
        tracked = [item for item in _git(self.root, "ls-files").splitlines() if item]
        docs = [
            item for item in tracked
            if item.casefold().endswith((".md", ".txt", ".json", ".yaml", ".yml"))
            and (
                item.casefold().startswith(("docs/", "readme", "data/"))
                or "brief" in item.casefold()
                or "checkpoint" in item.casefold()
            )
        ][:24]
        code = [
            item for item in tracked
            if item.casefold().endswith((".py", ".ts", ".tsx", ".js", ".mjs", ".css", ".html"))
        ][:80]
        excerpts: dict[str, str] = {}
        for relative in docs[:8]:
            text = _git(self.root, "show", f"HEAD:{relative}")
            if text:
                excerpts[relative] = text[:2000]
        return docs, code, excerpts

    def context(self) -> ProjectContinuationContext:
        runs = self.previous_runs()
        state = self.load_or_bootstrap()
        docs, code, excerpts = self._repository_inventory()
        return ProjectContinuationContext(
            project=self.project,
            state=state,
            previous_runs=runs,
            tracked_documents=docs,
            tracked_code=code,
            document_excerpts=excerpts,
        )

    def record_terminal_job(self, job_dir: Path) -> ProjectContinuationState:
        runs = self.previous_runs()
        current = self._run_summary(job_dir)
        if current is not None and all(item.job_id != current.job_id for item in runs):
            runs = [current, *runs]
        state = self._derived_state(sorted(runs, key=lambda item: item.updated_at, reverse=True))
        self._write(state)
        return state

