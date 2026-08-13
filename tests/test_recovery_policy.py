import pytest
from pydantic import ValidationError

from onebrief.budget_guard import BudgetExceeded
from onebrief.development_toolpack import CodeChangeSet
from onebrief.recovery_policy import ErrorClass, RecoveryAction, RecoveryPolicy


def _truncated_error() -> ValidationError:
    try:
        CodeChangeSet.model_validate_json(
            '{"summary":"Enhance Exchange","changes":[{"path":"web/app/page.tsx","content":"cut'
        )
    except ValidationError as exc:
        return exc
    raise AssertionError("fixture must be invalid")


def test_truncated_output_is_retried_once_then_stops() -> None:
    policy = RecoveryPolicy()

    first = policy.decide(
        _truncated_error(), context="developer_structured_output", attempt_number=1
    )
    repeated = policy.decide(
        _truncated_error(), context="developer_structured_output", attempt_number=2
    )

    assert first.error_class == ErrorClass.OUTPUT_TRUNCATION
    assert first.action == RecoveryAction.AUTO_RETRY
    assert first.retry_allowed is True
    assert repeated.action == RecoveryAction.STOP
    assert repeated.retry_allowed is False
    assert len(first.error_summary) < 1200


def test_project_change_contract_violation_returns_to_the_same_maker_once() -> None:
    policy = RecoveryPolicy()
    error = ValueError(
        "2 validation errors for ProjectCodeChangeSet "
        "changes.1 Value error, change content requests a prohibited host-runtime capability"
    )

    first = policy.decide(
        error, context="developer_structured_output", attempt_number=1
    )
    second = policy.decide(
        error, context="developer_structured_output", attempt_number=2
    )

    assert first.error_class == ErrorClass.ARTIFACT_VALIDATION
    assert first.action == RecoveryAction.AUTO_RETRY
    assert first.responsible_party == "maker"
    assert second.action == RecoveryAction.STOP


def test_promoted_project_safety_violation_returns_to_same_maker() -> None:
    error = ValueError(
        "1 validation error for ProjectCodeChangeSet changes.0 "
        "Value error, change content requests a prohibited host-runtime capability"
    )

    decision = RecoveryPolicy().decide(
        error, context="development_candidate_promotion", attempt_number=1
    )

    assert decision.error_class == ErrorClass.ARTIFACT_VALIDATION
    assert decision.action == RecoveryAction.RETURN_TO_AGENT
    assert decision.responsible_party == "maker"
    assert decision.retry_allowed is True


def test_failed_artifact_verification_returns_to_maker_once() -> None:
    decision = RecoveryPolicy().decide(
        RuntimeError("development verification failed: web_tests\nmissing marker"),
        context="development_verification",
        attempt_number=1,
    )

    assert decision.error_class == ErrorClass.ARTIFACT_VALIDATION
    assert decision.action == RecoveryAction.RETURN_TO_AGENT
    assert decision.responsible_party == "maker"

    second = RecoveryPolicy().decide(
        RuntimeError("development verification failed: compile\nsecond distinct error"),
        context="development_verification",
        attempt_number=2,
    )
    third = RecoveryPolicy().decide(
        RuntimeError("development verification failed: compile\nthird error"),
        context="development_verification",
        attempt_number=3,
    )
    assert second.action == RecoveryAction.RETURN_TO_AGENT
    assert second.retry_allowed is True
    assert third.action == RecoveryAction.STOP
    assert third.retry_allowed is False


def test_patch_hygiene_failure_returns_to_maker_once() -> None:
    decision = RecoveryPolicy().decide(
        RuntimeError(
            "development patch hygiene failed: Assets/Locale.cs:12: trailing whitespace."
        ),
        context="development_verification",
        attempt_number=1,
    )

    assert decision.error_class == ErrorClass.ARTIFACT_VALIDATION
    assert decision.action == RecoveryAction.RETURN_TO_AGENT
    assert decision.responsible_party == "maker"
    assert decision.retry_allowed is True


def test_unmatched_structural_edit_returns_to_maker_without_discarding_candidate() -> None:
    decision = RecoveryPolicy().decide(
        ValueError(
            "edit anchors could not rediscover one approved source range: web/app/page.tsx"
        ),
        context="development_candidate_promotion",
        attempt_number=2,
    )

    assert decision.error_class == ErrorClass.ARTIFACT_VALIDATION
    assert decision.action == RecoveryAction.RETURN_TO_AGENT
    assert decision.responsible_party == "maker"
    assert decision.retry_allowed is True


def test_missing_unity_runtime_screenshot_returns_to_maker() -> None:
    decision = RecoveryPolicy().decide(
        RuntimeError("Unity visual scenario locale_ko screenshot is unavailable"),
        context="development_verification",
        attempt_number=1,
    )

    assert decision.error_class == ErrorClass.ARTIFACT_VALIDATION


def test_zero_discovered_unity_visual_tests_returns_to_evidence_maker() -> None:
    decision = RecoveryPolicy().decide(
        RuntimeError(
            "Unity visual verification requires at least one executed OneBrief.Visual PlayMode test"
        ),
        context="development_verification",
        attempt_number=1,
    )

    assert decision.error_class == ErrorClass.ARTIFACT_VALIDATION
    assert decision.action == RecoveryAction.RETURN_TO_AGENT
    assert decision.retry_allowed is True
    assert decision.responsible_party == "maker"
    assert decision.action == RecoveryAction.RETURN_TO_AGENT
    assert decision.responsible_party == "maker"
    assert decision.retry_allowed is True


def test_failed_web_interaction_observation_returns_to_maker() -> None:
    decision = RecoveryPolicy().decide(
        RuntimeError(
            "web observation failed: Visible text did not change after the language control was activated."
        ),
        context="development_verification",
        attempt_number=1,
    )

    assert decision.error_class == ErrorClass.ARTIFACT_VALIDATION
    assert decision.action == RecoveryAction.RETURN_TO_AGENT
    assert decision.responsible_party == "maker"
    assert decision.retry_allowed is True


def test_failed_unity_semantic_observation_returns_to_same_maker() -> None:
    decision = RecoveryPolicy().decide(
        RuntimeError(
            "independent Unity semantic visual observation failed: mobile UI is clipped"
        ),
        context="development_verification",
        attempt_number=1,
    )

    assert decision.error_class == ErrorClass.ARTIFACT_VALIDATION
    assert decision.action == RecoveryAction.RETURN_TO_AGENT
    assert decision.responsible_party == "maker"
    assert decision.retry_allowed is True


def test_budget_failure_is_returned_to_the_user() -> None:
    decision = RecoveryPolicy().decide(
        BudgetExceeded("approval is insufficient"), context="budget_gate"
    )

    assert decision.error_class == ErrorClass.BUDGET
    assert decision.action == RecoveryAction.ASK_USER
    assert decision.retry_allowed is False


def test_missing_web_build_artifact_is_a_runtime_setup_error_not_a_maker_error() -> None:
    decision = RecoveryPolicy().decide(
        RuntimeError(
            "development verification failed: web_tests Error [ERR_MODULE_NOT_FOUND]: "
            "Cannot find module repository/web/dist/server/index.js"
        ),
        context="development_verification",
    )

    assert decision.error_class == ErrorClass.VERIFICATION_SETUP
    assert decision.action == RecoveryAction.STOP
    assert decision.responsible_party == "runtime"


def test_missing_validator_binary_is_a_runtime_setup_error_not_a_maker_error() -> None:
    decision = RecoveryPolicy().decide(
        RuntimeError("development verification failed: node_web_lint\nsh: 1: eslint: not found"),
        context="development_verification",
    )

    assert decision.error_class == ErrorClass.VERIFICATION_SETUP
    assert decision.action == RecoveryAction.STOP
    assert decision.responsible_party == "runtime"
    assert decision.retry_allowed is False


def test_dependency_restore_failure_is_never_returned_to_maker() -> None:
    decision = RecoveryPolicy().decide(
        RuntimeError(
            "development verification failed: node_web_dependencies "
            "Missing package from lock file; Usage: npm ci"
        ),
        context="development_verification",
    )

    assert decision.error_class == ErrorClass.VERIFICATION_SETUP
    assert decision.action == RecoveryAction.STOP
    assert decision.responsible_party == "runtime"


def test_windows_sharing_violation_is_runtime_setup_not_authority() -> None:
    error = PermissionError(
        13,
        "[WinError 32] 다른 프로세스가 파일을 사용 중이기 때문에 액세스할 수 없습니다",
    )
    error.winerror = 32

    decision = RecoveryPolicy().decide(error, context="pipeline")

    assert decision.error_class == ErrorClass.VERIFICATION_SETUP
    assert decision.action == RecoveryAction.STOP
    assert decision.responsible_party == "runtime"
    assert decision.retry_allowed is False


@pytest.mark.parametrize(
    "error,error_class",
    [
        (PermissionError("stage has no approved model binding"), ErrorClass.PLATFORM_POLICY),
        (RuntimeError("stale or missing base hash: web/app/page.tsx"), ErrorClass.SOURCE_DRIFT),
    ],
)
def test_policy_and_source_drift_fail_closed(error: Exception, error_class: ErrorClass) -> None:
    decision = RecoveryPolicy().decide(error, context="pipeline")

    assert decision.error_class == error_class
    assert decision.action == RecoveryAction.STOP
    assert decision.retry_allowed is False
