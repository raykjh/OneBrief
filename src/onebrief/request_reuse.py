"""Detect repeated user intent and safely seed compatible prior work artifacts."""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from onebrief.project_catalog import RegisteredProject
from onebrief.schemas import IntakeRequest, ToolPackId


def _normalized(value: str | None) -> str:
    return " ".join((value or "").casefold().split())


def _project_id(intake: IntakeRequest) -> str | None:
    if intake.existing_project_id:
        return intake.existing_project_id
    if ToolPackId.EXCHANGE_DEVELOPMENT in intake.toolpack_ids:
        return "exchange"
    return None


def request_fingerprint(intake: IntakeRequest) -> str:
    sources = []
    for source in intake.internal_sources:
        digest = source.sha256 or hashlib.sha256(source.content.encode("utf-8")).hexdigest()
        sources.append({
            "name": source.name.casefold(),
            "priority": source.priority.value,
            "requirement_keys": sorted(source.requirement_keys),
            "sha256": digest,
        })
    payload = {
        "goal": _normalized(intake.goal),
        "desired_output": _normalized(intake.desired_output),
        "output_target": intake.output_target.value,
        "existing_project_id": _project_id(intake),
        "public_research_allowed": intake.public_research_allowed,
        "max_revision_rounds": intake.max_revision_rounds,
        "toolpack_ids": sorted(item.value for item in intake.toolpack_ids),
        "sources": sorted(sources, key=lambda item: (item["name"], item["sha256"])),
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ReuseCandidate:
    job_id: str
    job_dir: Path
    status: str
    reusable_artifacts: tuple[str, ...]
    project_head_sha: str | None

    def public_summary(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "status": self.status,
            "reusable_artifacts": list(self.reusable_artifacts),
            "message": (
                "같은 요청의 이전 작업을 찾았습니다. 이미 완료된 조사·분석·호환 가능한 "
                "코드 초안을 재사용하여 처음부터 반복하지 않습니다."
            ),
        }


def _inspection_head(job_dir: Path) -> str | None:
    for toolpack_id in (
        ToolPackId.PROJECT_DEVELOPMENT.value,
        ToolPackId.EXCHANGE_DEVELOPMENT.value,
    ):
        path = (
            job_dir / "work" / "toolpacks" / toolpack_id / "evidence"
            / "repository_inspection.json"
        )
        try:
            value = json.loads(path.read_text(encoding="utf-8")).get("head_sha")
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(value, str) and len(value) == 40:
            return value
    return None


def _artifact_names(job_dir: Path, *, code_compatible: bool) -> tuple[str, ...]:
    work = job_dir / "work"
    names: list[str] = []
    for name in ("project_architecture.json", "public_research.json", "public_research.md", "analysis.json"):
        if (work / name).is_file():
            names.append(name)
    if code_compatible:
        if (work / "code_change_set_retry_r1.json").is_file():
            names.append("code_change_set_retry_r1.json")
        elif (work / "code_change_set.json").is_file():
            names.append("code_change_set.json")
    return tuple(names)


def find_reuse_candidate(
    jobs_root: Path,
    intake: IntakeRequest,
    project: RegisteredProject | None,
) -> ReuseCandidate | None:
    if not jobs_root.is_dir():
        return None
    wanted = request_fingerprint(intake)
    candidates: list[tuple[int, int, float, ReuseCandidate]] = []
    for job_dir in jobs_root.iterdir():
        if not job_dir.is_dir() or job_dir.name.startswith("."):
            continue
        try:
            prior = IntakeRequest.model_validate_json(
                (job_dir / "inputs" / "intake.json").read_text(encoding="utf-8")
            )
            record = json.loads((job_dir / "job.json").read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if request_fingerprint(prior) != wanted:
            continue
        status = str(record.get("status", ""))
        if status not in {
            "failed", "partial", "needs_budget", "needs_information",
            "needs_authorization",
        }:
            continue
        inspected_head = _inspection_head(job_dir)
        code_compatible = bool(
            project
            and project.ready_for_isolated_edit
            and project.head_sha
            and project.head_sha == inspected_head
        )
        names = _artifact_names(job_dir, code_compatible=code_compatible)
        if not names:
            continue
        candidate = ReuseCandidate(
            job_id=str(record.get("job_id", job_dir.name)),
            job_dir=job_dir.resolve(),
            status=status,
            reusable_artifacts=names,
            project_head_sha=inspected_head,
        )
        has_code = any(name.startswith("code_change_set") for name in names)
        legacy_delta_risk = bool(
            has_code
            and (job_dir / "work" / "code_change_set_retry_r1.json").is_file()
            and not (job_dir / "work" / "code_change_set_retry_delta_r1.json").is_file()
        )
        candidates.append((int(has_code), int(not legacy_delta_risk), job_dir.stat().st_mtime, candidate))
    return max(candidates, key=lambda item: item[:3])[3] if candidates else None


def seed_reusable_artifacts(candidate: ReuseCandidate, new_job_dir: Path) -> Path:
    source_work = candidate.job_dir / "work"
    target_work = new_job_dir / "work"
    copied: list[dict[str, str]] = []
    for name in candidate.reusable_artifacts:
        source = source_work / name
        target_name = "code_change_set.json" if name.startswith("code_change_set") else name
        target = target_work / target_name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        copied.append({"source": name, "target": target_name, "sha256": digest})
    manifest = {
        "schema_version": "onebrief-reuse-manifest-v1",
        "source_job_id": candidate.job_id,
        "reason": "identical request fingerprint and compatible approved project HEAD",
        "artifacts": copied,
    }
    path = target_work / "reuse_manifest.json"
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path
