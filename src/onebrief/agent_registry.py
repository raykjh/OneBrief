"""Project-neutral agent cards and the default OneBrief agent registry."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field, model_validator

from onebrief.temperament import Orientation, Pace, Scope


class AgentType(StrEnum):
    PROJECT_OWNER = "project_owner"
    ARCHITECT = "architect"
    INVESTIGATOR = "investigator"
    ANALYST = "analyst"
    CREATOR = "creator"
    MAKER = "maker"
    INTEGRATOR = "integrator"
    CRITIC = "critic"
    GUARDIAN = "guardian"


class ApprovedModel(StrEnum):
    GEMINI_3_1_PRO_PREVIEW = "gemini-3.1-pro-preview"
    GEMINI_3_5_FLASH = "gemini-3.5-flash"
    GEMINI_3_5_FLASH_LITE = "gemini-3.5-flash-lite"


APPROVED_MODEL_CATALOG: dict[ApprovedModel, str] = {
    ApprovedModel.GEMINI_3_1_PRO_PREVIEW: (
        "Higher-reasoning preview model reserved for decision-critical planning, "
        "independent verification, governance, and final approval."
    ),
    ApprovedModel.GEMINI_3_5_FLASH: "Primary model for complex planning, creation, and review.",
    ApprovedModel.GEMINI_3_5_FLASH_LITE: "Lower-cost model for bounded, simpler structured work.",
}


class MemoryScope(StrEnum):
    PROJECT_SHARED = "project_shared"
    TEAM_SHARED = "team_shared"
    PRIVATE_WORKING = "private_working"


class TemperamentAssignment(BaseModel):
    """Tie-breaker temperament assigned with a project's capability packs."""

    pace: Pace
    orientation: Orientation
    scope: Scope

    @property
    def code(self) -> str:
        return f"{self.pace.value}{self.orientation.value}{self.scope.value}"


class PackGrant(BaseModel):
    knowledge_packs: list[str] = Field(default_factory=list)
    skill_packs: list[str] = Field(default_factory=list)
    tool_packs: list[str] = Field(default_factory=list)
    template_packs: list[str] = Field(default_factory=list)
    rule_packs: list[str] = Field(default_factory=list)


class AgentCard(BaseModel):
    schema_version: str = "onebrief-agent-card-v1"
    agent_type: AgentType
    display_name: str
    mission: str
    activation_conditions: list[str]
    owned_deliverables: list[str]
    authorities: list[str]
    forbidden_actions: list[str]
    input_contracts: list[str]
    output_contracts: list[str]
    independent_from: list[AgentType] = Field(default_factory=list)

    @model_validator(mode="after")
    def require_distinct_accountability(self) -> "AgentCard":
        required = (
            self.display_name,
            self.mission,
            *self.activation_conditions,
            *self.owned_deliverables,
            *self.authorities,
            *self.forbidden_actions,
        )
        if any(not value.strip() for value in required):
            raise ValueError("agent cards cannot contain blank responsibilities")
        if not self.owned_deliverables:
            raise ValueError("every agent type must own at least one deliverable")
        return self


class AgentInstance(BaseModel):
    schema_version: str = "onebrief-agent-instance-v1"
    instance_id: str
    project_id: str
    team_id: str
    agent_type: AgentType
    role_title: str
    responsibility: str
    authority_grants: list[str]
    perspective: str
    temperament: TemperamentAssignment
    model: ApprovedModel
    model_selection_reason: str
    packs: PackGrant
    memory_scopes: list[MemoryScope]

    @model_validator(mode="after")
    def validate_assignment(self) -> "AgentInstance":
        if not self.instance_id.strip() or not self.project_id.strip() or not self.team_id.strip():
            raise ValueError("agent instances require stable project, team, and instance IDs")
        if not self.responsibility.strip() or not self.perspective.strip():
            raise ValueError("agent instances require responsibility and perspective")
        if not self.model_selection_reason.strip():
            raise ValueError("agent instances require a model selection reason")
        if len(self.memory_scopes) != len(set(self.memory_scopes)):
            raise ValueError("memory scopes must be unique")
        return self


DEFAULT_AGENT_CARDS: tuple[AgentCard, ...] = (
    AgentCard(agent_type=AgentType.PROJECT_OWNER, display_name="Project Owner", mission="Turn the goal into an approvable project contract and execution organization while preserving overall direction.", activation_conditions=["Project intake and operational authorization"], owned_deliverables=["Project contract", "Team plan", "Final operational approval"], authorities=["Form teams within the approved budget", "Assign work packs", "Request revision or termination"], forbidden_actions=["Unilaterally change the goal or budget", "Independently approve the quality of its own result"], input_contracts=["User goal", "Provided sources", "Budget ceiling"], output_contracts=["ProjectContract", "TeamPlan", "MergeDecision"], independent_from=[AgentType.CRITIC, AgentType.GUARDIAN]),
    AgentCard(agent_type=AgentType.ARCHITECT, display_name="Architect", mission="Decompose the goal into a work structure and execution order with explicit dependencies.", activation_conditions=["The goal requires multiple steps, teams, or deliverables"], owned_deliverables=["Work structure", "Dependency plan", "Completion criteria"], authorities=["Propose work decomposition and ordering"], forbidden_actions=["Give final approval to the plan", "Approve deliverable quality"], input_contracts=["ProjectContract", "Initial work packs"], output_contracts=["TaskPlan"], independent_from=[]),
    AgentCard(agent_type=AgentType.INVESTIGATOR, display_name="Investigator", mission="Find missing material and evidence, then submit source-grounded candidate knowledge.", activation_conditions=["Required evidence is missing or public research is authorized"], owned_deliverables=["Candidate knowledge bundle", "Source list", "Information-gap report"], authorities=["Search for and collect material within the authorized scope"], forbidden_actions=["Promote candidate knowledge to official knowledge", "Assert facts without sources"], input_contracts=["Information gaps", "Authorized research scope"], output_contracts=["KnowledgeCandidateBundle"], independent_from=[]),
    AgentCard(agent_type=AgentType.ANALYST, display_name="Analyst", mission="Compare and structure authorized material into traceable meaning and decision evidence.", activation_conditions=["The work requires interpretation, comparison, classification, or evidence extraction"], owned_deliverables=["Analysis findings", "Evidence bundle", "Uncertainty report"], authorities=["Interpret and compare authorized material"], forbidden_actions=["Assert unsupported conclusions", "Approve the final deliverable"], input_contracts=["Authorized knowledge", "Analysis question"], output_contracts=["EvidenceBundle", "AnalysisPackage"], independent_from=[]),
    AgentCard(agent_type=AgentType.CREATOR, display_name="Creator", mission="Propose new directions and alternatives within the approved contract and canon.", activation_conditions=["The task has no single answer and requires creative alternatives"], owned_deliverables=["Alternative set", "Creative direction", "Selection trade-offs"], authorities=["Propose new alternatives within the authorized scope"], forbidden_actions=["Make the final selection", "Change canon or rules"], input_contracts=["ProjectContract", "Creative constraints"], output_contracts=["CreativeOptions"], independent_from=[]),
    AgentCard(agent_type=AgentType.MAKER, display_name="Maker", mission="Implement the approved plan and evidence as a real user-facing deliverable.", activation_conditions=["A document, codebase, table, content set, or other artifact must be produced"], owned_deliverables=["Draft or deliverable revision", "Production decision record"], authorities=["Create and revise the assigned deliverable"], forbidden_actions=["Give final approval to its own result", "Unilaterally alter authorized sources"], input_contracts=["TaskPlan", "EvidenceBundle", "Selected direction"], output_contracts=["ArtifactRevision"], independent_from=[AgentType.CRITIC, AgentType.GUARDIAN]),
    AgentCard(agent_type=AgentType.INTEGRATOR, display_name="Integrator", mission="Combine approved partial results into one consistent deliverable without contradictions.", activation_conditions=["Two or more approved results must be combined"], owned_deliverables=["Integrated artifact", "Continuity record", "Merge-conflict report"], authorities=["Editorially merge approved results"], forbidden_actions=["Merge rejected results", "Resolve conflicts without evidence"], input_contracts=["AcceptedArtifact list", "Integration criteria"], output_contracts=["IntegratedArtifact"], independent_from=[]),
    AgentCard(agent_type=AgentType.CRITIC, display_name="Verifier", mission="Independently determine whether the result sufficiently meets the goal and success criteria.", activation_conditions=["A production result requires quality and completion review"], owned_deliverables=["Quality verdict", "Revision instructions", "Unmet criteria"], authorities=["Issue PASS, REVISE, or NEEDS_INFORMATION"], forbidden_actions=["Directly edit the reviewed artifact", "Waive rule violations"], input_contracts=["ProjectContract", "Artifact under review", "Success criteria"], output_contracts=["ReviewVerdict"], independent_from=[AgentType.MAKER, AgentType.PROJECT_OWNER]),
    AgentCard(agent_type=AgentType.GUARDIAN, display_name="Guardian", mission="Determine whether the result and process comply with canon, policy, authority, security, and safety boundaries.", activation_conditions=["Internal rules, canon, security, or authority boundaries apply"], owned_deliverables=["Compliance verdict", "Conflict report", "Rejection rationale"], authorities=["Issue PASS, REJECT, or USER_DECISION", "Recommend approval of official-knowledge candidates"], forbidden_actions=["Unilaterally merge conflicting canon", "Directly edit the reviewed artifact"], input_contracts=["Rule packs", "Canonical knowledge packs", "Artifact under review"], output_contracts=["GuardVerdict"], independent_from=[AgentType.MAKER, AgentType.PROJECT_OWNER]),
)


class AgentRegistry:
    def __init__(self, cards: tuple[AgentCard, ...] = DEFAULT_AGENT_CARDS):
        self._cards = {card.agent_type: card for card in cards}
        if len(self._cards) != len(cards):
            raise ValueError("agent registry contains duplicate agent types")
        missing = set(AgentType) - set(self._cards)
        if missing:
            raise ValueError(f"agent registry is missing: {sorted(item.value for item in missing)}")

    def list(self) -> tuple[AgentCard, ...]:
        return tuple(self._cards[item] for item in AgentType)

    def get(self, agent_type: AgentType) -> AgentCard:
        return self._cards[agent_type]
