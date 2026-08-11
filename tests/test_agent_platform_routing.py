from __future__ import annotations

from types import SimpleNamespace

from onebrief.agent_platform import return_tool_receipt_without_summarization


def test_cloud_run_receipt_skips_redundant_model_summarization() -> None:
    context = SimpleNamespace(actions=SimpleNamespace(skip_summarization=False))
    return_tool_receipt_without_summarization(
        tool=SimpleNamespace(name="start_approved_onebrief_job"),
        args={"job_uri": "gs://approved/jobs/id"},
        tool_context=context,
        tool_response={"operation_name": "operations/1"},
    )
    assert context.actions.skip_summarization is True


def test_inspection_receipt_keeps_normal_summarization() -> None:
    context = SimpleNamespace(actions=SimpleNamespace(skip_summarization=False))
    return_tool_receipt_without_summarization(
        tool=SimpleNamespace(name="inspect_onebrief_job"),
        args={},
        tool_context=context,
        tool_response={"managed": True},
    )
    assert context.actions.skip_summarization is False
