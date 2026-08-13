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
        or (
            "unity visual evidence" in normalized
            and "unity visual test contract" not in normalized
        )
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
    elif "unity visual test contract" in normalized:
        # Static checks are themselves a finite ladder. Reaching camera-backed
        # capture and then exposing overlay/viewport defects is later progress
        # than still reading the system framebuffer. Treating both as the same
        # stage let the older one-clause failure win merely because it reported
        # fewer blockers, so continuation regressed to an already-fixed state.
        if any(marker in normalized for marker in (
            "does not capture screenspaceoverlay ui",
            "responsive unity batchmode evidence must pass each requested viewport",
        )):
            stage = 3
        else:
            stage = 2
    elif "static" in normalized and "verification" in normalized:
        stage = 2
    elif "development verification failed" in normalized:
        stage = 3
    else:
        stage = 1

    clauses = [item.strip() for item in normalized.split(" | ") if item.strip()]
    audit_prefixes = (
        "rejected repair delta",
        "regression guard from the rejected attempt:",
        "repair control:",
        "previous failed verifier evidence:",
    )
    # Audit/control suffixes explain why an edit was rejected, but they are not
    # additional defects in the current product. Counting them as blockers made
    # an old one-line failure outrank a newer candidate that reached the same
    # trusted stage and carried richer rejection history.
    blockers = max(
        1,
        len([
            clause for clause in clauses
            if not clause.startswith(audit_prefixes)
        ]),
    )
    # At the same stage, fewer blockers are better.
    return stage, -blockers


def should_repair_regression_candidate(
    *, checkpoint_failure: str, attempted_failure: str,
) -> bool:
    """Keep a failed functional delta only long enough to repair its compiler error.

    The last runtime-valid checkpoint remains the rollback candidate.  However,
    returning the maker to that checkpoint before exposing the exact compiler
    diagnostic makes it recreate the same broken functional delta repeatedly.
    """

    checkpoint_stage, _ = development_failure_quality(checkpoint_failure)
    attempted_stage, _ = development_failure_quality(attempted_failure)
    return attempted_stage == 6 and checkpoint_stage > attempted_stage
