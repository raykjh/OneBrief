"""Resume any approved OneBrief cloud job without changing its authority."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source_job_uri")
    parser.add_argument("--resource", required=True)
    parser.add_argument("--projects-root", type=Path, required=True)
    parser.add_argument("--jobs-dir", type=Path, required=True)
    parser.add_argument("--project", default="onebrief-agent-20260805")
    parser.add_argument("--location", default="asia-northeast3")
    parser.add_argument("--bucket", default="onebrief-agent-20260805-jobs")
    parser.add_argument("--cloud-run-job", default="onebrief-worker")
    parser.add_argument("--user-id", default="onebrief-continuation")
    parser.add_argument("--additional-approved-usd", type=float)
    parser.add_argument("--targeted-repair", action="store_true")
    parser.add_argument("--low-cost-targeted-repair", action="store_true")
    parser.add_argument("--reverify-existing-candidate", action="store_true")
    args = parser.parse_args()

    os.environ.update({
        "ONEBRIEF_PROJECTS_ROOT": str(args.projects_root.resolve()),
        "GOOGLE_CLOUD_PROJECT": args.project,
        "ONEBRIEF_REGION": args.location,
        "ONEBRIEF_CLOUD_RUN_JOB": args.cloud_run_job,
    })
    from onebrief.cloud_continuation import continue_cloud_job_via_agent_platform

    receipt = continue_cloud_job_via_agent_platform(
        source_job_uri=args.source_job_uri,
        jobs_dir=args.jobs_dir.resolve(),
        bucket=args.bucket,
        agent_engine_resource=args.resource,
        project=args.project,
        location=args.location,
        user_id=args.user_id,
        explicit_child_approval_usd=args.additional_approved_usd,
        targeted_repair=args.targeted_repair,
        low_cost_targeted_repair=args.low_cost_targeted_repair,
        reverify_existing_candidate=args.reverify_existing_candidate,
    )
    print(json.dumps({
        **receipt.__dict__,
        "dispatch": receipt.dispatch.__dict__,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
