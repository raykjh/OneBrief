from onebrief.agent_registry import AgentType, PackGrant, TemperamentAssignment
from onebrief.team_planning import TeamDefinition, TeamMemberPlan, TeamPlan


def minimal_team_plan(project_id: str, *, public_research: bool = False) -> TeamPlan:
    types = [AgentType.PROJECT_OWNER, AgentType.ANALYST, AgentType.MAKER, AgentType.CRITIC]
    if public_research:
        types.insert(1, AgentType.INVESTIGATOR)
    members = [
        TeamMemberPlan(
            instance_id=f"{agent_type.value}-01",
            team_id="delivery",
            agent_type=agent_type,
            role_title=f"{agent_type.value} role",
            responsibility=f"Own the {agent_type.value} responsibility.",
            perspective=f"Independent {agent_type.value} perspective.",
            selection_reason="Required by the executable workflow.",
            temperament=TemperamentAssignment(pace="T", orientation="F", scope="G"),
            model=(
                "gemini-3.5-flash"
                if agent_type == AgentType.INVESTIGATOR
                else (
                    "gemini-3.5-flash-lite"
                    if agent_type == AgentType.ANALYST
                    else "gemini-3.5-flash"
                )
            ),
            model_selection_reason="Matches the stage capability and approved cost envelope.",
            packs=PackGrant(
                knowledge_packs=["project-contract", "approved-sources"],
                tool_packs=["structured-gemini", "artifact-workspace"],
                template_packs=["requested-output"],
                rule_packs=["budget-gate", "evidence-grounding", "independent-review"],
            ),
        )
        for agent_type in types
    ]
    stage_owners = {
        "evidence_analysis": "analyst-01",
        "long_form_draft": "maker-01",
        "revision": "maker-01",
        "independent_verification": "critic-01",
    }
    if public_research:
        stage_owners["public_research"] = "investigator-01"
    return TeamPlan(
        project_id=project_id,
        goal_summary="Execute the approved test goal.",
        planning_rationale="Use the smallest team supported by the executable workflow.",
        teams=[TeamDefinition(
            team_id="delivery",
            mission="Create and independently verify the requested result.",
            lead_instance_id="project_owner-01",
            member_instance_ids=[member.instance_id for member in members],
        )],
        members=members,
        stage_owners=stage_owners,
        omitted_agent_types=sorted(set(AgentType) - set(types), key=lambda item: item.value),
    )
