"""Hypothesis-driven convergence policy for bounded repair loops.

Duplicate blocking is only negative memory: it says what not to repeat.  This
module turns trusted failures into a small causal record, an explicit repair
contract, and a progress gate.  ToolPacks still own concrete diagnostics and
verification; the policy only decides whether another expensive maker turn is
justified by new evidence.
"""

from __future__ import annotations

import hashlib
import json
import re
from enum import StrEnum

from pydantic import BaseModel, Field

from onebrief.handoff_protocol import FailureCode, FailureOwner


CURRENT_CONVERGENCE_POLICY_REVISION = (
    "onebrief-convergence-2026-08-14-single-site-manifest-v5"
)
LEGACY_CONVERGENCE_POLICY_REVISION = "onebrief-convergence-legacy-v1"


class FailureLayer(StrEnum):
    STRUCTURED_OUTPUT = "structured_output"
    SOURCE_BINDING = "source_binding"
    BUILD = "build"
    RUNTIME = "runtime"
    EVIDENCE_RUNTIME = "evidence_runtime"
    EVIDENCE_TOPOLOGY = "evidence_topology"
    EVIDENCE_INTEGRITY = "evidence_integrity"
    SEMANTIC_PRODUCT = "semantic_product"
    AUTHORITY = "authority"
    PROVIDER = "provider"
    UNKNOWN = "unknown"


class ProgressKind(StrEnum):
    FIRST_OBSERVATION = "first_observation"
    NEW_HYPOTHESIS = "new_hypothesis"
    NARROWED_FAILURE = "narrowed_failure"
    CRITERION_ADVANCE = "criterion_advance"
    NO_PROGRESS = "no_progress"
    REGRESSION = "regression"


class FailureObservationV2(BaseModel):
    """A typed causal failure that no downstream agent has to re-parse."""

    schema_version: str = "onebrief-failure-observation-v2"
    observation_id: str = Field(pattern=r"^FO-[a-f0-9]{16}$")
    context: str
    code: FailureCode = FailureCode.UNKNOWN
    layer: FailureLayer
    owner: FailureOwner = FailureOwner.PRODUCT
    normalized_signature: str = Field(min_length=3, max_length=1200)
    evidence_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    affected_paths: list[str] = Field(default_factory=list, max_length=16)
    failed_criterion_ids: list[str] = Field(default_factory=list, max_length=16)
    symptom_keys: list[str] = Field(default_factory=list, max_length=32)
    strategy_fingerprint: str | None = None
    attempt_number: int = Field(ge=1)


# Keep the public name used by older modules and stored-ledger tests while all
# newly authored observations use the v2 schema and typed ownership fields.
FailureObservation = FailureObservationV2


class RepairHypothesis(BaseModel):
    hypothesis_id: str = Field(pattern=r"^RH-[a-f0-9]{16}$")
    suspected_cause: str = Field(min_length=3, max_length=800)
    cheapest_probe: str = Field(min_length=3, max_length=800)
    expected_signal: str = Field(min_length=3, max_length=800)
    repair_boundary: str = Field(min_length=3, max_length=800)
    requires_model_reasoning: bool = False


class RepairContract(BaseModel):
    schema_version: str = "onebrief-repair-contract-v1"
    contract_id: str = Field(pattern=r"^RC-[a-f0-9]{16}$")
    observation_id: str = Field(pattern=r"^FO-[a-f0-9]{16}$")
    progress_kind: ProgressKind
    occurrence: int = Field(ge=1)
    hypothesis: RepairHypothesis
    permitted_paths: list[str] = Field(default_factory=list, max_length=16)
    preserve_criterion_ids: list[str] = Field(default_factory=list, max_length=16)
    verification_ladder: list[str] = Field(min_length=1, max_length=8)
    execution_allowed: bool
    escalation_required: bool
    rationale: str = Field(min_length=3, max_length=1000)


class ConvergenceLedger(BaseModel):
    schema_version: str = "onebrief-convergence-ledger-v1"
    policy_revision: str = LEGACY_CONVERGENCE_POLICY_REVISION
    observations: list[FailureObservation] = Field(default_factory=list, max_length=64)
    repair_contracts: list[RepairContract] = Field(default_factory=list, max_length=64)


def new_convergence_ledger() -> ConvergenceLedger:
    return ConvergenceLedger(policy_revision=CURRENT_CONVERGENCE_POLICY_REVISION)


def repair_contract_blocks_resume(
    contract: RepairContract, *, verifier_only_revalidation: bool
) -> bool:
    """Keep repair stops authoritative without blocking a read-only recheck.

    ``execution_allowed`` governs another maker mutation.  A continuation that
    already holds a candidate may still rerun the independent verifier and
    deterministic tools; that action neither explores a new repair strategy
    nor expands write authority.
    """

    return not contract.execution_allowed and not verifier_only_revalidation


def _normalize(value: str) -> str:
    normalized = value.casefold().replace("\\", "/")
    normalized = re.sub(r"\b[0-9a-f]{8}-[0-9a-f-]{27,}\b", "<uuid>", normalized)
    normalized = re.sub(r"[a-z]:/[^\n\r:|]+", "<path>", normalized)
    normalized = re.sub(r"\bpid[=: ]+\d+\b", "pid=<n>", normalized)
    normalized = re.sub(r"\b\d+(?:\.\d+)?\s*(?:ms|seconds?|s)\b", "<duration>", normalized)
    return " ".join(normalized.split())[:1200]


def _digest(prefix: str, payload: object) -> str:
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return f"{prefix}-" + hashlib.sha256(encoded).hexdigest()[:16]


def classify_failure_layer(context: str, failure_text: str) -> FailureLayer:
    text = failure_text.casefold()
    if any(marker in text for marker in (
        "edit anchors could not rediscover", "catalog anchor is not approved",
        "stale or missing base hash", "string_pattern_mismatch",
    )):
        return FailureLayer.SOURCE_BINDING
    if any(marker in text for marker in (
        "eof while parsing", "invalid json", "json_invalid", "max_tokens",
    )):
        return FailureLayer.STRUCTURED_OUTPUT
    if any(marker in text for marker in (
        "screenshot is unavailable",
        "screenshot was not materialized",
        "screenshot file is unavailable",
        "png was not materialized",
    )):
        return FailureLayer.EVIDENCE_RUNTIME
    if any(marker in text for marker in (
        "unity visual test contract: add",
        "unity visual test contract:",
        "readpixels was called to read pixels from system frame buffer",
        "readpixels was called to read pixels from system framebuffer",
        "unity visual evidence requires a distinct rendered scenario",
        "responsive unity visual evidence must define and capture",
        "unity visual evidence reused an identical screenshot",
        "unity visual evidence did not exercise requested locale",
        "unity visual evidence manifest is invalid",
        "unity visual verification requires at least one executed onebrief.visual playmode test",
        "locale changed_visible_text_count must compare before/after visible text snapshots",
        "identical duplicate local ui-control declaration",
    )):
        return FailureLayer.EVIDENCE_TOPOLOGY
    if any(marker in text for marker in (
        "edits tests or evidence instead of production ui",
        "verification-code block", "synthetic ui", "construct synthetic",
    )):
        return FailureLayer.EVIDENCE_INTEGRITY
    if any(marker in text for marker in (
        "independent unity semantic visual observation failed",
        "did not visibly change any text",
        "not reachable from any committed .unity/.prefab script guid",
        "responsive layout", "rendered ui defect", "missing glyph", "glyph",
        "overlap", "clipped", "clipping", "unreadable",
    )):
        return FailureLayer.SEMANTIC_PRODUCT
    if any(marker in text for marker in (
        "compile error", "compilation failed", "build failed", "unity_compile",
        "err_module_not_found", "cannot find module",
    )):
        return FailureLayer.BUILD
    if any(marker in text for marker in (
        "playmode", "runtime", "http probe", "web observation failed",
        "test failed", "tests failed",
    )):
        return FailureLayer.RUNTIME
    if any(marker in text for marker in (
        "permissionerror", "outside the approved", "needs_authorization",
        "authority", "budget exceeded",
    )):
        return FailureLayer.AUTHORITY
    if any(marker in text for marker in (
        "429", "503", "service unavailable", "deadline exceeded", "timed out",
    )):
        return FailureLayer.PROVIDER
    if context == "development_candidate_promotion":
        return FailureLayer.SOURCE_BINDING
    return FailureLayer.UNKNOWN


def classify_failure_code(
    context: str, failure_text: str, layer: FailureLayer | None = None
) -> FailureCode:
    """Return a stable machine code before any repair routing decision."""

    text = failure_text.casefold()
    resolved = layer or classify_failure_layer(context, failure_text)
    if any(marker in text for marker in (
        "screenshot is unavailable",
        "screenshot was not materialized",
        "screenshot file is unavailable",
        "png was not materialized",
    )):
        return FailureCode.UNITY_SCREENSHOT_NOT_MATERIALIZED
    if any(marker in text for marker in (
        "evidence harness",
        "evidence topology",
        "unity visual test contract",
        "missing playmode test",
        "executed onebrief.visual playmode test",
        "add a discoverable unity playmode test",
        "testassemblies",
        "runtime-evidence.json",
        "screenshot capture is missing",
    )):
        return FailureCode.EVIDENCE_TOPOLOGY_INVALID
    return {
        FailureLayer.STRUCTURED_OUTPUT: FailureCode.STRUCTURED_OUTPUT_INVALID,
        FailureLayer.SOURCE_BINDING: FailureCode.SOURCE_REVISION_STALE,
        FailureLayer.BUILD: FailureCode.BUILD_FAILED,
        FailureLayer.RUNTIME: FailureCode.RUNTIME_FAILED,
        FailureLayer.EVIDENCE_RUNTIME: FailureCode.EVIDENCE_TOPOLOGY_INVALID,
        FailureLayer.EVIDENCE_TOPOLOGY: FailureCode.EVIDENCE_TOPOLOGY_INVALID,
        FailureLayer.EVIDENCE_INTEGRITY: FailureCode.EVIDENCE_INTEGRITY_INVALID,
        FailureLayer.SEMANTIC_PRODUCT: FailureCode.SEMANTIC_PRODUCT_DEFECT,
        FailureLayer.AUTHORITY: FailureCode.AUTHORITY_REQUIRED,
        FailureLayer.PROVIDER: FailureCode.PROVIDER_UNAVAILABLE,
        FailureLayer.UNKNOWN: FailureCode.UNKNOWN,
    }[resolved]


def failure_owner_for(code: FailureCode, layer: FailureLayer) -> FailureOwner:
    if code in {
        FailureCode.UNITY_SCREENSHOT_NOT_MATERIALIZED,
        FailureCode.EVIDENCE_TOPOLOGY_INVALID,
        FailureCode.EVIDENCE_INTEGRITY_INVALID,
    } or layer in {
        FailureLayer.EVIDENCE_RUNTIME,
        FailureLayer.EVIDENCE_TOPOLOGY,
        FailureLayer.EVIDENCE_INTEGRITY,
    }:
        return FailureOwner.EVIDENCE
    if layer in {FailureLayer.PROVIDER, FailureLayer.STRUCTURED_OUTPUT}:
        return FailureOwner.ENVIRONMENT
    if layer == FailureLayer.AUTHORITY:
        return FailureOwner.CONTRACT
    return FailureOwner.PRODUCT


def extract_symptom_keys(
    failure_text: str,
    *,
    failed_criterion_ids: list[str] | None = None,
) -> list[str]:
    """Extract stable failure atoms from noisy verifier prose.

    Independent observers may rephrase the same defect on every pass. Paths
    changed by the maker are also not the defect's identity.  These atoms bind
    the visible state and symptom category so wording changes cannot reset the
    progress counter, while a genuinely removed symptom is measurable progress.
    """

    text = failure_text.casefold().replace("\\", "/")
    keys = {f"criterion:{item.casefold()}" for item in (failed_criterion_ids or [])}
    segments = re.split(r"[;\n|]|(?=-\s+screenshots?/)", text)
    categories: tuple[tuple[str, tuple[str, ...]], ...] = (
        ("missing_glyph", ("missing glyph", "tofu", "□", "unsupported glyph")),
        ("overlap", ("overlap", "overlapping")),
        ("clipped", ("clipped", "clipping", "cut off")),
        ("unreadable", ("unreadable", "not readable")),
        ("not_responsive", ("not responsive", "poor responsiveness", "responsive design")),
        ("blank", ("blank screen", "empty screen", "nothing rendered")),
        ("wrong_language", (
            "wrong language", "untranslated", "translation missing",
            "did not visibly change any text",
        )),
        ("inactive_binding", (
            "not reachable from any committed .unity/.prefab script guid",
            "detached source file",
        )),
        ("navigation", ("navigation failed", "screen transition", "route failed")),
        ("runtime_failure", ("runtime test failed", "playmode failed", "interaction failed")),
        ("compile_failure", ("compile error", "compilation failed", "build failed")),
        ("missing_playmode_test", ("add a discoverable unity playmode test",)),
        ("missing_test_asmdef", ("add a unity test .asmdef", "testassemblies")),
        ("evidence_topology", (
            "distinct rendered scenario", "responsive unity visual evidence",
            "reused an identical screenshot", "did not exercise requested locale",
            "changed_visible_text_count must compare before/after visible text snapshots",
            "identical duplicate local ui-control declaration",
        )),
        ("runtime_evidence_json", ("must write onebrief-evidence/runtime-evidence.json",)),
        ("png_capture", ("must capture png runtime evidence",)),
        ("scenario_schema", ("must use schema_version onebrief-unity-visual-evidence-v1",)),
        ("invalid_evidence_manifest", ("unity visual evidence manifest is invalid",)),
        ("scenario_fields", ("each general ui evidence scenario must contain",)),
        ("overlay_capture", ("rendertexture does not capture screenspaceoverlay ui",)),
        ("real_transition", ("must perform a real ui interaction",)),
        ("visible_ui", ("must inspect and interact with visible ui objects",)),
        ("language_dropdown", ("languagedropdown",)),
        ("glyph_inspection", ("missing_glyph_count must inspect",)),
    )

    def surface(segment: str) -> str:
        screenshot = re.search(r"screenshots?/([a-z0-9_.-]+)", segment)
        if screenshot:
            return screenshot.group(1).removesuffix(".png")
        for name in (
            "login_mobile", "lobby_mobile", "settings_mobile",
            "login_desktop", "lobby_desktop", "settings_desktop",
            "mobile", "desktop",
        ):
            if name.replace("_", " ") in segment or name in segment:
                return name
        return "artifact"

    for segment in segments:
        active_surface = surface(segment)
        for category, markers in categories:
            if any(marker in segment for marker in markers):
                keys.add(f"{active_surface}:{category}")
    if not keys:
        # Non-visual/tooling errors retain their normalized causal identity.
        keys.add("message:" + hashlib.sha256(_normalize(failure_text).encode("utf-8")).hexdigest()[:16])
    return sorted(keys)[:32]


def _same_causal_boundary(
    previous: FailureObservation,
    current: FailureObservation,
) -> bool:
    if previous.layer != current.layer:
        return False
    if current.layer in {
        FailureLayer.EVIDENCE_RUNTIME,
        FailureLayer.EVIDENCE_TOPOLOGY,
    }:
        # The static Unity evidence checklist is one finite causal boundary.
        # Its individual blockers legitimately change as earlier requirements
        # are satisfied, and issue_contract measures that change explicitly.
        return True
    prior = set(previous.symptom_keys or extract_symptom_keys(
        previous.normalized_signature,
        failed_criterion_ids=previous.failed_criterion_ids,
    ))
    active = set(current.symptom_keys or extract_symptom_keys(
        current.normalized_signature,
        failed_criterion_ids=current.failed_criterion_ids,
    ))
    if prior and active:
        overlap = len(prior & active) / max(1, min(len(prior), len(active)))
        return overlap >= 0.6
    return previous.normalized_signature == current.normalized_signature


def _hypothesis(
    layer: FailureLayer,
    observation_id: str,
    evidence_signature: str = "",
) -> RepairHypothesis:
    templates: dict[FailureLayer, tuple[str, str, str, str, bool]] = {
        FailureLayer.SOURCE_BINDING: (
            "The proposed selector does not bind to the exact approved candidate snapshot, or an approved catalog ID was emitted in the wrong selector field.",
            "Normalize a recognized catalog ID, then rebuild the anchor catalog from the exact current candidate and require one unique range before another model turn.",
            "Exactly one approved source range resolves without changing product bytes.",
            "Runtime normalization or one bounded edit on the already failing source path only.",
            False,
        ),
        FailureLayer.STRUCTURED_OUTPUT: (
            "The provider response exceeded or violated the bounded structured-output envelope.",
            "Validate a smaller one-path schema response before any repository operation.",
            "The response parses and validates without truncation.",
            "Output shape only; do not change product scope or acceptance criteria.",
            False,
        ),
        FailureLayer.BUILD: (
            "The candidate violates a compiler, dependency, or build-order contract.",
            "Run the cheapest compile or dependency probe that reproduces the first diagnostic.",
            "The first build diagnostic disappears without weakening tests.",
            "The smallest source or locked-toolchain change tied to the first diagnostic.",
            False,
        ),
        FailureLayer.RUNTIME: (
            "The product compiles but fails a concrete runtime interaction or targeted test.",
            "Re-run only the first failing scenario with fixed inputs and preserve its trace.",
            "The targeted scenario passes and produces a new trusted receipt.",
            "One runtime behavior slice; preserve all previously passing scenarios.",
            True,
        ),
        FailureLayer.EVIDENCE_RUNTIME: (
            "The evidence test ran but did not durably materialize a referenced artifact.",
            "Run the evidence producer only and verify that every referenced file exists and hashes before writing its manifest.",
            "The PNG bytes and dimensions are verified before an atomically published manifest references them.",
            "Tests/PlayMode evidence source only; product source and previously passing criteria remain unchanged.",
            False,
        ),
        FailureLayer.EVIDENCE_TOPOLOGY: (
            "The executed evidence harness is missing or does not prove each requested real UI state.",
            "Add or repair only the smallest Tests/PlayMode source or test asmdef required by the first static contract failure.",
            "The static evidence contract passes and the targeted PlayMode test produces a new trusted receipt.",
            "Tests/PlayMode evidence topology only; product UI and acceptance criteria remain unchanged.",
            True,
        ),
        FailureLayer.EVIDENCE_INTEGRITY: (
            "The candidate changed its proof surface instead of repairing shipped behavior.",
            "Run the immutable proof-surface scan before build or model verification.",
            "No test, screenshot, assertion, or evidence adapter mutates product-visible state.",
            "Production source only; tests and evidence remain immutable.",
            False,
        ),
        FailureLayer.SEMANTIC_PRODUCT: (
            "The shipped result remains semantically wrong in at least one observed state.",
            "Select the smallest failing state and compare its trusted observation against a passing reference state.",
            "That state passes semantic observation without changing its proof surface.",
            "One failing production surface and one completion criterion per turn.",
            True,
        ),
        FailureLayer.AUTHORITY: (
            "Completion requires authority, scope, budget, or source revision not present in the active approval.",
            "Compare the requested action digest with the current authority envelope.",
            "An exact matching approval exists, or the run remains stopped.",
            "No autonomous authority expansion.",
            False,
        ),
        FailureLayer.PROVIDER: (
            "The provider or transport failed independently of artifact quality.",
            "Repeat the same idempotent request once without changing prompt, model authority, or product bytes.",
            "The provider returns a complete response or the retry budget is exhausted.",
            "Transport retry only.",
            False,
        ),
        FailureLayer.UNKNOWN: (
            "The available evidence is insufficient to identify a safe repair layer.",
            "Collect one deterministic reproduction and classify the first failing boundary.",
            "The failure is assigned to a concrete layer with a reproducible signal.",
            "Diagnostics only; product edits are not yet permitted.",
            True,
        ),
    }
    cause, probe, signal, boundary, reasoning = templates[layer]
    if (
        layer == FailureLayer.SEMANTIC_PRODUCT
        and "unity layout diagnostics" in evidence_signature.casefold()
    ):
        cause = (
            "The visible Unity defect is associated with a recorded Canvas or RectTransform "
            "topology risk, so another global scaler guess is not yet justified."
        )
        probe = (
            "Match the smallest failing screenshot surface to one hierarchy risk record, then "
            "inspect only that Canvas and its nearest risky RectTransform ancestry."
        )
        signal = (
            "One named hierarchy risk disappears and the corresponding rendered symptom is removed "
            "while previously passing surfaces remain unchanged."
        )
        boundary = "One diagnosed production RectTransform ancestry and one visible symptom."
    if (
        layer == FailureLayer.SEMANTIC_PRODUCT
        and "not reachable from any committed .unity/.prefab script guid"
        in evidence_signature.casefold()
    ):
        cause = (
            "The edited Unity MonoBehaviour compiles but is not attached to a committed scene/prefab and has no "
            "runtime production reference, so it cannot change the observed UI."
        )
        probe = (
            "Use committed scene script-GUID metadata to identify the active component that owns the failing "
            "control, then make one bounded production repair there or explicitly attach the intended binder."
        )
        signal = (
            "The repaired component is reachable in the executed scene and the unchanged observer measures a real "
            "visible state change."
        )
        boundary = "One active Unity component binding; the detached source and proof surface remain frozen."
    return RepairHypothesis(
        hypothesis_id=_digest("RH", [observation_id, layer.value, probe]),
        suspected_cause=cause,
        cheapest_probe=probe,
        expected_signal=signal,
        repair_boundary=boundary,
        requires_model_reasoning=reasoning,
    )


class ConvergencePolicy:
    """Issue one evidence-bound repair contract or stop a non-learning retry."""

    def observe(
        self,
        *,
        context: str,
        failure_text: str,
        attempt_number: int,
        affected_paths: list[str] | None = None,
        failed_criterion_ids: list[str] | None = None,
        strategy_fingerprint: str | None = None,
    ) -> FailureObservation:
        normalized = _normalize(failure_text)
        evidence_sha = hashlib.sha256(failure_text.encode("utf-8")).hexdigest()
        layer = classify_failure_layer(context, failure_text)
        code = classify_failure_code(context, failure_text, layer)
        # Changed paths describe the attempted strategy, not the trusted
        # failure's identity. A restart can preserve the exact verifier output
        # without reconstructing that optional envelope; binding the ID to the
        # paths turns one durable observation into a false extra occurrence.
        identity = [context, normalized]
        return FailureObservation(
            observation_id=_digest("FO", identity),
            context=context,
            code=code,
            layer=layer,
            owner=failure_owner_for(code, layer),
            normalized_signature=normalized,
            evidence_sha256=evidence_sha,
            affected_paths=list(dict.fromkeys(affected_paths or []))[:16],
            failed_criterion_ids=list(dict.fromkeys(failed_criterion_ids or []))[:16],
            symptom_keys=extract_symptom_keys(
                failure_text, failed_criterion_ids=failed_criterion_ids
            ),
            strategy_fingerprint=strategy_fingerprint,
            attempt_number=attempt_number,
        )

    def issue_contract(
        self,
        ledger: ConvergenceLedger,
        observation: FailureObservation,
        *,
        preserve_criterion_ids: list[str] | None = None,
    ) -> RepairContract:
        exact_replay = next((
            item for item in reversed(ledger.observations)
            if (
                item.model_dump(mode="json") == observation.model_dump(mode="json")
                or (
                    not observation.affected_paths
                    and observation.strategy_fingerprint is None
                    and item.context == observation.context
                    and item.normalized_signature == observation.normalized_signature
                    and item.evidence_sha256 == observation.evidence_sha256
                )
            )
        ), None)
        if exact_replay is not None:
            existing_contract = next((
                item for item in reversed(ledger.repair_contracts)
                if item.observation_id == exact_replay.observation_id
            ), None)
            if existing_contract is not None:
                # Verification and checkpoint hooks may request the same
                # contract. That is an idempotent read, not another experiment.
                return existing_contract
        matches = [
            item for item in ledger.observations
            if _same_causal_boundary(item, observation)
        ]
        same_strategy = bool(
            observation.strategy_fingerprint
            and any(
                item.strategy_fingerprint == observation.strategy_fingerprint
                for item in matches
            )
        )
        occurrence = len(matches) + 1
        current_symptoms = set(observation.symptom_keys)
        prior_symptoms = (
            set(matches[-1].symptom_keys or extract_symptom_keys(
                matches[-1].normalized_signature,
                failed_criterion_ids=matches[-1].failed_criterion_ids,
            ))
            if matches else set()
        )
        narrowed = bool(
            matches
            and current_symptoms
            and prior_symptoms
            and current_symptoms < prior_symptoms
        )
        changed_topology_signal = bool(
            matches
            and observation.layer == FailureLayer.EVIDENCE_TOPOLOGY
            and current_symptoms != prior_symptoms
        )
        if narrowed:
            progress = ProgressKind.CRITERION_ADVANCE
            allowed = True
            escalation = False
            rationale = (
                "Trusted evidence removed at least one prior symptom. Continue from the narrower failing boundary."
            )
        elif changed_topology_signal and occurrence <= 8:
            progress = ProgressKind.NARROWED_FAILURE
            allowed = True
            escalation = False
            rationale = (
                "The finite evidence contract exposed a different static blocker. Continue one bounded "
                "Tests/PlayMode repair and re-run the authoritative checklist."
            )
        elif same_strategy:
            progress = ProgressKind.NO_PROGRESS
            allowed = False
            escalation = True
            rationale = (
                "The same causal failure and repair strategy produced no new evidence; another maker call is blocked."
            )
        elif occurrence >= 3:
            progress = ProgressKind.NO_PROGRESS
            allowed = False
            escalation = True
            rationale = (
                "Two materially different repairs reached the same causal boundary; stop and escalate instead of exploring blindly."
            )
        elif matches:
            progress = ProgressKind.NEW_HYPOTHESIS
            allowed = True
            escalation = False
            rationale = (
                "The previous hypothesis was disproved. One materially different bounded hypothesis may be tested."
            )
        else:
            progress = ProgressKind.FIRST_OBSERVATION
            allowed = observation.layer != FailureLayer.AUTHORITY
            escalation = observation.layer == FailureLayer.AUTHORITY
            rationale = (
                "The first trusted observation authorizes only the cheapest discriminating probe and one bounded repair."
                if allowed else
                "The failure requires new human authority and cannot authorize an autonomous repair."
            )
        hypothesis = _hypothesis(
            observation.layer,
            observation.observation_id,
            observation.normalized_signature,
        )
        ladder_by_layer = {
            FailureLayer.SOURCE_BINDING: [
                "schema_and_selector_probe", "exact_source_promotion", "compile", "targeted_test", "full_verification"
            ],
            FailureLayer.STRUCTURED_OUTPUT: ["schema_validation", "exact_source_promotion", "targeted_verification"],
            FailureLayer.BUILD: ["exact_source_promotion", "compile", "targeted_test", "full_verification"],
            FailureLayer.RUNTIME: ["exact_source_promotion", "compile", "targeted_test", "full_verification"],
            FailureLayer.EVIDENCE_RUNTIME: [
                "evidence_source_validation", "compile", "targeted_test",
                "artifact_materialization", "evidence_integrity", "full_verification"
            ],
            FailureLayer.EVIDENCE_TOPOLOGY: [
                "proof_topology_scan", "compile", "targeted_test", "evidence_integrity", "full_verification"
            ],
            FailureLayer.EVIDENCE_INTEGRITY: ["immutable_proof_scan", "exact_source_promotion", "targeted_test", "full_verification"],
            FailureLayer.SEMANTIC_PRODUCT: ["exact_source_promotion", "compile", "targeted_state", "semantic_observation", "full_verification"],
            FailureLayer.AUTHORITY: ["authority_digest_check"],
            FailureLayer.PROVIDER: ["idempotent_provider_retry"],
            FailureLayer.UNKNOWN: ["deterministic_reproduction", "failure_layer_classification"],
        }
        contract_payload = [
            observation.observation_id, occurrence, hypothesis.hypothesis_id,
            sorted(observation.affected_paths), allowed,
        ]
        permitted_paths = (
            []
            if observation.layer in {
                FailureLayer.EVIDENCE_RUNTIME,
                FailureLayer.EVIDENCE_TOPOLOGY,
            }
            or "artifact:inactive_binding" in observation.symptom_keys
            else observation.affected_paths
        )
        return RepairContract(
            contract_id=_digest("RC", contract_payload),
            observation_id=observation.observation_id,
            progress_kind=progress,
            occurrence=occurrence,
            hypothesis=hypothesis,
            permitted_paths=permitted_paths,
            preserve_criterion_ids=list(dict.fromkeys(preserve_criterion_ids or []))[:16],
            verification_ladder=ladder_by_layer[observation.layer],
            execution_allowed=allowed,
            escalation_required=escalation,
            rationale=rationale,
        )

    def record(
        self,
        ledger: ConvergenceLedger,
        observation: FailureObservation,
        contract: RepairContract,
    ) -> ConvergenceLedger:
        if any(
            item.model_dump(mode="json") == observation.model_dump(mode="json")
            for item in ledger.observations
        ):
            return ledger
        return ledger.model_copy(update={
            "observations": [*ledger.observations, observation][-64:],
            "repair_contracts": [*ledger.repair_contracts, contract][-64:],
        })
