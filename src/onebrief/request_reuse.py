"""Detect repeated user intent and safely seed compatible prior work artifacts."""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from onebrief.project_catalog import RegisteredProject
from onebrief.development_progress import development_failure_quality
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
    development_quality: tuple[int, int] = (0, 0)

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


def _failed_development_pair(job_dir: Path) -> tuple[str, str, tuple[int, int]] | None:
    work = job_dir / "work"
    pairs: list[tuple[tuple[int, int], int, str, str]] = []
    if (
        (work / "development_best_candidate.json").is_file()
        and (work / "development_best_failure.txt").is_file()
    ):
        message = (work / "development_best_failure.txt").read_text(
            encoding="utf-8", errors="replace"
        )
        pairs.append((
            development_failure_quality(message),
            -1,
            "development_best_candidate.json",
            "development_best_failure.txt",
        ))
    for index in range(13):
        candidate = f"code_change_set_r{index}.json"
        failure = f"development_verification_failure_r{index}.txt"
        if (work / candidate).is_file() and (work / failure).is_file():
            message = (work / failure).read_text(encoding="utf-8", errors="replace")
            pairs.append((development_failure_quality(message), index, candidate, failure))
    if not pairs:
        return None
    quality, _round, candidate, failure = max(
        pairs, key=lambda item: (item[0], item[1])
    )
    return candidate, failure, quality


def _artifact_names(
    job_dir: Path, *, code_compatible: bool
) -> tuple[tuple[str, ...], tuple[int, int]]:
    work = job_dir / "work"
    names: list[str] = []
    for name in ("project_architecture.json", "public_research.json", "public_research.md", "analysis.json"):
        if (work / name).is_file():
            names.append(name)
    verified_development = False
    try:
        development = json.loads(
            (work / "development" / "development_run.json").read_text(encoding="utf-8")
        )
        verified_development = development.get("status") == "verified"
    except (OSError, json.JSONDecodeError):
        verified_development = False
    if code_compatible and verified_development:
        if (work / "code_change_set_retry_r1.json").is_file():
            names.append("code_change_set_retry_r1.json")
        elif (work / "code_change_set.json").is_file():
            names.append("code_change_set.json")
    quality = (0, 0)
    if code_compatible and not verified_development:
        failed_pair = _failed_development_pair(job_dir)
        if failed_pair is not None:
            candidate, failure, quality = failed_pair
            names.extend([candidate, failure])
    return tuple(names), quality


def find_reuse_candidate(
    jobs_root: Path,
    intake: IntakeRequest,
    project: RegisteredProject | None,
    *,
    preferred_job_dir: Path | None = None,
) -> ReuseCandidate | None:
    if not jobs_root.is_dir():
        return None
    wanted = request_fingerprint(intake)
    preferred = preferred_job_dir.resolve() if preferred_job_dir is not None else None
    candidates: list[
        tuple[int, int, int, int, int, float, ReuseCandidate]
    ] = []
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
        names, quality = _artifact_names(job_dir, code_compatible=code_compatible)
        if not names:
            continue
        candidate = ReuseCandidate(
            job_id=str(record.get("job_id", job_dir.name)),
            job_dir=job_dir.resolve(),
            status=status,
            reusable_artifacts=names,
            project_head_sha=inspected_head,
            development_quality=quality,
        )
        has_code = any(name.startswith("code_change_set") for name in names)
        legacy_delta_risk = bool(
            has_code
            and (job_dir / "work" / "code_change_set_retry_r1.json").is_file()
            and not (job_dir / "work" / "code_change_set_retry_delta_r1.json").is_file()
        )
        candidates.append((
            int(preferred is not None and job_dir.resolve() == preferred),
            int(has_code), quality[0], quality[1], int(not legacy_delta_risk),
            job_dir.stat().st_mtime, candidate,
        ))
    return max(candidates, key=lambda item: item[:6])[6] if candidates else None


def seed_reusable_artifacts(candidate: ReuseCandidate, new_job_dir: Path) -> Path:
    source_work = candidate.job_dir / "work"
    target_work = new_job_dir / "work"
    copied: list[dict[str, str]] = []
    for name in candidate.reusable_artifacts:
        source = source_work / name
        target_name = (
            "code_change_set.json"
            if name.startswith("code_change_set") or name == "development_best_candidate.json"
            else (
                "development_verification_failure.txt"
                if name.startswith("development_verification_failure")
                or name == "development_best_failure.txt"
                else name
            )
        )
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
