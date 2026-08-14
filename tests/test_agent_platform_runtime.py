from __future__ import annotations

from agent_runtime.onebrief_runtime import build_agent_engine_app


def test_deployed_runtime_uses_agent_platform_managed_sessions() -> None:
    app = build_agent_engine_app()

    assert app._tmpl_attrs["session_service_builder"] is None
    assert app._tmpl_attrs["enable_tracing"] is True
