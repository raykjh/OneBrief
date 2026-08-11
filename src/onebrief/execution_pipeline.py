"""Checkpointed and resumable analyst-writer-verifier-revision execution loop."""

from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
import os
import shutil
from pathlib import Path
from typing import TypeVar
from uuid import uuid4

from pydantic import BaseModel, ValidationError

from onebrief.budget_guard import BudgetExceeded, BudgetStore
from onebrief.adk_convergence import (
    MAKER_STATE_KEY,
    ROUND_STATE_KEY,
    SKIP_VERIFIER_STATE_KEY,
    VERIFICATION_STATE_KEY,
    REVERIFY_EXISTING_STATE_KEY,
    EXACT_EDIT_ANCHORS_STATE_KEY,
    REPAIR_PLAN_STATE_KEY,
    VERIFIER_CONTEXT_STATE_KEY,
    build_text_convergence_agent,
    run_convergence_agent,
)
from onebrief.completion_evidence import (
    apply_completion_evidence_override,
    validate_completion_evidence,
)
from onebrief.deterministic_verification import (
    append_authoritative_csv_tables,
    DeterministicVerification,
    apply_deterministic_override,
    validate_draft_grounding,
)
from onebrief.evidence_sufficiency import (
    apply_evidence_sufficiency_override,
    research_reentry_issues,
    validate_evidence_sufficiency,
)
from onebrief.development_toolpack import (
    CodeChangeSet,
    DevelopmentRun,
    ExchangeDevelopmentToolPack,
    RepositoryInspection,
)
from onebrief.development_progress import development_failure_quality
from onebrief.generic_development_toolpack import (
    ApprovedProjectDevelopmentToolPack,
    CompactProposedProjectCodeChangeSet,
    ProjectCodeChangeSet,
    ProposedProjectCodeChangeSet,
)
from onebrief.greenfield_web_toolpack import GreenfieldWebDevelopmentToolPack
from onebrief.dynamic_role_agents import DynamicRoleAgent, GovernanceAgent, GovernanceDecision, RoleHandoff
from onebrief.execution_agents import (
    AnalystAgent,
    DeveloperAgent,
    VerifierAgent,
    WriterAgent,
)
from onebrief.execution_limits import DEVELOPER_OUTPUT_CAP, VERIFIER_OUTPUT_CAP, WRITER_OUTPUT_CAP
from onebrief.execution_graph import ExecutionGraph, ExecutionGraphRuntime, NodeStatus
from onebrief.execution_schemas import (
    AnalysisPackage,
    DraftArtifact,
    ExecutionCheckpoint,
    PipelineStatus,
    VerificationReport,
    Verdict,
)
from onebrief.completion_ledger import refresh_completion_ledger, settle_consistent_verification
from onebrief.execution_profile import (
    compact_work_contract,
    effective_revision_rounds,
    is_small_document_task,
    requires_full_csv_preservation,
)
from onebrief.guarded_gemini import BudgetedGeminiClient
from onebrief.grounded_search import run_grounded_research
from onebrief.recovery_policy import RecoveryAction, RecoveryDecision, RecoveryPolicy
from onebrief.repair_planning import RepairPlan, build_repair_plan
from onebrief.reality_check import apply_reality_check_override, evaluate_reality_check
from onebrief.requirements_gate import require_ready_for_estimate
from onebrief.schemas import IntakeRequest, InternalSource, OutputTarget, RequirementsAnalysis, ToolPackId
from onebrief.public_research import PublicResearchResult
from onebrief.toolpacks import execute_toolpacks
from onebrief.workbook_export import export_workbook
from onebrief.temperament import (
    VERIFIER_PROFILE,
    WRITER_PROFILE,
    enforce_temperament_audit,
)

T = TypeVar("T", bound=BaseModel)


class ExecutionPipeline:
    def __init__(
        self,
        run_dir: Path,
        gateway: object | None = None,
        stage_models: dict[str, str] | None = None,
        stage_skills: dict[str, list[str]] | None = None,
        execution_graph: ExecutionGraph | None = None,
        project_registry_root: Path | None = None,
    ):
        self.run_dir = run_dir
        self.gateway = gateway or BudgetedGeminiClient(run_dir)
        selected = stage_models or {}
        self.stage_models = selected
        assigned_skills = stage_skills or {}
        self.stage_skills = assigned_skills
        self.execution_graph = execution_graph
        self.project_registry_root = project_registry_root
        self.analyst = AnalystAgent(
            self.gateway, selected.get("evidence_analysis", "gemini-3.5-flash"), assigned_skills.get("evidence_analysis")
        )
        self.writer = WriterAgent(
            self.gateway, selected.get("long_form_draft", "gemini-3.5-flash"), assigned_skills.get("long_form_draft")
        )
        self.developer = DeveloperAgent(
            self.gateway, selected.get("long_form_draft", "gemini-3.5-flash"), assigned_skills.get("long_form_draft")
        )
        self.verifier = VerifierAgent(
            self.gateway, selected.get("independent_verification", "gemini-3.5-flash"), assigned_skills.get("independent_verification")
        )
        self.recovery_policy = RecoveryPolicy()
        self.recovery_decisions: list[RecoveryDecision] = []

    @staticmethod
    def _is_development(intake: IntakeRequest) -> bool:
        return any(
            item in intake.toolpack_ids
            for item in (
                ToolPackId.EXCHANGE_DEVELOPMENT,
                ToolPackId.PROJECT_DEVELOPMENT,
                ToolPackId.GREENFIELD_WEB_DEVELOPMENT,
            )
        )

    def _development_components(self, intake: IntakeRequest, output_dir: Path | None = None):
        if ToolPackId.PROJECT_DEVELOPMENT in intake.toolpack_ids:
            if not intake.existing_project_id:
                raise ValueError("project development requires a selected imported project")
            pack = ApprovedProjectDevelopmentToolPack(
                intake.existing_project_id, registry_root=self.project_registry_root
            )
            developer = DeveloperAgent(
                self.gateway,
                self.stage_models.get("long_form_draft", "gemini-3.5-flash"),
                self.stage_skills.get("long_form_draft"),
                change_set_schema=ProjectCodeChangeSet,
                source_prefix="project-source/",
                path_approver=pack.approved_edit_path,
            )
            return ProjectCodeChangeSet, pack, developer
        if ToolPackId.GREENFIELD_WEB_DEVELOPMENT in intake.toolpack_ids:
            if output_dir is None:
                raise ValueError("greenfield web development requires an output directory")
            pack = GreenfieldWebDevelopmentToolPack(
                output_dir / "toolpacks" / ToolPackId.GREENFIELD_WEB_DEVELOPMENT.value / "scaffold"
            )
            developer = DeveloperAgent(
                self.gateway,
                self.stage_models.get("long_form_draft", "gemini-3.5-flash"),
                self.stage_skills.get("long_form_draft"),
                change_set_schema=ProjectCodeChangeSet,
                source_prefix="greenfield-source/",
                path_approver=pack.approved_edit_path,
            )
            return ProjectCodeChangeSet, pack, developer
        return CodeChangeSet, ExchangeDevelopmentToolPack(), self.developer

    def _write(self, path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(path.suffix + f".{uuid4().hex}.tmp")
        temp.write_text(text + "\n", encoding="utf-8")
        os.replace(temp, path)

    def _load(self, path: Path, schema: type[T]) -> T | None:
        if not path.exists():
            return None
        return schema.model_validate_json(path.read_text(encoding="utf-8"))


    @staticmethod
    def _merge_development_retry(previous: T, retry: T) -> T:
        """Overlay a bounded retry on the prior full change set."""
        prior_by_path = {item.path: item for item in previous.changes}
        order = [item.path for item in previous.changes]
        merged = dict(prior_by_path)
        directive = " ".join([
            retry.summary,
            *(item.reason for item in retry.changes),
        ]).casefold()
        if any(stem in directive for stem in ("remov", "delet")):
            for path, prior in prior_by_path.items():
                if prior.base_sha256 is None and Path(path).stem.casefold() in directive:
                    merged.pop(path, None)
                    order = [item for item in order if item != path]

        for item in retry.changes:
            prior = prior_by_path.get(item.path)
            removal = bool(
                prior is not None
                and prior.base_sha256 is None
                and item.content == ""
                and any(stem in item.reason.casefold() for stem in ("remov", "delet"))
            )
            if removal:
                merged.pop(item.path, None)
                order = [path for path in order if path != item.path]
                continue
            merged[item.path] = item
            if item.path not in order:
                order.append(item.path)
        payload = retry.model_dump(mode="json")
        payload["changes"] = [merged[path].model_dump(mode="json") for path in order]
        return type(previous).model_validate(payload)

    def _bind_project_change_set(
        self, intake: IntakeRequest, development_pack, change_set: T, output_dir: Path
    ) -> T:
        if ToolPackId.PROJECT_DEVELOPMENT not in intake.toolpack_ids:
            return change_set
        inspection_path = (
            output_dir / "toolpacks" / ToolPackId.PROJECT_DEVELOPMENT.value
            / "evidence" / "repository_inspection.json"
        )
        inspection = self._load(inspection_path, RepositoryInspection)
        if inspection is None:
            raise RuntimeError("approved project inspection is unavailable")
        return development_pack.bind_change_set_to_inspection(change_set, inspection)

    @staticmethod
    def _apply_development_change_set(
        intake: IntakeRequest,
        development_pack,
        change_set,
        development_dir: Path,
        contract: dict[str, object],
    ) -> DevelopmentRun:
        if any(item in intake.toolpack_ids for item in (
            ToolPackId.PROJECT_DEVELOPMENT,
            ToolPackId.GREENFIELD_WEB_DEVELOPMENT,
        )):
            return development_pack.apply_and_verify(
                change_set,
                development_dir,
                verification_goal=json.dumps(contract, ensure_ascii=False),
            )
        return development_pack.apply_and_verify(change_set, development_dir)
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

    def _append_recovery(self, decision: RecoveryDecision) -> None:
        key = decision.model_dump_json()
        if all(item.model_dump_json() != key for item in self.recovery_decisions):
            self.recovery_decisions.append(decision)

    def _capture_developer_recoveries(self, developer: DeveloperAgent | None = None) -> None:
        active = developer or self.developer
        for decision in active.last_recovery_decisions:
            self._append_recovery(decision)

    def _persist_recoveries(self, output_dir: Path) -> None:
        if not self.recovery_decisions:
            return
        payload = {
            "schema_version": "onebrief-recovery-log-v1",
            "decisions": [item.model_dump(mode="json") for item in self.recovery_decisions],
        }
        self._write(
            output_dir / "recovery_decisions.json",
            json.dumps(payload, ensure_ascii=False, indent=2),
        )

    def _development_evidence(self, output_dir: Path) -> dict[str, object] | None:
        development_dir = output_dir / "development"
        run = self._load(development_dir / "development_run.json", DevelopmentRun)
        change_set_path = output_dir / "code_change_set.json"
        if run is None or not change_set_path.is_file():
            return None
        try:
            change_set = json.loads(change_set_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        changed_files: list[dict[str, str]] = []
        remaining = 70_000
        for relative in run.changed_paths:
            path = development_dir / "changed_files" / Path(*relative.split("/"))
            if not path.is_file() or remaining <= 0:
                continue
            content = path.read_text(encoding="utf-8", errors="replace")
            excerpt = content[:remaining]
            remaining -= len(excerpt)
            changed_files.append({"path": relative, "content": excerpt})
        runtime_evidence: list[dict[str, str]] = []
        for relative in run.evidence_paths:
            evidence_root = (output_dir / Path(*relative.split("/"))).resolve()
            if not evidence_root.is_relative_to(output_dir.resolve()) or not evidence_root.is_dir():
                continue
            for path in sorted(evidence_root.rglob("*")):
                if not path.is_file() or path.suffix.casefold() not in {".json", ".xml"}:
                    continue
                runtime_evidence.append({
                    "path": path.relative_to(output_dir).as_posix(),
                    "content": path.read_text(encoding="utf-8", errors="replace")[:20_000],
                })
        trusted_observation_receipts: list[dict[str, object]] = []
        observation_dir = output_dir / "independent_observations"
        if observation_dir.is_dir():
            for path in sorted(observation_dir.glob("*.json")):
                try:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                if isinstance(payload, dict):
                    trusted_observation_receipts.append(payload)
        return {
            "change_set": change_set,
            "development_run": run.model_dump(mode="json"),
            "changed_files": changed_files,
            "runtime_evidence": runtime_evidence,
            "trusted_observation_receipts": trusted_observation_receipts,
            "verification_rule": (
                "Compare every deliverable and claimed feature with the actual changed files. "
                "Legacy tests prove regression safety only; new behavior needs relevant deterministic evidence. "
                "For user-facing work, require trusted runtime evidence and reject compile-only proof."
            ),
        }

    def _run_adk_document_convergence(
        self,
        *,
        intake: IntakeRequest,
        requirements: RequirementsAnalysis,
        sources: list[InternalSource],
        source_payload: list[dict[str, object]],
        contract: dict[str, object],
        analysis: AnalysisPackage,
        output_dir: Path,
    ) -> tuple[DraftArtifact, VerificationReport, int]:
        """Run the real ADK maker/verifier loop while deterministic gates retain veto power."""

        repair_fingerprints: list[str] = []

        def prepare_repair(
            report: VerificationReport, round_number: int
        ) -> RepairPlan | None:
            if requirements.completion_contract is None or report.verdict != Verdict.REVISE:
                return None
            plan = build_repair_plan(
                requirements.completion_contract,
                report,
                round_number=round_number,
                prior_fingerprints=repair_fingerprints,
            )
            if plan is None:
                return None
            repair_fingerprints.extend(item.fingerprint for item in plan.tasks)
            self._write(
                output_dir / f"repair_plan_r{round_number}.json",
                plan.model_dump_json(indent=2),
            )
            return plan

        def after_maker(raw: object, _ctx, round_number: int) -> dict[str, object]:
            draft = enforce_temperament_audit(
                DraftArtifact.model_validate(raw), WRITER_PROFILE
            )
            if intake.output_target == OutputTarget.SPREADSHEET:
                draft = append_authoritative_csv_tables(sources, draft)
            self._write(
                output_dir / f"draft_r{round_number}.json",
                draft.model_dump_json(indent=2),
            )
            return {MAKER_STATE_KEY: draft.model_dump(mode="json")}

        def verification_gate(
            raw_report: VerificationReport, _ctx, round_number: int
        ) -> VerificationReport:
            model_report = enforce_temperament_audit(raw_report, VERIFIER_PROFILE)
            self._write(
                output_dir / f"model_verification_r{round_number}.json",
                model_report.model_dump_json(indent=2),
            )
            draft = DraftArtifact.model_validate(_ctx.session.state[MAKER_STATE_KEY])
            grounding = validate_draft_grounding(
                sources, draft,
                require_full_csv_preservation=requires_full_csv_preservation(requirements),
            )
            self._write(
                output_dir / f"deterministic_verification_r{round_number}.json",
                grounding.model_dump_json(indent=2),
            )
            completion = validate_completion_evidence(intake, requirements, None)
            self._write(
                output_dir / f"completion_evidence_r{round_number}.json",
                completion.model_dump_json(indent=2),
            )
            report = apply_completion_evidence_override(model_report, completion)
            report = apply_deterministic_override(report, grounding)
            sufficiency = validate_evidence_sufficiency(
                intake, requirements, sources, draft
            )
            self._write(
                output_dir / f"evidence_sufficiency_r{round_number}.json",
                sufficiency.model_dump_json(indent=2),
            )
            report = apply_evidence_sufficiency_override(report, sufficiency)
            reality = evaluate_reality_check(intake, requirements, None)
            self._write(
                output_dir / f"reality_check_r{round_number}.json",
                reality.model_dump_json(indent=2),
            )
            report = apply_reality_check_override(report, reality)
            if requirements.completion_contract is not None:
                report = settle_consistent_verification(
                    requirements.completion_contract, report
                )
            repair_plan = prepare_repair(report, round_number)
            if repair_plan is not None:
                _ctx.session.state[REPAIR_PLAN_STATE_KEY] = repair_plan.model_dump(mode="json")
                if repair_plan.stop_after_this_round:
                    message = (
                        "The same evidence failures repeated after bounded repair and decomposition. "
                        "OneBrief stopped blind retry and preserved the unresolved candidate for a new plan."
                    )
                    report = VerificationReport(
                        verdict=Verdict.UNVERIFIABLE,
                        criterion_checks=report.criterion_checks,
                        blocking_issues=list(dict.fromkeys([
                            *report.blocking_issues,
                            message,
                        ])),
                        revision_instructions=[],
                        missing_information=report.missing_information,
                        temperament_decisions=report.temperament_decisions,
                    )
            self._write(
                output_dir / f"verification_r{round_number}.json",
                report.model_dump_json(indent=2),
            )
            return report

        maker_instruction = (
            "You are OneBrief's accountable artifact maker. Create the complete requested artifact from "
            "the work contract, analysis package, and authoritative sources in the user payload. On later "
            "iterations revise your own prior artifact, address every blocking issue, and preserve passing "
            "content. Every material claim must cite supplied F-prefixed finding IDs. Never change the goal, "
            "invent evidence, or make a high-impact human decision. For public research, expose direct source "
            "URLs beside the claims or rows they support; an F-prefixed finding ID alone is not inspectable "
            "evidence. Never infer that no equivalent exists from novelty, a registration date, or category-level "
            "comparison. Use bounded search-scope language and preserve uncertainty. Do not add unsolicited next "
            "steps beyond an explicit scope ceiling. When repair_plan is present, treat it as the complete scope "
            "of the revision, repair each listed evidence failure, and preserve passing criteria. Return only the "
            "required structured object. "
            + WRITER_PROFILE.instruction()
        )
        verifier_instruction = (
            "You are OneBrief's independent verifier and did not create the artifact. Test every acceptance "
            "criterion, grounding, citation, consistency, completeness, and authority boundary against the "
            "user payload and current artifact. PASS only with explicit evidence and no blocker. Use REVISE for "
            "correctable maker work and NEEDS_INFORMATION only for missing authoritative user information. "
            "A category or market segment is not an individually named product or item. If a criterion requires "
            "N products or examples, verify N named, directly source-linked entries. Novelty does not prove absence "
            "of equivalents, and absolute safety or uniqueness claims fail without bounded evidence. Reject any "
            "section beyond an explicit user scope ceiling. "
            "For each completion_contract criterion, return exactly one check with its Q-prefixed criterion_id; "
            "leave criterion_id null only for additional system checks. Give exact revision instructions and "
            "never edit the artifact. Return only the structured object. "
            + VERIFIER_PROFILE.instruction()
        )
        reusable_drafts = sorted(
            output_dir.glob("draft_r*.json"),
            key=lambda path: int(path.stem.rsplit("r", 1)[-1]),
        )
        reverify_only = (
            (output_dir / "reverify_existing_candidate.json").is_file()
            and bool(reusable_drafts)
        )
        initial_state = None
        if reusable_drafts:
            previous_draft = DraftArtifact.model_validate_json(
                reusable_drafts[-1].read_text(encoding="utf-8")
            )
            initial_state = {
                MAKER_STATE_KEY: previous_draft.model_dump(mode="json"),
                # Verify the restored candidate before paying its original
                # maker for a repair. If it fails, later ADK rounds retain the
                # same maker identity and receive the deterministic repair plan.
                REVERIFY_EXISTING_STATE_KEY: True,
            }
        agent = build_text_convergence_agent(
            gateway=self.gateway,
            maker_model=self.stage_models.get("long_form_draft", "gemini-3.5-flash"),
            verifier_model=self.stage_models.get(
                "independent_verification", "gemini-3.5-flash"
            ),
            maker_schema=DraftArtifact,
            max_revision_rounds=(
                0 if reverify_only else effective_revision_rounds(intake, requirements)
            ),
            maker_instruction=maker_instruction,
            verifier_instruction=verifier_instruction,
            maker_output_tokens=WRITER_OUTPUT_CAP,
            verifier_output_tokens=VERIFIER_OUTPUT_CAP,
            after_maker=after_maker,
            verification_gate=verification_gate,
        )
        state, trace = asyncio.run(run_convergence_agent(agent, {
            "work_contract": compact_work_contract(
                contract, enabled=is_small_document_task(intake, requirements)
            ),
            "analysis_package": analysis.model_dump(mode="json"),
            "authoritative_sources": source_payload,
        }, initial_state=initial_state))
        self._write(
            output_dir / "adk_convergence_trace.json",
            json.dumps({
                "schema_version": "onebrief-adk-convergence-trace-v1",
                "agent_tree": {
                    "root": agent.name,
                    "maker": agent.maker.name,
                    "verifier": agent.verifier.name,
                    "same_maker_reused": True,
                },
                "events": trace,
            }, ensure_ascii=False, indent=2),
        )
        round_number = int(state.get(ROUND_STATE_KEY, 0))
        draft = DraftArtifact.model_validate(state[MAKER_STATE_KEY])
        report = VerificationReport.model_validate(state[VERIFICATION_STATE_KEY])
        return draft, report, round_number

    def _run_adk_development_convergence(
        self,
        *,
        intake: IntakeRequest,
        requirements: RequirementsAnalysis,
        sources: list[InternalSource],
        source_payload: list[dict[str, object]],
        contract: dict[str, object],
        analysis: AnalysisPackage,
        output_dir: Path,
    ) -> tuple[DraftArtifact, VerificationReport, int]:
        """Run code creation, isolated verification, review, and same-maker repair in ADK."""

        change_schema, development_pack, developer = self._development_components(intake, output_dir)
        prepared_sources: list[dict[str, object]] = []
        for source in source_payload:
            prepared = dict(source)
            name = str(prepared.get("name", ""))
            candidate = (
                name.removeprefix(developer.source_prefix)
                if name.startswith(developer.source_prefix)
                else ""
            )
            repository_path = developer.path_approver(candidate) if candidate else None
            prepared["repository_path"] = repository_path
            normalized = name.casefold().replace("\\", "/")
            if "/tests/" in normalized or normalized.startswith(
                f"{developer.source_prefix}tests/"
            ):
                prepared["source_role"] = "immutable_acceptance_contract"
            elif repository_path is not None:
                prepared["source_role"] = "editable_source"
            else:
                prepared["source_role"] = "read_only_context"
            prepared_sources.append(prepared)

        # A Cloud continuation may restore the last verified-or-failed candidate
        # into the fresh child work directory.  Preserve that candidate as the
        # same maker's starting point instead of silently asking the model to
        # reconstruct the whole change set from source context again.
        previous_change_set: BaseModel | None = self._load(
            output_dir / "code_change_set.json", change_schema
        )
        best_failed_candidate: BaseModel | None = self._load(
            output_dir / "development_best_candidate.json", change_schema
        )
        best_failure_message: str | None = None
        best_failure_quality: tuple[int, int] | None = None
        best_failure_path = output_dir / "development_best_failure.txt"
        if best_failed_candidate is not None and best_failure_path.is_file():
            best_failure_message = best_failure_path.read_text("utf-8")
            best_failure_quality = development_failure_quality(best_failure_message)
        latest_run: DevelopmentRun | None = None
        initial_state: dict[str, object] = {}
        repair_fingerprints: list[str] = []
        for plan_path in sorted(output_dir.glob("repair_plan_r*.json")):
            try:
                prior_plan = RepairPlan.model_validate_json(
                    plan_path.read_text(encoding="utf-8")
                )
            except (OSError, ValidationError):
                continue
            repair_fingerprints.extend(item.fingerprint for item in prior_plan.tasks)

        def prepare_repair(
            report: VerificationReport, round_number: int
        ) -> RepairPlan | None:
            if requirements.completion_contract is None or report.verdict != Verdict.REVISE:
                return None
            plan = build_repair_plan(
                requirements.completion_contract,
                report,
                round_number=round_number,
                prior_fingerprints=repair_fingerprints,
            )
            if plan is None:
                return None
            repair_fingerprints.extend(item.fingerprint for item in plan.tasks)
            self._write(
                output_dir / (
                    f"repair_plan_r{round_number}_n{len(repair_fingerprints):02d}.json"
                ),
                plan.model_dump_json(indent=2),
            )
            return plan

        maker_sources = prepared_sources
        prior_failure = output_dir / "development_verification_failure.txt"
        if previous_change_set is not None:
            initial_state[MAKER_STATE_KEY] = previous_change_set.model_dump(mode="json")
            if (output_dir / "reverify_existing_candidate.json").is_file():
                initial_state[REVERIFY_EXISTING_STATE_KEY] = True
            changed_paths = {
                str(getattr(item, "path", ""))
                for item in getattr(previous_change_set, "changes", [])
            }
            # The current candidate is already supplied in ADK state. Avoid
            # sending the original version of the same large file a second
            # time, while retaining the trusted full sources in the closure
            # for exact-edit promotion and base-hash enforcement.
            maker_sources = []
            for source in prepared_sources:
                compact = dict(source)
                if str(compact.get("repository_path", "")) in changed_paths:
                    compact["content"] = (
                        "Current candidate content is authoritative in previous_artifact. "
                        "Use an exact small repair against that candidate."
                    )
                maker_sources.append(compact)
        if previous_change_set is not None and prior_failure.is_file():
            feedback = " ".join(prior_failure.read_text("utf-8").split())[:12_000]
            initial_state[VERIFICATION_STATE_KEY] = VerificationReport(
                verdict=Verdict.REVISE,
                criterion_checks=[{
                    "criterion": "Latest isolated continuation check",
                    "passed": False,
                    "evidence": feedback,
                }],
                blocking_issues=[feedback],
                revision_instructions=[
                    "Repair only this evidenced failure and preserve all passing behavior."
                ],
                missing_information=[],
            ).model_dump(mode="json")
            continuation_report = VerificationReport.model_validate(
                initial_state[VERIFICATION_STATE_KEY]
            )
            continuation_plan = prepare_repair(continuation_report, 0)
            if continuation_plan is not None:
                initial_state[REPAIR_PLAN_STATE_KEY] = continuation_plan.model_dump(mode="json")
        repair_feedback = (
            " ".join(prior_failure.read_text("utf-8").split())[:12_000]
            if prior_failure.is_file()
            else None
        )
        exact_edit_anchors = developer.exact_edit_anchors(
            previous_change_set, repair_feedback
        )
        current_exact_edit_anchors = exact_edit_anchors

        def after_maker(raw: object, _ctx, round_number: int) -> dict[str, object]:
            nonlocal previous_change_set, latest_run
            nonlocal best_failed_candidate, best_failure_message, best_failure_quality
            nonlocal current_exact_edit_anchors
            reverify_existing = (
                round_number == 0
                and bool(_ctx.session.state.get(REVERIFY_EXISTING_STATE_KEY))
                and previous_change_set is not None
            )
            try:
                delta = (
                    previous_change_set
                    if reverify_existing
                    else developer.promote_candidate(
                        raw,
                        prepared_sources,
                        previous_change_set,
                        current_exact_edit_anchors,
                    )
                )
            except (ValidationError, ValueError) as exc:
                if previous_change_set is None:
                    raise
                decision = self.recovery_policy.decide(
                    exc,
                    context="development_candidate_promotion",
                    attempt_number=round_number + 1,
                )
                self._append_recovery(decision)
                self._persist_recoveries(output_dir)
                if (
                    decision.action != RecoveryAction.RETURN_TO_AGENT
                    or not decision.retry_allowed
                ):
                    raise
                feedback = " ".join(str(exc).split())[:12_000]
                self._write(
                    output_dir / f"development_candidate_promotion_failure_r{round_number}.txt",
                    feedback,
                )
                report = VerificationReport(
                    verdict=Verdict.REVISE,
                    criterion_checks=[{
                        "criterion": "Safe structural edit promotion",
                        "passed": False,
                        "evidence": feedback,
                    }],
                    blocking_issues=[feedback],
                    revision_instructions=[
                        "Keep previous_artifact unchanged and copy a small exact search or both anchors verbatim from it."
                    ],
                    missing_information=[],
                )
                repair_plan = prepare_repair(report, round_number)
                return {
                    MAKER_STATE_KEY: previous_change_set.model_dump(mode="json"),
                    VERIFICATION_STATE_KEY: report.model_dump(mode="json"),
                    **({
                        REPAIR_PLAN_STATE_KEY: repair_plan.model_dump(mode="json")
                    } if repair_plan is not None else {}),
                    EXACT_EDIT_ANCHORS_STATE_KEY: current_exact_edit_anchors,
                    SKIP_VERIFIER_STATE_KEY: True,
                }
            self._write(
                output_dir / f"code_change_set_delta_r{round_number}.json",
                delta.model_dump_json(indent=2),
            )
            candidate = previous_change_set if reverify_existing else (
                self._merge_development_retry(previous_change_set, delta)
                if previous_change_set is not None
                else delta
            )
            candidate = self._bind_project_change_set(
                intake, development_pack, candidate, output_dir
            )
            previous_change_set = candidate
            self._write(
                output_dir / f"code_change_set_r{round_number}.json",
                candidate.model_dump_json(indent=2),
            )
            self._write(
                output_dir / "code_change_set.json", candidate.model_dump_json(indent=2)
            )
            development_dir = output_dir / "development"
            if development_dir.exists():
                shutil.rmtree(development_dir)
            try:
                latest_run = self._apply_development_change_set(
                    intake, development_pack, candidate, development_dir, contract
                )
            except RuntimeError as exc:
                latest_run = None
                decision = self.recovery_policy.decide(
                    exc,
                    context="development_verification",
                    attempt_number=round_number + 1,
                )
                self._append_recovery(decision)
                self._persist_recoveries(output_dir)
                feedback = " ".join(str(exc).split())[:12_000]
                self._write(
                    output_dir / f"development_verification_failure_r{round_number}.txt",
                    feedback,
                )
                self._write(output_dir / "development_verification_failure.txt", feedback)
                quality = development_failure_quality(feedback)
                if best_failure_quality is None or quality > best_failure_quality:
                    best_failed_candidate = candidate
                    best_failure_message = feedback
                    best_failure_quality = quality
                    self._write(
                        output_dir / "development_best_candidate.json",
                        candidate.model_dump_json(indent=2),
                    )
                    self._write(output_dir / "development_best_failure.txt", feedback)
                elif best_failed_candidate is not None and best_failure_message is not None:
                    # Do not let a later repair erase already demonstrated
                    # progress.  The next maker turn and any continuation both
                    # resume from the best deterministic checkpoint.
                    previous_change_set = best_failed_candidate
                    feedback = best_failure_message
                    self._write(
                        output_dir / "code_change_set.json",
                        best_failed_candidate.model_dump_json(indent=2),
                    )
                    self._write(
                        output_dir / "development_verification_failure.txt", feedback
                    )
                if (
                    decision.action != RecoveryAction.RETURN_TO_AGENT
                    or not decision.retry_allowed
                ):
                    raise
                report = VerificationReport(
                    verdict=Verdict.REVISE,
                    criterion_checks=[{
                        "criterion": "Isolated build and test execution",
                        "passed": False,
                        "evidence": feedback,
                    }],
                    blocking_issues=[feedback],
                    revision_instructions=[
                        "Correct the reported build or test failure without removing passing behavior."
                    ],
                    missing_information=[],
                )
                self._write(
                    output_dir / f"verification_r{round_number}.json",
                    report.model_dump_json(indent=2),
                )
                current_exact_edit_anchors = developer.exact_edit_anchors(
                    previous_change_set, feedback
                )
                repair_plan = prepare_repair(report, round_number)
                return {
                    MAKER_STATE_KEY: previous_change_set.model_dump(mode="json"),
                    VERIFICATION_STATE_KEY: report.model_dump(mode="json"),
                    **({
                        REPAIR_PLAN_STATE_KEY: repair_plan.model_dump(mode="json")
                    } if repair_plan is not None else {}),
                    EXACT_EDIT_ANCHORS_STATE_KEY: current_exact_edit_anchors,
                    SKIP_VERIFIER_STATE_KEY: True,
                }
            evidence = self._development_evidence(output_dir)
            return {
                MAKER_STATE_KEY: candidate.model_dump(mode="json"),
                VERIFIER_CONTEXT_STATE_KEY: evidence or {},
                SKIP_VERIFIER_STATE_KEY: False,
            }

        def verification_gate(
            raw_report: VerificationReport, ctx, round_number: int
        ) -> VerificationReport:
            model_report = enforce_temperament_audit(raw_report, VERIFIER_PROFILE)
            if bool(ctx.session.state.get(SKIP_VERIFIER_STATE_KEY, False)):
                return model_report
            self._write(
                output_dir / f"model_verification_r{round_number}.json",
                model_report.model_dump_json(indent=2),
            )
            # Source-level product behavior is proven by the isolated implementation
            # evidence. Markdown/CSV grounding applies only to document artifacts.
            grounding = DeterministicVerification()
            self._write(
                output_dir / f"deterministic_verification_r{round_number}.json",
                grounding.model_dump_json(indent=2),
            )
            evidence = self._development_evidence(output_dir)
            completion = validate_completion_evidence(intake, requirements, evidence)
            self._write(
                output_dir / f"completion_evidence_r{round_number}.json",
                completion.model_dump_json(indent=2),
            )
            report = apply_completion_evidence_override(model_report, completion)
            report = apply_deterministic_override(report, grounding)
            reality = evaluate_reality_check(intake, requirements, evidence)
            self._write(
                output_dir / f"reality_check_r{round_number}.json",
                reality.model_dump_json(indent=2),
            )
            report = apply_reality_check_override(report, reality)
            if requirements.completion_contract is not None:
                report = settle_consistent_verification(
                    requirements.completion_contract, report
                )
            repair_plan = prepare_repair(report, round_number)
            if repair_plan is not None:
                ctx.session.state[REPAIR_PLAN_STATE_KEY] = repair_plan.model_dump(mode="json")
                if repair_plan.stop_after_this_round:
                    report = VerificationReport(
                        verdict=Verdict.UNVERIFIABLE,
                        criterion_checks=report.criterion_checks,
                        blocking_issues=list(dict.fromkeys([
                            *report.blocking_issues,
                            "The same completion failure repeated three times; blind retries are stopped.",
                        ])),
                        revision_instructions=[],
                        missing_information=report.missing_information,
                        temperament_decisions=report.temperament_decisions,
                    )
            feedback = " ".join([
                *report.blocking_issues,
                *report.revision_instructions,
            ])[:12_000]
            ctx.session.state[EXACT_EDIT_ANCHORS_STATE_KEY] = (
                developer.exact_edit_anchors(previous_change_set, feedback)
            )
            self._write(
                output_dir / f"verification_r{round_number}.json",
                report.model_dump_json(indent=2),
            )
            return report

        maker_instruction = (
            "You are OneBrief's accountable software maker. Return the smallest complete runnable source "
            "change set that satisfies the work contract. Existing-file paths must exactly match a non-null "
            "repository_path and retain its exact sha256 as base_sha256; new text source files use null. "
            "Files marked immutable_acceptance_contract may not be changed. Never touch secrets, dependencies, "
            "Git metadata, deployment, accounts, financial transactions, or paths outside the approved project. "
            "For every existing-file change, use one exact search/replace edit and never return the entire file; "
            "the search text must occur exactly once in the approved source. On revision, repair every build, test, "
            "When exact_edit_anchors are supplied, prefer its anchor_id and return the complete replacement for "
            "that displayed source window; OneBrief resolves the ID deterministically. Otherwise copy search text "
            "only from those verbatim windows and keep each edit to the smallest unique anchor. "
            "runtime, or independent-review failure while preserving all "
            "previously passing behavior. New files may use complete content; existing files must use exact edits. "
            "Prefer small incremental changes that can be verified and extended in later rounds. Return only the schema."
            " When repair_plan is present, treat it as the complete scope of this revision: repair those failed "
            "criterion slices only, preserve every passing criterion listed there, and do not redesign unrelated behavior. "
            "A decompose_scope task must be reduced to one independently verifiable source change before editing."
            " For web language repairs, expose a real select whose identity contains language or locale and whose "
            "option values are canonical locale codes such as ko and en. Activating every option must update "
            "document.documentElement.lang and visibly change all meaningful page copy, not only the status line. "
            "For an English state, translate or conditionally render the headings, controls, cards, explanatory "
            "copy, and disclaimer so the remaining non-Latin copy does not dominate the page. A generic button with only "
            "an aria-label is not a verifiable language control."
            + (("\n\n" + developer.skill_context) if developer.skill_context else "")
        )
        verifier_instruction = (
            "You are OneBrief's independent software verifier. You did not author the code. Compare every "
            "deliverable and acceptance criterion against the changed source, isolated build and test commands, "
            "runtime evidence, and trusted observation receipts. Legacy regression tests alone do not prove new "
            "behavior. PASS only when the implementation evidence proves the requested behavior and no blocker "
            "remains. Use REVISE for correctable code and NEEDS_INFORMATION only for an absent authoritative user "
            "decision. For each completion_contract criterion, return exactly one check with its Q-prefixed "
            "criterion_id; leave criterion_id null only for additional system checks. Never edit the code. "
            "Return exact, actionable revision instructions and only the schema. "
            + VERIFIER_PROFILE.instruction()
        )
        agent = build_text_convergence_agent(
            gateway=self.gateway,
            maker_model=self.stage_models.get("long_form_draft", "gemini-3.5-flash"),
            verifier_model=self.stage_models.get(
                "independent_verification", "gemini-3.5-flash"
            ),
            maker_schema=(
                (
                    CompactProposedProjectCodeChangeSet
                    if prior_failure.is_file()
                    else ProposedProjectCodeChangeSet
                )
                if change_schema is ProjectCodeChangeSet
                else change_schema
            ),
            max_revision_rounds=intake.max_revision_rounds,
            maker_instruction=maker_instruction,
            verifier_instruction=verifier_instruction,
            maker_output_tokens=(
                min(DEVELOPER_OUTPUT_CAP, 8_000)
                if prior_failure.is_file()
                else DEVELOPER_OUTPUT_CAP
            ),
            verifier_output_tokens=VERIFIER_OUTPUT_CAP,
            after_maker=after_maker,
            verification_gate=verification_gate,
        )
        state, trace = asyncio.run(run_convergence_agent(agent, {
            "work_contract": contract,
            "analysis_package": analysis.model_dump(mode="json"),
            "approved_repository_files": maker_sources,
            "exact_edit_anchors": exact_edit_anchors,
        }, initial_state={
            **initial_state,
            EXACT_EDIT_ANCHORS_STATE_KEY: exact_edit_anchors,
        }))
        self._write(
            output_dir / "adk_convergence_trace.json",
            json.dumps({
                "schema_version": "onebrief-adk-convergence-trace-v1",
                "workflow": "software_creation_isolated_verification_review_revision",
                "agent_tree": {
                    "root": agent.name,
                    "maker": agent.maker.name,
                    "verifier": agent.verifier.name,
                    "same_maker_reused": True,
                },
                "events": trace,
            }, ensure_ascii=False, indent=2),
        )
        round_number = int(state.get(ROUND_STATE_KEY, 0))
        report = VerificationReport.model_validate(state[VERIFICATION_STATE_KEY])
        if latest_run is None:
            draft = DraftArtifact(
                title="소프트웨어 제작 검증 미완료",
                body_markdown=(
                    "격리된 빌드 또는 테스트가 아직 통과하지 못했습니다. 검증 기록과 수정 지시를 "
                    "보존했으며 승인된 수정 횟수 안에서 더 이상 수렴하지 못했습니다."
                ),
                cited_finding_ids=[item.finding_id for item in analysis.findings],
                drafting_decisions=["실행 증거가 없는 코드를 완성본으로 표시하지 않았습니다."],
            )
        else:
            commands = "\n".join(
                f"- `{item.command_id}`: 통과 (종료 코드 {item.exit_code})"
                for item in latest_run.commands
            )
            changed = "\n".join(f"- `{item}`" for item in latest_run.changed_paths)
            draft = DraftArtifact(
                title="격리 빌드·테스트를 통과한 소프트웨어 개선본",
                body_markdown=(
                    "요청된 변경을 원본과 분리된 작업 공간에서 구현하고 검증했습니다.\n\n"
                    f"## 변경 파일\n\n{changed}\n\n## 자동 검증\n\n{commands}\n\n"
                    "## 전달물\n\n- `development/changes.patch`\n- `development/changed_files/`\n"
                    "- `development/development_run.json`"
                ),
                cited_finding_ids=[item.finding_id for item in analysis.findings],
                drafting_decisions=[
                    "원본 대신 격리 복제본에서 변경했습니다.",
                    "실제 빌드·테스트 증거를 독립 검증에 전달했습니다.",
                ],
            )
        self._write(
            output_dir / f"draft_r{round_number}.json", draft.model_dump_json(indent=2)
        )
        return draft, report, round_number
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
        contract = getattr(self, "_active_completion_contract", None)
        if contract is not None:
            refresh_completion_ledger(contract, output_dir)

    def run(
        self,
        *,
        intake: IntakeRequest,
        requirements: RequirementsAnalysis,
        sources: list[InternalSource],
        output_dir: Path,
    ) -> ExecutionCheckpoint:
        self.recovery_decisions = []
        requirements = require_ready_for_estimate(
            intake.model_copy(update={"internal_sources": sources}), requirements, sources
        )
        self._active_completion_contract = requirements.completion_contract
        contract = {
            "goal": intake.goal,
            "desired_output": intake.desired_output,
            "output_target": intake.output_target.value,
            "normalized_goal": requirements.normalized_goal,
            "deliverables": requirements.deliverables,
            "acceptance_criteria": requirements.acceptance_criteria,
            "completion_contract": (
                requirements.completion_contract.model_dump(mode="json") if requirements.completion_contract else None
            ),
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
        def fail_running_graph(message: str, *, blocked: bool = False) -> None:
            if runtime is None:
                return
            for node_id, record in runtime.state.nodes.items():
                if record.status == NodeStatus.RUNNING:
                    runtime.fail(node_id, message, blocked=blocked)


        try:
            architecture = run_handoff(
                "project_architecture",
                {"contract": contract, "sources": source_payload},
            )
            if architecture is not None:
                contract["project_architecture"] = architecture.model_dump(mode="json")

            public_research: PublicResearchResult | None = None
            tool_sources: list[InternalSource] = []
            research_reentry_path = output_dir / "research_reentry_request.json"
            research_reentry_requested = research_reentry_path.is_file()
            parallel_work: dict[str, object] = {}
            with ThreadPoolExecutor(max_workers=2, thread_name_prefix="onebrief-context") as executor:
                if intake.toolpack_ids:
                    graph_begin("tool_execution")
                    self._checkpoint(
                        output_dir, PipelineStatus.RUNNING, "parallel_context", completed, 0
                    )
                    if ToolPackId.PROJECT_DEVELOPMENT in intake.toolpack_ids:
                        parallel_work["tool_execution"] = executor.submit(
                            execute_toolpacks, intake.toolpack_ids,
                            output_dir / "toolpacks", intake.existing_project_id,
                            "\n".join((intake.goal, intake.desired_output or "")),
                            self.project_registry_root,
                        )
                    else:
                        parallel_work["tool_execution"] = executor.submit(
                            execute_toolpacks, intake.toolpack_ids, output_dir / "toolpacks"
                        )

                if intake.public_research_allowed:
                    research_path = output_dir / "public_research.json"
                    graph_begin("public_research")
                    self._checkpoint(
                        output_dir, PipelineStatus.RUNNING, "parallel_context", completed, 0
                    )

                    def load_or_research() -> PublicResearchResult | None:
                        existing = self._load(research_path, PublicResearchResult)
                        if existing is not None and not research_reentry_requested:
                            return existing
                        try:
                            request = (
                                json.loads(research_reentry_path.read_text(encoding="utf-8"))
                                if research_reentry_requested else {}
                            )
                            max_calls = int(request.get("max_refinement_calls", 1))
                            max_calls = max(1, min(max_calls, 3))
                            prior = existing
                            blockers = [str(item) for item in request.get("blocking_issues", [])]
                            result = None
                            remaining_issues = []
                            for attempt in range(1, max_calls + 1):
                                stage = (
                                    f"public_research_refinement_r{attempt}"
                                    if research_reentry_requested else "public_research"
                                )
                                result = run_grounded_research(
                                    self.gateway,
                                    goal=intake.goal,
                                    desired_output=intake.desired_output,
                                    completion_contract=(
                                        requirements.completion_contract.model_dump(mode="json")
                                        if requirements.completion_contract else None
                                    ),
                                    stage=stage,
                                    prior_research=(prior.answer_markdown if prior else None),
                                    blocking_issues=blockers,
                                )
                                if research_reentry_requested:
                                    self._write(
                                        output_dir / f"public_research_refinement_r{attempt}.json",
                                        result.model_dump_json(indent=2),
                                    )
                                    self._write(
                                        output_dir / f"public_research_refinement_r{attempt}.md",
                                        result.answer_markdown,
                                    )
                                    remaining_issues = research_reentry_issues(
                                        intake, requirements, result.answer_markdown
                                    )
                                    if not remaining_issues:
                                        break
                                    blockers = [item.message for item in remaining_issues]
                                    prior = result
                            if result is None:
                                raise RuntimeError("research re-entry produced no result")
                            if research_reentry_requested:
                                self._write(
                                    output_dir / "research_reentry_status.json",
                                    json.dumps({
                                        "schema_version": "onebrief-research-reentry-status-v1",
                                        "resolved": not remaining_issues,
                                        "remaining_issues": [
                                            item.model_dump(mode="json") for item in remaining_issues
                                        ],
                                    }, ensure_ascii=False, indent=2),
                                )
                        except ValueError as exc:
                            if (
                                "no grounded source urls" not in str(exc).casefold()
                                or not sources
                            ):
                                raise
                            self._write(
                                output_dir / "public_research_unavailable.json",
                                json.dumps({
                                    "status": "no_grounded_sources",
                                    "message": str(exc),
                                    "fallback": "authoritative internal sources",
                                }, ensure_ascii=False, indent=2),
                            )
                            return None
                        self._write(research_path, result.model_dump_json(indent=2))
                        self._write(output_dir / "public_research.md", result.answer_markdown)
                        if result.search_suggestions_html:
                            self._write(
                                output_dir / "google_search_suggestions.html",
                                result.search_suggestions_html,
                            )
                        return result

                    parallel_work["public_research"] = executor.submit(load_or_research)

                outcomes: dict[str, object] = {}
                failures: dict[str, Exception] = {}
                for stage, future in parallel_work.items():
                    try:
                        outcomes[stage] = future.result()
                    except Exception as exc:
                        failures[stage] = exc

            if "tool_execution" in outcomes:
                _, tool_sources = outcomes["tool_execution"]
                graph_complete(
                    "tool_execution",
                    "toolpacks/toolpack_execution.json",
                    message="Approved ToolPack checks completed in the parallel context phase.",
                )
                completed.append("tool_execution")

            if "public_research" in outcomes:
                public_research = outcomes["public_research"]
                if public_research is None:
                    graph_complete(
                        "public_research",
                        "public_research_unavailable.json",
                        message=(
                            "Google Search returned no grounded URLs; the run continued only because "
                            "authoritative internal sources were already supplied."
                        ),
                    )
                else:
                    graph_complete("public_research", "public_research.json", "public_research.md")
                completed.append("public_research")

            if failures:
                if runtime is not None:
                    for stage, exc in failures.items():
                        node = runtime.graph.node_for_stage(stage)
                        if runtime.state.nodes[node.node_id].status == NodeStatus.RUNNING:
                            runtime.fail(node.node_id, str(exc))
                raise next(iter(failures.values()))

            sources = [*sources, *tool_sources]
            if public_research is not None:
                sources.append(public_research.as_internal_source())
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
            analysis_path = output_dir / "analysis.json"
            graph_begin("evidence_analysis")
            analysis = (
                None if research_reentry_requested
                else self._load(analysis_path, AnalysisPackage)
            )
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

            adk_report: VerificationReport | None = None
            advertised_adk = getattr(self.gateway, "supports_adk", None)
            use_adk_convergence = (
                bool(advertised_adk)
                if advertised_adk is not None
                else callable(getattr(self.gateway, "generate_adk_response", None))
            )
            # A trusted automatic-resume child already carries the same maker's
            # verified change set. Continue with the independent verifier instead
            # of paying a new maker to recreate identical work.
            if (
                use_adk_convergence
                and self._is_development(intake)
                and (output_dir / "automatic_resume.json").is_file()
                and self._development_evidence(output_dir) is not None
            ):
                use_adk_convergence = False
            if use_adk_convergence and (output_dir / "adk_convergence_trace.json").is_file():
                completed_rounds = sorted(
                    int(path.stem.rsplit("r", 1)[1])
                    for path in output_dir.glob("verification_r*.json")
                    if path.stem.rsplit("r", 1)[-1].isdigit()
                )
                if not completed_rounds:
                    raise RuntimeError("ADK convergence trace has no verification result")
                revision_round = completed_rounds[-1]
                draft = self._load(
                    output_dir / f"draft_r{revision_round}.json", DraftArtifact
                )
                adk_report = self._load(
                    output_dir / f"verification_r{revision_round}.json", VerificationReport
                )
                if draft is None or adk_report is None:
                    raise RuntimeError("ADK convergence result is incomplete")
            elif use_adk_convergence and not (output_dir / "draft_r0.json").exists():
                self._checkpoint(
                    output_dir, PipelineStatus.RUNNING, "adk_quality_convergence", completed, 0
                )
                convergence_runner = (
                    self._run_adk_development_convergence
                    if self._is_development(intake)
                    else self._run_adk_document_convergence
                )
                draft, adk_report, revision_round = convergence_runner(
                    intake=intake, requirements=requirements, sources=sources,
                    source_payload=source_payload, contract=contract,
                    analysis=analysis, output_dir=output_dir,
                )
            elif (
                use_adk_convergence
                and not self._is_development(intake)
                and (output_dir / "continuation_manifest.json").is_file()
            ):
                # A Cloud continuation restores one canonical narrative
                # candidate. Re-enter the native maker/verifier convergence
                # loop instead of the legacy checkpoint loop so repair plans,
                # repeated-failure stopping, and same-maker identity remain in
                # force after a budget or authorization pause.
                self._checkpoint(
                    output_dir, PipelineStatus.RUNNING, "adk_quality_convergence", completed, 0
                )
                draft, adk_report, revision_round = self._run_adk_document_convergence(
                    intake=intake,
                    requirements=requirements,
                    sources=sources,
                    source_payload=source_payload,
                    contract=contract,
                    analysis=analysis,
                    output_dir=output_dir,
                )
            elif use_adk_convergence:
                # A legacy or interrupted pre-ADK run has no durable ADK session.
                # Resume it through the existing checkpointed path instead of
                # repeating already billed model calls.
                use_adk_convergence = False

            draft_path = output_dir / (
                f"draft_r{revision_round}.json" if adk_report is not None else "draft_r0.json"
            )
            graph_begin("long_form_draft")
            draft = draft if adk_report is not None else self._load(draft_path, DraftArtifact)
            if draft is None:
                self._checkpoint(output_dir, PipelineStatus.RUNNING, "long_form_draft", completed, 0)
                if self._is_development(intake):
                    change_schema, development_pack, developer = self._development_components(intake, output_dir)
                    change_set_path = output_dir / "code_change_set.json"
                    change_set = self._load(change_set_path, change_schema)
                    if change_set is None:
                        change_set = developer.run(contract, analysis, source_payload)
                        self._capture_developer_recoveries(developer)
                        self._persist_recoveries(output_dir)
                        self._write(change_set_path, change_set.model_dump_json(indent=2))
                    development_dir = output_dir / "development"
                    change_set = self._bind_project_change_set(
                        intake, development_pack, change_set, output_dir
                    )
                    self._write(change_set_path, change_set.model_dump_json(indent=2))
                    development_run = self._load(
                        development_dir / "development_run.json", DevelopmentRun
                    )
                    if development_run is None:
                        try:
                            development_run = self._apply_development_change_set(
                                intake, development_pack, change_set, development_dir, contract
                            )
                        except RuntimeError as exc:
                            feedback = str(exc)
                            decision = self.recovery_policy.decide(
                                exc, context="development_verification", attempt_number=1
                            )
                            self._append_recovery(decision)
                            self._persist_recoveries(output_dir)
                            if (
                                decision.action != RecoveryAction.RETURN_TO_AGENT
                                or not decision.retry_allowed
                            ):
                                raise
                            self._write(
                                output_dir / "development_verification_failure_r0.txt",
                                feedback,
                            )
                            retry_change_set = developer.run(
                                contract,
                                analysis,
                                source_payload,
                                verification_feedback=feedback,
                                previous_change_set=change_set,
                            )
                            self._write(
                                output_dir / "code_change_set_retry_delta_r1.json",
                                retry_change_set.model_dump_json(indent=2),
                            )
                            retry_change_set = self._merge_development_retry(
                                change_set, retry_change_set
                            )
                            retry_change_set = self._bind_project_change_set(
                                intake, development_pack, retry_change_set, output_dir
                            )
                            self._capture_developer_recoveries(developer)
                            self._persist_recoveries(output_dir)
                            self._write(
                                output_dir / "code_change_set_retry_r1.json",
                                retry_change_set.model_dump_json(indent=2),
                            )
                            self._write(change_set_path, retry_change_set.model_dump_json(indent=2))
                            try:
                                development_run = self._apply_development_change_set(
                                    intake, development_pack, retry_change_set, development_dir, contract
                                )
                            except RuntimeError as retry_exc:
                                retry_feedback = str(retry_exc)
                                retry_decision = self.recovery_policy.decide(
                                    retry_exc,
                                    context="development_verification",
                                    attempt_number=2,
                                )
                                self._append_recovery(retry_decision)
                                self._persist_recoveries(output_dir)
                                if (
                                    retry_decision.action != RecoveryAction.RETURN_TO_AGENT
                                    or not retry_decision.retry_allowed
                                ):
                                    raise
                                self._write(
                                    output_dir / "development_verification_failure_r1.txt",
                                    retry_feedback,
                                )
                                second_retry_delta = developer.run(
                                    contract,
                                    analysis,
                                    source_payload,
                                    verification_feedback=retry_feedback,
                                    previous_change_set=retry_change_set,
                                )
                                self._write(
                                    output_dir / "code_change_set_retry_delta_r2.json",
                                    second_retry_delta.model_dump_json(indent=2),
                                )
                                second_retry = self._merge_development_retry(
                                    retry_change_set, second_retry_delta
                                )
                                second_retry = self._bind_project_change_set(
                                    intake, development_pack, second_retry, output_dir
                                )
                                self._capture_developer_recoveries(developer)
                                self._persist_recoveries(output_dir)
                                self._write(
                                    output_dir / "code_change_set_retry_r2.json",
                                    second_retry.model_dump_json(indent=2),
                                )
                                self._write(
                                    change_set_path, second_retry.model_dump_json(indent=2)
                                )
                                development_run = self._apply_development_change_set(
                                    intake,
                                    development_pack,
                                    second_retry,
                                    development_dir,
                                    contract,
                                )
                    finding_ids = [item.finding_id for item in analysis.findings]
                    command_lines = "\n".join(
                        f"- `{item.command_id}`: 통과 (종료 코드 {item.exit_code})"
                        for item in development_run.commands
                    )
                    changed_lines = "\n".join(
                        f"- `{item}`" for item in development_run.changed_paths
                    )
                    draft = DraftArtifact(
                        title="검증된 Exchange 웹프로그램 개선본",
                        body_markdown=(
                            "요청된 개선을 원본과 분리된 작업 공간에서 구현하고 검증했습니다. "
                            f"작업 범위는 분석 근거 [{finding_ids[0]}]에 따릅니다.\n\n"
                            "## 변경된 실행 파일\n\n"
                            f"{changed_lines}\n\n"
                            "## 자동 검증\n\n"
                            f"{command_lines}\n\n"
                            "## 전달물\n\n"
                            "- `development/changes.patch`: 검토 후 기존 저장소에 적용할 변경 묶음\n"
                            "- `development/changed_files/`: 변경된 전체 실행 파일\n"
                            "- `development/development_run.json`: 테스트·빌드 및 안전 경계 기록\n\n"
                            "원본 저장소, 원격 저장소, 배포 환경, 계정 및 거래 기능은 변경하지 않았습니다."
                        ),
                        cited_finding_ids=finding_ids,
                        drafting_decisions=[
                            "원본 대신 격리 복제본에서 변경했습니다.",
                            "고정된 테스트와 웹 빌드를 모두 통과한 결과만 반환했습니다.",
                        ],
                    )
                else:
                    draft = self.writer.run(contract, analysis, source_payload)
                    if intake.output_target == OutputTarget.SPREADSHEET:
                        draft = append_authoritative_csv_tables(sources, draft)
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
            active_verification_round = revision_round if adk_report is not None else 0
            verification_path = output_dir / f"verification_r{active_verification_round}.json"
            grounding_path = output_dir / f"deterministic_verification_r{active_verification_round}.json"
            completion_evidence_path = output_dir / f"completion_evidence_r{active_verification_round}.json"
            reality_check_path = output_dir / f"reality_check_r{active_verification_round}.json"
            evidence_sufficiency_path = (
                output_dir / f"evidence_sufficiency_r{active_verification_round}.json"
            )
            model_verification_path = output_dir / f"model_verification_r{active_verification_round}.json"
            report = adk_report or self._load(verification_path, VerificationReport)
            if (
                report is None
                or not grounding_path.exists()
                or not completion_evidence_path.exists()
                or not reality_check_path.exists()
                or not evidence_sufficiency_path.exists()
            ):
                self._checkpoint(output_dir, PipelineStatus.RUNNING, "verification_r0", completed, 0)
                model_report = self._load(model_verification_path, VerificationReport)
                if model_report is None:
                    model_report = report or self.verifier.run(
                        contract, analysis, draft, 0, source_payload,
                        self._development_evidence(output_dir),
                    )
                    self._write(
                        model_verification_path, model_report.model_dump_json(indent=2)
                    )
                grounding = validate_draft_grounding(
                    sources, draft,
                    require_full_csv_preservation=requires_full_csv_preservation(requirements),
                )
                self._write(grounding_path, grounding.model_dump_json(indent=2))
                completion_evidence = validate_completion_evidence(
                    intake, requirements, self._development_evidence(output_dir)
                )
                self._write(
                    completion_evidence_path,
                    completion_evidence.model_dump_json(indent=2),
                )
                report = apply_completion_evidence_override(
                    model_report, completion_evidence
                )
                report = apply_deterministic_override(report, grounding)
                evidence_sufficiency = validate_evidence_sufficiency(
                    intake, requirements, sources, draft
                )
                self._write(
                    evidence_sufficiency_path,
                    evidence_sufficiency.model_dump_json(indent=2),
                )
                report = apply_evidence_sufficiency_override(
                    report, evidence_sufficiency
                )
                reality_check = evaluate_reality_check(
                    intake, requirements, self._development_evidence(output_dir)
                )
                self._write(reality_check_path, reality_check.model_dump_json(indent=2))
                report = apply_reality_check_override(report, reality_check)
                self._write(verification_path, report.model_dump_json(indent=2))
            completed.append("verification_r0")

            while report.verdict == Verdict.REVISE and revision_round < intake.max_revision_rounds:
                revision_round += 1
                if self._is_development(intake):
                    change_schema, development_pack, developer = self._development_components(intake, output_dir)
                    revision_stage = f"development_revision_r{revision_round}"
                    self._checkpoint(
                        output_dir,
                        PipelineStatus.RUNNING,
                        revision_stage,
                        completed,
                        revision_round,
                    )
                    previous_change_set = self._load(
                        output_dir / "code_change_set.json", change_schema
                    )
                    if previous_change_set is None:
                        raise RuntimeError("development revision lost the prior change set")
                    feedback = "\n".join([
                        *report.blocking_issues,
                        *report.revision_instructions,
                    ])
                    retry_change_set = developer.run(
                        contract,
                        analysis,
                        source_payload,
                        verification_feedback=feedback,
                        previous_change_set=previous_change_set,
                    )
                    self._write(
                        output_dir / f"code_change_set_revision_delta_r{revision_round}.json",
                        retry_change_set.model_dump_json(indent=2),
                    )
                    retry_change_set = self._merge_development_retry(
                        previous_change_set, retry_change_set
                    )
                    retry_change_set = self._bind_project_change_set(
                        intake, development_pack, retry_change_set, output_dir
                    )
                    self._capture_developer_recoveries(developer)
                    self._persist_recoveries(output_dir)
                    self._write(
                        output_dir / f"code_change_set_revision_r{revision_round}.json",
                        retry_change_set.model_dump_json(indent=2),
                    )
                    self._write(
                        output_dir / "code_change_set.json",
                        retry_change_set.model_dump_json(indent=2),
                    )
                    development_dir = output_dir / "development"
                    if development_dir.exists():
                        shutil.rmtree(development_dir)
                    self._apply_development_change_set(
                        intake, development_pack, retry_change_set, development_dir, contract
                    )
                    draft = DraftArtifact(
                        title=draft.title,
                        body_markdown=(
                            draft.body_markdown
                            + f"\n\n## 구현 수정 {revision_round}\n\n"
                            + "독립 검증의 차단 항목을 소프트웨어 제작자에게 반환하고 "
                            + "변경 코드를 다시 빌드·테스트했습니다."
                        ),
                        cited_finding_ids=draft.cited_finding_ids,
                        drafting_decisions=[
                            *draft.drafting_decisions,
                            *report.revision_instructions,
                        ],
                        temperament_decisions=list(draft.temperament_decisions),
                    )
                    self._write(
                        output_dir / f"draft_r{revision_round}.json",
                        draft.model_dump_json(indent=2),
                    )
                    completed.append(revision_stage)

                    verification_stage = f"verification_r{revision_round}"
                    self._checkpoint(
                        output_dir,
                        PipelineStatus.RUNNING,
                        verification_stage,
                        completed,
                        revision_round,
                    )
                    model_report = self.verifier.run(
                        contract,
                        analysis,
                        draft,
                        revision_round,
                        source_payload,
                        self._development_evidence(output_dir),
                    )
                    self._write(
                        output_dir / f"model_verification_r{revision_round}.json",
                        model_report.model_dump_json(indent=2),
                    )
                    grounding = validate_draft_grounding(
                        sources, draft,
                        require_full_csv_preservation=requires_full_csv_preservation(requirements),
                    )
                    self._write(
                        output_dir / f"deterministic_verification_r{revision_round}.json",
                        grounding.model_dump_json(indent=2),
                    )
                    completion_evidence = validate_completion_evidence(
                        intake, requirements, self._development_evidence(output_dir)
                    )
                    self._write(
                        output_dir / f"completion_evidence_r{revision_round}.json",
                        completion_evidence.model_dump_json(indent=2),
                    )
                    report = apply_completion_evidence_override(
                        model_report, completion_evidence
                    )
                    report = apply_deterministic_override(report, grounding)
                    evidence_sufficiency = validate_evidence_sufficiency(
                        intake, requirements, sources, draft
                    )
                    self._write(
                        output_dir / f"evidence_sufficiency_r{revision_round}.json",
                        evidence_sufficiency.model_dump_json(indent=2),
                    )
                    report = apply_evidence_sufficiency_override(
                        report, evidence_sufficiency
                    )
                    reality_check = evaluate_reality_check(
                        intake, requirements, self._development_evidence(output_dir)
                    )
                    self._write(
                        output_dir / f"reality_check_r{revision_round}.json",
                        reality_check.model_dump_json(indent=2),
                    )
                    report = apply_reality_check_override(report, reality_check)
                    self._write(
                        output_dir / f"verification_r{revision_round}.json",
                        report.model_dump_json(indent=2),
                    )
                    completed.append(verification_stage)
                    continue
                revision_stage = f"maker_revision_r{revision_round}"
                revision_path = output_dir / f"draft_r{revision_round}.json"
                revised_draft = self._load(revision_path, DraftArtifact)
                if revised_draft is None:
                    self._checkpoint(
                        output_dir,
                        PipelineStatus.RUNNING,
                        revision_stage,
                        completed,
                        revision_round,
                    )
                    revised_draft = self.writer.run(
                        contract,
                        analysis,
                        source_payload,
                        verification_feedback=report,
                        previous_draft=draft,
                        round_number=revision_round,
                    )
                    if intake.output_target == OutputTarget.SPREADSHEET:
                        revised_draft = append_authoritative_csv_tables(sources, revised_draft)
                    self._write(revision_path, revised_draft.model_dump_json(indent=2))
                draft = revised_draft
                completed.append(revision_stage)

                verification_stage = f"verification_r{revision_round}"
                verification_path = output_dir / f"verification_r{revision_round}.json"
                completion_evidence_path = output_dir / f"completion_evidence_r{revision_round}.json"
                reality_check_path = output_dir / f"reality_check_r{revision_round}.json"
                grounding_path = output_dir / f"deterministic_verification_r{revision_round}.json"
                evidence_sufficiency_path = (
                    output_dir / f"evidence_sufficiency_r{revision_round}.json"
                )
                model_verification_path = output_dir / f"model_verification_r{revision_round}.json"
                next_report = self._load(verification_path, VerificationReport)
                if (
                    next_report is None
                    or not grounding_path.exists()
                    or not completion_evidence_path.exists()
                    or not reality_check_path.exists()
                    or not evidence_sufficiency_path.exists()
                ):
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
                            contract, analysis, draft, revision_round, source_payload,
                            self._development_evidence(output_dir),
                        )
                        self._write(
                            model_verification_path, model_report.model_dump_json(indent=2)
                        )
                    grounding = validate_draft_grounding(
                        sources, draft,
                        require_full_csv_preservation=requires_full_csv_preservation(requirements),
                    )
                    self._write(grounding_path, grounding.model_dump_json(indent=2))
                    completion_evidence = validate_completion_evidence(
                        intake, requirements, self._development_evidence(output_dir)
                    )
                    self._write(
                        completion_evidence_path,
                        completion_evidence.model_dump_json(indent=2),
                    )
                    next_report = apply_completion_evidence_override(
                        model_report, completion_evidence
                    )
                    next_report = apply_deterministic_override(next_report, grounding)
                    evidence_sufficiency = validate_evidence_sufficiency(
                        intake, requirements, sources, draft
                    )
                    self._write(
                        evidence_sufficiency_path,
                        evidence_sufficiency.model_dump_json(indent=2),
                    )
                    next_report = apply_evidence_sufficiency_override(
                        next_report, evidence_sufficiency
                    )
                    reality_check = evaluate_reality_check(
                        intake, requirements, self._development_evidence(output_dir)
                    )
                    self._write(reality_check_path, reality_check.model_dump_json(indent=2))
                    next_report = apply_reality_check_override(next_report, reality_check)
                    self._write(verification_path, next_report.model_dump_json(indent=2))
                report = next_report
                completed.append(verification_stage)

            graph_complete(
                "independent_verification",
                f"completion_evidence_r{revision_round}.json",
                f"reality_check_r{revision_round}.json",
                f"verification_r{revision_round}.json",
                f"deterministic_verification_r{revision_round}.json",
                f"evidence_sufficiency_r{revision_round}.json",
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
            elif report.verdict == Verdict.UNVERIFIABLE:
                status = PipelineStatus.PARTIAL
                message = (
                    "A required independent observation capability was unavailable; "
                    "the result was not accepted as complete."
                )
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
                        max_output_tokens=(
                            600
                            if (output_dir / "development_verification_failure.txt").is_file()
                            else 1200
                        ),
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
            if intake.output_target in {OutputTarget.AUTO, OutputTarget.SPREADSHEET}:
                export_workbook(final_text, output_dir / "result.xlsx", public_research)
            self._write(output_dir / "final_verification.json", report.model_dump_json(indent=2))
            self._write(
                output_dir / "temperament_decisions.json",
                json.dumps(self._temperament_audit(output_dir), ensure_ascii=False, indent=2),
            )
            BudgetStore(self.run_dir).complete()
            self._persist_recoveries(output_dir)
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
            self._append_recovery(self.recovery_policy.decide(
                exc, context="budget_gate", attempt_number=1
            ))
            self._persist_recoveries(output_dir)
            fail_running_graph(str(exc), blocked=True)
            self._checkpoint(
                output_dir,
                PipelineStatus.NEEDS_BUDGET,
                "budget_gate",
                completed,
                revision_round,
                message=str(exc),
            )
            raise
        except PermissionError as exc:
            self._append_recovery(self.recovery_policy.decide(
                exc, context="authorization_gate", attempt_number=1
            ))
            self._persist_recoveries(output_dir)
            fail_running_graph(str(exc), blocked=True)
            try:
                BudgetStore(self.run_dir).fail(f"PermissionError: {exc}")
            except Exception:
                pass
            self._checkpoint(
                output_dir,
                PipelineStatus.NEEDS_AUTHORIZATION,
                "authorization_gate",
                completed,
                revision_round,
                message=(
                    "The approved capability boundary is insufficient. "
                    f"Return to stage 1 and approve an amended plan: {exc}"
                ),
            )
            return ExecutionCheckpoint.model_validate_json(
                (output_dir / "execution_checkpoint.json").read_text(encoding="utf-8")
            )
        except Exception as exc:
            self._capture_developer_recoveries()
            decision = self.recovery_policy.decide(
                exc, context="pipeline", attempt_number=1
            )
            if not self.recovery_decisions or (
                self.recovery_decisions[-1].error_summary != decision.error_summary
            ):
                self._append_recovery(decision)
            self._persist_recoveries(output_dir)
            fail_running_graph(f"{type(exc).__name__}: {exc}")
            try:
                BudgetStore(self.run_dir).fail(f"{type(exc).__name__}: {exc}")
            except Exception:
                pass
            self._checkpoint(
                output_dir,
                PipelineStatus.FAILED,
                "failed",
                completed,
                revision_round,
                message=f"{type(exc).__name__}: {exc}",
            )
            raise
