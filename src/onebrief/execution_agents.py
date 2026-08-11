"""Four specialized agents whose every call uses the budgeted gateway."""

from __future__ import annotations

import json
import hashlib
import re
from typing import Any, Callable, Protocol, TypeVar

from pydantic import BaseModel, ValidationError
from onebrief.development_toolpack import CodeChangeSet, approved_edit_path
from onebrief.generic_development_toolpack import (
    CompactProposedProjectCodeChangeSet,
    ProjectCodeChangeSet,
    ProposedProjectCodeChangeSet,
)

from onebrief.execution_limits import (
    ANALYST_OUTPUT_CAP,
    DEVELOPER_OUTPUT_CAP,
    REVISION_OUTPUT_CAP,
    VERIFIER_OUTPUT_CAP,
    WRITER_OUTPUT_CAP,
)
from onebrief.recovery_policy import RecoveryAction, RecoveryDecision, RecoveryPolicy
from onebrief.skill_packs import skill_instruction
from onebrief.execution_schemas import AnalysisPackage, DraftArtifact, RevisionArtifact, VerificationReport

from onebrief.temperament import (
    ANALYST_PROFILE,
    REVISION_PROFILE,
    VERIFIER_PROFILE,
    WRITER_PROFILE,
    enforce_temperament_audit,
)

T = TypeVar("T", bound=BaseModel)


class StructuredGateway(Protocol):
    def generate_json(
        self,
        *,
        stage: str,
        model: str,
        contents: str,
        schema: type[T],
        max_output_tokens: int,
        system_instruction: str,
        temperature: float = 0.1,
    ) -> T: ...


def _json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)


class AnalystAgent:
    stage = "evidence_analysis"

    def __init__(self, gateway: StructuredGateway, model: str = "gemini-3.5-flash", skill_ids: list[str] | None = None):
        self.gateway = gateway
        self.model = model
        self.skill_context = skill_instruction(skill_ids or [])

    def run(self, contract: dict[str, Any], sources: list[dict[str, Any]]) -> AnalysisPackage:
        result = self.gateway.generate_json(
            stage=self.stage,
            model=self.model,
            contents=_json({"work_contract": contract, "authoritative_sources": sources}),
            schema=AnalysisPackage,
            max_output_tokens=ANALYST_OUTPUT_CAP,
            system_instruction=(
                "You are OneBrief's evidence analyst. Use only supplied authoritative sources. "
                "Extract decision-relevant findings, assign "
                "stable F01-style IDs, preserve conflicts, and design a structure for the requested "
                "deliverable. Do not draft the final artifact or invent missing facts. Write in the "
                "goal's language and return only the required structured object."
                + " "
                + ANALYST_PROFILE.instruction()
                + (("\n\n" + self.skill_context) if self.skill_context else "")
            ),
        )
        return enforce_temperament_audit(result, ANALYST_PROFILE)


class WriterAgent:
    stage = "long_form_draft"

    def __init__(self, gateway: StructuredGateway, model: str = "gemini-3.5-flash", skill_ids: list[str] | None = None):
        self.gateway = gateway
        self.model = model
        self.skill_context = skill_instruction(skill_ids or [])

    def run(
        self,
        contract: dict[str, Any],
        analysis: AnalysisPackage,
        sources: list[dict[str, Any]] | None = None,
        verification_feedback: VerificationReport | None = None,
        previous_draft: DraftArtifact | None = None,
        round_number: int = 0,
    ) -> DraftArtifact:
        result = self.gateway.generate_json(
            stage=(self.stage if not verification_feedback else f"{self.stage}_revision_r{round_number}"),
            model=self.model,
            contents=_json(
                {
                    "work_contract": contract,
                    "analysis_package": analysis.model_dump(mode="json"),
                    "authoritative_sources": sources or [],
                    "previous_draft": previous_draft.model_dump(mode="json") if previous_draft else None,
                    "verification_feedback": (
                        verification_feedback.model_dump(mode="json") if verification_feedback else None
                    ),
                }
            ),
            schema=DraftArtifact,
            max_output_tokens=WRITER_OUTPUT_CAP,
            system_instruction=(
                "You are OneBrief's artifact maker and the owner of this output. Draft the complete requested "
                "artifact from the analysis package. If previous_draft and verification_feedback are supplied, "
                "revise your own work, address every blocking issue, and preserve every passing part. Cover every "
                "requested item, but keep wording concise enough for the output cap. If the requested output is "
                "Excel or a candidate list, include one clean Markdown "
                "table with one candidate per row so it can be exported deterministically. Every material "
                "claim must be traceable to cited finding IDs. Never change the "
                "goal, invent a source, or make a high-impact decision for a human. Write in the goal's "
                "language and return only the required structured object."
                + " "
                + WRITER_PROFILE.instruction()
                + (("\n\n" + self.skill_context) if self.skill_context else "")
            ),
        )
        return enforce_temperament_audit(result, WRITER_PROFILE)


class DeveloperAgent:
    """Produces bounded full-file edits for the isolated development ToolPack."""

    stage = "long_form_draft"

    def __init__(
        self,
        gateway: StructuredGateway,
        model: str = "gemini-3.5-flash",
        skill_ids: list[str] | None = None,
        *,
        change_set_schema: type[BaseModel] = CodeChangeSet,
        source_prefix: str = "exchange-source/",
        path_approver: Callable[[str], str | None] = approved_edit_path,
    ):
        self.gateway = gateway
        self.model = model
        self.change_set_schema = change_set_schema
        self.source_prefix = source_prefix
        self.path_approver = path_approver
        self.skill_context = skill_instruction(skill_ids or [])
        self.recovery_policy = RecoveryPolicy()
        self.last_recovery_decisions: list[RecoveryDecision] = []

    def promote_candidate(
        self,
        raw: object,
        approved_sources: list[dict[str, Any]] | None = None,
        previous_change_set: BaseModel | None = None,
        approved_anchor_catalog: list[dict[str, object]] | None = None,
    ) -> BaseModel:
        """Promote an untrusted proposal through the exact approved path boundary."""
        if self.change_set_schema is ProjectCodeChangeSet:
            proposed = ProposedProjectCodeChangeSet.model_validate(
                raw.model_dump(mode="json") if isinstance(raw, BaseModel) else raw
            )
            source_map = {
                str(item.get("repository_path")): item
                for item in (approved_sources or [])
                if item.get("repository_path") and isinstance(item.get("content"), str)
            }
            previous_map = {
                str(getattr(item, "path", "")): str(getattr(item, "content", ""))
                for item in getattr(previous_change_set, "changes", [])
            }
            catalog = {
                (str(group.get("path", "")), str(anchor.get("anchor_id", ""))): str(
                    anchor.get("text", "")
                )
                for group in (approved_anchor_catalog or [])
                for anchor in group.get("anchors", [])
                if isinstance(anchor, dict) and anchor.get("anchor_id")
            }
            # Compose several bounded edits for one file into one final change.
            # The trusted schema still receives exactly one row per path.
            changes_by_path: dict[str, dict[str, Any]] = {}
            for item in proposed.changes:
                change = item.model_dump(mode="json")
                path = str(change.get("path", ""))
                if self.path_approver(path) is None:
                    continue
                pending = changes_by_path.get(path.casefold())
                if pending is not None and change.get("content") is not None:
                    raise ValueError(
                        f"duplicate full-file proposals are ambiguous: {path}"
                    )
                if change.get("content") is None:
                    needle = str(change.get("search") or "")
                    approved_baseline = str(source_map.get(path, {}).get("content", ""))
                    previous_baseline = previous_map.get(path, "")
                    pending_baseline = str(pending.get("content", "")) if pending else ""
                    baseline = ""
                    match: tuple[int, int] | None = None
                    anchor_id = str(change.get("anchor_id") or "")
                    if anchor_id:
                        needle = catalog.get((path, anchor_id), "")
                        if not needle:
                            raise ValueError(
                                f"catalog anchor is not approved for path: {path}#{anchor_id}"
                            )
                    if needle:
                        for candidate in (
                            pending_baseline,
                            previous_baseline,
                            approved_baseline,
                        ):
                            if candidate.count(needle) == 1:
                                start = candidate.index(needle)
                                baseline = candidate
                                match = (start, start + len(needle))
                                break
                    if match is None:
                        # Preserve the exact non-whitespace token sequence while
                        # tolerating a model's formatting-only JSX reflow. The
                        # edit remains valid only when one approved baseline has
                        # exactly one complete match.
                        chunks = re.findall(r"\S+", needle)
                        if chunks:
                            pattern = re.compile(r"\s+".join(re.escape(chunk) for chunk in chunks))
                            for candidate in (
                                pending_baseline,
                                previous_baseline,
                                approved_baseline,
                            ):
                                matches = list(pattern.finditer(candidate))
                                if len(matches) == 1:
                                    baseline = candidate
                                    match = matches[0].span()
                                    break
                    if match is None and change.get("start_anchor") and change.get("end_anchor"):
                        start_anchor = str(change["start_anchor"])
                        end_anchor = str(change["end_anchor"])
                        for candidate in (
                            pending_baseline,
                            previous_baseline,
                            approved_baseline,
                        ):
                            starts = [m.start() for m in re.finditer(re.escape(start_anchor), candidate)]
                            ends = [m.end() for m in re.finditer(re.escape(end_anchor), candidate)]
                            spans = [(start, end) for start in starts for end in ends if end >= start]
                            if len(spans) == 1:
                                baseline = candidate
                                match = spans[0]
                                break
                    if match is None:
                        raise ValueError(
                            f"edit anchors could not rediscover one approved source range: {path}"
                        )
                    start, end = match
                    change["content"] = (
                        baseline[:start]
                        + str(change.get("replace") or "")
                        + baseline[end:]
                    )
                    if change.get("base_sha256") is None and path in source_map:
                        change["base_sha256"] = source_map[path].get("sha256")
                    if pending is not None and pending.get("base_sha256") is not None:
                        change["base_sha256"] = pending["base_sha256"]
                change.pop("search", None)
                change.pop("replace", None)
                change.pop("start_anchor", None)
                change.pop("end_anchor", None)
                change.pop("anchor_id", None)
                changes_by_path[path.casefold()] = change
            proposal = proposed.model_dump(mode="json")
            proposal["changes"] = list(changes_by_path.values())
            return self.change_set_schema.model_validate(proposal)
        return self.change_set_schema.model_validate(raw)

    @staticmethod
    def exact_edit_anchors(
        previous_change_set: BaseModel | None,
        verification_feedback: str | None,
    ) -> list[dict[str, object]]:
        """Extract bounded verbatim source windows for a repair agent."""
        if not verification_feedback or previous_change_set is None:
            return []
        feedback_terms = {
            term.casefold()
            for term in re.findall(r"[A-Za-z가-힣]{4,}", verification_feedback)
        }
        language_failure = bool(
            re.search(
                r"cjk|language|locale|translation|한글|한국어|영어|번역|언어",
                verification_feedback,
                re.IGNORECASE,
            )
        )
        exact_edit_anchors: list[dict[str, object]] = []
        for change in getattr(previous_change_set, "changes", []):
            content = str(getattr(change, "content", ""))
            lines = content.splitlines()
            selected: list[int] = []
            for index, line in enumerate(lines):
                folded = line.casefold()
                if any(term in folded for term in feedback_terms) or (
                    language_failure and re.search(r"[가-힣一-龥ぁ-んァ-ン]", line)
                ):
                    selected.append(index)
            anchors = []
            used: set[tuple[int, int]] = set()
            for index in selected:
                start = max(0, index - 1)
                end = min(len(lines), index + 2)
                span = (start, end)
                if span in used:
                    continue
                used.add(span)
                anchors.append({
                    "anchor_id": "A" + hashlib.sha256(
                        (str(getattr(change, "path", "")) + "\0" + "\n".join(lines[start:end])).encode("utf-8")
                    ).hexdigest()[:12],
                    "start_line": start + 1,
                    "end_line": end,
                    "text": "\n".join(lines[start:end]),
                })
                if len(anchors) >= 24:
                    break
            if anchors:
                exact_edit_anchors.append({
                    "path": str(getattr(change, "path", "")),
                    "anchors": anchors,
                })
        return exact_edit_anchors

    def run(
        self,
        contract: dict[str, Any],
        analysis: AnalysisPackage,
        sources: list[dict[str, Any]],
        verification_feedback: str | None = None,
        previous_change_set: BaseModel | None = None,
    ) -> BaseModel:
        self.last_recovery_decisions = []
        developer_sources = []
        for source in sources:
            prepared = dict(source)
            name = str(prepared.get("name", ""))
            candidate = name.removeprefix(self.source_prefix) if name.startswith(self.source_prefix) else ""
            repository_path = self.path_approver(candidate) if candidate else None
            prepared["repository_path"] = repository_path
            normalized_name = name.casefold().replace("\\", "/")
            if "/tests/" in normalized_name or normalized_name.startswith(f"{self.source_prefix}tests/"):
                prepared["source_role"] = "immutable_acceptance_contract"
            elif repository_path is not None:
                prepared["source_role"] = "editable_source"
            else:
                prepared["source_role"] = "read_only_context"
            developer_sources.append(prepared)
        exact_edit_anchors = self.exact_edit_anchors(
            previous_change_set, verification_feedback
        )
        contents = _json({
            "work_contract": contract,
            "analysis_package": analysis.model_dump(mode="json"),
            "approved_repository_files": developer_sources,
            "previous_change_set": (
                previous_change_set.model_dump(mode="json") if previous_change_set else None
            ),
            "verification_feedback": verification_feedback,
            "exact_edit_anchors": exact_edit_anchors,
        })
        base_instruction = (
            "You are OneBrief's software maker. Return the smallest complete source changes that "
            "implement the requested executable artifact. For an existing file, path must exactly equal its "
            f"repository_path; never copy the provenance name beginning with {self.source_prefix}. Only edit files "
            "with a non-null repository_path in approved_repository_files or add a necessary text source file "
            "under the same project. For an existing file, copy its exact "
            "sha256 into base_sha256. For a small existing-file edit, prefer one exact search/replace pair "
            "instead of complete content; search must be copied verbatim and occur exactly once. Never invent, "
            "summarize, or elide exact search text. When a repair changes multiple distant regions of one file "
            "(for example a new interaction plus all affected visible copy), return that file's complete content "
            "instead of a fragile search/replace edit. For a new file use null and complete content. "
            "Never touch secrets, dependencies, generated data, Git metadata, deployment, accounts, or trading. "
            "Files marked immutable_acceptance_contract are binding regression contracts: do not edit them and "
            "preserve every behavior, marker, control, and data contract they assert. Prefer additive, localized "
            "changes over redesigning or replacing a mature implementation. If verification_feedback is present, "
            "correct every reported failure while retaining all previously passing behavior. Do not return a report "
            "When exact_edit_anchors contains a matching verified source window, prefer its anchor_id and return "
            "the complete replacement for that displayed window; never invent or retype the anchor text. "
            "in place of runnable source code. Keep existing correct behavior, make no unsupported financial claim, "
            "and stay within the acceptance criteria. For Unity visual or localization work, include a real PlayMode "
            "test whose full name begins with OneBrief.Visual. The test must perform the requested runtime interaction, "
            "and must be placed in a discoverable Unity test assembly. When the project does not already expose one, "
            "add a matching .asmdef whose optionalUnityReferences contains TestAssemblies. The test source and its "
            ".asmdef must live under a dedicated Assets/.../Tests/PlayMode/ directory. Never put a test .asmdef in "
            "a production Scripts or Localization directory because it would move production scripts into the test assembly. "
            "Unity test assemblies cannot directly reference types compiled into the predefined Assembly-CSharp; "
            "when production scripts have no asmdef, do not name those production types in test code; load the real "
            "project scene and interact through discovered scene objects, public UI controls, or reflection. The test "
            "must find visible UI objects from that loaded scene and must not construct a fake GameObject/TMP canvas. "
            "Use valid C# interpolated strings beginning with $\" (never JavaScript-style ${). "
            "capture a PNG under onebrief-evidence/, and write onebrief-evidence/runtime-evidence.json with schema_version "
            "Unity batchmode does not reliably support ScreenCapture.CaptureScreenshot or yielding WaitForEndOfFrame. Do not "
            "use either. Capture deterministically by rendering the real scene camera and UI Canvas to a RenderTexture, "
            "calling Texture2D.ReadPixels, EncodeToPNG, and File.WriteAllBytes synchronously; restore modified camera/canvas "
            "state afterwards. A camera RenderTexture does not include ScreenSpaceOverlay canvases: temporarily route every "
            "active overlay Canvas that contains the measured UI through that camera (ScreenSpaceCamera, worldCamera and an "
            "appropriate planeDistance), force Canvas/TMP layout updates, render, then restore every original canvas setting. "
            "The resulting PNG must visibly contain the measured UI, and every locale screenshot must have different image "
            "bytes; a blank, background-only, or duplicated capture is invalid. Never write the JSON manifest before every "
            "referenced PNG has been durably created. "
            "onebrief-unity-visual-evidence-v1. Each scenario must contain scenario_id, expected_locale, observed_locale, "
            "changed_visible_text_count, missing_glyph_count, and an evidence-relative screenshot_path. Derive "
            "observed_locale, changed text count, and missing glyph count from the running UI; do not hard-code a passing "
            "claim. For every locale scenario, first switch the real UI to a deliberately different supported reference "
            "locale and capture that scenario's own baseline, then operate the dropdown to select the target locale and "
            "measure the target UI against that baseline. Never reuse one startup snapshot for all locales: the startup "
            "locale may equal the first target and falsely report zero visible changes. The ToolPack independently rejects "
            "missing tests, missing or invalid PNG files, duplicated screenshots, unchanged text, "
            "locale mismatches, missing glyphs, and requested locales that were not exercised. "
            "Keep the combined replacement "
            "content below 60000 UTF-8 bytes. Return only the schema."
        )
        approved_existing_paths = {
            str(item["repository_path"])
            for item in developer_sources
            if item.get("repository_path")
        }
        previous_new_paths = {
            str(getattr(item, "path", ""))
            for item in getattr(previous_change_set, "changes", [])
            if getattr(item, "base_sha256", None) is None
        }
        approved_new_roots = sorted({
            path.split("/", 1)[0] + "/"
            for path in approved_existing_paths
            if "/" in path
        })
        base_instruction += (
            " New files are allowed only below these approved source roots: "
            + ", ".join(approved_new_roots)
            + ". Fixed package, build, server, runner, and ToolPack infrastructure may appear in the "
            "completion contract because it must be executed, but it is already supplied and read-only: "
            "never regenerate or modify package.json, scripts/, build configuration, server code, or command "
            "runners. Product tests may inspect the product artifact but must not spawn processes, execute shell "
            "commands, access environment variables, or implement a server. The fixed validation adapter owns "
            "build, process startup, HTTP checks, and command execution."
        )
        stage_base = (
            f"{self.stage}_verification_retry" if verification_feedback else self.stage
        )
        result = None
        last_contract_error = ""
        for attempt in range(2):
            instruction = base_instruction
            if attempt:
                instruction += (
                    " The previous response failed structured-output or repository-path validation. Retry from "
                    "scratch with at most three changed files. Every existing-file path must exactly equal the "
                    f"repository_path field and must not include {self.source_prefix}. For every existing file, "
                    "use a minimal exact search/replace edit copied verbatim from the approved source. If the exact "
                    "text is no longer uniquely discoverable, use start_anchor and end_anchor copied verbatim from "
                    "the current approved file and replace only that inclusive structural range. Do not return "
                    "full-file content on this compact retry. Full content is allowed only for a necessary new file "
                    "smaller than 8000 characters. Prefer the smallest existing source files, remove comments and "
                    "repetition, and keep the complete JSON response below 12000 characters while preserving a "
                    "runnable implementation."
                )
                if last_contract_error:
                    instruction += " Exact validation failure: " + last_contract_error
            try:
                provider_schema = (
                    (
                        CompactProposedProjectCodeChangeSet
                        if attempt
                        else ProposedProjectCodeChangeSet
                    )
                    if self.change_set_schema is ProjectCodeChangeSet
                    else self.change_set_schema
                )
                candidate = self.gateway.generate_json(
                    stage=stage_base if not attempt else f"{stage_base}_compact_retry",
                    model=self.model,
                    contents=contents,
                    schema=provider_schema,
                    max_output_tokens=DEVELOPER_OUTPUT_CAP,
                    system_instruction=instruction + (("\n\n" + self.skill_context) if self.skill_context else ""),
                    temperature=0.1,
                )
                candidate = self.promote_candidate(
                    candidate, developer_sources, previous_change_set, exact_edit_anchors
                )
                normalized_changes = []
                for change in getattr(candidate, "changes", []):
                    path = str(getattr(change, "path", ""))
                    base_sha256 = getattr(change, "base_sha256", None)
                    if path in previous_new_paths and base_sha256 is not None:
                        # A retry may see its own prior overlay and mistake it for a
                        # repository file. Its provenance is still "new relative to
                        # approved HEAD", so preserve the trusted null base hash.
                        change = change.model_copy(update={"base_sha256": None})
                        base_sha256 = None
                    if base_sha256 is not None and path not in approved_existing_paths:
                        raise ValueError(
                            "existing file is absent from approved_repository_files and cannot be replaced: "
                            f"{path}. Use only a listed repository_path, or add a small new sidecar/partial "
                            "source file with base_sha256 null."
                        )
                    normalized_changes.append(change)
                if normalized_changes != list(getattr(candidate, "changes", [])):
                    candidate = candidate.model_copy(update={"changes": normalized_changes})
                result = candidate
                break
            except (ValidationError, ValueError) as exc:
                last_contract_error = " ".join(str(exc).split())[:1000]
                decision = self.recovery_policy.decide(
                    exc, context="developer_structured_output", attempt_number=attempt + 1
                )
                self.last_recovery_decisions.append(decision)
                if decision.action != RecoveryAction.AUTO_RETRY or not decision.retry_allowed:
                    raise
        if not isinstance(result, self.change_set_schema):
            raise TypeError("developer returned an invalid code change set")
        return result

class VerifierAgent:
    stage = "independent_verification"

    def __init__(self, gateway: StructuredGateway, model: str = "gemini-3.5-flash", skill_ids: list[str] | None = None):
        self.gateway = gateway
        self.model = model
        self.skill_context = skill_instruction(skill_ids or [])

    def run(
        self,
        contract: dict[str, Any],
        analysis: AnalysisPackage,
        draft: DraftArtifact,
        round_number: int,
        sources: list[dict[str, Any]] | None = None,
        implementation_evidence: dict[str, Any] | None = None,
    ) -> VerificationReport:
        result = self.gateway.generate_json(
            stage=f"{self.stage}_r{round_number}",
            model=self.model,
            contents=_json(
                {
                    "work_contract": contract,
                    "analysis_package": analysis.model_dump(mode="json"),
                    "draft": draft.model_dump(mode="json"),
                    "authoritative_sources": sources or [],
                    "implementation_evidence": implementation_evidence,
                }
            ),
            schema=VerificationReport,
            max_output_tokens=VERIFIER_OUTPUT_CAP,
            system_instruction=(
                "You are OneBrief's independent verifier. You did not write the draft. Test every "
                "acceptance criterion, factual "
                "grounding, citation coverage, internal consistency, completeness, and human-authority "
                "boundary. When implementation_evidence is supplied, inspect the actual changed source and test evidence rather than trusting the maker summary; map every claimed feature and acceptance criterion to code and a relevant test. PASS only when no blocking issue remains. Use REVISE for correctable issues "
                "and NEEDS_INFORMATION only when supplied evidence cannot support a required conclusion. "
                "Give exact revision instructions. For every completion_contract quality criterion, "
                "return exactly one criterion_check and copy its Q-prefixed criterion_id into "
                "criterion_id. Keep system or safety checks separate and leave criterion_id null for them. "
                "Write every user-facing field in the goal's language. "
                "Return only the structured object."
                + " "
                + VERIFIER_PROFILE.instruction()
                + (("\n\n" + self.skill_context) if self.skill_context else "")
            ),
        )
        return enforce_temperament_audit(result, VERIFIER_PROFILE)


class RevisionAgent:
    stage = "revision"

    def __init__(self, gateway: StructuredGateway, model: str = "gemini-3.5-flash", skill_ids: list[str] | None = None):
        self.gateway = gateway
        self.model = model
        self.skill_context = skill_instruction(skill_ids or [])

    def run(
        self,
        contract: dict[str, Any],
        analysis: AnalysisPackage,
        draft: DraftArtifact,
        report: VerificationReport,
        round_number: int,
        sources: list[dict[str, Any]] | None = None,
    ) -> RevisionArtifact:
        result = self.gateway.generate_json(
            stage=f"{self.stage}_r{round_number}",
            model=self.model,
            contents=_json(
                {
                    "work_contract": contract,
                    "analysis_package": analysis.model_dump(mode="json"),
                    "current_draft": draft.model_dump(mode="json"),
                    "verification_report": report.model_dump(mode="json"),
                    "authoritative_sources": sources or [],
                }
            ),
            schema=RevisionArtifact,
            max_output_tokens=REVISION_OUTPUT_CAP,
            system_instruction=(
                "You are OneBrief's revision specialist. "
                "Apply every blocking revision instruction while preserving correct grounded content. "
                "Do not hide unresolved issues or broaden scope. Keep material claims linked to existing "
                "finding IDs. Write in the goal's language and return only the structured object."
                + " "
                + REVISION_PROFILE.instruction()
                + (("\n\n" + self.skill_context) if self.skill_context else "")
            ),
        )
        return enforce_temperament_audit(result, REVISION_PROFILE)
