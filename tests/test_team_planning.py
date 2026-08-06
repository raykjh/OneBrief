import json
from pathlib import Path

import pytest

from onebrief.agent_registry import AgentType, PackGrant, TemperamentAssignment
from onebrief.schemas import IntakeRequest, InternalSource, RequirementsAnalysis, SourcePriority
from onebrief.team_planning import (
    ProjectOwnerAgent,
    TeamAssembler,
    TeamDefinition,
    TeamMemberPlan,
    TeamPlan,
    TeamPlanningCoordinator,
    validate_team_plan,
)


def _member(instance_id: str, agent_type: AgentType, team_id: str = "delivery") -> TeamMemberPlan:
    return TeamMemberPlan(
        instance_id=instance_id,
        team_id=team_id,
        agent_type=agent_type,
        role_title=f"{agent_type.value} role",
        responsibility=f"Own the {agent_type.value} deliverable.",
        perspective=f"Independent {agent_type.value} perspective.",
        selection_reason="Required by the goal and execution contract.",
        temperament=TemperamentAssignment(pace="T", orientation="F", scope="G"),
        model=(
            "gemini-2.5-flash"
            if agent_type == AgentType.INVESTIGATOR
            else "gemini-3.5-flash"
        ),
        model_selection_reason="Approved capability and cost fit.",
        packs=PackGrant(
            knowledge_packs=["project-contract", "approved-sources"],
            tool_packs=["structured-gemini", "artifact-workspace"],
            template_packs=["requested-output"],
            rule_packs=["budget-gate", "evidence-grounding", "independent-review"],
        ),
    )


def _plan(project_id: str = "job-123") -> TeamPlan:
    members = [
        _member("owner-01", AgentType.PROJECT_OWNER),
        _member("analyst-01", AgentType.ANALYST),
        _member("maker-01", AgentType.MAKER),
        _member("critic-01", AgentType.CRITIC),
    ]
    return TeamPlan(
        project_id=project_id,
        goal_summary="Create a grounded guide.",
        planning_rationale="A compact evidence, production, and review team is sufficient.",
        teams=[TeamDefinition(
            team_id="delivery",
            mission="Produce and independently verify the requested artifact.",
            lead_instance_id="owner-01",
            member_instance_ids=[member.instance_id for member in members],
        )],
        members=members,
        stage_owners={
            "evidence_analysis": "analyst-01",
            "long_form_draft": "maker-01",
            "revision": "maker-01",
            "independent_verification": "critic-01",
        },
        omitted_agent_types=[
            AgentType.ARCHITECT, AgentType.INVESTIGATOR, AgentType.CREATOR,
            AgentType.INTEGRATOR, AgentType.GUARDIAN,
        ],
    )


def _requirements() -> RequirementsAnalysis:
    return RequirementsAnalysis(
        supported=True,
        support_reason="Inputs are sufficient.",
        normalized_goal="Create a grounded guide.",
        deliverables=["Guide"],
        mandatory_information=[], optional_information=[],
        acceptance_criteria=["Cite the source."], assumptions=[], consolidated_questions=[],
        ready_for_estimate=True,
    )


def _source() -> InternalSource:
    return InternalSource(name="policy.md", priority=SourcePriority.MANDATORY, content="Rule A.")


class FakeGateway:
    def __init__(self, plan: TeamPlan):
        self.plan = plan
        self.calls = 0

    def generate_json(self, *, schema: type, **_: object):
        self.calls += 1
        assert schema is TeamPlan
        return self.plan


def test_project_owner_selects_and_validates_minimal_team() -> None:
    gateway = FakeGateway(_plan())
    result = ProjectOwnerAgent(gateway).run(
        project_id="job-123",
        intake=IntakeRequest(goal="Create a grounded guide."),
        requirements=_requirements(),
        sources=[_source()],
    )
    assert gateway.calls == 1
    assert [member.agent_type for member in result.members] == [
        AgentType.PROJECT_OWNER, AgentType.ANALYST, AgentType.MAKER, AgentType.CRITIC,
    ]


def test_wrong_stage_owner_is_rejected() -> None:
    plan = _plan().model_copy(deep=True)
    plan.stage_owners["independent_verification"] = "maker-01"
    with pytest.raises(ValueError, match="must be owned by critic"):
        validate_team_plan(plan, public_research_allowed=False)


def test_assembler_derives_authority_and_materializes_instances(tmp_path: Path) -> None:
    deployment = TeamAssembler(tmp_path).deploy(
        _plan(),
        intake=IntakeRequest(goal="Create a grounded guide."),
        requirements=_requirements(),
        sources=[_source()],
    )
    project = tmp_path / "projects" / "job-123"
    assert deployment.deployed_instance_ids == ["owner-01", "analyst-01", "maker-01", "critic-01"]
    maker = json.loads(
        (project / "03_team_workspaces" / "delivery" / "agents" / "maker-01" / "instance.json")
        .read_text(encoding="utf-8")
    )
    assert maker["authority_grants"]
    assert maker["packs"]["knowledge_packs"] == ["project-contract", "approved-sources"]
    assert (project / "02_plan_and_teams" / "team_plan.json").exists()
    assert (project / "00_contract" / "project_contract.json").exists()


def test_coordinator_resumes_without_replanning(tmp_path: Path) -> None:
    gateway = FakeGateway(_plan())
    coordinator = TeamPlanningCoordinator(gateway, tmp_path)
    kwargs = {
        "project_id": "job-123",
        "intake": IntakeRequest(goal="Create a grounded guide."),
        "requirements": _requirements(),
        "sources": [_source()],
    }
    coordinator.plan_and_deploy(**kwargs)
    coordinator.plan_and_deploy(**kwargs)
    assert gateway.calls == 1
