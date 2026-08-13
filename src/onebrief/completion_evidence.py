"""Deterministic proof-of-use checks for user-facing development work."""

from __future__ import annotations

import re
from enum import StrEnum

from pydantic import BaseModel, Field

from onebrief.execution_schemas import CriterionCheck, VerificationReport, Verdict
from onebrief.handoff_protocol import (
    EvidenceKind,
    EvidenceStatus,
    create_evidence_binding,
)
from onebrief.schemas import IntakeRequest, OutputTarget, RequirementsAnalysis


class CompletionEvidenceKind(StrEnum):
    AUTOMATED_TEST = "automated_test"
    RUNTIME_INTERACTION = "runtime_interaction"
    VISUAL_INTEGRITY = "visual_integrity"


class CompletionEvidenceIssue(BaseModel):
    kind: CompletionEvidenceKind
    message: str


class CompletionEvidenceVerification(BaseModel):
    required: list[CompletionEvidenceKind] = Field(default_factory=list)
    observed_command_ids: list[str] = Field(default_factory=list)
    issues: list[CompletionEvidenceIssue] = Field(default_factory=list)
    verdict_override: Verdict | None = None


_USER_INTERFACE = re.compile(
    r"(?:\bui\b|user[ -]?interface|screen|button|dropdown|render|visual|display|"
    r"화면|버튼|드롭다운|표시|시각|렌더)",
    re.IGNORECASE,
)
_LOCALIZATION = re.compile(
    r"(?:locali[sz]ation|internationali[sz]ation|\bi18n\b|locale|language|"
    r"translation|multilingual|한국어|영어|중국어|일본어|스페인어|다국어|언어|번역)",
    re.IGNORECASE,
)
_UNITY = re.compile(r"(?:unity|유니티)", re.IGNORECASE)


def _contract_text(intake: IntakeRequest, requirements: RequirementsAnalysis) -> str:
    contract = requirements.completion_contract
    parts = [
        intake.goal,
        intake.desired_output or "",
        requirements.normalized_goal,
        *requirements.deliverables,
        *requirements.acceptance_criteria,
    ]
    if contract is not None:
        parts.extend([contract.target_state, contract.pass_condition])
        for criterion in contract.quality_criteria:
            parts.extend([criterion.description, criterion.evidence_required])
    return "\n".join(parts)


def _command_ids(development_evidence: dict[str, object] | None) -> list[str]:
    if not development_evidence:
        return []
    run = development_evidence.get("development_run")
    if not isinstance(run, dict):
        return []
    commands = run.get("commands")
    if not isinstance(commands, list):
        return []
    return [
        str(item.get("command_id", "")).casefold()
        for item in commands
        if isinstance(item, dict) and item.get("command_id")
    ]


def _has_command(commands: list[str], *markers: str) -> bool:
    return any(any(marker in command for marker in markers) for command in commands)


def validate_completion_evidence(
    intake: IntakeRequest,
    requirements: RequirementsAnalysis,
    development_evidence: dict[str, object] | None,
) -> CompletionEvidenceVerification:
    """Require proof that matches the observable behavior promised to the user.

    A successful compile or build is regression evidence, not proof that a newly
    requested user-facing behavior works. The gate is intentionally narrow: it
    activates only for development results that promise an interactive UI.
    """

    text = _contract_text(intake, requirements)
    commands = _command_ids(development_evidence)
    is_unity = (
        intake.output_target == OutputTarget.UNITY_APP
        or bool(_UNITY.search(text))
        or _has_command(commands, "unity_")
    )
    software_targets = {
        OutputTarget.AUTO,
        OutputTarget.EXISTING_PROJECT,
        OutputTarget.WEB_APP,
        OutputTarget.UNITY_APP,
    }
    is_ui = intake.output_target in software_targets and bool(_USER_INTERFACE.search(text))
    is_localization = bool(_LOCALIZATION.search(text))

    required: list[CompletionEvidenceKind] = []
    if development_evidence is not None:
        required.append(CompletionEvidenceKind.AUTOMATED_TEST)
    if is_ui:
        required.append(CompletionEvidenceKind.RUNTIME_INTERACTION)
    if is_ui and (is_unity or is_localization):
        required.append(CompletionEvidenceKind.VISUAL_INTEGRITY)

    observed = {
        CompletionEvidenceKind.AUTOMATED_TEST: _has_command(
            commands, "test", "lint", "verification"
        ),
        CompletionEvidenceKind.RUNTIME_INTERACTION: _has_command(
            commands, "playmode", "e2e", "http", "runtime", "interaction", "smoke"
        ),
        CompletionEvidenceKind.VISUAL_INTEGRITY: _has_command(
            commands, "visual", "screenshot", "glyph", "render"
        ),
    }
    labels = {
        CompletionEvidenceKind.AUTOMATED_TEST: (
            "The changed behavior has no relevant automated test evidence; a compile/build alone "
            "does not prove the requested feature."
        ),
        CompletionEvidenceKind.RUNTIME_INTERACTION: (
            "The result promises an interactive user interface, but no runtime interaction, "
            "PlayMode, browser E2E, or HTTP smoke evidence was produced."
        ),
        CompletionEvidenceKind.VISUAL_INTEGRITY: (
            "The result promises visual or localized UI behavior, but no rendered-state evidence "
            "checked visible text, missing glyphs, and the requested state changes."
        ),
    }
    issues = [
        CompletionEvidenceIssue(kind=kind, message=labels[kind])
        for kind in required
        if not observed[kind]
    ]
    return CompletionEvidenceVerification(
        required=required,
        observed_command_ids=commands,
        issues=issues,
        verdict_override=(
            Verdict.UNVERIFIABLE
            if issues and development_evidence is None
            else Verdict.REVISE if issues else None
        ),
    )


def apply_completion_evidence_override(
    model_report: VerificationReport,
    evidence: CompletionEvidenceVerification,
) -> VerificationReport:
    if evidence.verdict_override is None:
        return model_report
    checks = [*model_report.criterion_checks]
    for issue in evidence.issues:
        checks.append(
            CriterionCheck(
                criterion=f"Completion evidence: {issue.kind.value}",
                passed=False,
                evidence=issue.message,
            )
        )
    messages = [item.message for item in evidence.issues]
    if evidence.verdict_override == Verdict.UNVERIFIABLE:
        return VerificationReport(
            verdict=Verdict.UNVERIFIABLE,
            criterion_checks=checks,
            blocking_issues=list(
                dict.fromkeys([*model_report.blocking_issues, *messages])
            ),
            revision_instructions=[],
            missing_information=model_report.missing_information,
            temperament_decisions=model_report.temperament_decisions,
        )
    if model_report.verdict == Verdict.NEEDS_INFORMATION:
        return VerificationReport(
            verdict=Verdict.NEEDS_INFORMATION,
            criterion_checks=checks,
            blocking_issues=list(
                dict.fromkeys([*model_report.blocking_issues, *messages])
            ),
            revision_instructions=[],
            missing_information=model_report.missing_information,
            temperament_decisions=model_report.temperament_decisions,
        )

    return VerificationReport(
        verdict=Verdict.REVISE,
        criterion_checks=checks,
        blocking_issues=list(dict.fromkeys([*model_report.blocking_issues, *messages])),
        revision_instructions=list(
            dict.fromkeys([*model_report.revision_instructions, *messages])
        ),
        missing_information=model_report.missing_information,
        temperament_decisions=model_report.temperament_decisions,
    )


def apply_trusted_development_evidence(
    model_report: VerificationReport,
    requirements: RequirementsAnalysis,
    development_evidence: dict[str, object] | None,
) -> VerificationReport:
    """Let executed command receipts settle matching deterministic criteria.

    Warning-rich logs remain visible to the independent verifier, but a model
    cannot turn an exit-code-zero compiler receipt into a failed compile.
    Runtime and visual criteria require their own executed adapters.
    """

    contract = requirements.completion_contract
    if contract is None or not development_evidence:
        return model_report
    run = development_evidence.get("development_run")
    commands = run.get("commands") if isinstance(run, dict) else None
    if not isinstance(commands, list):
        return model_report

    passed_commands: dict[EvidenceKind, str] = {}
    for item in commands:
        if not isinstance(item, dict) or item.get("exit_code") != 0:
            continue
        command_id = str(item.get("command_id", ""))
        normalized = command_id.casefold()
        if "compile" in normalized or "build" in normalized:
            passed_commands.setdefault(EvidenceKind.COMPILE, command_id)
        if any(marker in normalized for marker in (
            "playmode", "interaction", "e2e", "http", "runtime",
        )):
            passed_commands.setdefault(EvidenceKind.BEHAVIOR, command_id)
        if any(marker in normalized for marker in (
            "visual", "screenshot", "glyph", "render",
        )):
            passed_commands.setdefault(EvidenceKind.VISUAL, command_id)

    def criterion_kind(text: str) -> EvidenceKind | None:
        normalized = text.casefold()
        if any(marker in normalized for marker in (
            "visual", "screenshot", "rendered png", "layout", "png",
            "시각", "스크린샷", "렌더", "레이아웃",
        )):
            return EvidenceKind.VISUAL
        if any(marker in normalized for marker in (
            "behavior", "interaction", "transition", "navigation", "playmode",
            "동작", "상호작용", "이동", "전환",
        )):
            return EvidenceKind.BEHAVIOR
        if any(marker in normalized for marker in (
            "compile", "compilation", "build", "컴파일", "빌드",
        )):
            return EvidenceKind.COMPILE
        return None

    replacements: dict[str, CriterionCheck] = {}
    for criterion in contract.quality_criteria:
        kind = criterion_kind(
            f"{criterion.description} {criterion.evidence_required}"
        )
        command_id = passed_commands.get(kind) if kind is not None else None
        if command_id is None:
            continue
        binding = create_evidence_binding(
            criterion_id=criterion.criterion_id,
            kind=kind,
            status=EvidenceStatus.PASSED,
            summary=f"Trusted adapter {command_id} completed with exit code 0.",
            command_id=command_id,
        )
        replacements[criterion.criterion_id] = CriterionCheck(
            criterion_id=criterion.criterion_id,
            criterion=criterion.description,
            passed=True,
            evidence=binding.summary,
            evidence_bindings=[binding],
        )
    if not replacements:
        return model_report
    checks = [
        check for check in model_report.criterion_checks
        if check.criterion_id not in replacements
    ]
    checks.extend(replacements.values())
    return model_report.model_copy(update={"criterion_checks": checks})
