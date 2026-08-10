from __future__ import annotations

from types import SimpleNamespace

import pytest

from onebrief.agent_platform_client import dispatch_approved_job_via_agent_platform


class FakeEngine:
    def stream_query(self, **kwargs):
        assert "session_id" not in kwargs
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
    assert receipt.agent_engine_session.startswith("ephemeral-")
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
