"""Deterministic failure classification and bounded recovery decisions."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field

from onebrief.budget_guard import BudgetExceeded


class ErrorClass(StrEnum):
    OUTPUT_TRUNCATION = "output_truncation"
    ARTIFACT_VALIDATION = "artifact_validation"
    TRANSIENT_PROVIDER = "transient_provider"
    MISSING_INFORMATION = "missing_information"
    BUDGET = "budget"
    VERIFICATION_SETUP = "verification_setup"
    PLATFORM_POLICY = "platform_policy"
    SOURCE_DRIFT = "source_drift"
    UNKNOWN = "unknown"


class RecoveryAction(StrEnum):
    AUTO_RETRY = "auto_retry"
    RETURN_TO_AGENT = "return_to_agent"
    ASK_USER = "ask_user"
    STOP = "stop"


class RecoveryDecision(BaseModel):
    schema_version: str = "onebrief-recovery-decision-v1"
    context: str
    error_class: ErrorClass
    action: RecoveryAction
    responsible_party: str
    attempt_number: int = Field(ge=1)
    recovery_limit: int = Field(ge=0)
    retry_allowed: bool
    error_type: str
    error_summary: str = Field(max_length=1200)
    rationale: str = Field(max_length=600)


def _summary(error: Exception) -> str:
    text = " ".join(str(error).split())
    for marker in ("[type=", "input_value="):
        if marker in text:
            text = text.split(marker, 1)[0].rstrip(" [,\n")
    return text[:1200] or type(error).__name__


class RecoveryPolicy:
    """Fail closed while allowing only small, classified, bounded recoveries."""

    def decide(
        self,
        error: Exception,
        *,
        context: str,
        attempt_number: int = 1,
    ) -> RecoveryDecision:
        message = str(error).casefold()
        error_class = ErrorClass.UNKNOWN
        action = RecoveryAction.STOP
        responsible = "maintainer"
        limit = 0
        rationale = "Unclassified failures stop rather than expanding agent authority."

        truncation_markers = (
            "eof while parsing", "json_invalid", "invalid json", "unterminated string",
            "finish_reason=max_tokens", "output token",
        )
        transient_markers = (
            "429", "503", "temporarily unavailable", "connection reset", "timed out", "timeout",
        )
        missing_markers = (
            "needs_information", "missing required information", "required information is missing",
        )
        structured_markers = (
            "path is outside", "change path must", "base_sha256", "string_pattern_mismatch",
            "change set exceeds", "too_long", "validation error for codechangeset",
            "validation error for projectcodechangeset",
            "validation errors for projectcodechangeset",
            "prohibited host-runtime capability",
            "absent from approved_repository_files",
        )
        verification_setup_markers = (
            "command not found", "eslint: not found",
            "is not recognized as an internal or external command",
            "could not determine executable to run",
        )
        windows_sharing_violation = (
            getattr(error, "winerror", None) in {32, 33}
            or "winerror 32" in message
            or "winerror 33" in message
            or "being used by another process" in message
            or "file is being used by another process" in message
            or "다른 프로세스가 파일을 사용 중" in message
        )

        if isinstance(error, BudgetExceeded):
            error_class = ErrorClass.BUDGET
            action = RecoveryAction.ASK_USER
            responsible = "user"
            rationale = "Only the user may approve additional spend or reduce scope."
        elif isinstance(error, PermissionError) and windows_sharing_violation:
            error_class = ErrorClass.VERIFICATION_SETUP
            responsible = "runtime"
            rationale = (
                "A Windows sharing violation is a transient verifier collision, not an authority "
                "expansion. The runtime must retry from preserved evidence after the competing "
                "process exits."
            )
        elif any(marker in message for marker in truncation_markers):
            error_class = ErrorClass.OUTPUT_TRUNCATION
            action = RecoveryAction.AUTO_RETRY
            responsible = "runtime"
            limit = 1
            rationale = (
                "A truncated structured response may be retried once with the approved model and a "
                "larger pre-budgeted output envelope."
            )
        elif "err_module_not_found" in message and "dist" in message and "server" in message:
            error_class = ErrorClass.VERIFICATION_SETUP
            responsible = "runtime"
            rationale = (
                "A required build artifact is missing; the runtime must repair command ordering rather "
                "than asking the maker to alter product code."
            )
        elif context == "development_verification" and any(
            marker in message for marker in verification_setup_markers
        ):
            error_class = ErrorClass.VERIFICATION_SETUP
            responsible = "runtime"
            rationale = (
                "A fixed validator dependency is unavailable; the runtime must restore its locked "
                "toolchain instead of spending maker retries on unchanged product code."
            )
        elif (
            context == "development_verification"
            and message.startswith("development verification failed: node_")
            and "_dependencies" in message
        ):
            error_class = ErrorClass.VERIFICATION_SETUP
            responsible = "runtime"
            rationale = (
                "Dependency restoration is a fixed runtime responsibility and must never consume "
                "maker revision calls."
            )
        elif message.startswith((
            "development verification failed:",
            "development patch hygiene failed:",
            "web observation failed:",
            "independent unity semantic visual observation failed:",
        )) or message.startswith((
            "unity visual scenario ",
            "unity visual evidence ",
            "unity visual verification requires ",
        )):
            error_class = ErrorClass.ARTIFACT_VALIDATION
            action = RecoveryAction.RETURN_TO_AGENT
            responsible = "maker"
            limit = 2
            rationale = "The maker may correct its artifact within two bounded retries; the same deterministic checks must pass."
        elif context == "development_candidate_promotion" and (
            "edit anchors could not rediscover" in message
            or "duplicate full-file proposals" in message
        ):
            error_class = ErrorClass.ARTIFACT_VALIDATION
            action = RecoveryAction.RETURN_TO_AGENT
            responsible = "maker"
            limit = 2
            rationale = (
                "The proposed delta did not bind to the preserved candidate; the same maker may retry "
                "with verbatim bounded anchors without changing the accepted artifact."
            )
        elif context in {
            "developer_structured_output", "development_candidate_promotion"
        } and any(
            marker in message for marker in structured_markers
        ):
            error_class = ErrorClass.ARTIFACT_VALIDATION
            action = (
                RecoveryAction.RETURN_TO_AGENT
                if context == "development_candidate_promotion"
                else RecoveryAction.AUTO_RETRY
            )
            responsible = "maker"
            limit = 2 if context == "development_candidate_promotion" else 1
            rationale = (
                "The maker may correct a bounded schema, safety, or repository-contract error "
                "without changing scope or bypassing the deterministic policy gate."
            )
        elif "stale or missing base hash" in message:
            error_class = ErrorClass.SOURCE_DRIFT
            responsible = "project_owner"
            rationale = "Changed source evidence requires a fresh inspected project run, not an agent bypass."
        elif isinstance(error, PermissionError) or "approved model binding" in message:
            error_class = ErrorClass.PLATFORM_POLICY
            responsible = "maintainer"
            rationale = "Agents may not modify or bypass model, security, or platform authorization policy."
        elif any(marker in message for marker in missing_markers):
            error_class = ErrorClass.MISSING_INFORMATION
            action = RecoveryAction.ASK_USER
            responsible = "user"
            rationale = "A result-changing missing decision must be returned to the user in one batch."
        elif any(marker in message for marker in transient_markers):
            error_class = ErrorClass.TRANSIENT_PROVIDER
            action = RecoveryAction.AUTO_RETRY
            responsible = "runtime"
            limit = 2
            rationale = "A transient provider failure may be retried without changing scope or authority."

        retry_allowed = action in {
            RecoveryAction.AUTO_RETRY, RecoveryAction.RETURN_TO_AGENT
        } and attempt_number <= limit
        if not retry_allowed and action in {
            RecoveryAction.AUTO_RETRY, RecoveryAction.RETURN_TO_AGENT
        }:
            action = RecoveryAction.STOP
            rationale += " The bounded recovery limit has been reached."

        return RecoveryDecision(
            context=context,
            error_class=error_class,
            action=action,
            responsible_party=responsible,
            attempt_number=attempt_number,
            recovery_limit=limit,
            retry_allowed=retry_allowed,
            error_type=type(error).__name__,
            error_summary=_summary(error),
            rationale=rationale,
        )
