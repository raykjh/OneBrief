"""Curated Google ADK SkillToolset bridge for goal-scoped OneBrief agents."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable

from google.adk.integrations.skill_registry.gcp_skill_registry import GCPSkillRegistry
from google.adk.skills import Skill, load_skill_from_dir
from google.adk.tools.skill_toolset import SkillToolset

SKILL_ROOT = Path(__file__).with_name("skills")
LOCAL_SKILL_IDS = (
    "existing-project-development",
    "implementation-verification",
    "financial-signal-validation",
)


def load_local_skills(skill_ids: Iterable[str]) -> list[Skill]:
    requested = list(dict.fromkeys(skill_ids))
    unknown = set(requested) - set(LOCAL_SKILL_IDS)
    if unknown:
        raise ValueError(f"unknown OneBrief skills: {sorted(unknown)}")
    return [load_skill_from_dir(SKILL_ROOT / skill_id) for skill_id in requested]


def build_skill_toolset(
    skill_ids: Iterable[str],
    *,
    additional_tools: list[object] | None = None,
    use_gcp_registry: bool = False,
) -> SkillToolset:
    registry = None
    if use_gcp_registry:
        registry = GCPSkillRegistry(
            project_id=os.environ.get("GOOGLE_CLOUD_PROJECT"),
            location=os.environ.get("GOOGLE_CLOUD_LOCATION", "global"),
        )
    return SkillToolset(
        skills=load_local_skills(skill_ids),
        registry=registry,
        additional_tools=additional_tools or [],
    )


def skill_instruction(skill_ids: Iterable[str]) -> str:
    """Compatibility context for structured calls until every stage runs as an ADK LlmAgent."""
    skills = load_local_skills(skill_ids)
    if not skills:
        return ""
    sections = [
        f"ACTIVE SKILL: {skill.name}\n{skill.instructions.strip()}"
        for skill in skills
    ]
    return "\n\n".join(sections)