"""Generic observation gate for claims that tests and self-reports cannot prove."""

from __future__ import annotations

import re
from enum import StrEnum

from pydantic import BaseModel, Field

from onebrief.execution_schemas import CriterionCheck, VerificationReport, Verdict
from onebrief.schemas import IntakeRequest, OutputTarget, RequirementsAnalysis


class RealityCapability(StrEnum):
    AUTOMATED_EXECUTION = "automated_execution"
    RUNTIME_INTERACTION = "runtime_interaction"
    RENDERED_ARTIFACT = "rendered_artifact"
    SEMANTIC_OBSERVATION = "semantic_observation"


class ObservationStatus(StrEnum):
    OBSERVED = "observed"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"


class ObservationRequirement(BaseModel):
    capability: RealityCapability
    reason: str
    acceptance_dimensions: list[str] = Field(default_factory=list)


class ObservationReceipt(BaseModel):
    capability: RealityCapability
    observer_pack_id: str
    status: ObservationStatus
    independent_from_maker: bool = False
    artifact_paths: list[str] = Field(default_factory=list)
    findings: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


class RealityCheckResult(BaseModel):
    requirements: list[ObservationRequirement] = Field(default_factory=list)
    receipts: list[ObservationReceipt] = Field(default_factory=list)
    issues: list[str] = Field(default_factory=list)
    verdict_override: Verdict | None = None


_INTERACTIVE = re.compile(
    r"(?:\bui\b|user[ -]?interface|screen|button|dropdown|render|browser|"
    r"화면|버튼|드롭다운|표시)", re.IGNORECASE,
)
_SEMANTIC_VISUAL = re.compile(
    r"(?:locali[sz]ation|internationali[sz]ation|\bi18n\b|locale|language|"
    r"translation|multilingual|readab|legib|design|layout|brand|visual quality|"
    r"한국어|영어|중국어|일본어|스페인어|다국어|언어|번역|가독|디자인|레이아웃|브랜드|깨진 글자)",
    re.IGNORECASE,
)


def _contract_text(intake: IntakeRequest, requirements: RequirementsAnalysis) -> str:
    contract = requirements.completion_contract
    parts = [
        intake.goal,
        intake.desired_output or "",
        requirements.normalized_goal,
        *requirements.deliverables,
        *requirements.acceptance_criteria,
    ]
    if contract:
        parts.extend([contract.target_state, contract.pass_condition])
        for criterion in contract.quality_criteria:
            parts.extend([criterion.description, criterion.evidence_required])
    return "\n".join(parts)


def _command_ids(evidence: dict[str, object] | None) -> list[str]:
    run = evidence.get("development_run") if evidence else None
    commands = run.get("commands") if isinstance(run, dict) else None
    if not isinstance(commands, list):
        return []
    return [
        str(item.get("command_id", "")).casefold()
        for item in commands if isinstance(item, dict) and item.get("command_id")
    ]


def _trusted_receipts(evidence: dict[str, object] | None) -> list[ObservationReceipt]:
    if not evidence:
        return []
    receipts: list[ObservationReceipt] = []
    # Only the pipeline-owned observation area is accepted. Development runtime
    # evidence is maker-controlled and can never certify its own independence.
    items = evidence.get("trusted_observation_receipts")
    if not isinstance(items, list):
        return receipts
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            receipt = ObservationReceipt.model_validate(item)
        except (ValueError, TypeError):
            continue
        receipts.append(receipt)
    return receipts


def evaluate_reality_check(
    intake: IntakeRequest,
    requirements: RequirementsAnalysis,
    development_evidence: dict[str, object] | None,
) -> RealityCheckResult:
    """Select observation capabilities from the contract and reject unobservable PASSes."""

    if development_evidence is None:
        return RealityCheckResult()
    text = _contract_text(intake, requirements)
    commands = _command_ids(development_evidence)
    software_targets = {
        OutputTarget.AUTO,
        OutputTarget.EXISTING_PROJECT,
        OutputTarget.WEB_APP,
        OutputTarget.UNITY_APP,
    }
    is_interactive = (
        intake.output_target in software_targets and bool(_INTERACTIVE.search(text))
    )
    needs_semantic_visual = is_interactive and bool(_SEMANTIC_VISUAL.search(text))
    requirements_list = [
        ObservationRequirement(
            capability=RealityCapability.AUTOMATED_EXECUTION,
            reason="Changed software must execute under an independent automated check.",
        )
    ]
    if is_interactive:
        requirements_list.append(ObservationRequirement(
            capability=RealityCapability.RUNTIME_INTERACTION,
            reason="Interactive behavior must be exercised in a running artifact.",
        ))
    if needs_semantic_visual:
        requirements_list.extend([
            ObservationRequirement(
                capability=RealityCapability.RENDERED_ARTIFACT,
                reason="The requested quality is visible only in rendered output.",
            ),
            ObservationRequirement(
                capability=RealityCapability.SEMANTIC_OBSERVATION,
                reason="A screenshot hash cannot prove language, readability, design, or meaning.",
                acceptance_dimensions=["visible meaning", "readability", "requested state"],
            ),
        ])

    def has(*markers: str) -> bool:
        return any(any(marker in command for marker in markers) for command in commands)

    receipts = _trusted_receipts(development_evidence)
    observed = {
        RealityCapability.AUTOMATED_EXECUTION: has("test", "lint", "verification"),
        RealityCapability.RUNTIME_INTERACTION: has(
            "playmode", "e2e", "http", "runtime", "interaction", "smoke"
        ),
        RealityCapability.RENDERED_ARTIFACT: has(
            "visual", "screenshot", "glyph", "render"
        ),
        RealityCapability.SEMANTIC_OBSERVATION: any(
            receipt.capability == RealityCapability.SEMANTIC_OBSERVATION
            and receipt.status == ObservationStatus.OBSERVED
            and receipt.independent_from_maker
            for receipt in receipts
        ),
    }
    messages = {
        RealityCapability.AUTOMATED_EXECUTION: "No independent automated execution evidence was produced.",
        RealityCapability.RUNTIME_INTERACTION: "No running interaction evidence was produced.",
        RealityCapability.RENDERED_ARTIFACT: "No rendered artifact evidence was produced.",
        RealityCapability.SEMANTIC_OBSERVATION: (
            "No independent semantic observer examined the rendered artifact. "
            "Self-reported locale names, screenshot hashes, and test command names cannot prove visible language or quality."
        ),
    }
    issues = [messages[item.capability] for item in requirements_list if not observed[item.capability]]
    return RealityCheckResult(
        requirements=requirements_list,
        receipts=receipts,
        issues=issues,
        verdict_override=Verdict.UNVERIFIABLE if issues else None,
    )


def apply_reality_check_override(
    report: VerificationReport, result: RealityCheckResult
) -> VerificationReport:
    if result.verdict_override is None or report.verdict == Verdict.NEEDS_INFORMATION:
        return report
    checks = [*report.criterion_checks]
    for issue in result.issues:
        checks.append(CriterionCheck(
            criterion="Reality check: independent observation",
            passed=False,
            evidence=issue,
        ))
    return VerificationReport(
        verdict=Verdict.UNVERIFIABLE,
        criterion_checks=checks,
        blocking_issues=list(dict.fromkeys([*report.blocking_issues, *result.issues])),
        revision_instructions=[],
        missing_information=report.missing_information,
        temperament_decisions=report.temperament_decisions,
    )
