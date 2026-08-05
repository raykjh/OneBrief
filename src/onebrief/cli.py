"""Minimal non-chat interface for OneBrief intake and preparation."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from onebrief.producer import estimate_budget
from onebrief.runner import analyze_requirements, reinspect_requirements
from onebrief.schemas import IntakeRequest, RequirementsAnalysis, UploadManifest
from onebrief.source_loader import load_uploads, source_records


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(prog="onebrief")
    subparsers = parser.add_subparsers(dest="command", required=True)

    analyze = subparsers.add_parser("analyze", help="analyze one intake JSON file")
    analyze.add_argument("input", type=Path)
    analyze.add_argument("--output", type=Path)

    prepare = subparsers.add_parser(
        "prepare",
        help="load required sources, reinspect requirements, and estimate the approved work",
    )
    prepare.add_argument("input", type=Path, help="original intake JSON")
    prepare.add_argument("previous_analysis", type=Path)
    prepare.add_argument("upload_manifest", type=Path)
    prepare.add_argument("--output-dir", type=Path, required=True)

    args = parser.parse_args()
    if args.command == "analyze":
        intake = IntakeRequest.model_validate_json(args.input.read_text(encoding="utf-8"))
        result = asyncio.run(analyze_requirements(intake))
        rendered = result.model_dump_json(indent=2)
        if args.output:
            _write(args.output, rendered)
        else:
            print(rendered)
        return

    intake = IntakeRequest.model_validate_json(args.input.read_text(encoding="utf-8"))
    previous = RequirementsAnalysis.model_validate_json(
        args.previous_analysis.read_text(encoding="utf-8")
    )
    manifest = UploadManifest.model_validate_json(args.upload_manifest.read_text(encoding="utf-8"))
    uploads = load_uploads(manifest, args.upload_manifest.parent)
    augmented = intake.model_copy(update={"internal_sources": [*intake.internal_sources, *uploads]})
    reinspection = asyncio.run(reinspect_requirements(augmented, previous))

    _write(
        args.output_dir / "source_manifest.json",
        json.dumps(
            [record.model_dump(mode="json") for record in source_records(uploads)],
            ensure_ascii=False,
            indent=2,
        ),
    )
    _write(args.output_dir / "requirements_reinspection.json", reinspection.model_dump_json(indent=2))
    if not reinspection.ready_for_estimate:
        print("NEEDS_INFORMATION")
        return

    budget = estimate_budget(augmented, reinspection)
    _write(args.output_dir / "budget_estimate.json", budget.model_dump_json(indent=2))
    print(f"{budget.status.value}: approve ${budget.recommended_approval_usd:.4f}")


if __name__ == "__main__":
    main()

