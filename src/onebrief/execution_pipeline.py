"""Checkpointed and resumable analyst-writer-verifier-revision execution loop."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TypeVar
from uuid import uuid4

from pydantic import BaseModel

from onebrief.budget_guard import BudgetExceeded, BudgetStore
from onebrief.execution_agents import AnalystAgent, RevisionAgent, VerifierAgent, WriterAgent
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
from onebrief.schemas import IntakeRequest, InternalSource, RequirementsAnalysis

T = TypeVar("T", bound=BaseModel)


class ExecutionPipeline:
    def __init__(self, run_dir: Path, gateway: object | None = None):
        self.run_dir = run_dir
        self.gateway = gateway or BudgetedGeminiClient(run_dir)
        self.analyst = AnalystAgent(self.gateway)
        self.writer = WriterAgent(self.gateway)
        self.verifier = VerifierAgent(self.gateway)
        self.reviser = RevisionAgent(self.gateway)

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
        if not requirements.ready_for_estimate:
            raise ValueError("execution requires a passed requirements reinspection")
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
        try:
            analysis_path = output_dir / "analysis.json"
            analysis = self._load(analysis_path, AnalysisPackage)
            if analysis is None:
                self._checkpoint(output_dir, PipelineStatus.RUNNING, "evidence_analysis", completed, 0)
                analysis = self.analyst.run(contract, source_payload)
                self._write(analysis_path, analysis.model_dump_json(indent=2))
            completed.append("evidence_analysis")

            draft_path = output_dir / "draft_r0.json"
            draft = self._load(draft_path, DraftArtifact)
            if draft is None:
                self._checkpoint(output_dir, PipelineStatus.RUNNING, "long_form_draft", completed, 0)
                draft = self.writer.run(contract, analysis)
                self._write(draft_path, draft.model_dump_json(indent=2))
            completed.append("long_form_draft")

            verification_path = output_dir / "verification_r0.json"
            report = self._load(verification_path, VerificationReport)
            if report is None:
                self._checkpoint(output_dir, PipelineStatus.RUNNING, "verification_r0", completed, 0)
                report = self.verifier.run(contract, analysis, draft, 0)
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
                    revision = self.reviser.run(contract, analysis, draft, report, revision_round)
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
                next_report = self._load(verification_path, VerificationReport)
                if next_report is None:
                    self._checkpoint(
                        output_dir,
                        PipelineStatus.RUNNING,
                        verification_stage,
                        completed,
                        revision_round,
                    )
                    next_report = self.verifier.run(contract, analysis, draft, revision_round)
                    self._write(verification_path, next_report.model_dump_json(indent=2))
                report = next_report
                completed.append(verification_stage)

            if report.verdict == Verdict.NEEDS_INFORMATION:
                status = PipelineStatus.NEEDS_INFORMATION
                message = "Verifier found a required conclusion unsupported by supplied evidence."
            elif report.verdict == Verdict.PASS:
                status = PipelineStatus.COMPLETE
                message = "Independent verification passed."
            else:
                status = PipelineStatus.PARTIAL
                message = "Revision limit reached before verification passed."

            body = draft.body_markdown.strip()
            final_text = body if body.startswith("# ") else f"# {draft.title}\n\n{body}"
            self._write(output_dir / "final.md", final_text)
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

