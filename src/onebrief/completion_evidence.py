"""Deterministic proof-of-use checks for user-facing development work."""

from __future__ import annotations

import json
import re
from enum import StrEnum

from pydantic import BaseModel, Field, ValidationError

from onebrief.delivery_intent import requires_new_product_construction
from onebrief.execution_schemas import CriterionCheck, VerificationReport, Verdict
from onebrief.handoff_protocol import (
    EvidenceBinding,
    EvidenceKind,
    EvidenceStatus,
    create_evidence_binding,
)
from onebrief.schemas import IntakeRequest, OutputTarget, RequirementsAnalysis


class CompletionEvidenceKind(StrEnum):
    AUTOMATED_TEST = "automated_test"
    RUNTIME_INTERACTION = "runtime_interaction"
    VISUAL_INTEGRITY = "visual_integrity"
    PRODUCT_CONSTRUCTION = "product_construction"


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


def new_product_construction_paths(change_set: object | None) -> set[str]:
    """Return normalized production surfaces introduced by a change set."""

    if isinstance(change_set, BaseModel):
        change_set = change_set.model_dump(mode="python")
    if not isinstance(change_set, dict):
        return set()
    changes = change_set.get("changes")
    if not isinstance(changes, list):
        return set()
    paths: set[str] = set()
    direct_surface_suffixes = {".unity", ".prefab", ".uxml", ".uss"}
    client_source_markers = (
        "/client/", "client", "/ui/", "ui", "screen", "surface",
        "presentation", "view", "login", "lobby", "settings",
    )
    for change in changes:
        if not isinstance(change, dict):
            continue
        path = str(change.get("path", "")).replace("\\", "/")
        lowered = f"/{path.casefold()}"
        if not path or "/tests/" in lowered or "/test/" in lowered:
            continue
        if any(marker in lowered for marker in (
            "/evidence/", "/screenshots/", "/observations/", "/_patch_notes/",
        )):
            continue
        suffix = "." + path.rsplit(".", 1)[-1].casefold() if "." in path else ""
        is_new = change.get("base_sha256") is None
        content = str(change.get("content") or "")
        is_trusted_incremental_client = (
            change.get("base_sha256") is not None
            and "KHALINOS_DECLARATIVE_CLIENT_V1" in content
        )
        is_surface = suffix in direct_surface_suffixes
        is_client_source = suffix == ".cs" and any(
            marker in lowered for marker in client_source_markers
        )
        if (is_new or is_trusted_incremental_client) and (is_surface or is_client_source):
            paths.add(path.replace("\\", "/").casefold())
    return paths


def has_new_product_construction(change_set: object | None) -> bool:
    """Distinguish a new production surface from tests and legacy-file tweaks."""

    return bool(new_product_construction_paths(change_set))


def repairs_quest_product_construction(
    change_set: object | None,
    construction_lineage: set[str],
) -> bool:
    """Keep repair authority for surfaces created earlier in the same Quest."""

    proposed = {
        str(getattr(item, "path", item.get("path", "") if isinstance(item, dict) else ""))
        .replace("\\", "/")
        .casefold()
        for item in (
            change_set.get("changes", [])
            if isinstance(change_set, dict)
            else getattr(change_set, "changes", [])
        )
    }
    return bool(proposed & construction_lineage)


def _has_new_product_construction(
    development_evidence: dict[str, object] | None,
) -> bool:
    if not development_evidence:
        return False
    return has_new_product_construction(development_evidence.get("change_set"))


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
    requires_construction = requires_new_product_construction(text)

    required: list[CompletionEvidenceKind] = []
    if development_evidence is not None:
        required.append(CompletionEvidenceKind.AUTOMATED_TEST)
    if is_ui:
        required.append(CompletionEvidenceKind.RUNTIME_INTERACTION)
    if is_ui and (is_unity or is_localization):
        required.append(CompletionEvidenceKind.VISUAL_INTEGRITY)
    if requires_construction:
        required.append(CompletionEvidenceKind.PRODUCT_CONSTRUCTION)

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
        CompletionEvidenceKind.PRODUCT_CONSTRUCTION: _has_new_product_construction(
            development_evidence
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
        CompletionEvidenceKind.PRODUCT_CONSTRUCTION: (
            "The approved outcome requires a newly constructed product surface, but the changed-file "
            "manifest contains no new production scene, prefab, UI source, or client source. Test-only "
            "changes and minor edits to legacy files are baseline or maintenance evidence, not construction."
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

    runtime_scenarios: list[dict[str, object]] = []
    raw_runtime_evidence = development_evidence.get("runtime_evidence")
    if isinstance(raw_runtime_evidence, list):
        for item in raw_runtime_evidence:
            if not isinstance(item, dict):
                continue
            path = str(item.get("path", "")).casefold()
            content = item.get("content")
            if not path.endswith("runtime-evidence.json") or not isinstance(content, str):
                continue
            try:
                payload = json.loads(content)
            except json.JSONDecodeError:
                continue
            scenarios = payload.get("scenarios") if isinstance(payload, dict) else None
            if isinstance(scenarios, list):
                runtime_scenarios.extend(
                    scenario for scenario in scenarios if isinstance(scenario, dict)
                )

    def behavior_contract_gaps(text: str) -> list[str]:
        """Require named control effects in the executed journey, not any PlayMode pass."""

        normalized = " ".join(text.casefold().split())
        interactions = [
            str(scenario.get("interaction", "")).casefold()
            for scenario in runtime_scenarios
        ]
        trace = " -> ".join(interactions)
        gaps: list[str] = []
        asks_language = any(marker in normalized for marker in (
            "language", "locale", "localization", "translation",
        ))
        asks_volume = any(marker in normalized for marker in (
            "volume", "audio", "bgm", "sfx",
        ))
        if asks_language and not (
            "select_dropdown:" in trace
            and "language" in trace
            and "assert_player_pref_string:" in trace
        ):
            gaps.append(
                "the executed journey must select the shipped language control and assert its persisted client state"
            )
        if asks_volume:
            slider_traces = re.findall(r"set_slider:([^\s>]+)", trace)
            preference_traces = re.findall(
                r"assert_player_pref_float:([^\s>]+)", trace
            )
            required_count = 2 if "bgm" in normalized and "sfx" in normalized else 1
            if (
                len(set(slider_traces)) < required_count
                or len(set(preference_traces)) < required_count
            ):
                gaps.append(
                    "the executed journey must operate each required volume slider and assert the corresponding persisted client state"
                )
        asks_lobby_data = "lobby" in normalized and "data" in normalized
        if asks_lobby_data and not any(
            "assert_text_not_equals:newclientlobbydata" in interaction
            for interaction in interactions
        ):
            gaps.append(
                "the executed journey must reject placeholder Lobby text and prove non-empty runtime data"
            )
        asks_lobby_navigation = "lobby" in normalized and any(
            marker in normalized for marker in ("navigation", "transition", "destination")
        )
        if asks_lobby_navigation and not (
            any("click:newclientlobbynavigationbutton" in interaction for interaction in interactions)
            and any("wait_for_scene:" in interaction for interaction in interactions)
        ):
            gaps.append(
                "the executed journey must click the new Lobby navigation control and reach its approved destination scene"
            )
        asks_settings_return = (
            "settings" in normalized
            and any(marker in normalized for marker in (
                "back to lobby", "return to lobby", "settings -> lobby",
                "settings to lobby",
            ))
        )
        if asks_settings_return:
            settings_index = next((
                index for index, scenario in enumerate(runtime_scenarios)
                if "settings" in str(scenario.get("scenario_id", "")).casefold()
            ), None)
            returned = bool(
                settings_index is not None
                and any(
                    "lobby" in str(scenario.get("scenario_id", "")).casefold()
                    and any(marker in str(scenario.get("interaction", "")).casefold() for marker in (
                        "click:close", "click:back", "click:settings",
                    ))
                    for scenario in runtime_scenarios[settings_index + 1:]
                )
            )
            if not returned:
                gaps.append(
                    "the executed journey must close Settings through a shipped control and capture the returned Lobby state"
                )
        return gaps

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

    trusted_bindings: dict[EvidenceKind, EvidenceBinding] = {}
    raw_receipt = development_evidence.get("verification_receipt")
    if isinstance(raw_receipt, dict):
        raw_bindings = raw_receipt.get("evidence_bindings")
        if isinstance(raw_bindings, list):
            for raw_binding in raw_bindings:
                try:
                    binding = EvidenceBinding.model_validate(raw_binding)
                except (TypeError, ValidationError):
                    continue
                if binding.status == EvidenceStatus.PASSED:
                    trusted_bindings.setdefault(binding.kind, binding)

    replacements: dict[str, CriterionCheck] = {}
    deterministic_gaps: list[str] = []
    used_binding_ids: set[str] = set()
    for criterion in contract.quality_criteria:
        criterion_text = f"{criterion.description} {criterion.evidence_required}"
        kind = criterion_kind(criterion_text)
        command_id = passed_commands.get(kind) if kind is not None else None
        if command_id is None:
            continue
        gaps = behavior_contract_gaps(criterion_text) if kind == EvidenceKind.BEHAVIOR else []
        if gaps:
            message = "; ".join(gaps) + ". A generic PlayMode PASS is insufficient."
            deterministic_gaps.append(
                f"{criterion.criterion_id} ({criterion.description}): {message}"
            )
            replacements[criterion.criterion_id] = CriterionCheck(
                criterion_id=criterion.criterion_id,
                criterion=criterion.description,
                passed=False,
                evidence=message,
                evidence_bindings=[],
            )
            continue
        trusted = trusted_bindings.get(kind)
        binding = (
            trusted.model_copy(update={"criterion_id": criterion.criterion_id})
            if trusted is not None
            else create_evidence_binding(
                criterion_id=criterion.criterion_id,
                kind=kind,
                status=EvidenceStatus.PASSED,
                summary=f"Trusted adapter {command_id} completed with exit code 0.",
                command_id=command_id,
            )
        )
        used_binding_ids.add(binding.binding_id)
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
    for kind, binding in trusted_bindings.items():
        if binding.binding_id in used_binding_ids:
            continue
        checks.append(CriterionCheck(
            criterion=f"Trusted development evidence: {kind.value}",
            passed=True,
            evidence=binding.summary,
            evidence_bindings=[binding],
        ))
    update: dict[str, object] = {"criterion_checks": checks}
    if deterministic_gaps:
        update.update({
            "verdict": Verdict.REVISE,
            "blocking_issues": list(dict.fromkeys([
                *model_report.blocking_issues,
                *deterministic_gaps,
            ])),
            "revision_instructions": list(dict.fromkeys([
                *model_report.revision_instructions,
                "Update the declarative runtime journey to execute and assert every named control effect before recapturing evidence.",
            ])),
        })
    return model_report.model_copy(update=update)
