import json
from pathlib import Path

import pytest

from onebrief.agent_registry import (
    AgentInstance,
    AgentRegistry,
    AgentType,
    MemoryScope,
    PackGrant,
    TemperamentAssignment,
)
from onebrief.workspaces import WorkspaceManager, WorkspaceRevision, sha256_file


def _instance() -> AgentInstance:
    return AgentInstance(
        instance_id="ares-maker-01",
        project_id="ares-feature",
        team_id="story-team",
        agent_type=AgentType.MAKER,
        role_title="ARES 장면 제작자",
        responsibility="승인된 장면 계획을 각본 장면으로 제작한다.",
        authority_grants=["배정된 장면 초안 작성"],
        perspective="현대전 전술과 드라마의 동시 가독성",
        temperament=TemperamentAssignment(pace="T", orientation="N", scope="L"),
        model="gemini-3.5-flash",
        model_selection_reason="Long-form artifact creation requires the primary model.",
        packs=PackGrant(
            knowledge_packs=["ares-canon"],
            tool_packs=["markdown-writer"],
            template_packs=["screenplay-scene"],
            rule_packs=["canon-priority"],
        ),
        memory_scopes=[MemoryScope.PROJECT_SHARED, MemoryScope.TEAM_SHARED, MemoryScope.PRIVATE_WORKING],
    )


def test_default_registry_has_exactly_nine_distinct_types() -> None:
    cards = AgentRegistry().list()
    assert len(cards) == 9
    assert {card.agent_type for card in cards} == set(AgentType)
    assert len({item for card in cards for item in card.owned_deliverables}) >= 9


def test_library_and_project_spaces_are_materialized(tmp_path: Path) -> None:
    manager = WorkspaceManager(tmp_path)
    library = manager.initialize_agent_library()
    assert json.loads((library / "registry.json").read_text(encoding="utf-8"))["agent_types"] == [
        item.value for item in AgentType
    ]
    for agent_type in AgentType:
        space = library / agent_type.value
        assert (space / "card.json").exists()
        assert {path.name for path in space.iterdir()} >= {
            "card.json", "packs", "examples", "evaluations", "versions"
        }

    project = manager.create_project("ares-feature", "Create one validated ARES screenplay.")
    assert (project / "01_sources" / "immutable").is_dir()
    assert (project / "01_sources" / "knowledge_candidates").is_dir()
    assert (project / "05_accepted_artifacts").is_dir()


def test_agent_instance_receives_temperament_and_packs_together(tmp_path: Path) -> None:
    manager = WorkspaceManager(tmp_path)
    manager.initialize_agent_library()
    manager.create_project("ares-feature", "Create one validated ARES screenplay.")
    space = manager.add_agent_instance(_instance())
    payload = json.loads((space / "instance.json").read_text(encoding="utf-8"))
    assert payload["temperament"] == {"orientation": "N", "pace": "T", "scope": "L"}
    assert payload["packs"]["knowledge_packs"] == ["ares-canon"]
    assert payload["packs"]["tool_packs"] == ["markdown-writer"]
    manifest = json.loads(
        (tmp_path / "projects" / "ares-feature" / "project.json").read_text(encoding="utf-8")
    )
    assert manifest["agent_instances"] == ["ares-maker-01"]


def test_changed_card_is_not_silently_overwritten(tmp_path: Path) -> None:
    manager = WorkspaceManager(tmp_path)
    library = manager.initialize_agent_library()
    card = library / AgentType.MAKER.value / "card.json"
    card.write_text("user change\n", encoding="utf-8")
    with pytest.raises(FileExistsError, match="refusing to overwrite"):
        manager.initialize_agent_library()


def test_revision_ledger_is_append_only_and_content_addressed(tmp_path: Path) -> None:
    manager = WorkspaceManager(tmp_path)
    project = manager.create_project("ares-feature", "Create one validated ARES screenplay.")
    artifact = project / "03_team_workspaces" / "story-team" / "scene.md"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text("Scene one", encoding="utf-8")
    revision = WorkspaceRevision(
        revision_id="rev-001",
        author_instance_id="ares-maker-01",
        artifact_path=artifact.relative_to(project).as_posix(),
        artifact_sha256=sha256_file(artifact),
        change_reason="Initial scene submission",
    )
    path = manager.record_revision("ares-feature", revision)
    assert path.exists()
    with pytest.raises(FileExistsError, match="refusing to overwrite"):
        manager.record_revision(
            "ares-feature",
            revision.model_copy(update={"change_reason": "Changed history"}),
        )
