"""Goal-driven team planning and deterministic project instance deployment."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator

from onebrief.agent_registry import (
    APPROVED_MODEL_CATALOG,
    AgentInstance,
    AgentRegistry,
    AgentType,
    ApprovedModel,
    MemoryScope,
    PackGrant,
    TemperamentAssignment,
)
from onebrief.schemas import IntakeRequest, InternalSource, RequirementsAnalysis
from onebrief.workspaces import WorkspaceManager


TEAM_PLANNING_OUTPUT_CAP = 3000

PACK_CATALOG: dict[str, tuple[str, ...]] = {
    "knowledge_packs": ("project-contract", "approved-sources", "acceptance-criteria"),
    "tool_packs": ("structured-gemini", "artifact-workspace"),
    "template_packs": ("requested-output", "evidence-handoff", "review-verdict"),
    "rule_packs": ("budget-gate", "evidence-grounding", "independent-review"),
}


class StructuredGateway(Protocol):
    def generate_json(self, **kwargs: object) -> BaseModel: ...


class TeamMemberPlan(BaseModel):
    instance_id: str
    team_id: str
    agent_type: AgentType
    role_title: str
    responsibility: str
    perspective: str
    selection_reason: str
    temperament: TemperamentAssignment
    model: ApprovedModel
    model_selection_reason: str
    packs: PackGrant


class TeamDefinition(BaseModel):
    team_id: str
    mission: str
    lead_instance_id: str
    member_instance_ids: list[str]


class TeamPlan(BaseModel):
    schema_version: str = "onebrief-team-plan-v1"
    project_id: str
    goal_summary: str
    planning_rationale: str
    teams: list[TeamDefinition]
    members: list[TeamMemberPlan] = Field(min_length=4, max_length=9)
    stage_owners: dict[str, str]
    omitted_agent_types: list[AgentType] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_references(self) -> "TeamPlan":
        member_ids = [member.instance_id for member in self.members]
        if len(member_ids) != len(set(member_ids)):
            raise ValueError("team plan contains duplicate instance IDs")
        team_ids = [team.team_id for team in self.teams]
        if len(team_ids) != len(set(team_ids)):
            raise ValueError("team plan contains duplicate team IDs")
        if len([m for m in self.members if m.agent_type == AgentType.PROJECT_OWNER]) != 1:
            raise ValueError("team plan requires exactly one project owner")
        known_members = set(member_ids)
        known_teams = set(team_ids)
        for member in self.members:
            if member.team_id not in known_teams:
                raise ValueError(f"member references unknown team: {member.team_id}")
        for team in self.teams:
            if team.lead_instance_id not in team.member_instance_ids:
                raise ValueError(f"team lead is not a member of {team.team_id}")
            if not team.member_instance_ids or not set(team.member_instance_ids) <= known_members:
                raise ValueError(f"team contains unknown members: {team.team_id}")
            assigned = {m.instance_id for m in self.members if m.team_id == team.team_id}
            if assigned != set(team.member_instance_ids):
                raise ValueError(f"team membership is inconsistent: {team.team_id}")
        if not set(self.stage_owners.values()) <= known_members:
            raise ValueError("stage owner references an unknown agent instance")
        selected_types = {member.agent_type for member in self.members}
        if len(selected_types) != len(self.members):
            raise ValueError("a project may activate at most one instance of each base agent type")
        if selected_types & set(self.omitted_agent_types):
            raise ValueError("an active agent type cannot also be omitted")
        expected_omitted = set(AgentType) - selected_types
        if set(self.omitted_agent_types) != expected_omitted:
            raise ValueError("omitted_agent_types must exactly list every inactive base type")
        return self


class TeamDeployment(BaseModel):
    schema_version: str = "onebrief-team-deployment-v1"
    project_id: str
    team_plan_path: str
    deployed_instance_ids: list[str]
    stage_owners: dict[str, str]


def _safe_segment(value: str, field: str) -> None:
    if not value.strip() or value in {".", ".."} or "/" in value or "\\" in value:
        raise ValueError(f"{field} must be one safe path segment")


def validate_team_plan(plan: TeamPlan, *, public_research_allowed: bool) -> TeamPlan:
    """Enforce authority separation and executable stage ownership after model planning."""
    registry = AgentRegistry()
    for member in plan.members:
        _safe_segment(member.instance_id, "instance_id")
        _safe_segment(member.team_id, "team_id")
        registry.get(member.agent_type)
        if not member.model_selection_reason.strip():
            raise ValueError("every selected model requires a reason")
        for field, allowed in PACK_CATALOG.items():
            values = getattr(member.packs, field)
            unknown = set(values) - set(allowed)
            if unknown:
                raise ValueError(f"unknown {field}: {sorted(unknown)}")

    required_owners = {
        "evidence_analysis": AgentType.ANALYST,
        "long_form_draft": AgentType.MAKER,
        "revision": AgentType.MAKER,
        "independent_verification": AgentType.CRITIC,
    }
    if public_research_allowed:
        required_owners["public_research"] = AgentType.INVESTIGATOR
    by_id = {member.instance_id: member for member in plan.members}
    for stage, required_type in required_owners.items():
        owner_id = plan.stage_owners.get(stage)
        if owner_id is None:
            raise ValueError(f"team plan is missing stage owner: {stage}")
        if by_id[owner_id].agent_type != required_type:
            raise ValueError(f"{stage} must be owned by {required_type.value}")
    if plan.stage_owners["long_form_draft"] == plan.stage_owners["independent_verification"]:
        raise ValueError("artifact creation and independent verification must be separated")
    if public_research_allowed:
        investigator = by_id[plan.stage_owners["public_research"]]
        if investigator.model != ApprovedModel.GEMINI_2_5_FLASH:
            raise ValueError("public_research must use the approved Google Search-grounded model")
    for stage, owner_id in plan.stage_owners.items():
        if stage != "public_research" and by_id[owner_id].model == ApprovedModel.GEMINI_2_5_FLASH:
            raise ValueError("gemini-2.5-flash is reserved for public_research")
    return plan


class ProjectOwnerAgent:
    """The only model actor allowed to select a project's initial team."""

    def __init__(self, gateway: StructuredGateway):
        self.gateway = gateway

    def run(
        self,
        *,
        project_id: str,
        intake: IntakeRequest,
        requirements: RequirementsAnalysis,
        sources: list[InternalSource],
    ) -> TeamPlan:
        registry = AgentRegistry()
        cards = [
            {
                "agent_type": card.agent_type.value,
                "mission": card.mission,
                "activation_conditions": card.activation_conditions,
                "owned_deliverables": card.owned_deliverables,
                "authorities": card.authorities,
                "forbidden_actions": card.forbidden_actions,
            }
            for card in registry.list()
        ]
        contents = json.dumps(
            {
                "project_id": project_id,
                "goal": intake.goal,
                "desired_output": intake.desired_output,
                "public_research_allowed": intake.public_research_allowed,
                "normalized_goal": requirements.normalized_goal,
                "deliverables": requirements.deliverables,
                "acceptance_criteria": requirements.acceptance_criteria,
                "source_inventory": [
                    {"name": source.name, "priority": source.priority.value}
                    for source in sources
                ],
                "agent_cards": cards,
                "pack_catalog": PACK_CATALOG,
                "approved_model_catalog": {
                    model.value: description
                    for model, description in APPROVED_MODEL_CATALOG.items()
                },
            },
            ensure_ascii=False,
        )
        plan = self.gateway.generate_json(
            stage="team_planning",
            model="gemini-3.5-flash",
            contents=contents,
            schema=TeamPlan,
            max_output_tokens=TEAM_PLANNING_OUTPUT_CAP,
            temperature=0.1,
            system_instruction=(
                "You are the OneBrief Project Owner. Select the smallest sufficient team from the "
                "nine supplied agent cards. Never select all types by default. Exactly one project_owner "
                "must supervise the project. The current executable workflow always needs an analyst for "
                "evidence_analysis, a maker for long_form_draft and revision, and an independent critic "
                "for independent_verification; add an investigator for public_research when enabled. "
                "Add architect, creator, integrator, or guardian only when the goal genuinely activates "
                "their distinct accountability. Use only pack IDs in pack_catalog. Assign APT-3 temperament "
                "as a tie-breaker profile, not authority. Select one model for every member from "
                "approved_model_catalog and explain the cost/capability reason. Use gemini-2.5-flash only "
                "for the investigator that owns public_research; use gemini-3.5-flash for complex work and "
                "gemini-3.5-flash-lite only for bounded simpler work. Model selection never changes role "
                "authority. Use short safe lowercase IDs with hyphens and map every executable stage to "
                "one selected instance. List every unselected type in "
                "omitted_agent_types. Do not grant permissions; code derives authority from the registry."
            ),
        )
        if not isinstance(plan, TeamPlan):
            raise TypeError("project owner returned an invalid TeamPlan type")
        if plan.project_id != project_id:
            raise ValueError("project owner changed the immutable project ID")
        return validate_team_plan(plan, public_research_allowed=intake.public_research_allowed)


def _atomic_json(path: Path, value: BaseModel | dict[str, object]) -> None:
    payload = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
    content = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_text(encoding="utf-8") != content:
            raise FileExistsError(f"refusing to overwrite changed project record: {path}")
        return
    temp = path.with_suffix(path.suffix + f".{uuid4().hex}.tmp")
    temp.write_text(content, encoding="utf-8")
    os.replace(temp, path)


class TeamAssembler:
    """Turn a validated plan into authority-bounded agent instances and workspaces."""

    def __init__(self, workspace_root: Path):
        self.manager = WorkspaceManager(workspace_root)
        self.registry = self.manager.registry

    def deploy(
        self,
        plan: TeamPlan,
        *,
        intake: IntakeRequest,
        requirements: RequirementsAnalysis,
        sources: list[InternalSource],
    ) -> TeamDeployment:
        self.manager.initialize_agent_library()
        project = self.manager.create_project(plan.project_id, plan.goal_summary)
        _atomic_json(
            project / "00_contract" / "project_contract.json",
            {
                "goal": intake.goal,
                "desired_output": intake.desired_output,
                "deliverables": requirements.deliverables,
                "acceptance_criteria": requirements.acceptance_criteria,
                "public_research_allowed": intake.public_research_allowed,
            },
        )
        _atomic_json(
            project / "01_sources" / "approved_knowledge" / "source_inventory.json",
            {
                "sources": [
                    {"name": item.name, "priority": item.priority.value, "sha256": item.sha256}
                    for item in sources
                ]
            },
        )
        plan_path = project / "02_plan_and_teams" / "team_plan.json"
        _atomic_json(plan_path, plan)

        deployed: list[str] = []
        for member in plan.members:
            card = self.registry.get(member.agent_type)
            instance = AgentInstance(
                instance_id=member.instance_id,
                project_id=plan.project_id,
                team_id=member.team_id,
                agent_type=member.agent_type,
                role_title=member.role_title,
                responsibility=member.responsibility,
                authority_grants=list(card.authorities),
                perspective=member.perspective,
                temperament=member.temperament,
                model=member.model,
                model_selection_reason=member.model_selection_reason,
                packs=member.packs,
                memory_scopes=[
                    MemoryScope.PROJECT_SHARED,
                    MemoryScope.TEAM_SHARED,
                    MemoryScope.PRIVATE_WORKING,
                ],
            )
            self.manager.add_agent_instance(instance)
            deployed.append(instance.instance_id)

        deployment = TeamDeployment(
            project_id=plan.project_id,
            team_plan_path=plan_path.relative_to(project).as_posix(),
            deployed_instance_ids=deployed,
            stage_owners=plan.stage_owners,
        )
        _atomic_json(project / "02_plan_and_teams" / "deployment.json", deployment)
        return deployment


class TeamPlanningCoordinator:
    """Resume-safe boundary joining the owner decision to deterministic deployment."""

    def __init__(self, gateway: StructuredGateway, workspace_root: Path):
        self.owner = ProjectOwnerAgent(gateway)
        self.assembler = TeamAssembler(workspace_root)
        self.workspace_root = workspace_root

    def plan_and_deploy(
        self,
        *,
        project_id: str,
        intake: IntakeRequest,
        requirements: RequirementsAnalysis,
        sources: list[InternalSource],
    ) -> TeamDeployment:
        plan_path = (
            self.workspace_root / "projects" / project_id / "02_plan_and_teams" / "team_plan.json"
        )
        if plan_path.exists():
            plan = TeamPlan.model_validate_json(plan_path.read_text(encoding="utf-8"))
            validate_team_plan(plan, public_research_allowed=intake.public_research_allowed)
        else:
            plan = self.owner.run(
                project_id=project_id,
                intake=intake,
                requirements=requirements,
                sources=sources,
            )
        return self.assembler.deploy(
            plan,
            intake=intake,
            requirements=requirements,
            sources=sources,
        )
