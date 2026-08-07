"""Deterministic compilation and durable state for goal-specific execution DAGs."""

from __future__ import annotations

import hashlib
import json
import os
import re
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator

from onebrief.agent_registry import AgentType
if TYPE_CHECKING:
    from onebrief.team_planning import TeamPlan


STAGE_BY_AGENT: dict[AgentType, tuple[str, ...]] = {
    AgentType.PROJECT_OWNER: ("final_approval",),
    AgentType.ARCHITECT: ("project_architecture",),
    AgentType.INVESTIGATOR: ("public_research",),
    AgentType.ANALYST: ("evidence_analysis",),
    AgentType.CREATOR: ("creative_direction",),
    AgentType.MAKER: ("tool_execution", "long_form_draft", "revision"),
    AgentType.INTEGRATOR: ("artifact_integration",),
    AgentType.CRITIC: ("independent_verification",),
    AgentType.GUARDIAN: ("policy_guard",),
}


class NodeStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETE = "complete"
    BLOCKED = "blocked"
    FAILED = "failed"


class ExecutionNode(BaseModel):
    node_id: str
    stage: str
    owner_instance_id: str
    agent_type: AgentType
    depends_on: list[str] = Field(default_factory=list)
    activation_reason: str
    parallel_group: str | None = None


class ExecutionGraph(BaseModel):
    schema_version: str = "onebrief-execution-graph-v1"
    project_id: str
    team_plan_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    nodes: list[ExecutionNode]

    @model_validator(mode="after")
    def validate_dag(self) -> "ExecutionGraph":
        ids = [node.node_id for node in self.nodes]
        if not ids or len(ids) != len(set(ids)):
            raise ValueError("execution graph requires unique nodes")
        known = set(ids)
        for node in self.nodes:
            if node.node_id in node.depends_on or not set(node.depends_on) <= known:
                raise ValueError(f"invalid dependency for {node.node_id}")
        visiting: set[str] = set()
        visited: set[str] = set()
        by_id = {node.node_id: node for node in self.nodes}

        def visit(node_id: str) -> None:
            if node_id in visiting:
                raise ValueError("execution graph contains a cycle")
            if node_id in visited:
                return
            visiting.add(node_id)
            for dependency in by_id[node_id].depends_on:
                visit(dependency)
            visiting.remove(node_id)
            visited.add(node_id)

        for node_id in ids:
            visit(node_id)
        approvals = [node for node in self.nodes if node.stage == "final_approval"]
        if len(approvals) != 1:
            raise ValueError("execution graph requires exactly one final approval")
        if set(approvals[0].depends_on) != set(self.terminal_work_nodes()):
            raise ValueError("final approval must depend on every terminal work node")
        return self

    def terminal_work_nodes(self) -> list[str]:
        work = [node for node in self.nodes if node.stage != "final_approval"]
        referenced = {item for node in work for item in node.depends_on}
        return [node.node_id for node in work if node.node_id not in referenced]

    def node_for_stage(self, stage: str) -> ExecutionNode:
        base = re.sub(r"_r\d+$", "", stage)
        if base == "verification":
            base = "independent_verification"
        for node in self.nodes:
            if node.stage == base:
                return node
        raise KeyError(f"stage is not present in execution graph: {stage}")


class NodeRunRecord(BaseModel):
    status: NodeStatus = NodeStatus.PENDING
    attempt: int = Field(default=0, ge=0)
    output_paths: list[str] = Field(default_factory=list)
    message: str = ""


class ExecutionGraphState(BaseModel):
    schema_version: str = "onebrief-execution-graph-state-v1"
    project_id: str
    team_plan_sha256: str
    nodes: dict[str, NodeRunRecord]


def team_plan_hash(plan: TeamPlan) -> str:
    canonical = json.dumps(
        plan.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def compile_execution_graph(plan: TeamPlan, toolpack_ids: list[object] | None = None) -> ExecutionGraph:
    """Compile selected agents into an executable DAG; inactive roles never become nodes."""
    by_type = {member.agent_type: member for member in plan.members}
    nodes: list[ExecutionNode] = []

    def add(agent_type: AgentType, stage: str, dependencies: list[str]) -> None:
        member = by_type.get(agent_type)
        if member is None:
            return
        nodes.append(ExecutionNode(
            node_id=stage,
            stage=stage,
            owner_instance_id=member.instance_id,
            agent_type=agent_type,
            depends_on=[item for item in dependencies if item in {node.node_id for node in nodes}],
            activation_reason=member.selection_reason,
            parallel_group="context_fanout" if stage in {"tool_execution", "public_research"} else None,
        ))

    add(AgentType.ARCHITECT, "project_architecture", [])
    if toolpack_ids:
        add(AgentType.MAKER, "tool_execution", ["project_architecture"])
    add(AgentType.INVESTIGATOR, "public_research", ["project_architecture"])
    analysis_dependencies = ["project_architecture", "tool_execution", "public_research"]
    add(AgentType.ANALYST, "evidence_analysis", analysis_dependencies)
    add(AgentType.CREATOR, "creative_direction", ["evidence_analysis"])
    add(AgentType.MAKER, "long_form_draft", ["evidence_analysis", "creative_direction"])
    add(AgentType.INTEGRATOR, "artifact_integration", ["long_form_draft"])
    add(
        AgentType.CRITIC,
        "independent_verification",
        ["long_form_draft", "artifact_integration"],
    )
    add(AgentType.GUARDIAN, "policy_guard", ["independent_verification"])

    owner = by_type[AgentType.PROJECT_OWNER]
    work_ids = {node.node_id for node in nodes}
    referenced = {item for node in nodes for item in node.depends_on}
    terminals = [node.node_id for node in nodes if node.node_id not in referenced]
    nodes.append(ExecutionNode(
        node_id="final_approval",
        stage="final_approval",
        owner_instance_id=owner.instance_id,
        agent_type=AgentType.PROJECT_OWNER,
        depends_on=terminals,
        activation_reason="The project owner must accept or return the completed work.",
    ))
    graph = ExecutionGraph(
        project_id=plan.project_id,
        team_plan_sha256=team_plan_hash(plan),
        nodes=nodes,
    )
    active = {member.agent_type for member in plan.members}
    represented = {node.agent_type for node in graph.nodes}
    if active != represented:
        missing = sorted(item.value for item in active - represented)
        raise ValueError(f"active agents without executable nodes: {missing}")
    if AgentType.INVESTIGATOR in active and "public_research" not in work_ids:
        raise ValueError("investigator requires a public_research node")
    return graph


def persist_execution_graph(graph: ExecutionGraph, path: Path) -> None:
    content = json.dumps(
        graph.model_dump(mode="json"), ensure_ascii=False, indent=2, sort_keys=True
    ) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_text(encoding="utf-8") != content:
            raise FileExistsError(f"refusing to overwrite changed execution graph: {path}")
        return
    temporary = path.with_suffix(f".{uuid4().hex}.tmp")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, path)


class ExecutionGraphRuntime:
    """Atomic, resume-safe state transitions with dependency enforcement."""

    def __init__(self, graph: ExecutionGraph, state_path: Path):
        self.graph = graph
        self.state_path = state_path
        if state_path.exists():
            self.state = ExecutionGraphState.model_validate_json(
                state_path.read_text(encoding="utf-8")
            )
            if self.state.team_plan_sha256 != graph.team_plan_sha256:
                raise RuntimeError("execution graph state belongs to a different TeamPlan")
        else:
            self.state = ExecutionGraphState(
                project_id=graph.project_id,
                team_plan_sha256=graph.team_plan_sha256,
                nodes={node.node_id: NodeRunRecord() for node in graph.nodes},
            )
            self._save()

    def _save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix(f".{uuid4().hex}.tmp")
        temporary.write_text(self.state.model_dump_json(indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, self.state_path)

    def ready_nodes(self) -> list[ExecutionNode]:
        ready: list[ExecutionNode] = []
        for node in self.graph.nodes:
            record = self.state.nodes[node.node_id]
            if record.status != NodeStatus.PENDING:
                continue
            if all(self.state.nodes[item].status == NodeStatus.COMPLETE for item in node.depends_on):
                ready.append(node)
        return ready

    def start(self, node_id: str) -> None:
        record = self.state.nodes[node_id]
        if record.status == NodeStatus.RUNNING:
            return
        ready = {node.node_id for node in self.ready_nodes()}
        if node_id not in ready:
            raise RuntimeError(f"node is not ready: {node_id}")
        record.status = NodeStatus.RUNNING
        record.attempt += 1
        self._save()

    def complete(self, node_id: str, *output_paths: str, message: str = "") -> None:
        record = self.state.nodes[node_id]
        if record.status != NodeStatus.RUNNING:
            raise RuntimeError(f"node is not running: {node_id}")
        record.status = NodeStatus.COMPLETE
        record.output_paths = list(output_paths)
        record.message = message
        self._save()

    def fail(self, node_id: str, message: str, *, blocked: bool = False) -> None:
        record = self.state.nodes[node_id]
        if record.status != NodeStatus.RUNNING:
            raise RuntimeError(f"node is not running: {node_id}")
        record.status = NodeStatus.BLOCKED if blocked else NodeStatus.FAILED
        record.message = message
        self._save()
