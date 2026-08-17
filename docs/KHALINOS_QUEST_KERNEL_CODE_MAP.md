# KHALINOS Quest Kernel Product Refocus

Status: architecture decision map; no production deletion is authorized by this document.

## Product boundary

KHALINOS is an evidence-gated Project Owner for delegated software projects.
It binds an approved outcome, authority envelope, budget, current repository
checkpoint, and independent verification into one adaptive Quest loop.

It does not promise zero-shot expertise in every artifact domain. The Kernel is
general; execution and proof capabilities are explicit adapters. PUZZLE TELOS on Godot
is the primary greenfield hackathon demonstration; JULPAE remains an existing-project
Unity stress case and regression source.

```text
Human approval
  -> Outcome Contract + Authority + Budget
  -> Quest Kernel
       -> issue exactly one current Quest
       -> dispatch to an approved execution adapter
       -> receive raw independent verification
       -> interpret the Receipt
       -> PASS / REPAIR_PRODUCT / REPAIR_EVIDENCE /
          NEEDS_AUTHORIZATION / STRUCTURAL_STOP / COMPLETE
       -> issue the next bounded Quest
  -> verified deliverable and receipt chain
```

## Naming decision

- Keep `KHALINOS` as the public candidate.
- Do not rename to `CALINOS` or `KALINOS`: both have substantial prior commercial
  use and are phonetically confusable with each other.
- Retain the Python package and CLI name `onebrief` during the refactor. A package
  rename adds migration risk without proving the new product. Change public UI,
  README, container labels, and submission copy first; rename internal imports only
  after the hackathon.
- A formal trademark clearance search remains separate from this engineering decision.

## Current architecture finding

The repository already contains a partial Quest system:

- `quest_orchestration.py` defines `OutcomeSketch`, `QuestContract`,
  `QuestVerificationReceipt`, `ProjectCanvas`, and `QuestStore`.
- `milestones.py::execute_milestone_plan` issues one Quest and records its result.
- `web_service.py` exposes the Quest state and `web/index.html` renders the active
  Quest.
- `execution_pipeline.py` reads the active Quest's initial phase.

The missing product link is ownership. `QuestStore` is currently a durable wrapper
around the milestone executor, while the 5,000+ line `ExecutionPipeline` still owns
most planning, repair routing, evidence construction, and final verification
decisions. KHALINOS therefore records Quests but does not yet consistently *think and
advance through* the Quest Kernel.

## Target package map

```text
src/onebrief/
  quest_kernel/
    models.py              # Outcome, Quest, Receipt, Canvas, decisions
    store.py               # append-only contracts/receipts and atomic canvas
    authority.py           # scope/budget/tool/revocation invariants
    receipt_interpreter.py # raw verifier output -> typed transition
    issuer.py              # verified checkpoint -> next Quest proposal
    transition_guard.py    # deterministic validation of proposed transition
    engine.py              # the only state-machine owner
  project_owner.py         # application service around QuestKernelEngine
  execution_adapters/
    protocol.py            # execute(QuestContract) -> ExecutionResult
    software.py            # existing-project software adapter
    unity.py               # Unity compile/layout/PlayMode adapter binding
  verification/
    protocol.py            # independent verifier boundary
    evidence_receipt.py    # executable evidence bindings
```

The target does not require moving every file immediately. Compatibility imports can
keep the current package stable while each responsibility is extracted and tested.

## KEEP

These modules already implement differentiating product value and remain production
dependencies.

| Current file | Keep as | Reason |
|---|---|---|
| `quest_orchestration.py` | seed for `quest_kernel/models.py` and `store.py` | Existing digest-bound Quest, Receipt, Canvas, and one-active-Quest invariants |
| `completion_ledger.py` | verification input | Criterion-level completion facts; a process status cannot manufacture PASS |
| `handoff_protocol.py` | typed boundary | Structured sender/recipient/authority handoff instead of agent chat |
| `convergence_policy.py` | Kernel stop policy | Detects repeated causal failure and non-learning repair |
| `phase_execution.py` | typed repair ownership input | Separates product, evidence, environment, and contract failures |
| `completion_evidence.py` | verification policy | Distinguishes real product construction from test-only or legacy tweaks |
| `budget_guard.py` | authority invariant | Immutable approval, reservation, settlement, and hard stop |
| `guarded_gemini.py` | model gateway | All Gemini calls remain budgeted and auditable |
| `jobs.py` | durable job envelope | Immutable input snapshot and result package lifecycle |
| `cloud_jobs.py` | runtime transport | Cloud Run and approved local capability handoff |
| `project_snapshot.py` | input checkpoint | Exact repository fact base for every Quest chain |
| `project_import.py`, `project_catalog.py` | repository authority | Source identity and safe isolated workspace |
| `toolpack_lifecycle.py` | adapter approval | Digest-bound capabilities and command allowlists |
| `safe_apply.py` | delivery boundary | Only verified changes may reach a target repository |
| `lineage.py` | audit lineage | Connects approval, job, budget, and result digests |
| `adk_convergence.py` | ADK execution primitive | Retains meaningful Google ADK agent orchestration |
| Unity evidence/runtime modules | Existing-project Unity adapter internals | Real compile, layout, PlayMode, screenshot, and semantic observation |
| `godot_topology.py` | Primary greenfield topology compiler | Deterministically materializes declared regions as native scenes plus a headless probe |

## REDUCE

These modules remain, but their active hackathon responsibility becomes narrower.

| Current file | Reduction |
|---|---|
| `execution_pipeline.py` | Stop owning the project state machine. Retain one-Quest maker/verifier execution behind `ExecutionAdapter`; extract phase and receipt decisions into the Kernel. |
| `milestones.py` | Retain skeleton generation, dependency checkpoints, clean-baseline integration, and candidate replay. Remove next-action ownership; the Kernel issues the next Quest. |
| `web_service.py` | Expose Outcome, Authorization, Active Quest, Receipt, cost, and final delivery. Hide agent topology and internal repair controls from the primary product flow. |
| `web/index.html` | Replace diagnostic-heavy status with four views: Approve Outcome, Active Quest, Verification Receipt, Project Canvas. English is the submission default. |
| `schemas.py` | Keep compatibility exports; move new Kernel schemas out so this shared file stops becoming the universal dependency. |
| `automatic_resume.py` | Resume only from a compatible Receipt/checkpoint. Remove broad heuristics that infer continuity from loose artifact presence. |
| `cloud_continuation.py` | Transport a typed Kernel transition; do not decide product/evidence ownership. |
| `team_planning.py`, `execution_graph.py` | Use a fixed Project Owner -> Maker -> Verifier path for the demo. Dynamic role selection becomes secondary diagnostics. |
| `model_policy.py` | Flash by default; Pro only for an explicitly classified semantic decision. No escalation for schema, routing, environment, or repeated-strategy failures. |
| `capability_packs.py`, `toolpacks.py` | Present one software-development capability, the primary Godot adapter, and the retained Unity existing-project adapter. Hide unused packs. |
| `sixsense.py`, `assurance.py` | Reduce public choices to Outcome preferences plus `strict`, `balanced`, or `exploratory` assurance. Preserve the non-negotiable truth floor. |

## REMOVE FROM THE ACTIVE HACKATHON PATH

Do not delete these files in the first migration. Stop routing the primary KHALINOS
demo through them, mark them deferred, then delete only after dependency and regression
checks prove they are unused.

| Current area | Action | Reason |
|---|---|---|
| `public_research.py`, `grounded_search.py` | defer | OEM/research workflows dilute the specific software-project story |
| `evidence_sufficiency.py` document-claim branches | defer | Not needed for the Unity completion demonstration |
| `workbook_export.py` | defer | Spreadsheet delivery is outside the product wedge |
| `parallel_campaign.py` and stabilization campaign UI | archive | Evaluation infrastructure, not end-user value |
| `greenfield_web_toolpack.py`, `web_runtime_evidence.py` | holdout only | Useful later to prove a second adapter, not before JULPAE completion |
| financial/knowledge-work skills | remove from active registry | Avoid implying universal domain expertise |
| `observation_packs.py` generic branches | defer | Unity observation is the only shipping adapter |
| CLIO/LOGOS fit-gap and governance expansion | research archive | Explicitly outside the hackathon product boundary |
| generic long-form draft delivery | compatibility only | KHALINOS is not submitted as a writing agent |

## ADD: Quest Kernel responsibilities

### `models.py`

Move and refine the existing models without changing stored v1 payloads initially.

- `OutcomeContract`: approved target, preservation boundary, prohibitions, assurance
- `AuthorityEnvelope`: files, tools, external actions, budget, expiry/revocation
- `QuestContract`: exactly one executable current objective
- `VerificationReceipt`: raw evidence bindings plus verifier verdict
- `TransitionDecision`: typed next action and failure owner
- `ProjectCanvas`: user-readable current verified state

### `receipt_interpreter.py`

This is the central Gemini decision surface. It receives the raw, unmanually-normalized
verifier receipt and may return only:

```text
PASS
REPAIR_PRODUCT
REPAIR_EVIDENCE
NEEDS_AUTHORIZATION
STRUCTURAL_STOP
COMPLETE
```

Its output is never authority by itself. `transition_guard.py` verifies that the
decision is supported by the raw Receipt and remains inside the approved envelope.

### `issuer.py`

The Project Owner proposes the next detailed Quest from:

- immutable Outcome Contract;
- current Project Canvas;
- latest verified repository checkpoint;
- latest raw Receipt and typed transition;
- preserved PASS criteria;
- remaining budget and authority.

It does not receive future-milestone implementation detail that could become stale.

### `transition_guard.py`

Deterministically reject any proposal that:

- expands authority, budget, tools, or external actions;
- weakens or drops required completion criteria;
- loses prior PASS receipts;
- assigns maker and verifier to the same role;
- repairs evidence during a product-owned turn or vice versa;
- edits an original/baseline file without explicit authority;
- loses same-Quest lineage for newly created product surfaces;
- repeats a failed strategy without a new discriminating probe.

### `engine.py`

`QuestKernelEngine` becomes the only owner of state transitions.

```python
decision = engine.interpret(raw_receipt)
guarded = engine.validate_transition(decision)
next_quest = engine.issue_next(guarded)  # or terminal state
engine.persist(next_quest)
```

Execution adapters cannot issue Quests, widen authority, or mark a project complete.

## Current-to-target call map

### Current

```text
web_service
  -> jobs.run_job
  -> milestones.execute_milestone_plan
       -> QuestStore.issue
       -> ExecutionPipeline.run       # still owns most decisions
       -> QuestStore.record_result
       -> next milestone loop
```

### Target

```text
web_service
  -> ProjectOwner.start(approved_snapshot)
  -> QuestKernelEngine.issue_current
  -> ExecutionAdapter.execute(quest)
  -> IndependentVerifier.verify(execution_result)
  -> QuestKernelEngine.interpret_and_advance(raw_receipt)
       -> repair Quest / next Milestone Quest / authorization / stop / complete
  -> Project Canvas + verified result
```

## Migration sequence

### Change 1: characterize and freeze

- Add golden tests for the current Outcome/Quest/Receipt/Canvas JSON.
- Add one raw Receipt holdout set covering product failure, evidence failure,
  authorization, repeated strategy, and PASS.
- Record the current JULPAE M01 failure as a non-training regression fixture.

Exit: current behavior is reproducible without another paid model run.

### Change 2: extract the Kernel without behavior change

- Create `quest_kernel/models.py` and `quest_kernel/store.py` from
  `quest_orchestration.py`.
- Keep compatibility re-exports in `quest_orchestration.py`.
- Move no runtime decisions yet.

Exit: existing tests and serialized v1 digests remain valid.

### Change 3: add Receipt interpretation and transition guard

- Implement `TransitionDecision` and deterministic guard.
- Run same-model natural-language vs typed-Quest A/B on unpublished holdouts.
- Require the Quest path to beat natural-language routing on task success,
  authorization preservation, false-PASS rate, and repeat-strategy rate.

Exit: measurable decision advantage, not architectural preference.

### Change 4: make the Kernel the state owner

- Change `milestones.execute_milestone_plan` to call `QuestKernelEngine`.
- Convert `ExecutionPipeline` into a one-Quest executor result provider.
- Remove next-phase/next-milestone issuance from the executor.

Exit: only the Kernel writes active Quest and Project Canvas transitions.

### Change 5: ship the narrow UI

- Rename public OneBrief strings to KHALINOS.
- Show Outcome/Authorization, Active Quest, Receipt, and Project Canvas.
- Keep JSON traces downloadable but not primary.

Exit: a judge can understand the product without reading logs.

### Change 6: prove the demo

- Fresh PUZZLE TELOS Godot snapshot; no prior draft, patch, evidence, or ledger reuse.
- M01 builds all approved product regions as real runtime-reachable scenes.
- Real Godot headless topology, runtime screenshot, and Windows export Receipt.
- Keep the existing Unity compile, layout, and PlayMode regressions green.
- Kernel issues M02 only after M01 PASS.
- Record Google Cloud execution and immutable digest chain.

Exit: one uninterrupted end-to-end demo and a reproducible result package.

### Change 7: deactivate deferred paths

- Remove deferred modules from the public registry and navigation.
- Run dependency checks before physical deletion.
- Physical deletion happens after submission unless it directly reduces demo risk.

## Hackathon acceptance gates

The refocus is successful only if all are true:

1. A user approves one Outcome, authority envelope, budget, and evidence policy.
2. Only one detailed Quest is active.
3. Raw verification changes the next Quest without manual normalization.
4. A false PASS, authority expansion, or repeated strategy is deterministically blocked.
5. Maker and verifier remain independently attributable.
6. PUZZLE TELOS M01 passes real Godot execution and M02 is issued from its Receipt; Unity regressions remain green.
7. The demo shows Gemini and ADK making typed project decisions, not merely generating text.
8. Cloud Run execution and the approved local engine capability handoff share one immutable job lineage.

## Explicit non-goals before submission

- universal artifact completion;
- national/organizational AI governance;
- a new inter-agent global protocol;
- LOGOS or CLIO integration;
- autonomous authority expansion;
- multiple simultaneous active Quests;
- dynamic marketplace of arbitrary ToolPacks;
- internal Python package rename from `onebrief` to `khalinos`.

## First implementation slice

The first code change after approval should be Change 1, not deletion. Add the raw
Receipt holdouts and transition-decision schema, then demonstrate that a guarded Quest
decision correctly routes the already captured v68/v69 failures. This gives the new
Kernel an evidence-backed foundation before it is allowed to replace the current
milestone loop.
