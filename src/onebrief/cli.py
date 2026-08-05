"""Minimal non-chat interface for OneBrief intake analysis."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from onebrief.runner import analyze_requirements
from onebrief.schemas import IntakeRequest


def main() -> None:
    parser = argparse.ArgumentParser(prog="onebrief")
    subparsers = parser.add_subparsers(dest="command", required=True)
    analyze = subparsers.add_parser("analyze", help="analyze one intake JSON file")
    analyze.add_argument("input", type=Path)
    analyze.add_argument("--output", type=Path)
    args = parser.parse_args()

    intake = IntakeRequest.model_validate_json(args.input.read_text(encoding="utf-8"))
    result = asyncio.run(analyze_requirements(intake))
    rendered = result.model_dump_json(indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    else:
        print(rendered)


if __name__ == "__main__":
    main()

