"""Run the final PUZZLE M99 audit without modifying product bytes."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
from pathlib import Path

from onebrief.godot_final_audit import GodotM99Request, _sha, execute_godot_m99
from onebrief.project_import import ExternalProjectImporter, MANIFEST_NAME
from onebrief.toolpack_lifecycle import ProjectToolPackLifecycle


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--registry-root", type=Path, required=True)
    parser.add_argument("--m01-result", type=Path, required=True)
    parser.add_argument("--m02-result", type=Path, required=True)
    parser.add_argument("--m03-result", type=Path, required=True)
    parser.add_argument("--m04-result", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--godot", type=Path, required=True)
    args = parser.parse_args()
    project_root = args.project_root.resolve()
    registry_root = args.registry_root.resolve()
    os.environ["KHALINOS_GODOT_EXECUTABLE"] = str(args.godot.resolve())
    imported = ExternalProjectImporter(registry_root).import_bytes((project_root / MANIFEST_NAME).read_bytes())
    lifecycle = ProjectToolPackLifecycle(imported.record.manifest.project_id, registry_root)
    generated = lifecycle.generate_and_qualify()
    approved = lifecycle.approve(generated.qualification.toolpack_sha256)
    source_revision = subprocess.run(["git", "rev-parse", "HEAD"], cwd=project_root, capture_output=True, text=True, check=True).stdout.strip()
    outcome = (project_root / "docs" / "PUZZLE_OUTCOME.md").read_bytes()
    milestone_plan_sha256 = hashlib.sha256(outcome + b"\0M99:whole-product-independent-audit").hexdigest()
    authority = _sha({"toolpack_sha256": approved.generated.sha256, "source_revision": source_revision, "final_verification_only": True, "budget_usd": 0})
    result = execute_godot_m99(
        GodotM99Request(
            project_id=imported.record.manifest.project_id, source_revision=source_revision,
            toolpack_sha256=approved.generated.sha256, milestone_plan_sha256=milestone_plan_sha256,
            authority_envelope_sha256=authority,
            predecessor_result_dirs=[str(args.m01_result.resolve()), str(args.m02_result.resolve()), str(args.m03_result.resolve()), str(args.m04_result.resolve())],
        ), registry_root=registry_root, output_dir=args.output_dir.resolve(),
    )
    print(json.dumps({"quest_id": result.quest_contract.quest_id, "receipt_id": result.quest_receipt.receipt_id, "state": result.quest_receipt.state.value, "passed_criterion_ids": result.quest_receipt.passed_criterion_ids, "release_executable": result.release_executable, "output_dir": result.output_dir}, ensure_ascii=False, indent=2))
    return 0 if result.quest_receipt.state.value == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
