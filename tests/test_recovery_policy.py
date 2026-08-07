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


def test_failed_artifact_verification_returns_to_maker_once() -> None:
    decision = RecoveryPolicy().decide(
        RuntimeError("development verification failed: web_tests\nmissing marker"),
        context="development_verification",
        attempt_number=1,
    )

    assert decision.error_class == ErrorClass.ARTIFACT_VALIDATION
    assert decision.action == RecoveryAction.RETURN_TO_AGENT
    assert decision.responsible_party == "maker"


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
