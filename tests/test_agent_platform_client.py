from __future__ import annotations

from types import SimpleNamespace

import pytest

from onebrief.agent_platform_client import dispatch_approved_job_via_agent_platform


class FakeEngine:
    def create_session(self, **kwargs):
        assert kwargs == {"user_id": "user-1"}
        return {"id": "managed-session-1"}

    def stream_query(self, **kwargs):
        assert kwargs["session_id"] == "managed-session-1"
        yield {"content": {"function_response": {
            "job_uri": kwargs["message"].split('"job_uri": "', 1)[1].split('"', 1)[0],
            "operation_name": "operations/run-1",
        }}}


def test_dispatch_requires_matching_tool_receipt() -> None:
    client = SimpleNamespace(
        agent_engines=SimpleNamespace(get=lambda name: FakeEngine())
    )
    receipt = dispatch_approved_job_via_agent_platform(
        resource_name="projects/p/locations/r/reasoningEngines/1",
        job_uri="gs://bucket/jobs/12345678-1234-1234-1234-123456789abc",
        user_id="user-1",
        project="p",
        location="r",
        client=client,
    )
    assert receipt.agent_engine_session == "managed-session-1"
    assert receipt.cloud_run_operation == "operations/run-1"


def test_dispatch_fails_closed_without_operation() -> None:
    engine = FakeEngine()
    engine.stream_query = lambda **kwargs: iter([{"text": "done"}])
    client = SimpleNamespace(agent_engines=SimpleNamespace(get=lambda name: engine))
    with pytest.raises(RuntimeError, match="no matching"):
        dispatch_approved_job_via_agent_platform(
            resource_name="engine",
            job_uri="gs://bucket/jobs/12345678-1234-1234-1234-123456789abc",
            user_id="user-1",
            project="p",
            location="r",
            client=client,
        )


def test_dispatch_returns_on_tool_receipt_without_consuming_stream_tail() -> None:
    class ReceiptThenHangEngine:
        def create_session(self, **kwargs):
            return {"id": "managed-session-fast"}

        def stream_query(self, **kwargs):
            yield {"job_uri": kwargs["message"].split('"job_uri": "', 1)[1].split('"', 1)[0],
                   "operation_name": "operations/run-fast"}
            raise AssertionError("irrelevant stream tail was consumed")

    client = SimpleNamespace(
        agent_engines=SimpleNamespace(get=lambda name: ReceiptThenHangEngine())
    )
    receipt = dispatch_approved_job_via_agent_platform(
        resource_name="engine",
        job_uri="gs://bucket/jobs/12345678-1234-1234-1234-123456789abc",
        user_id="user-1",
        project="p",
        location="r",
        client=client,
    )
    assert receipt.cloud_run_operation == "operations/run-fast"
    assert receipt.event_count == 1


def test_dispatch_fails_closed_without_managed_session_id() -> None:
    engine = FakeEngine()
    engine.create_session = lambda **kwargs: {"user_id": kwargs["user_id"]}
    client = SimpleNamespace(agent_engines=SimpleNamespace(get=lambda name: engine))
    with pytest.raises(RuntimeError, match="no identifiable managed session"):
        dispatch_approved_job_via_agent_platform(
            resource_name="engine",
            job_uri="gs://bucket/jobs/12345678-1234-1234-1234-123456789abc",
            user_id="user-1",
            project="p",
            location="r",
            client=client,
        )
