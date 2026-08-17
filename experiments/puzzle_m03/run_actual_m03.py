"""Run PUZZLE M03 from the complete raw M01 to M02 receipt chain."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
from pathlib import Path

from onebrief.godot_gameplay_shaping import GodotGameplayShapingPlan
from onebrief.godot_shaping_execution import GodotM03Request, _sha, execute_godot_m03
from onebrief.project_import import ExternalProjectImporter, MANIFEST_NAME
from onebrief.toolpack_lifecycle import ProjectToolPackLifecycle


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--registry-root", type=Path, required=True)
    parser.add_argument("--m01-result", type=Path, required=True)
    parser.add_argument("--m02-result", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--godot", type=Path, required=True)
    parser.add_argument("--plan", type=Path, default=Path(__file__).with_name("shaping_plan.json"))
    args = parser.parse_args()
    project_root = args.project_root.resolve()
    registry_root = args.registry_root.resolve()
    os.environ["KHALINOS_GODOT_EXECUTABLE"] = str(args.godot.resolve())
    imported = ExternalProjectImporter(registry_root).import_bytes((project_root / MANIFEST_NAME).read_bytes())
    lifecycle = ProjectToolPackLifecycle(imported.record.manifest.project_id, registry_root)
    generated = lifecycle.generate_and_qualify()
    if generated.qualification is None or generated.qualification.status != "passed":
        raise RuntimeError("PUZZLE M03 ToolPack qualification did not pass")
    approved = lifecycle.approve(generated.qualification.toolpack_sha256)
    if not approved.execution_ready or approved.generated is None:
        raise RuntimeError("PUZZLE M03 ToolPack is not execution-ready")
    source_revision = subprocess.run(["git", "rev-parse", "HEAD"], cwd=project_root, capture_output=True, text=True, check=True).stdout.strip()
    outcome = (project_root / "docs" / "PUZZLE_OUTCOME.md").read_bytes()
    milestone_plan_sha256 = hashlib.sha256(outcome + b"\0M03:rule-complete-shaping-layer").hexdigest()
    binding = approved.generated.approved_host_executables[0]
    authority = _sha({
        "toolpack_sha256": approved.generated.sha256,
        "allowed_write_prefixes": approved.generated.allowed_write_prefixes,
        "adapter_id": binding.adapter_id.value,
        "executable_sha256": binding.executable_sha256,
        "trusted_compiler_sha256": approved.generated.trusted_component_digests["godot_gameplay_shaping_compiler"],
        "budget_usd": 0,
        "preserve_receipt_chain": True,
    })
    result = execute_godot_m03(
        GodotM03Request(
            project_id=imported.record.manifest.project_id,
            source_revision=source_revision,
            toolpack_sha256=approved.generated.sha256,
            milestone_plan_sha256=milestone_plan_sha256,
            authority_envelope_sha256=authority,
            predecessor_result_dirs=[str(args.m01_result.resolve()), str(args.m02_result.resolve())],
            plan=GodotGameplayShapingPlan.model_validate_json(args.plan.read_text(encoding="utf-8")),
        ), registry_root=registry_root, output_dir=args.output_dir.resolve(),
    )
    print(json.dumps({
        "quest_id": result.quest_contract.quest_id,
        "parent_quest_id": result.quest_contract.parent_quest_id,
        "receipt_id": result.quest_receipt.receipt_id,
        "state": result.quest_receipt.state.value,
        "passed_criterion_ids": result.quest_receipt.passed_criterion_ids,
        "preserved_receipt_ids": result.quest_receipt.preserved_receipt_ids,
        "output_dir": result.output_dir,
    }, ensure_ascii=False, indent=2))
    return 0 if result.quest_receipt.state.value == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
