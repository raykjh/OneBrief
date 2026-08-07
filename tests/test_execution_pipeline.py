import json
from pathlib import Path

import pytest

from onebrief.development_toolpack import CodeChangeSet, DevelopmentCommandResult, DevelopmentRun
from onebrief.budget_guard import BudgetExceeded, BudgetStore, RunStatus
from onebrief.execution_pipeline import ExecutionPipeline
from onebrief.execution_agents import DeveloperAgent
from onebrief.execution_limits import DEVELOPER_OUTPUT_CAP
from onebrief.execution_schemas import (
    AnalysisPackage,
    DraftArtifact,
    PipelineStatus,
    VerificationReport,
)
from onebrief.producer import estimate_budget
from onebrief.schemas import IntakeRequest, InternalSource, OutputTarget, RequirementsAnalysis, SourcePriority, ToolPackId


class FakeGateway:
    def __init__(self, outputs: list[object]):
        self.outputs = outputs
        self.calls: list[tuple[str, str]] = []
        self.payloads: list[dict[str, object]] = []

    def generate_json(self, *, stage: str, model: str, schema: type, **kwargs: object):
        self.calls.append((stage, model))
        self.payloads.append(kwargs)
        value = self.outputs.pop(0)
        if isinstance(value, BaseException):
            raise value
        assert isinstance(value, schema)
        return value


class BlockingGateway:
    def generate_json(self, **_: object):
        raise BudgetExceeded("blocked before generation")


def _requirements() -> RequirementsAnalysis:
    return RequirementsAnalysis(
        supported=True,
        support_reason="Complete internal inputs were supplied.",
        normalized_goal="Create a grounded guide.",
        deliverables=["Grounded guide"],
        mandatory_information=[],
        optional_information=[],
        acceptance_criteria=["Every material claim cites supplied evidence."],
        assumptions=[],
        consolidated_questions=[],
        ready_for_estimate=True,
    )


def _source() -> InternalSource:
    return InternalSource(
        name="policy.md",
        priority=SourcePriority.MANDATORY,
        requirement_keys=["policy"],
        content="The policy requires manager approval for remote work.",
    )


def _analysis() -> AnalysisPackage:
    return AnalysisPackage(
        objective="Create a grounded guide.",
        findings=[
            {
                "finding_id": "F01",
                "source_name": "policy.md",
                "evidence": "Manager approval is required.",
                "implication": "The guide must instruct employees to request approval.",
            }
        ],
        recommended_structure=["Rule", "Procedure"],
        constraints=["Use only the policy."],
        risks=[],
    )


def _draft(text: str = "Employees must request manager approval before remote work. [F01]") -> DraftArtifact:
    return DraftArtifact(
        title="Remote Work Guide",
        body_markdown=text,
        cited_finding_ids=["F01"],
        drafting_decisions=["Used the mandatory policy."],
    )


def _verification(verdict: str) -> VerificationReport:
    revise = verdict == "REVISE"
    return VerificationReport(
        verdict=verdict,
        criterion_checks=[
            {
                "criterion": "Every material claim cites evidence.",
                "passed": not revise,
                "evidence": "F01 is present." if not revise else "The procedure is incomplete.",
            }
        ],
        blocking_issues=["Add the approval request procedure."] if revise else [],
        revision_instructions=["Add a clear approval request step."] if revise else [],
        missing_information=[],
    )


def _approve(tmp_path: Path, intake: IntakeRequest, source: InternalSource) -> Path:
    run_dir = tmp_path / "run"
    estimate = estimate_budget(
        intake.model_copy(update={"internal_sources": [source]}),
        _requirements(),
    )
    BudgetStore(run_dir).approve(estimate, estimate.recommended_approval_usd)
    return run_dir


def test_revision_is_always_reverified_through_same_gateway(tmp_path: Path) -> None:
    intake = IntakeRequest(goal="Create a guide.", max_revision_rounds=2)
    source = _source()
    run_dir = _approve(tmp_path, intake, source)
    gateway = FakeGateway(
        [
            _analysis(),
            _draft(),
            _verification("REVISE"),
            DraftArtifact(
                title="Remote Work Guide",
                body_markdown=(
                    "Employees must submit a request and receive manager approval before remote work. [F01]"
                ),
                drafting_decisions=["Added the approval request procedure."],
                cited_finding_ids=["F01"],
                temperament_decisions=[
                    {
                        "agent": "writer",
                        "agent_type": "TNL",
                        "options": ["targeted correction", "full rewrite"],
                        "selected": "targeted correction",
                        "deciding_axis": "scope",
                        "reason": "Both met the contract; Local favored the smaller correction.",
                    }
                ],
            ),
            _verification("PASS"),
        ]
    )
    result = ExecutionPipeline(run_dir, gateway=gateway).run(
        intake=intake,
        requirements=_requirements(),
        sources=[source],
        output_dir=tmp_path / "output",
    )
    assert result.status == PipelineStatus.COMPLETE
    assert [stage for stage, _ in gateway.calls] == [
        "evidence_analysis",
        "long_form_draft",
        "independent_verification_r0",
        "long_form_draft_revision_r1",
        "independent_verification_r1",
    ]
    assert BudgetStore(run_dir).read().status == RunStatus.COMPLETE
    audit = json.loads(
        (tmp_path / "output" / "temperament_decisions.json").read_text(encoding="utf-8")
    )
    assert len(audit) == 1
    assert audit[0]["agent_type"] == "TNL"
    retry_payload = json.loads(gateway.payloads[3]["contents"])
    assert retry_payload["previous_draft"]["title"] == "Remote Work Guide"
    assert retry_payload["verification_feedback"]["verdict"] == "REVISE"


def test_budget_block_writes_resumable_checkpoint(tmp_path: Path) -> None:
    intake = IntakeRequest(goal="Create a guide.")
    source = _source()
    run_dir = _approve(tmp_path, intake, source)


    output_dir = tmp_path / "blocked"
    with pytest.raises(BudgetExceeded):
        ExecutionPipeline(run_dir, gateway=BlockingGateway()).run(
            intake=intake,
            requirements=_requirements(),
            sources=[source],
            output_dir=output_dir,
        )
    checkpoint = (output_dir / "execution_checkpoint.json").read_text(encoding="utf-8")
    assert '"status": "needs_budget"' in checkpoint

def test_exchange_development_returns_verified_web_changes_without_replacing_them_with_excel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    intake = IntakeRequest(
        goal="Improve the existing Exchange web application.",
        output_target=OutputTarget.EXISTING_PROJECT,
        toolpack_ids=[ToolPackId.EXCHANGE_DEVELOPMENT],
    )
    source = _source()
    run_dir = _approve(tmp_path, intake, source)
    change_set = CodeChangeSet(
        summary="Improve the web application status panel.",
        changes=[{
            "path": "web/src/status.ts",
            "base_sha256": None,
            "content": "export const status = 'verified';\n",
            "reason": "Expose a verified status in the existing web application.",
        }],
    )
    gateway = FakeGateway([_analysis(), change_set, _verification("PASS")])

    monkeypatch.setattr(
        "onebrief.execution_pipeline.execute_toolpacks",
        lambda toolpack_ids, output_dir: ([], []),
    )

    def fake_apply(_self, supplied: CodeChangeSet, output_dir: Path) -> DevelopmentRun:
        assert supplied == change_set
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "changes.patch").write_text("verified patch\n", encoding="utf-8")
        run = DevelopmentRun(
            status="verified",
            repository_name="exchange",
            base_head_sha="a" * 40,
            summary=supplied.summary,
            changed_paths=[item.path for item in supplied.changes],
            commands=[
                DevelopmentCommandResult(
                    command_id="web_build", argv=["npm", "run", "build"],
                    exit_code=0, duration_seconds=0.1, output_tail="passed",
                )
            ],
            patch_path="development/changes.patch",
            safety_boundary=["isolated clone only"],
        )
        (output_dir / "development_run.json").write_text(
            run.model_dump_json(indent=2), encoding="utf-8"
        )
        return run

    monkeypatch.setattr(
        "onebrief.execution_pipeline.ExchangeDevelopmentToolPack.apply_and_verify", fake_apply
    )
    output_dir = tmp_path / "development-output"
    result = ExecutionPipeline(run_dir, gateway=gateway).run(
        intake=intake, requirements=_requirements(), sources=[source], output_dir=output_dir
    )
    assert result.status == PipelineStatus.COMPLETE
    assert (output_dir / "development" / "changes.patch").is_file()
    assert "web/src/status.ts" in (output_dir / "final.md").read_text(encoding="utf-8")
    assert not (output_dir / "result.xlsx").exists()
    assert [stage for stage, _ in gateway.calls] == [
        "evidence_analysis", "long_form_draft", "independent_verification_r0"
    ]

def test_exchange_development_repairs_a_failed_regression_check_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    intake = IntakeRequest(
        goal="Improve the existing Exchange web application.",
        output_target=OutputTarget.EXISTING_PROJECT,
        toolpack_ids=[ToolPackId.EXCHANGE_DEVELOPMENT],
    )
    source = _source()
    run_dir = _approve(tmp_path, intake, source)
    initial = CodeChangeSet(
        summary="Initial implementation.",
        changes=[{
            "path": "web/src/status.ts", "base_sha256": None,
            "content": "export const status = 'initial';\n",
            "reason": "Implement the first attempt.",
        }],
    )
    repaired = CodeChangeSet(
        summary="Regression-safe implementation.",
        changes=[{
            "path": "web/src/status.ts", "base_sha256": None,
            "content": "export const status = 'repaired';\n",
            "reason": "Repair the reported regression.",
        }],
    )
    gateway = FakeGateway([_analysis(), initial, repaired, _verification("PASS")])
    monkeypatch.setattr(
        "onebrief.execution_pipeline.execute_toolpacks",
        lambda toolpack_ids, output_dir: ([], []),
    )
    attempts: list[CodeChangeSet] = []

    def fake_apply(_self, supplied: CodeChangeSet, output_dir: Path) -> DevelopmentRun:
        attempts.append(supplied)
        if len(attempts) == 1:
            raise RuntimeError("development verification failed: web_tests\nmissing required marker")
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "changes.patch").write_text("repaired patch\n", encoding="utf-8")
        run = DevelopmentRun(
            status="verified", repository_name="exchange", base_head_sha="a" * 40,
            summary=supplied.summary, changed_paths=[item.path for item in supplied.changes],
            commands=[DevelopmentCommandResult(
                command_id="web_tests", argv=["npm", "test"], exit_code=0,
                duration_seconds=0.1, output_tail="passed",
            )],
            patch_path="development/changes.patch", safety_boundary=["isolated clone only"],
        )
        (output_dir / "development_run.json").write_text(
            run.model_dump_json(indent=2), encoding="utf-8"
        )
        return run

    monkeypatch.setattr(
        "onebrief.execution_pipeline.ExchangeDevelopmentToolPack.apply_and_verify", fake_apply
    )
    output_dir = tmp_path / "repair-output"
    result = ExecutionPipeline(run_dir, gateway=gateway).run(
        intake=intake, requirements=_requirements(), sources=[source], output_dir=output_dir
    )

    assert result.status == PipelineStatus.COMPLETE
    assert attempts == [initial, repaired]
    assert (output_dir / "development_verification_failure_r0.txt").is_file()
    assert (output_dir / "code_change_set_retry_r1.json").is_file()
    recovery = json.loads((output_dir / "recovery_decisions.json").read_text(encoding="utf-8"))
    assert recovery["decisions"][0]["error_class"] == "artifact_validation"
    assert recovery["decisions"][0]["action"] == "return_to_agent"
    assert recovery["decisions"][0]["responsible_party"] == "maker"
    assert [stage for stage, _ in gateway.calls] == [
        "evidence_analysis", "long_form_draft", "long_form_draft_verification_retry",
        "independent_verification_r0",
    ]




def test_developer_retries_truncated_json_with_a_larger_bounded_output() -> None:
    truncated = CodeChangeSet.model_validate_json
    try:
        truncated('{"schema_version":"onebrief-code-change-set-v1","summary":"cut","changes":[{"path":"web/src/status.ts","content":"')
    except Exception as exc:
        parse_error = exc
    change_set = CodeChangeSet(
        summary="Compact verified implementation.",
        changes=[{
            "path": "web/src/status.ts",
            "base_sha256": None,
            "content": "export const status = 'verified';\n",
            "reason": "Implement the requested status.",
        }],
    )
    gateway = FakeGateway([parse_error, change_set])

    result = DeveloperAgent(gateway).run({}, _analysis(), [])

    assert result == change_set
    assert [stage for stage, _ in gateway.calls] == [
        "long_form_draft",
        "long_form_draft_compact_retry",
    ]


def test_developer_stops_after_repeated_truncated_output() -> None:
    errors = []
    for _ in range(2):
        try:
            CodeChangeSet.model_validate_json(
                '{"summary":"cut","changes":[{"path":"web/app/page.tsx","content":"'
            )
        except Exception as exc:
            errors.append(exc)
    gateway = FakeGateway(errors)
    developer = DeveloperAgent(gateway)

    with pytest.raises(Exception, match="Invalid JSON"):
        developer.run({}, _analysis(), [])

    assert [stage for stage, _ in gateway.calls] == [
        "long_form_draft", "long_form_draft_compact_retry"
    ]
    assert [item.action.value for item in developer.last_recovery_decisions] == [
        "auto_retry", "stop"
    ]

def test_developer_retries_provenance_name_used_as_repository_path() -> None:
    try:
        CodeChangeSet.model_validate({
            "summary": "Invalid provenance path.",
            "changes": [{
                "path": "exchange-source/web/app/page.tsx",
                "base_sha256": "a" * 64,
                "content": "export default function Page(){return null}\n",
                "reason": "Attempted page update.",
            }],
        })
    except Exception as exc:
        path_error = exc
    corrected = CodeChangeSet(
        summary="Correct repository path.",
        changes=[{
            "path": "web/app/page.tsx",
            "base_sha256": "a" * 64,
            "content": "export default function Page(){return null}\n",
            "reason": "Update the approved page.",
        }],
    )
    gateway = FakeGateway([path_error, corrected])

    assert DeveloperAgent(gateway).run({}, _analysis(), []) == corrected
    assert [stage for stage, _ in gateway.calls] == [
        "long_form_draft", "long_form_draft_compact_retry"
    ]



def test_developer_separates_repository_path_from_source_provenance_name() -> None:
    change_set = CodeChangeSet(
        summary="Use the actual repository path.",
        changes=[{
            "path": "web/app/page.tsx",
            "base_sha256": "a" * 64,
            "content": "export default function Page(){return <main>FX</main>}\n",
            "reason": "Improve the approved page.",
        }],
    )

    class CapturingGateway:
        contents = ""

        def generate_json(self, *, contents: str, **_: object):
            self.contents = contents
            return change_set

    gateway = CapturingGateway()
    result = DeveloperAgent(gateway).run({}, _analysis(), [
        {"name": "exchange-source/web/app/page.tsx", "sha256": "a" * 64,
         "content": "export default function Page(){return null}\n"},
        {"name": "exchange-source/docs/product.md", "sha256": "b" * 64,
         "content": "Evidence only."},
    ])

    payload = json.loads(gateway.contents)
    source = payload["approved_repository_files"][0]
    assert result == change_set
    assert source["name"] == "exchange-source/web/app/page.tsx"
    assert source["repository_path"] == "web/app/page.tsx"
    assert payload["approved_repository_files"][1]["repository_path"] is None

    assert source["source_role"] == "editable_source"
    assert payload["approved_repository_files"][1]["source_role"] == "read_only_context"

def test_exchange_development_budget_includes_compact_retry_capacity() -> None:
    intake = IntakeRequest(goal="Improve Exchange.", toolpack_ids=[ToolPackId.EXCHANGE_DEVELOPMENT])
    estimate = estimate_budget(intake, _requirements())
    draft = next(stage for stage in estimate.stages if stage.stage == "long_form_draft")
    assert draft.output_tokens_per_call == DEVELOPER_OUTPUT_CAP
    assert DEVELOPER_OUTPUT_CAP >= 20_000
    assert (draft.minimum_calls, draft.recommended_calls, draft.maximum_calls) == (1, 2, 2)


def test_resume_reuses_completed_analysis_without_a_new_model_call(tmp_path: Path) -> None:
    intake = IntakeRequest(goal="Create a guide.")
    source = _source()
    run_dir = _approve(tmp_path, intake, source)
    output_dir = tmp_path / "resumed"
    output_dir.mkdir()
    (output_dir / "analysis.json").write_text(
        _analysis().model_dump_json(indent=2),
        encoding="utf-8",
    )
    gateway = FakeGateway([_draft(), _verification("PASS")])

    result = ExecutionPipeline(run_dir, gateway=gateway).run(
        intake=intake,
        requirements=_requirements(),
        sources=[source],
        output_dir=output_dir,
    )

    assert result.status == PipelineStatus.COMPLETE
    assert [stage for stage, _ in gateway.calls] == [
        "long_form_draft",
        "independent_verification_r0",
    ]
    final_text = (output_dir / "final.md").read_text(encoding="utf-8")
    assert "Remote Work Guide" in final_text
    assert json.loads((output_dir / "temperament_decisions.json").read_text(encoding="utf-8")) == []


def test_exchange_development_returns_failed_acceptance_to_the_software_maker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    intake = IntakeRequest(
        goal="Improve the existing Exchange web application.",
        output_target=OutputTarget.EXISTING_PROJECT,
        toolpack_ids=[ToolPackId.EXCHANGE_DEVELOPMENT],
        max_revision_rounds=1,
    )
    source = _source()
    run_dir = _approve(tmp_path, intake, source)
    initial = CodeChangeSet(
        summary="Claims a feature without implementing it.",
        changes=[{
            "path": "web/src/status.ts", "base_sha256": None,
            "content": "export const status = 'initial';\n",
            "reason": "Initial incomplete implementation.",
        }],
    )
    corrected = CodeChangeSet(
        summary="Implements the verified feature.",
        changes=[{
            "path": "web/src/status.ts", "base_sha256": None,
            "content": "export const status = 'corrected';\n",
            "reason": "Address the independent implementation review.",
        }],
    )
    gateway = FakeGateway([
        _analysis(), initial, _verification("REVISE"), corrected, _verification("PASS")
    ])
    monkeypatch.setattr(
        "onebrief.execution_pipeline.execute_toolpacks",
        lambda toolpack_ids, output_dir: ([], []),
    )
    attempts: list[CodeChangeSet] = []

    def fake_apply(_self, supplied: CodeChangeSet, output_dir: Path) -> DevelopmentRun:
        attempts.append(supplied)
        output_dir.mkdir(parents=True, exist_ok=True)
        changed = output_dir / "changed_files" / "web" / "src" / "status.ts"
        changed.parent.mkdir(parents=True, exist_ok=True)
        changed.write_text(supplied.changes[0].content, encoding="utf-8")
        (output_dir / "changes.patch").write_text("patch\n", encoding="utf-8")
        run = DevelopmentRun(
            status="verified", repository_name="exchange", base_head_sha="a" * 40,
            summary=supplied.summary, changed_paths=["web/src/status.ts"],
            commands=[DevelopmentCommandResult(
                command_id="web_tests", argv=["npm", "test"], exit_code=0,
                duration_seconds=0.1, output_tail="passed",
            )],
            patch_path="development/changes.patch", safety_boundary=["isolated clone only"],
        )
        (output_dir / "development_run.json").write_text(
            run.model_dump_json(indent=2), encoding="utf-8"
        )
        return run

    monkeypatch.setattr(
        "onebrief.execution_pipeline.ExchangeDevelopmentToolPack.apply_and_verify", fake_apply
    )
    output_dir = tmp_path / "acceptance-revision-output"
    result = ExecutionPipeline(run_dir, gateway=gateway).run(
        intake=intake, requirements=_requirements(), sources=[source], output_dir=output_dir
    )

    assert result.status == PipelineStatus.COMPLETE
    assert attempts == [initial, corrected]
    assert (output_dir / "code_change_set_revision_r1.json").is_file()
    assert "corrected" in (
        output_dir / "development" / "changed_files" / "web" / "src" / "status.ts"
    ).read_text(encoding="utf-8")
    verifier_payloads = [
        json.loads(payload["contents"])
        for (stage, _), payload in zip(gateway.calls, gateway.payloads)
        if stage.startswith("independent_verification")
    ]
    assert verifier_payloads[0]["implementation_evidence"]["changed_files"][0]["content"]
    assert [stage for stage, _ in gateway.calls] == [
        "evidence_analysis",
        "long_form_draft",
        "independent_verification_r0",
        "long_form_draft_verification_retry",
        "independent_verification_r1",
    ]