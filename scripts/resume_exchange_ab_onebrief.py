"""Resume the Exchange A/B run through Agent Platform without expanding budget."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source_job_uri")
    parser.add_argument("--resource", required=True)
    parser.add_argument("--project", default="onebrief-agent-20260805")
    parser.add_argument("--location", default="asia-northeast3")
    parser.add_argument("--bucket", default="onebrief-agent-20260805-jobs")
    parser.add_argument("--additional-approved-usd", type=float)
    parser.add_argument("--targeted-repair", action="store_true")
    parser.add_argument("--low-cost-targeted-repair", action="store_true")
    parser.add_argument("--reverify-existing-candidate", action="store_true")
    args = parser.parse_args()

    run_root = ROOT / "benchmarks" / "exchange-release-candidate"
    os.environ.update({
        "ONEBRIEF_PROJECTS_ROOT": str(run_root / "registry"),
        "GOOGLE_CLOUD_PROJECT": args.project,
        "ONEBRIEF_REGION": args.location,
        "ONEBRIEF_CLOUD_RUN_JOB": "onebrief-worker",
    })
    from onebrief.cloud_continuation import continue_cloud_job_via_agent_platform

    receipt = continue_cloud_job_via_agent_platform(
        source_job_uri=args.source_job_uri,
        jobs_dir=run_root / "continuation-jobs",
        bucket=args.bucket,
        agent_engine_resource=args.resource,
        project=args.project,
        location=args.location,
        user_id="exchange-ab-evaluator",
        explicit_child_approval_usd=args.additional_approved_usd,
        targeted_repair=args.targeted_repair,
        low_cost_targeted_repair=args.low_cost_targeted_repair,
        reverify_existing_candidate=args.reverify_existing_candidate,
    )
    payload = {
        "schema_version": "onebrief-exchange-ab-continuation-receipt-v1",
        "created_at": datetime.now(UTC).isoformat(),
        **receipt.__dict__,
        "dispatch": receipt.dispatch.__dict__,
    }
    output = run_root / "onebrief-continuation.json"
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", "utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
