"""Create, collect, integrate, and revalidate a OneBrief parallel campaign."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from onebrief.parallel_campaign import (  # noqa: E402
    CampaignDefinition,
    LaneEvidence,
    ParallelCampaignStore,
    RevalidationReceipt,
)


def main() -> None:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create")
    create.add_argument("definition", type=Path)
    create.add_argument("campaign_root", type=Path)
    create.add_argument("--repo-root", type=Path, default=ROOT)
    record = commands.add_parser("record")
    record.add_argument("campaign_root", type=Path)
    record.add_argument("evidence", type=Path)
    integrate = commands.add_parser("integrate")
    integrate.add_argument("campaign_root", type=Path)
    revalidate = commands.add_parser("revalidate")
    revalidate.add_argument("campaign_root", type=Path)
    revalidate.add_argument("receipt", type=Path)
    args = parser.parse_args()
    store = ParallelCampaignStore(args.campaign_root)
    if args.command == "create":
        definition = CampaignDefinition.model_validate_json(
            args.definition.read_text(encoding="utf-8")
        )
        print(store.create(repo_root=args.repo_root, definition=definition).model_dump_json(indent=2))
    elif args.command == "record":
        evidence = LaneEvidence.model_validate_json(args.evidence.read_text(encoding="utf-8"))
        print(store.record_evidence(evidence))
    elif args.command == "integrate":
        print(store.integrate().model_dump_json(indent=2))
    else:
        receipt = RevalidationReceipt.model_validate_json(
            args.receipt.read_text(encoding="utf-8")
        )
        print(store.record_revalidation(receipt).model_dump_json(indent=2))


if __name__ == "__main__":
    main()
