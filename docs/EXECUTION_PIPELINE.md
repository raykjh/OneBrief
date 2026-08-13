# Guarded Multi-Agent Execution

## Call graph

```text
Approved budget
  -> Analyst
  -> ADK Convergence Agent
       -> Accountable Maker (ADK LlmAgent)
       -> Independent Verifier (ADK LlmAgent)
       -> Deterministic Grounding / Completion / Reality Gates
            -> PASS -> final package
            -> NEEDS_INFORMATION -> user checkpoint
            -> REVISE -> the same Accountable Maker -> Independent Verifier
                         (within the user-approved revision limit)
  -> Completion ledger records criterion-level evidence and unresolved failures
  -> Safe delivery/application only when every required criterion passes
```

The maker and verifier are native Google ADK agents coordinated by a custom ADK
`BaseAgent`. Their model adapter receives only a `BudgetedGeminiClient`; it cannot call
the Google model client directly. Every ADK turn therefore performs input token
counting, worst-case reservation, provider generation, and actual-usage settlement.

## Role boundaries

- Analyst extracts traceable `F01`-style findings and never drafts.
- Accountable Maker creates the artifact from the contract, findings, and authoritative source
  payload but cannot approve it.
- Verifier is independent, checks every acceptance criterion, and returns `PASS`,
  `REVISE`, or `NEEDS_INFORMATION`.
- The original Accountable Maker receives its prior artifact plus the verifier's
  blocking instructions. A separate replacement agent is not introduced during revision.
- The ADK session persists structured maker and verifier handoffs and emits a durable
  event trace; deterministic OneBrief gates retain veto power over a model-issued PASS.

## Software convergence

For approved existing-project work, the Accountable Maker returns a bounded structured
change set. OneBrief applies it only in the ToolPack's disposable repository and runs the
allowlisted build, test, runtime, and observation commands. A correctable execution
failure becomes a structured `REVISE` handoff directly to the same maker; the independent
verifier is not billed for code that has already failed deterministic execution. After
execution succeeds, the verifier receives the changed files and trusted evidence rather
than the maker's summary. Unknown, policy, permission, and authority failures still stop
through the recovery-policy layer instead of expanding agent authority.

Every software repair is now preceded by a causal repair contract. Verification proceeds
from the cheapest boundary outward: schema/selector promotion, compile, targeted test or
state, semantic observation, and only then full verification. A structural selector
mistake is normalized or re-cataloged by trusted runtime code and never purchases a
stronger reasoning model. Semantic product failures may keep the same maker identity and
use an approved higher reasoning rung, but only when the repair contract identifies
reasoning as the missing capability.

Medium existing-project work adds a coordinator above this unchanged maker/verifier
loop. `milestone_plan.json` scopes the overall contract into vertical slices. Each slice
runs the normal deterministic execution and independent verification path, then writes
an immutable checkpoint before the isolated integration repository can advance. The
final slice seeds the accumulated change set into a clean approved baseline and executes
the complete contract. Ephemeral repository clones are excluded from Cloud transport and
result packages; only bounded change sets, evidence, checkpoint receipts, and the final
verified result are durable.

Budget approval remains project-wide but is milestone-aware: all expected slice and
integration calls are reserved in the estimate before approval, while the hard cost
ledger still stops actual spend at the exact owner-approved cap.

Managed execution never weakens a ToolPack because its container lacks an approved
desktop runtime. When an immutable snapshot requires Unity Compile, PlayMode, or layout
adapters unavailable in Cloud Run, the managed worker publishes a digest-bound runtime
handoff without claiming or spending the job. An already approved local Capability
Runner then claims that same GCS job and executes the exact contract. Every milestone
PASS immediately synchronizes its checkpoint, evidence, and cost ledger back to GCS;
the transient integration repository itself is never uploaded.

Software execution now has two modification phases. The initial maker owns product
implementation. After trusted verification, `phase_decision_rN.json` classifies the
failure and routes the same persistent maker to either a product-repair or
evidence-repair stage. Product stages cannot edit tests, screenshots, or proof files;
evidence stages cannot change shipped product behavior. Environment failures stop model
repair, and authority failures return to the approval boundary. This routing is enforced
by code, phase-scoped budgets, and path checks rather than prompt wording alone.

## Deterministic grounding gate

The model verifier cannot overrule this gate. For every authoritative CSV, the gate
locates every first-column record identifier in the result table and compares each
subsequent source field with the corresponding result cell. A missing row or changed
value forces `REVISE`.

The gate also extracts threshold and category-to-score mappings. If a generated scoring
conversion cannot be matched to one authoritative source line, the result becomes
`NEEDS_INFORMATION`; the system asks for an approved conversion table or formula rather
than allowing an agent to invent one.

Each handoff is a Pydantic schema rather than chat prose. Every stage and revision is
written as a checkpoint so a crash or budget block leaves inspectable state.

## Outputs

- `analysis.json`
- `draft_r0.json`
- `model_verification_r0.json`
- `deterministic_verification_r0.json`
- `verification_r0.json`
- optional `revision_rN.json` and `verification_rN.json`
- `final.md`
- `final_verification.json`
- `completion_ledger.json`
- `convergence_ledger.json`
- `repair_contract.json` and versioned `repair_contract_fNN.json`
- `evidence_specification.json`
- `phase_decision_rN.json` and optional `phase_scope_deferred_rN.json`
- audit `phase_budget_policy.json` and `phase_attempts.json`
- `execution_checkpoint.json`
- `adk_convergence_trace.json`
- `milestone_state/milestone_plan.json`
- `milestone_state/receipts/<checkpoint-sha256>.json`
- `milestone_state/current/Mnn.json`
- `milestones/Mnn/` scoped execution and evidence artifacts

## Commands

Recalculate the revised four-agent budget without another Gemini call:

```powershell
onebrief estimate INPUT REQUIREMENTS UPLOAD_MANIFEST --output BUDGET_JSON
```

After explicitly approving that estimate:

```powershell
onebrief execute INPUT REQUIREMENTS UPLOAD_MANIFEST `
  --run-dir RUN_DIRECTORY `
  --output-dir OUTPUT_DIRECTORY
```

