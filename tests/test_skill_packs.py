import asyncio

import pytest

from onebrief.skill_packs import (
    LOCAL_SKILL_IDS,
    build_skill_toolset,
    load_local_skills,
    skill_instruction,
)


def test_local_adk_skills_load_with_progressive_disclosure_metadata() -> None:
    skills = load_local_skills(LOCAL_SKILL_IDS)
    assert [skill.name for skill in skills] == list(LOCAL_SKILL_IDS)
    assert all(skill.description and skill.instructions for skill in skills)


def test_skill_toolset_exposes_adk_skill_tools() -> None:
    toolset = build_skill_toolset(["implementation-verification"])
    tools = asyncio.run(toolset.get_tools())
    names = {tool.name for tool in tools}
    assert {"list_skills", "load_skill", "load_skill_resource", "run_skill_script"} <= names


def test_skill_instruction_is_bounded_to_selected_skills() -> None:
    text = skill_instruction(["financial-signal-validation"])
    assert "Financial signal validation" in text
    assert "Existing project development" not in text


def test_unknown_skill_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown OneBrief skills"):
        load_local_skills(["download-everything"])