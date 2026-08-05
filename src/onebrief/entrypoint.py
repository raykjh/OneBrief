"""User-facing CLI boundary with concise safety failures."""

from __future__ import annotations

import sys

from onebrief.budget_guard import BudgetGuardError
from onebrief.cli import main


def entrypoint() -> None:
    try:
        main()
    except BudgetGuardError as exc:
        print(f"BLOCKED: {exc}", file=sys.stderr)
        raise SystemExit(2) from None

