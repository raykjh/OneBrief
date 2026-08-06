from __future__ import annotations

from pathlib import Path
from fastapi.testclient import TestClient

from onebrief.agent_registry import AgentType
from onebrief.execution_graph import compile_execution_graph
from onebrief.web_service import InMemoryWebSessionStore, app, get_session_store
from team_plan_support import minimal_team_plan
from onebrief.schemas import IntakeRequest, RequirementsAnalysis, ToolPackId
from onebrief.toolpacks import (
    EXCHANGE_EVIDENCE,
    ExchangeToolPack,
    ToolCommandResult,
    attach_toolpack_descriptors,
    execute_toolpacks,
)


def _fixture(root: Path) -> None:
    (root / "package.json").parent.mkdir(parents=True, exist_ok=True)
    (root / "package.json").write_text("{}\n", encoding="utf-8")
    for relative in EXCHANGE_EVIDENCE:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        value = "{}\n" if path.suffix == ".json" else f"# {relative}\nverified evidence\n"
        path.write_text(value, encoding="utf-8")


def test_exchange_toolpack_executes_only_fixed_checks_and_packages_evidence(tmp_path: Path) -> None:
    root = tmp_path / "exchange"
    _fixture(root)
    observed: list[list[str]] = []

    def fake_runner(argv: list[str], cwd: Path, _timeout: int) -> ToolCommandResult:
        observed.append(argv)
        assert cwd == root.resolve()
        return ToolCommandResult(
            command_id="pending",
            argv=argv,
            exit_code=0,
            duration_seconds=0.01,
            output_tail="ok",
        )

    output = tmp_path / "work" / "toolpacks"
    run, sources = ExchangeToolPack(root, fake_runner).execute(output)
    assert run.status == "passed" and run.readonly
    assert [item[1:] for item in observed] == [["test"], ["run", "verify:transfer"]]
    assert len(run.evidence) == len(EXCHANGE_EVIDENCE)
    assert len(sources) == len(EXCHANGE_EVIDENCE)


def test_toolpack_execution_resumes_without_rerunning_commands(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "exchange"
    _fixture(root)
    output = tmp_path / "work" / "toolpacks"
    calls = 0

    def fake_runner(argv: list[str], cwd: Path, _timeout: int) -> ToolCommandResult:
        nonlocal calls
        calls += 1
        return ToolCommandResult(
            command_id="pending", argv=argv, exit_code=0, duration_seconds=0, output_tail="ok"
        )

    monkeypatch.setattr("onebrief.toolpacks.ExchangeToolPack", lambda: ExchangeToolPack(root, fake_runner))
    first_runs, first_sources = execute_toolpacks([ToolPackId.EXCHANGE], output)
    second_runs, second_sources = execute_toolpacks([ToolPackId.EXCHANGE], output)
    assert calls == 2
    assert first_runs == second_runs
    assert len(first_sources) == len(second_sources) == len(EXCHANGE_EVIDENCE)


def test_descriptor_makes_selected_toolpack_visible_to_requirements() -> None:
    intake = attach_toolpack_descriptors(
        IntakeRequest(goal="?? ?? ???? ????", toolpack_ids=[ToolPackId.EXCHANGE])
    )
    assert intake.internal_sources[0].name == "toolpack-exchange-capability.md"
    assert "cannot place trades" in intake.internal_sources[0].content


def test_exchange_toolpack_adds_tool_execution_before_analysis() -> None:
    graph = compile_execution_graph(minimal_team_plan("exchange-job"), ["exchange"])
    tool = graph.node_for_stage("tool_execution")
    analysis = graph.node_for_stage("evidence_analysis")
    assert tool.agent_type == AgentType.MAKER
    assert tool.node_id in analysis.depends_on
    assert graph.node_for_stage("long_form_draft").depends_on == ["evidence_analysis"]


def test_web_ui_exposes_exchange_as_readonly_toolpack() -> None:
    response = TestClient(app).get("/")
    assert response.status_code == 200
    assert 'name="toolpack_ids" value="exchange"' in response.text


def test_inspect_persists_selected_toolpack_and_descriptor(monkeypatch) -> None:
    store = InMemoryWebSessionStore()

    async def fake_inspect(_intake):
        return RequirementsAnalysis(
            supported=True,
            support_reason="supported",
            normalized_goal="Build a bounded FX research package.",
            deliverables=["report"],
            mandatory_information=[],
            optional_information=[],
            acceptance_criteria=["No trade execution"],
            assumptions=[],
            consolidated_questions=[],
            ready_for_estimate=False,
        )

    monkeypatch.setattr("onebrief.web_service.inspect_requirements", fake_inspect)
    app.dependency_overrides[get_session_store] = lambda: store
    try:
        response = TestClient(app).post(
            "/api/inspect",
            data={"goal": "Build an FX research package.", "toolpack_ids": "exchange"},
        )
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 200
    session = store.read(response.json()["session_id"])
    assert session.intake.toolpack_ids == [ToolPackId.EXCHANGE]
    assert session.intake.internal_sources[0].name == "toolpack-exchange-capability.md"
