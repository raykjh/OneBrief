import json
from pathlib import Path

import pytest

from onebrief.agent_registry import AgentType, PackGrant, TemperamentAssignment
from onebrief.schemas import IntakeRequest, InternalSource, RequirementsAnalysis, SourcePriority, ToolPackId
from onebrief.team_planning import (
    ProjectOwnerAgent,
    TeamAssembler,
    TeamDefinition,
    TeamMemberPlan,
    TeamPlan,
    TeamPlanDraft,
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
            "gemini-3.5-flash"
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
        self.models: list[str] = []

    def generate_json(self, *, schema: type, model: str, **_: object):
        self.calls += 1
        self.models.append(model)
        assert schema is TeamPlanDraft
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
    assert gateway.models == ["gemini-3.1-pro-preview"]
    by_type = {member.agent_type: member for member in result.members}
    assert by_type[AgentType.PROJECT_OWNER].model.value == "gemini-3.1-pro-preview"
    assert by_type[AgentType.CRITIC].model.value == "gemini-3.1-pro-preview"
    assert by_type[AgentType.ANALYST].model.value == "gemini-3.5-flash"
    assert by_type[AgentType.MAKER].model.value == "gemini-3.5-flash"


def test_decision_policy_downgrades_pro_from_routine_roles() -> None:
    plan = _plan().model_copy(deep=True)
    analyst = next(member for member in plan.members if member.agent_type == AgentType.ANALYST)
    analyst.model = "gemini-3.1-pro-preview"
    analyst.model_selection_reason = "Provider selected Pro for all complex work."

    result = ProjectOwnerAgent(FakeGateway(plan)).run(
        project_id="job-123",
        intake=IntakeRequest(goal="Create a grounded guide."),
        requirements=_requirements(),
        sources=[_source()],
    )

    selected = next(member for member in result.members if member.agent_type == AgentType.ANALYST)
    assert selected.model.value == "gemini-3.5-flash"
    assert "Pro is reserved" in selected.model_selection_reason
def test_project_owner_makes_the_final_toolpack_selection() -> None:
    plan = _plan().model_copy(update={"toolpack_ids": [ToolPackId.EXCHANGE]})
    result = ProjectOwnerAgent(FakeGateway(plan)).run(
        project_id="job-123",
        intake=IntakeRequest(
            goal="Analyze exchange evidence.", toolpack_ids=[ToolPackId.EXCHANGE]
        ),
        requirements=_requirements(),
        sources=[_source()],
    )
    assert result.toolpack_ids == [ToolPackId.EXCHANGE]


def test_project_owner_cannot_activate_an_unavailable_toolpack() -> None:
    plan = _plan().model_copy(update={"toolpack_ids": [ToolPackId.EXCHANGE_DEVELOPMENT]})
    with pytest.raises(ValueError, match="unavailable ToolPacks"):
        ProjectOwnerAgent(FakeGateway(plan)).run(
            project_id="job-123",
            intake=IntakeRequest(
                goal="Analyze exchange evidence.", toolpack_ids=[ToolPackId.EXCHANGE]
            ),
            requirements=_requirements(),
            sources=[_source()],
        )


def test_project_owner_binds_public_research_to_the_grounded_model() -> None:
    base = _plan()
    investigator = _member("investigator-01", AgentType.INVESTIGATOR).model_copy(
        update={"model": "gemini-3.5-flash-lite"}
    )
    members = [*base.members, investigator]
    plan = base.model_copy(update={
        "members": members,
        "teams": [base.teams[0].model_copy(
            update={"member_instance_ids": [member.instance_id for member in members]}
        )],
        "stage_owners": {**base.stage_owners, "public_research": investigator.instance_id},
        "omitted_agent_types": [
            item for item in base.omitted_agent_types if item != AgentType.INVESTIGATOR
        ],
    })

    result = ProjectOwnerAgent(FakeGateway(plan)).run(
        project_id="job-123",
        intake=IntakeRequest(goal="Create a grounded guide.", public_research_allowed=True),
        requirements=_requirements(),
        sources=[_source()],
    )

    selected = next(
        member for member in result.members if member.agent_type == AgentType.INVESTIGATOR
    )
    assert selected.model.value == "gemini-3.5-flash"


def test_provider_omitted_investigator_is_recovered_for_public_research() -> None:
    result = ProjectOwnerAgent(FakeGateway(_plan())).run(
        project_id="job-123",
        intake=IntakeRequest(
            goal="Research and improve the existing project.",
            public_research_allowed=True,
        ),
        requirements=_requirements(),
        sources=[_source()],
    )

    investigator = next(
        member for member in result.members if member.agent_type == AgentType.INVESTIGATOR
    )
    assert result.stage_owners["public_research"] == investigator.instance_id
    assert investigator.model.value == "gemini-3.5-flash"
    assert "workflow-mandatory" in investigator.selection_reason



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

def test_provider_team_membership_references_are_repaired_before_strict_validation() -> None:
    valid = _plan()
    payload = valid.model_dump(mode="json")
    payload["teams"][0]["lead_instance_id"] = "missing-lead"
    payload["teams"][0]["member_instance_ids"] = ["critic-01"]
    draft = TeamPlanDraft.model_validate(payload)
    gateway = FakeGateway(draft)

    result = ProjectOwnerAgent(gateway).run(
        project_id="job-123",
        intake=IntakeRequest(goal="Create a grounded guide."),
        requirements=_requirements(),
        sources=[_source()],
    )

    team = result.teams[0]
    assert team.lead_instance_id == "owner-01"
    assert team.member_instance_ids == [member.instance_id for member in result.members]
    assert result.stage_owners["final_approval"] == "owner-01"


def test_provider_skill_assigned_to_wrong_role_is_repaired() -> None:
    payload = _plan().model_dump(mode="json")
    analyst = next(item for item in payload["members"] if item["agent_type"] == "analyst")
    analyst["packs"]["skill_packs"] = ["implementation-verification"]
    draft = TeamPlanDraft.model_validate(payload)

    result = ProjectOwnerAgent(FakeGateway(draft)).run(
        project_id="job-123",
        intake=IntakeRequest(goal="Create a grounded guide."),
        requirements=_requirements(),
        sources=[_source()],
    )

    normalized_analyst = next(
        member for member in result.members if member.agent_type == AgentType.ANALYST
    )
    assert "implementation-verification" not in normalized_analyst.packs.skill_packs
