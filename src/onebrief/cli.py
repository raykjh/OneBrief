"""Non-chat interface for OneBrief intake, approval, and guarded execution."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

from onebrief.budget_guard import BudgetStore, micros_to_dollars
from onebrief.cloud_jobs import (
    GCSJobStore,
    run_local_capability_worker,
    run_cloud_worker,
    submit_cloud_job,
)
from onebrief.execution_pipeline import ExecutionPipeline
from onebrief.guarded_gemini import BudgetedGeminiClient
from onebrief.jobs import JobStatus, JobStore, create_job, run_job, start_background_job
from onebrief.producer import estimate_budget
from onebrief.runner import analyze_requirements, reinspect_requirements
from onebrief.schemas import BudgetEnvelope, IntakeRequest, RequirementsAnalysis, UploadManifest
from onebrief.source_loader import load_uploads, source_records


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text + "\n", encoding="utf-8")


def _ledger_summary(store: BudgetStore) -> dict[str, object]:
    ledger = store.read()
    approved = ledger.approval.approved_usd_micros
    remaining = approved - ledger.actual_usd_micros - ledger.reserved_usd_micros
    return {
        "run_id": ledger.run_id,
        "status": ledger.status.value,
        "revision": ledger.revision,
        "approved_usd": micros_to_dollars(approved),
        "actual_usd": micros_to_dollars(ledger.actual_usd_micros),
        "reserved_usd": micros_to_dollars(ledger.reserved_usd_micros),
        "remaining_usd": micros_to_dollars(remaining),
        "calls": len(ledger.entries),
        "phases": {
            phase: {
                key: (
                    micros_to_dollars(value)
                    if key.endswith("usd_micros") else value
                )
                for key, value in values.items()
            }
            for phase, values in store.phase_summary().items()
        },
    }


def _load_execution_inputs(
    intake_path: Path,
    requirements_path: Path,
    manifest_path: Path,
) -> tuple[IntakeRequest, RequirementsAnalysis, list]:
    intake = IntakeRequest.model_validate_json(intake_path.read_text(encoding="utf-8"))
    requirements = RequirementsAnalysis.model_validate_json(
        requirements_path.read_text(encoding="utf-8")
    )
    manifest = UploadManifest.model_validate_json(manifest_path.read_text(encoding="utf-8"))
    uploads = load_uploads(manifest, manifest_path.parent)
    return intake, requirements, [*intake.internal_sources, *uploads]


def main() -> None:
    parser = argparse.ArgumentParser(prog="onebrief")
    subparsers = parser.add_subparsers(dest="command", required=True)

    analyze = subparsers.add_parser("analyze", help="analyze one intake JSON file")
    analyze.add_argument("input", type=Path)
    analyze.add_argument("--output", type=Path)

    prepare = subparsers.add_parser(
        "prepare",
        help="load sources, reinspect requirements, and estimate execution",
    )
    prepare.add_argument("input", type=Path)
    prepare.add_argument("previous_analysis", type=Path)
    prepare.add_argument("upload_manifest", type=Path)
    prepare.add_argument("--output-dir", type=Path, required=True)

    reestimate = subparsers.add_parser(
        "estimate",
        help="recalculate execution cost from an already passed reinspection without Gemini",
    )
    reestimate.add_argument("input", type=Path)
    reestimate.add_argument("requirements", type=Path)
    reestimate.add_argument("upload_manifest", type=Path)
    reestimate.add_argument("--output", type=Path, required=True)

    approve = subparsers.add_parser("approve", help="create one immutable run approval")
    approve.add_argument("budget_estimate", type=Path)
    approve.add_argument("--run-dir", type=Path, required=True)
    approval_choice = approve.add_mutually_exclusive_group(required=True)
    approval_choice.add_argument("--recommended", action="store_true")
    approval_choice.add_argument("--amount", type=float)

    execute = subparsers.add_parser(
        "execute",
        help="run analyst, writer, verifier, and revision agents through the budget gateway",
    )
    execute.add_argument("input", type=Path)
    execute.add_argument("requirements", type=Path)
    execute.add_argument("upload_manifest", type=Path)
    execute.add_argument("--run-dir", type=Path, required=True)
    execute.add_argument("--output-dir", type=Path, required=True)


    job_create = subparsers.add_parser(
        "job-create",
        help="snapshot inputs and approved budget into one durable queued job",
    )
    job_create.add_argument("input", type=Path)
    job_create.add_argument("requirements", type=Path)
    job_create.add_argument("upload_manifest", type=Path)
    job_create.add_argument("budget_estimate", type=Path)
    job_create.add_argument("--jobs-dir", type=Path, required=True)
    job_approval = job_create.add_mutually_exclusive_group(required=True)
    job_approval.add_argument("--recommended", action="store_true")
    job_approval.add_argument("--amount", type=float)

    job_start = subparsers.add_parser(
        "job-start",
        help="start one queued job in a detached background worker",
    )
    job_start.add_argument("job_dir", type=Path)

    job_worker = subparsers.add_parser(
        "job-worker",
        help="execute one queued job in the current process",
    )
    job_worker.add_argument("job_dir", type=Path)

    job_status = subparsers.add_parser(
        "job-status",
        help="show durable job state and result package location",
    )
    job_status.add_argument("job_dir", type=Path)


    cloud_submit = subparsers.add_parser(
        "cloud-submit",
        help="upload one queued job to GCS and start the Cloud Run Job asynchronously",
    )
    cloud_submit.add_argument("job_dir", type=Path)
    cloud_submit.add_argument("--bucket", required=True)
    cloud_submit.add_argument("--project", required=True)
    cloud_submit.add_argument("--region", default="asia-northeast3")
    cloud_submit.add_argument("--cloud-run-job", default="onebrief-worker")

    cloud_status = subparsers.add_parser(
        "cloud-status",
        help="read the durable job state directly from Cloud Storage",
    )
    cloud_status.add_argument("job_uri")

    cloud_download = subparsers.add_parser(
        "cloud-download",
        help="download a completed result package from Cloud Storage",
    )
    cloud_download.add_argument("job_uri")
    cloud_download.add_argument("--output-dir", type=Path, required=True)

    cloud_worker = subparsers.add_parser(
        "cloud-worker",
        help="Cloud Run Job entry point; downloads, executes, and uploads one job",
    )
    cloud_worker.add_argument("--job-uri")

    capability_worker = subparsers.add_parser(
        "capability-worker",
        help="verify a managed runtime handoff and execute its approved local adapters",
    )
    capability_worker.add_argument("job_uri")

    status = subparsers.add_parser("status", help="show approved, used, reserved, and remaining")
    status.add_argument("run_dir", type=Path)

    guarded = subparsers.add_parser("guarded-call", help="run one budgeted Gemini text call")
    guarded.add_argument("run_dir", type=Path)
    guarded.add_argument("--stage", required=True)
    guarded.add_argument("--model", default="gemini-3.5-flash-lite")
    guarded.add_argument("--prompt", required=True)
    guarded.add_argument("--max-output-tokens", required=True, type=int)

    complete = subparsers.add_parser("complete", help="close a run with no pending calls")
    complete.add_argument("run_dir", type=Path)

    args = parser.parse_args()
    if args.command == "analyze":
        intake = IntakeRequest.model_validate_json(args.input.read_text(encoding="utf-8"))
        result = asyncio.run(analyze_requirements(intake))
        rendered = result.model_dump_json(indent=2)
        _write(args.output, rendered) if args.output else print(rendered)
        return

    if args.command == "prepare":
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
        return

    if args.command == "estimate":
        intake, requirements, sources = _load_execution_inputs(
            args.input,
            args.requirements,
            args.upload_manifest,
        )
        augmented = intake.model_copy(update={"internal_sources": sources})
        budget = estimate_budget(augmented, requirements)
        _write(args.output, budget.model_dump_json(indent=2))
        print(f"{budget.status.value}: approve ${budget.recommended_approval_usd:.4f}")
        return

    if args.command == "approve":
        estimate = BudgetEnvelope.model_validate_json(args.budget_estimate.read_text(encoding="utf-8"))
        amount = estimate.recommended_approval_usd if args.recommended else args.amount
        BudgetStore(args.run_dir).approve(estimate, amount)
        print(json.dumps(_ledger_summary(BudgetStore(args.run_dir)), indent=2))
        return

    if args.command == "execute":
        intake, requirements, sources = _load_execution_inputs(
            args.input,
            args.requirements,
            args.upload_manifest,
        )
        checkpoint = ExecutionPipeline(args.run_dir).run(
            intake=intake,
            requirements=requirements,
            sources=sources,
            output_dir=args.output_dir,
        )
        print(checkpoint.model_dump_json(indent=2))
        print(json.dumps(_ledger_summary(BudgetStore(args.run_dir)), indent=2))
        return


    if args.command == "job-create":
        intake, requirements, sources = _load_execution_inputs(
            args.input,
            args.requirements,
            args.upload_manifest,
        )
        estimate = BudgetEnvelope.model_validate_json(
            args.budget_estimate.read_text(encoding="utf-8")
        )
        amount = estimate.recommended_approval_usd if args.recommended else args.amount
        job_dir = create_job(
            jobs_dir=args.jobs_dir,
            intake=intake,
            requirements=requirements,
            sources=sources,
            estimate=estimate,
            approved_usd=amount,
        )
        print(JobStore(job_dir).read().model_dump_json(indent=2))
        print(f"job_dir={job_dir}")
        return

    if args.command == "job-start":
        record = start_background_job(args.job_dir)
        print(record.model_dump_json(indent=2))
        return

    if args.command == "job-worker":
        record = run_job(args.job_dir)
        print(record.model_dump_json(indent=2))
        return

    if args.command == "job-status":
        record = JobStore(args.job_dir).read()
        print(record.model_dump_json(indent=2))
        checkpoint_path = args.job_dir / "work" / "execution_checkpoint.json"
        if checkpoint_path.exists():
            print("pipeline_checkpoint=")
            print(checkpoint_path.read_text(encoding="utf-8").rstrip())
        if record.result_package:
            print(f"result_package={args.job_dir / record.result_package}")
        return


    if args.command == "cloud-submit":
        receipt = submit_cloud_job(
            args.job_dir,
            bucket=args.bucket,
            project=args.project,
            region=args.region,
            cloud_run_job=args.cloud_run_job,
        )
        print(receipt.model_dump_json(indent=2))
        return

    if args.command == "cloud-status":
        record = GCSJobStore(args.job_uri).read_job()
        print(record.model_dump_json(indent=2))
        return

    if args.command == "cloud-download":
        result = GCSJobStore(args.job_uri).download_result(args.output_dir)
        print(f"result_package={result}")
        return

    if args.command == "cloud-worker":
        job_uri = args.job_uri or os.environ.get("ONEBRIEF_JOB_URI")
        if not job_uri:
            raise ValueError("cloud-worker requires --job-uri or ONEBRIEF_JOB_URI")
        record = run_cloud_worker(job_uri)
        print(record.model_dump_json(indent=2))
        if record.status == JobStatus.FAILED:
            raise SystemExit(1)
        return

    if args.command == "capability-worker":
        record = run_local_capability_worker(args.job_uri)
        print(record.model_dump_json(indent=2))
        if record.status == JobStatus.FAILED:
            raise SystemExit(1)
        return

    if args.command == "status":
        print(json.dumps(_ledger_summary(BudgetStore(args.run_dir)), indent=2))
        return

    if args.command == "guarded-call":
        result = BudgetedGeminiClient(args.run_dir).generate_text(
            stage=args.stage,
            model=args.model,
            contents=args.prompt,
            max_output_tokens=args.max_output_tokens,
        )
        print(result)
        print(json.dumps(_ledger_summary(BudgetStore(args.run_dir)), indent=2))
        return

    BudgetStore(args.run_dir).complete()
    print(json.dumps(_ledger_summary(BudgetStore(args.run_dir)), indent=2))


if __name__ == "__main__":
    main()
