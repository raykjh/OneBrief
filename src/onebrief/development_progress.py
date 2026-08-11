"""Deterministic ranking for failed software candidates.

The rank is intentionally coarse.  A candidate that reached browser observation
has passed more of the isolated tool chain than one that stopped at lint/build,
and fewer reported blockers at the same stage is better.  The model never gets
to declare its own progress.
"""

from __future__ import annotations


def development_failure_quality(message: str) -> tuple[int, int]:
    """Return a sortable quality where larger values represent more progress."""
    normalized = " ".join(message.split()).casefold()
    if "web observation failed" in normalized:
        stage = 4
    elif "http" in normalized and "failed" in normalized:
        stage = 3
    elif "development verification failed" in normalized:
        stage = 2
    else:
        stage = 1

    if " | " in normalized:
        blockers = len([item for item in normalized.split(" | ") if item.strip()])
    else:
        blockers = 1
    # At the same stage, fewer blockers are better.
    return stage, -blockers

