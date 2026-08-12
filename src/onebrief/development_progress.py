"""Deterministic ranking for failed software candidates.

The rank is intentionally coarse.  A candidate that reached browser observation
has passed more of the isolated tool chain than one that stopped at lint/build,
and fewer reported blockers at the same stage is better.  The model never gets
to declare its own progress.
"""

from __future__ import annotations


def development_failure_quality(message: str) -> tuple[int, int]:
    """Return a sortable quality where larger values represent more progress.

    Verification failures are checkpoints, not equivalent errors.  A candidate
    that compiled and reached a real Unity PlayMode assertion must never be
    replaced by one that only failed the static visual contract.  Keep the
    ordering deterministic and based on verifier-owned stage names.
    """
    normalized = " ".join(message.split()).casefold()
    if "independent unity semantic visual observation failed" in normalized:
        stage = 9
    elif (
        "unity runtime evidence" in normalized
        or "unity visual evidence requires a distinct rendered scenario" in normalized
        or "unity visual evidence reused an identical screenshot" in normalized
        or "runtime evidence validation" in normalized
        or "unity visual scenario" in normalized
        or (
            "viewport" in normalized
            and "png" in normalized
            and "match" in normalized
        )
    ):
        stage = 8
    elif (
        "unity_playmode_visual_tests" in normalized
        or "unity test failures" in normalized
        or "test run completed. exiting with code 2" in normalized
    ):
        stage = 7
    elif "unity_compile" in normalized:
        stage = 6
    elif "web observation failed" in normalized:
        stage = 5
    elif "http" in normalized and "failed" in normalized:
        stage = 4
    elif (
        "unity visual test contract" in normalized
        or "static" in normalized and "verification" in normalized
    ):
        stage = 2
    elif "development verification failed" in normalized:
        stage = 3
    else:
        stage = 1

    if " | " in normalized:
        blockers = len([item for item in normalized.split(" | ") if item.strip()])
    else:
        blockers = 1
    # At the same stage, fewer blockers are better.
    return stage, -blockers
