"""Run the fixed Exchange goal as a single-pass Gemini software-maker baseline."""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

RUN_ROOT = ROOT / "benchmarks" / "exchange-release-candidate"
PROJECT_ROOT = RUN_ROOT / "single-agent-baseline"
REGISTRY = RUN_ROOT / "registry-baseline"


def main() -> None:
    os.environ.update({
        "GOOGLE_GENAI_USE_VERTEXAI": "TRUE",
        "GOOGLE_CLOUD_PROJECT": "onebrief-agent-20260805",
        "GOOGLE_CLOUD_LOCATION": "global",
    })
    from onebrief.budget_guard import BudgetStore, micros_to_dollars
    from onebrief.execution_agents import DeveloperAgent
    from onebrief.execution_schemas import AnalysisPackage, EvidenceFinding
    from onebrief.generic_development_toolpack import (
        ApprovedProjectDevelopmentToolPack,
        ProjectCodeChangeSet,
    )
    from onebrief.guarded_gemini import BudgetedGeminiClient
    from onebrief.project_import import ExternalProjectImporter
    from onebrief.schemas import BudgetEnvelope
    from onebrief.toolpack_lifecycle import ProjectToolPackLifecycle

    case = json.loads(
        (ROOT / "benchmark_cases" / "exchange_release_candidate.json").read_text(
            encoding="utf-8"
        )
    )
    submission = json.loads((RUN_ROOT / "onebrief-submission.json").read_text(encoding="utf-8"))
    estimate = BudgetEnvelope.model_validate(submission["budget"])
    manifest = PROJECT_ROOT / "ONEBRIEF_PROJECT.json"
    ExternalProjectImporter(REGISTRY).import_bytes(manifest.read_bytes())
    lifecycle = ProjectToolPackLifecycle("exchange-release-baseline", REGISTRY)
    state = lifecycle.generate_and_qualify()
    if state.status != "approved":
        state = lifecycle.approve(state.qualification.toolpack_sha256)
    if not state.execution_ready:
        raise RuntimeError(f"baseline ToolPack is blocked: {state.execution_blockers}")

    output = RUN_ROOT / "baseline-output"
    pack = ApprovedProjectDevelopmentToolPack(
        "exchange-release-baseline", REGISTRY
    )
    _inspection, sources = pack.inspect(output / "inspection", focus_text=case["goal"])
    source_payload = [item.model_dump(mode="json") for item in sources]
    contract = {
        "goal": case["goal"],
        "acceptance_criteria": case["acceptance_criteria"],
        "verification_commands": case["verification_commands"],
        "execution_rules": case["execution_rules"],
        "variant": "single_agent_baseline",
        "instruction": (
            "Make one implementation proposal. There is no verifier or revision loop and no "
            "mid-run user feedback. The deterministic ToolPack will accept or reject it."
        ),
    }
    analysis = AnalysisPackage(
        objective=case["goal"],
        findings=[EvidenceFinding(
            finding_id="F01",
            source_name="fixed A/B benchmark contract",
            evidence="The goal, acceptance criteria, clean source revision, and validation commands are fixed.",
            implication="Implement the smallest complete release-candidate change without altering the protected original.",
        )],
        recommended_structure=[
            "Preserve existing data pipeline and product behavior",
            "Complete the user-facing web release criteria",
            "Return bounded source changes for deterministic validation",
        ],
        constraints=list(case["acceptance_criteria"]),
        risks=[
            "A single model pass cannot inspect test failures and revise its own output.",
            "Subjective UI quality can only be approximated from the fixed contract.",
        ],
    )
    run_dir = output / "run"
    BudgetStore(run_dir).approve(estimate, float(submission["approved_usd"]))
    developer = DeveloperAgent(
        BudgetedGeminiClient(run_dir),
        change_set_schema=ProjectCodeChangeSet,
        source_prefix="project-source/",
        path_approver=pack.approved_edit_path,
    )
    started = time.monotonic()
    result: dict[str, object] = {
        "schema_version": "onebrief-ab-baseline-v1",
        "variant": "single_agent_baseline",
        "started_at": datetime.now(UTC).isoformat(),
        "case_id": case["case_id"],
        "source_revision": case["source_revision"],
        "approved_usd": submission["approved_usd"],
        "user_interventions": 0,
        "verifier_calls": 0,
        "revision_rounds": 0,
    }
    try:
        change_set = developer.run(contract, analysis, source_payload)
        (output / "code_change_set.json").write_text(
            change_set.model_dump_json(indent=2) + "\n", encoding="utf-8"
        )
        development = pack.apply_and_verify(
            change_set, output / "development", verification_goal=case["goal"]
        )
        result.update({
            "status": "verified",
            "changed_files": [item.path for item in change_set.changes],
            "verification": development.model_dump(mode="json"),
        })
    except Exception as exc:
        result.update({
            "status": "failed",
            "error_type": type(exc).__name__,
            "error": str(exc)[:8000],
        })
    ledger = BudgetStore(run_dir).read()
    result.update({
        "finished_at": datetime.now(UTC).isoformat(),
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "model_calls": len(ledger.entries),
        "actual_cost_usd": micros_to_dollars(ledger.actual_usd_micros),
        "cost_entries": [item.model_dump(mode="json") for item in ledger.entries],
    })
    target = RUN_ROOT / "baseline-result.json"
    target.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
