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


class FailureLayer(StrEnum):
    STRUCTURED_OUTPUT = "structured_output"
    SOURCE_BINDING = "source_binding"
    BUILD = "build"
    RUNTIME = "runtime"
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


class FailureObservation(BaseModel):
    schema_version: str = "onebrief-failure-observation-v1"
    observation_id: str = Field(pattern=r"^FO-[a-f0-9]{16}$")
    context: str
    layer: FailureLayer
    normalized_signature: str = Field(min_length=3, max_length=1200)
    evidence_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    affected_paths: list[str] = Field(default_factory=list, max_length=16)
    failed_criterion_ids: list[str] = Field(default_factory=list, max_length=16)
    strategy_fingerprint: str | None = None
    attempt_number: int = Field(ge=1)


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
    observations: list[FailureObservation] = Field(default_factory=list, max_length=64)
    repair_contracts: list[RepairContract] = Field(default_factory=list, max_length=64)


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
        "edits tests or evidence instead of production ui",
        "verification-code block", "synthetic ui", "construct synthetic",
    )):
        return FailureLayer.EVIDENCE_INTEGRITY
    if any(marker in text for marker in (
        "independent unity semantic visual observation failed",
        "responsive layout", "rendered ui defect", "missing glyph",
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


def _hypothesis(layer: FailureLayer, observation_id: str) -> RepairHypothesis:
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
        identity = [context, normalized, sorted(affected_paths or [])]
        return FailureObservation(
            observation_id=_digest("FO", identity),
            context=context,
            layer=classify_failure_layer(context, failure_text),
            normalized_signature=normalized,
            evidence_sha256=evidence_sha,
            affected_paths=list(dict.fromkeys(affected_paths or []))[:16],
            failed_criterion_ids=list(dict.fromkeys(failed_criterion_ids or []))[:16],
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
        matches = [
            item for item in ledger.observations
            if item.context == observation.context
            and item.normalized_signature == observation.normalized_signature
            and item.affected_paths == observation.affected_paths
        ]
        same_strategy = bool(
            observation.strategy_fingerprint
            and any(
                item.strategy_fingerprint == observation.strategy_fingerprint
                for item in matches
            )
        )
        occurrence = len(matches) + 1
        if same_strategy:
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
        hypothesis = _hypothesis(observation.layer, observation.observation_id)
        ladder_by_layer = {
            FailureLayer.SOURCE_BINDING: [
                "schema_and_selector_probe", "exact_source_promotion", "compile", "targeted_test", "full_verification"
            ],
            FailureLayer.STRUCTURED_OUTPUT: ["schema_validation", "exact_source_promotion", "targeted_verification"],
            FailureLayer.BUILD: ["exact_source_promotion", "compile", "targeted_test", "full_verification"],
            FailureLayer.RUNTIME: ["exact_source_promotion", "compile", "targeted_test", "full_verification"],
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
        return RepairContract(
            contract_id=_digest("RC", contract_payload),
            observation_id=observation.observation_id,
            progress_kind=progress,
            occurrence=occurrence,
            hypothesis=hypothesis,
            permitted_paths=observation.affected_paths,
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
        return ledger.model_copy(update={
            "observations": [*ledger.observations, observation][-64:],
            "repair_contracts": [*ledger.repair_contracts, contract][-64:],
        })
