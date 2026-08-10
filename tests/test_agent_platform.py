from __future__ import annotations

import pytest

from onebrief.agent_platform import AgentPlatformSettings, validate_approved_job_uri


def test_settings_are_narrow_and_configurable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ONEBRIEF_PROJECT", "project-x")
    monkeypatch.setenv("ONEBRIEF_JOB_BUCKET", "bucket-x")
    settings = AgentPlatformSettings.from_env()
    assert settings.project == "project-x"
    assert settings.bucket == "bucket-x"
    assert settings.cloud_run_job == "onebrief-worker"


def test_approved_repository_rejects_other_bucket() -> None:
    settings = AgentPlatformSettings(
        project="p",
        region="asia-northeast3",
        bucket="approved",
        cloud_run_job="onebrief-worker",
        model="gemini-3.5-flash",
    )
    with pytest.raises(PermissionError, match="approved OneBrief bucket"):
        validate_approved_job_uri(
            "gs://other/jobs/12345678-1234-1234-1234-123456789abc", settings
        )


def test_approved_repository_rejects_non_job_namespace() -> None:
    settings = AgentPlatformSettings(
        project="p",
        region="asia-northeast3",
        bucket="approved",
        cloud_run_job="onebrief-worker",
        model="gemini-3.5-flash",
    )
    with pytest.raises(PermissionError, match="jobs namespace"):
        validate_approved_job_uri("gs://approved/arbitrary/input", settings)


def test_approved_uri_is_preserved() -> None:
    settings = AgentPlatformSettings(
        project="p",
        region="asia-northeast3",
        bucket="approved",
        cloud_run_job="onebrief-worker",
        model="gemini-3.5-flash",
    )
    uri = "gs://approved/jobs/12345678-1234-1234-1234-123456789abc"
    assert validate_approved_job_uri(uri, settings) == uri
