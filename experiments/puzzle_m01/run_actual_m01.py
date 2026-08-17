"""Run the approved PUZZLE M01 vertical path against a local Godot host."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
from pathlib import Path

from onebrief.godot_quest_execution import GodotM01Request, _sha, execute_godot_m01
from onebrief.godot_topology import GodotTopologyPlan
from onebrief.project_import import ExternalProjectImporter, MANIFEST_NAME
from onebrief.toolpack_lifecycle import ProjectToolPackLifecycle


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--registry-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--godot", type=Path, required=True)
    parser.add_argument(
        "--plan",
        type=Path,
        default=Path(__file__).with_name("topology_plan.json"),
    )
    args = parser.parse_args()

    project_root = args.project_root.resolve()
    registry_root = args.registry_root.resolve()
    output_dir = args.output_dir.resolve()
    godot = args.godot.resolve()
    os.environ["KHALINOS_GODOT_EXECUTABLE"] = str(godot)

    manifest_path = project_root / MANIFEST_NAME
    imported = ExternalProjectImporter(registry_root).import_bytes(
        manifest_path.read_bytes()
    )
    lifecycle = ProjectToolPackLifecycle(
        imported.record.manifest.project_id, registry_root
    )
    generated = lifecycle.generate_and_qualify()
    if generated.qualification is None or generated.qualification.status != "passed":
        raise RuntimeError("PUZZLE Godot ToolPack qualification did not pass")
    approved = lifecycle.approve(generated.qualification.toolpack_sha256)
    if not approved.execution_ready or approved.generated is None:
        raise RuntimeError("PUZZLE Godot ToolPack approval is not execution-ready")

    source_revision = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=project_root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
        shell=False,
    ).stdout.strip()
    plan = GodotTopologyPlan.model_validate_json(
        args.plan.read_text(encoding="utf-8")
    )
    outcome = (project_root / "docs" / "PUZZLE_OUTCOME.md").read_bytes()
    milestone_plan_sha256 = hashlib.sha256(
        outcome + b"\0M01:whole-product-topology"
    ).hexdigest()
    binding = approved.generated.approved_host_executables[0]
    authority_envelope_sha256 = _sha({
        "toolpack_sha256": approved.generated.sha256,
        "allowed_write_prefixes": approved.generated.allowed_write_prefixes,
        "adapter_id": binding.adapter_id.value,
        "executable_sha256": binding.executable_sha256,
        "trusted_compiler_sha256": approved.generated.trusted_component_digests[
            "godot_topology_compiler"
        ],
        "budget_usd": 0,
    })
    result = execute_godot_m01(
        GodotM01Request(
            project_id=imported.record.manifest.project_id,
            source_revision=source_revision,
            toolpack_sha256=approved.generated.sha256,
            milestone_plan_sha256=milestone_plan_sha256,
            authority_envelope_sha256=authority_envelope_sha256,
            plan=plan,
        ),
        registry_root=registry_root,
        output_dir=output_dir,
    )
    print(json.dumps({
        "quest_id": result.quest_contract.quest_id,
        "receipt_id": result.quest_receipt.receipt_id,
        "state": result.quest_receipt.state.value,
        "passed_criterion_ids": result.quest_receipt.passed_criterion_ids,
        "output_dir": result.output_dir,
    }, ensure_ascii=False, indent=2))
    return 0 if result.quest_receipt.state.value == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
