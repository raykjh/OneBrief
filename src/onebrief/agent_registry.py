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
    GEMINI_3_5_FLASH = "gemini-3.5-flash"
    GEMINI_3_5_FLASH_LITE = "gemini-3.5-flash-lite"
    GEMINI_2_5_FLASH = "gemini-2.5-flash"


APPROVED_MODEL_CATALOG: dict[ApprovedModel, str] = {
    ApprovedModel.GEMINI_3_5_FLASH: "Primary model for complex planning, creation, and review.",
    ApprovedModel.GEMINI_3_5_FLASH_LITE: "Lower-cost model for bounded, simpler structured work.",
    ApprovedModel.GEMINI_2_5_FLASH: "Model reserved for Google Search-grounded public research.",
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
    AgentCard(
        agent_type=AgentType.PROJECT_OWNER,
        display_name="프로젝트 책임자",
        mission="목표를 승인 가능한 프로젝트 계약과 실행 조직으로 전환하고 전체 방향을 유지한다.",
        activation_conditions=["모든 프로젝트의 접수와 운영 승인"],
        owned_deliverables=["프로젝트 계약", "팀 구성안", "최종 운영 승인"],
        authorities=["승인 예산 안의 팀 편성", "작업팩 배정", "수정 또는 중단 요청"],
        forbidden_actions=["목표 또는 예산의 임의 변경", "자기 결과의 독립 품질 승인"],
        input_contracts=["사용자 목표", "제공 자료", "예산 한도"],
        output_contracts=["ProjectContract", "TeamPlan", "MergeDecision"],
        independent_from=[AgentType.CRITIC, AgentType.GUARDIAN],
    ),
    AgentCard(
        agent_type=AgentType.ARCHITECT,
        display_name="설계자",
        mission="목표를 의존성이 명확한 작업 구조와 실행 순서로 분해한다.",
        activation_conditions=["여러 단계·팀·결과물이 필요한 목표"],
        owned_deliverables=["작업 구조", "의존성 계획", "완료 조건"],
        authorities=["작업 분할과 순서 제안"],
        forbidden_actions=["계획의 최종 승인", "결과물의 품질 승인"],
        input_contracts=["ProjectContract", "초기 작업팩"],
        output_contracts=["TaskPlan"],
    ),
    AgentCard(
        agent_type=AgentType.INVESTIGATOR,
        display_name="탐색자",
        mission="목표 달성에 부족한 자료와 근거를 찾아 출처가 있는 후보 지식으로 제출한다.",
        activation_conditions=["필수 근거가 부족하거나 공개자료 조사가 허용된 경우"],
        owned_deliverables=["후보 지식 묶음", "출처 목록", "정보 공백 보고"],
        authorities=["허용 범위의 자료 검색과 수집"],
        forbidden_actions=["후보 지식의 공식 지식 승격", "출처 없는 사실 확정"],
        input_contracts=["정보 공백", "검색 허용 범위"],
        output_contracts=["KnowledgeCandidateBundle"],
    ),
    AgentCard(
        agent_type=AgentType.ANALYST,
        display_name="분석가",
        mission="승인된 자료를 비교·구조화해 추적 가능한 의미와 판단 근거를 만든다.",
        activation_conditions=["자료 해석·비교·분류 또는 근거 추출이 필요한 경우"],
        owned_deliverables=["분석 결과", "근거 묶음", "불확실성 보고"],
        authorities=["승인 자료 안의 해석과 비교"],
        forbidden_actions=["근거 없는 결론 확정", "최종 산출물 승인"],
        input_contracts=["승인 지식", "분석 질문"],
        output_contracts=["EvidenceBundle", "AnalysisPackage"],
    ),
    AgentCard(
        agent_type=AgentType.CREATOR,
        display_name="창안자",
        mission="계약과 정본이 허용하는 범위에서 새로운 방향과 대안을 제안한다.",
        activation_conditions=["단일 정답이 없고 창의적 대안이 필요한 경우"],
        owned_deliverables=["대안 묶음", "창작 방향", "선택 트레이드오프"],
        authorities=["허용 범위의 신규 대안 제안"],
        forbidden_actions=["대안의 최종 선택", "정본 또는 규칙 변경"],
        input_contracts=["ProjectContract", "창작 제약"],
        output_contracts=["CreativeOptions"],
    ),
    AgentCard(
        agent_type=AgentType.MAKER,
        display_name="제작자",
        mission="승인된 계획과 근거를 실제 사용자 결과물로 구현한다.",
        activation_conditions=["문서·코드·표·콘텐츠 등 산출물 제작이 필요한 경우"],
        owned_deliverables=["초안 또는 결과물 버전", "제작 결정 기록"],
        authorities=["배정된 산출물의 작성과 수정"],
        forbidden_actions=["자기 결과의 최종 승인", "승인 자료의 임의 변경"],
        input_contracts=["TaskPlan", "EvidenceBundle", "선택된 방향"],
        output_contracts=["ArtifactRevision"],
        independent_from=[AgentType.CRITIC, AgentType.GUARDIAN],
    ),
    AgentCard(
        agent_type=AgentType.INTEGRATOR,
        display_name="통합자",
        mission="승인된 부분 결과를 모순 없이 하나의 일관된 결과물로 결합한다.",
        activation_conditions=["둘 이상의 승인된 결과를 결합해야 하는 경우"],
        owned_deliverables=["통합본", "연속성 기록", "병합 충돌 보고"],
        authorities=["승인된 결과의 편집적 병합"],
        forbidden_actions=["거절된 결과의 병합", "충돌의 무근거 해소"],
        input_contracts=["AcceptedArtifact 목록", "통합 기준"],
        output_contracts=["IntegratedArtifact"],
    ),
    AgentCard(
        agent_type=AgentType.CRITIC,
        display_name="비평가",
        mission="결과가 목표와 성공 기준을 충분히 달성하는지 독립적으로 판단한다.",
        activation_conditions=["제작 결과의 품질·완성도 검수가 필요한 경우"],
        owned_deliverables=["품질 판정", "수정 지시", "미충족 기준"],
        authorities=["PASS·REVISE·NEEDS_INFORMATION 판정"],
        forbidden_actions=["검수 대상의 직접 수정", "규칙 위반의 임의 허용"],
        input_contracts=["ProjectContract", "검수 대상", "성공 기준"],
        output_contracts=["ReviewVerdict"],
        independent_from=[AgentType.MAKER, AgentType.PROJECT_OWNER],
    ),
    AgentCard(
        agent_type=AgentType.GUARDIAN,
        display_name="수호자",
        mission="결과와 작업 과정이 정본·정책·권한·안전 경계를 준수하는지 판정한다.",
        activation_conditions=["내부 규칙·정본·보안·권한 경계가 적용되는 경우"],
        owned_deliverables=["준수 판정", "충돌 보고", "거절 근거"],
        authorities=["PASS·REJECT·USER_DECISION 판정", "공식 지식 후보 승인 제안"],
        forbidden_actions=["충돌한 정본의 임의 병합", "검수 대상의 직접 수정"],
        input_contracts=["규칙팩", "정본 지식팩", "검수 대상"],
        output_contracts=["GuardVerdict"],
        independent_from=[AgentType.MAKER, AgentType.PROJECT_OWNER],
    ),
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
