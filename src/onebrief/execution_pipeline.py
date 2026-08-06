"""Checkpointed and resumable analyst-writer-verifier-revision execution loop."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TypeVar
from uuid import uuid4

from pydantic import BaseModel

from onebrief.budget_guard import BudgetExceeded, BudgetStore
from onebrief.deterministic_verification import (
    apply_deterministic_override,
    validate_draft_grounding,
)
from onebrief.dynamic_role_agents import DynamicRoleAgent, GovernanceAgent, GovernanceDecision, RoleHandoff
from onebrief.execution_agents import AnalystAgent, RevisionAgent, VerifierAgent, WriterAgent
from onebrief.execution_graph import ExecutionGraph, ExecutionGraphRuntime, NodeStatus
from onebrief.execution_schemas import (
    AnalysisPackage,
    DraftArtifact,
    ExecutionCheckpoint,
    PipelineStatus,
    RevisionArtifact,
    VerificationReport,
    Verdict,
)
from onebrief.guarded_gemini import BudgetedGeminiClient
from onebrief.grounded_search import run_grounded_research
from onebrief.requirements_gate import require_ready_for_estimate
from onebrief.schemas import IntakeRequest, InternalSource, RequirementsAnalysis
from onebrief.public_research import PublicResearchResult
from onebrief.workbook_export import export_workbook

T = TypeVar("T", bound=BaseModel)


class ExecutionPipeline:
    def __init__(
        self,
        run_dir: Path,
        gateway: object | None = None,
        stage_models: dict[str, str] | None = None,
        execution_graph: ExecutionGraph | None = None,
    ):
        self.run_dir = run_dir
        self.gateway = gateway or BudgetedGeminiClient(run_dir)
        selected = stage_models or {}
        self.stage_models = selected
        self.execution_graph = execution_graph
        self.analyst = AnalystAgent(
            self.gateway, selected.get("evidence_analysis", "gemini-3.5-flash")
        )
        self.writer = WriterAgent(
            self.gateway, selected.get("long_form_draft", "gemini-3.5-flash")
        )
        self.verifier = VerifierAgent(
            self.gateway, selected.get("independent_verification", "gemini-3.5-flash")
        )
        self.reviser = RevisionAgent(
            self.gateway, selected.get("revision", "gemini-3.5-flash")
        )

    def _write(self, path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(path.suffix + f".{uuid4().hex}.tmp")
        temp.write_text(text + "\n", encoding="utf-8")
        os.replace(temp, path)

    def _load(self, path: Path, schema: type[T]) -> T | None:
        if not path.exists():
            return None
        return schema.model_validate_json(path.read_text(encoding="utf-8"))

    def _temperament_audit(self, output_dir: Path) -> list[dict[str, object]]:
        """Collect only APT-3 decisions that actually broke an equal-choice tie."""
        decisions: list[dict[str, object]] = []
        seen: set[str] = set()
        for path in sorted(output_dir.glob("*.json")):
            if path.name in {"execution_checkpoint.json", "temperament_decisions.json"}:
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict):
                continue
            for decision in payload.get("temperament_decisions", []):
                if not isinstance(decision, dict):
                    continue
                key = json.dumps(decision, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                if key not in seen:
                    seen.add(key)
                    decisions.append(decision)
        return decisions

    def _checkpoint(
        self,
        output_dir: Path,
        status: PipelineStatus,
        stage: str,
        completed: list[str],
        revision_round: int,
        verdict: Verdict | None = None,
        message: str = "",
    ) -> None:
        checkpoint = ExecutionCheckpoint(
            status=status,
            current_stage=stage,
            completed_stages=completed,
            revision_round=revision_round,
            final_verdict=verdict,
            message=message,
        )
        self._write(output_dir / "execution_checkpoint.json", checkpoint.model_dump_json(indent=2))

    def run(
        self,
        *,
        intake: IntakeRequest,
        requirements: RequirementsAnalysis,
        sources: list[InternalSource],
        output_dir: Path,
    ) -> ExecutionCheckpoint:
        requirements = require_ready_for_estimate(
            intake.model_copy(update={"internal_sources": sources}), requirements, sources
        )
        contract = {
            "goal": intake.goal,
            "desired_output": intake.desired_output,
            "normalized_goal": requirements.normalized_goal,
            "deliverables": requirements.deliverables,
            "acceptance_criteria": requirements.acceptance_criteria,
            "assumptions": requirements.assumptions,
            "public_research_allowed": intake.public_research_allowed,
        }
        source_payload = [
            {
                "name": source.name,
                "priority": source.priority.value,
                "requirement_keys": source.requirement_keys,
                "content": source.content,
                "sha256": source.sha256,
            }
            for source in sources
        ]
        completed: list[str] = []
        revision_round = 0
        runtime = (
            ExecutionGraphRuntime(self.execution_graph, output_dir / "execution_graph_state.json")
            if self.execution_graph is not None
            else None
        )

        def graph_begin(stage: str) -> bool:
            if runtime is None:
                return True
            node = runtime.graph.node_for_stage(stage)
            if runtime.state.nodes[node.node_id].status == NodeStatus.COMPLETE:
                return False
            runtime.start(node.node_id)
            return True

        def graph_complete(stage: str, *paths: str, message: str = "") -> None:
            if runtime is None:
                return
            node = runtime.graph.node_for_stage(stage)
            if runtime.state.nodes[node.node_id].status != NodeStatus.COMPLETE:
                runtime.complete(node.node_id, *paths, message=message)

        def has_graph_stage(stage: str) -> bool:
            if runtime is None:
                return False
            try:
                runtime.graph.node_for_stage(stage)
                return True
            except KeyError:
                return False

        def run_handoff(stage: str, payload: dict[str, object]) -> RoleHandoff | None:
            if not has_graph_stage(stage):
                return None
            path = output_dir / f"{stage}.json"
            handoff = self._load(path, RoleHandoff)
            graph_begin(stage)
            if handoff is None:
                handoff = DynamicRoleAgent(
                    self.gateway,
                    stage,
                    self.stage_models.get(stage, "gemini-3.5-flash"),
                ).run(payload)
                self._write(path, handoff.model_dump_json(indent=2))
            graph_complete(stage, path.name)
            return handoff

        try:
            architecture = run_handoff(
                "project_architecture",
                {"contract": contract, "sources": source_payload},
            )
            if architecture is not None:
                contract["project_architecture"] = architecture.model_dump(mode="json")

            public_research: PublicResearchResult | None = None
            if intake.public_research_allowed:
                research_path = output_dir / "public_research.json"
                graph_begin("public_research")
                public_research = self._load(research_path, PublicResearchResult)
                if public_research is None:
                    self._checkpoint(output_dir, PipelineStatus.RUNNING, "public_research", completed, 0)
                    public_research = run_grounded_research(
                        self.gateway, goal=intake.goal, desired_output=intake.desired_output
                    )
                    self._write(research_path, public_research.model_dump_json(indent=2))
                    self._write(output_dir / "public_research.md", public_research.answer_markdown)
                    if public_research.search_suggestions_html:
                        self._write(
                            output_dir / "google_search_suggestions.html",
                            public_research.search_suggestions_html,
                        )
                sources = [*sources, public_research.as_internal_source()]
                source_payload = [
                    {
                        "name": source.name,
                        "priority": source.priority.value,
                        "requirement_keys": source.requirement_keys,
                        "content": source.content,
                        "sha256": source.sha256,
                    }
                    for source in sources
                ]
                graph_complete("public_research", "public_research.json", "public_research.md")
                completed.append("public_research")

            analysis_path = output_dir / "analysis.json"
            graph_begin("evidence_analysis")
            analysis = self._load(analysis_path, AnalysisPackage)
            if analysis is None:
                self._checkpoint(output_dir, PipelineStatus.RUNNING, "evidence_analysis", completed, 0)
                analysis = self.analyst.run(contract, source_payload)
                self._write(analysis_path, analysis.model_dump_json(indent=2))
            graph_complete("evidence_analysis", "analysis.json")
            completed.append("evidence_analysis")

            creative = run_handoff(
                "creative_direction",
                {"contract": contract, "analysis": analysis.model_dump(mode="json")},
            )
            if creative is not None:
                contract["creative_direction"] = creative.model_dump(mode="json")

            draft_path = output_dir / "draft_r0.json"
            graph_begin("long_form_draft")
            draft = self._load(draft_path, DraftArtifact)
            if draft is None:
                self._checkpoint(output_dir, PipelineStatus.RUNNING, "long_form_draft", completed, 0)
                draft = self.writer.run(contract, analysis, source_payload)
                self._write(draft_path, draft.model_dump_json(indent=2))
            graph_complete("long_form_draft", "draft_r0.json")
            completed.append("long_form_draft")

            integration = run_handoff(
                "artifact_integration",
                {
                    "contract": contract,
                    "analysis": analysis.model_dump(mode="json"),
                    "draft": draft.model_dump(mode="json"),
                },
            )
            if integration is not None:
                contract["artifact_integration"] = integration.model_dump(mode="json")

            graph_begin("independent_verification")
            verification_path = output_dir / "verification_r0.json"
            grounding_path = output_dir / "deterministic_verification_r0.json"
            model_verification_path = output_dir / "model_verification_r0.json"
            report = self._load(verification_path, VerificationReport)
            if report is None or not grounding_path.exists():
                self._checkpoint(output_dir, PipelineStatus.RUNNING, "verification_r0", completed, 0)
                model_report = self._load(model_verification_path, VerificationReport)
                if model_report is None:
                    model_report = report or self.verifier.run(
                        contract, analysis, draft, 0, source_payload
                    )
                    self._write(
                        model_verification_path, model_report.model_dump_json(indent=2)
                    )
                grounding = validate_draft_grounding(sources, draft)
                self._write(grounding_path, grounding.model_dump_json(indent=2))
                report = apply_deterministic_override(model_report, grounding)
                self._write(verification_path, report.model_dump_json(indent=2))
            completed.append("verification_r0")

            while report.verdict == Verdict.REVISE and revision_round < intake.max_revision_rounds:
                revision_round += 1
                revision_stage = f"revision_r{revision_round}"
                revision_path = output_dir / f"revision_r{revision_round}.json"
                revision = self._load(revision_path, RevisionArtifact)
                if revision is None:
                    self._checkpoint(
                        output_dir,
                        PipelineStatus.RUNNING,
                        revision_stage,
                        completed,
                        revision_round,
                    )
                    revision = self.reviser.run(
                        contract, analysis, draft, report, revision_round, source_payload
                    )
                    self._write(revision_path, revision.model_dump_json(indent=2))
                draft = DraftArtifact(
                    title=revision.title,
                    body_markdown=revision.revised_body_markdown,
                    cited_finding_ids=revision.cited_finding_ids,
                    drafting_decisions=[*draft.drafting_decisions, *revision.addressed_issues],
                    temperament_decisions=[
                        *draft.temperament_decisions,
                        *revision.temperament_decisions,
                    ],
                )
                completed.append(revision_stage)

                verification_stage = f"verification_r{revision_round}"
                verification_path = output_dir / f"verification_r{revision_round}.json"
                grounding_path = output_dir / f"deterministic_verification_r{revision_round}.json"
                model_verification_path = output_dir / f"model_verification_r{revision_round}.json"
                next_report = self._load(verification_path, VerificationReport)
                if next_report is None or not grounding_path.exists():
                    self._checkpoint(
                        output_dir,
                        PipelineStatus.RUNNING,
                        verification_stage,
                        completed,
                        revision_round,
                    )
                    model_report = self._load(model_verification_path, VerificationReport)
                    if model_report is None:
                        model_report = next_report or self.verifier.run(
                            contract, analysis, draft, revision_round, source_payload
                        )
                        self._write(
                            model_verification_path, model_report.model_dump_json(indent=2)
                        )
                    grounding = validate_draft_grounding(sources, draft)
                    self._write(grounding_path, grounding.model_dump_json(indent=2))
                    next_report = apply_deterministic_override(model_report, grounding)
                    self._write(verification_path, next_report.model_dump_json(indent=2))
                report = next_report
                completed.append(verification_stage)

            graph_complete(
                "independent_verification",
                f"verification_r{revision_round}.json",
                f"deterministic_verification_r{revision_round}.json",
            )

            governance: GovernanceDecision | None = None
            if has_graph_stage("policy_guard"):
                guard_path = output_dir / "policy_guard.json"
                governance = self._load(guard_path, GovernanceDecision)
                graph_begin("policy_guard")
                if governance is None:
                    governance = GovernanceAgent(
                        self.gateway,
                        "policy_guard",
                        self.stage_models.get("policy_guard", "gemini-3.5-flash"),
                    ).run({
                        "contract": contract,
                        "verification": report.model_dump(mode="json"),
                        "draft": draft.model_dump(mode="json"),
                    })
                    self._write(guard_path, governance.model_dump_json(indent=2))
                graph_complete("policy_guard", "policy_guard.json")

            if report.verdict == Verdict.NEEDS_INFORMATION:
                status = PipelineStatus.NEEDS_INFORMATION
                message = "Verifier found a required conclusion unsupported by supplied evidence."
            elif report.verdict == Verdict.PASS:
                status = PipelineStatus.COMPLETE
                message = "Independent verification passed."
            else:
                status = PipelineStatus.PARTIAL
                message = "Revision limit reached before verification passed."

            if governance is not None and governance.verdict != Verdict.PASS:
                status = (
                    PipelineStatus.NEEDS_INFORMATION
                    if governance.verdict == Verdict.NEEDS_INFORMATION
                    else PipelineStatus.PARTIAL
                )
                message = f"Policy guard returned {governance.verdict.value}: {governance.rationale}"

            if has_graph_stage("final_approval"):
                approval_path = output_dir / "final_approval.json"
                approval = self._load(approval_path, GovernanceDecision)
                graph_begin("final_approval")
                if approval is None:
                    approval = GovernanceAgent(
                        self.gateway,
                        "final_approval",
                        self.stage_models.get("final_approval", "gemini-3.5-flash"),
                    ).run({
                        "contract": contract,
                        "pipeline_status": status.value,
                        "verification": report.model_dump(mode="json"),
                        "policy_guard": governance.model_dump(mode="json") if governance else None,
                    })
                    self._write(approval_path, approval.model_dump_json(indent=2))
                if status == PipelineStatus.COMPLETE and approval.verdict != Verdict.PASS:
                    status = (
                        PipelineStatus.NEEDS_INFORMATION
                        if approval.verdict == Verdict.NEEDS_INFORMATION
                        else PipelineStatus.PARTIAL
                    )
                    message = f"Project owner returned {approval.verdict.value}: {approval.rationale}"
                if status != PipelineStatus.COMPLETE and approval.verdict == Verdict.PASS:
                    raise ValueError("project owner cannot override a failed critic or guardian gate")
                graph_complete("final_approval", "final_approval.json")

            body = draft.body_markdown.strip()
            final_text = body if body.startswith("# ") else f"# {draft.title}\n\n{body}"
            self._write(output_dir / "final.md", final_text)
            export_workbook(final_text, output_dir / "result.xlsx", public_research)
            self._write(output_dir / "final_verification.json", report.model_dump_json(indent=2))
            self._write(
                output_dir / "temperament_decisions.json",
                json.dumps(self._temperament_audit(output_dir), ensure_ascii=False, indent=2),
            )
            BudgetStore(self.run_dir).complete()
            self._checkpoint(
                output_dir,
                status,
                "finished",
                completed,
                revision_round,
                report.verdict,
                message,
            )
            return ExecutionCheckpoint.model_validate_json(
                (output_dir / "execution_checkpoint.json").read_text(encoding="utf-8")
            )
        except BudgetExceeded as exc:
            self._checkpoint(
                output_dir,
                PipelineStatus.NEEDS_BUDGET,
                "budget_gate",
                completed,
                revision_round,
                message=str(exc),
            )
            raise
        except Exception as exc:
            self._checkpoint(
                output_dir,
                PipelineStatus.FAILED,
                "failed",
                completed,
                revision_round,
                message=f"{type(exc).__name__}: {exc}",
            )
            raise

