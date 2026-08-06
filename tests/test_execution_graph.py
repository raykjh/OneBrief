from pathlib import Path

import pytest

from onebrief.agent_registry import AgentType
from onebrief.execution_graph import (
    ExecutionGraphRuntime,
    NodeStatus,
    compile_execution_graph,
)
from team_plan_support import minimal_team_plan


def test_minimal_team_compiles_to_real_dependency_graph() -> None:
    graph = compile_execution_graph(minimal_team_plan("graph-job"))
    assert [node.node_id for node in graph.nodes] == [
        "evidence_analysis",
        "long_form_draft",
        "independent_verification",
        "final_approval",
    ]
    assert graph.node_for_stage("independent_verification").depends_on == ["long_form_draft"]
    assert graph.node_for_stage("final_approval").depends_on == ["independent_verification"]


def test_optional_selected_roles_change_the_graph() -> None:
    plan = minimal_team_plan("graph-job", public_research=True)
    graph = compile_execution_graph(plan)
    assert graph.node_for_stage("public_research").agent_type == AgentType.INVESTIGATOR
    assert graph.node_for_stage("evidence_analysis").depends_on == ["public_research"]


def test_runtime_only_releases_nodes_after_dependencies_complete(tmp_path: Path) -> None:
    graph = compile_execution_graph(minimal_team_plan("graph-job"))
    runtime = ExecutionGraphRuntime(graph, tmp_path / "state.json")
    assert [node.node_id for node in runtime.ready_nodes()] == ["evidence_analysis"]
    with pytest.raises(RuntimeError, match="not ready"):
        runtime.start("long_form_draft")
    runtime.start("evidence_analysis")
    runtime.complete("evidence_analysis", "analysis.json")
    assert runtime.state.nodes["evidence_analysis"].status == NodeStatus.COMPLETE
    assert [node.node_id for node in runtime.ready_nodes()] == ["long_form_draft"]


def test_runtime_rejects_state_from_changed_plan(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    first = compile_execution_graph(minimal_team_plan("graph-job"))
    ExecutionGraphRuntime(first, path)
    changed = minimal_team_plan("graph-job", public_research=True)
    with pytest.raises(RuntimeError, match="different TeamPlan"):
        ExecutionGraphRuntime(compile_execution_graph(changed), path)
