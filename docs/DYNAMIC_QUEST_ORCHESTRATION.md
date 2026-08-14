# Dynamic Quest orchestration

OneBrief keeps the approved `MilestonePlan` as the project skeleton, but it no
longer treats every future slice as an immediately executable instruction.  A
single digest-bound Quest is the only active unit of work.

## Lifecycle

1. The user's approved Completion Contract becomes an `OutcomeSketch`.
2. The full MilestonePlan records dependencies, budget weights, and the maximum
   authorized outcome of each slice.
3. Only the first ready implementation milestone receives a detailed
   `QuestContract`.
4. The accountable maker executes that contract inside the approved ToolPack
   write prefixes and budget allocation.
5. An independent verifier produces executable evidence. OneBrief derives a
   `QuestVerificationReceipt` from the Completion Ledger, final verification,
   and execution checkpoint.
6. A PASS receipt closes the Quest and becomes the input checkpoint for the next
   Quest. A blocked or authorization-required receipt cannot be treated as PASS.

The next contract therefore reflects the verified project state rather than an
assumption made before earlier work existed.

## Durable state

`work/quest_state/` contains:

- `outcome_sketch.json`: the user-readable target and non-negotiable boundaries;
- `project_canvas.json`: the current active/completed/blocked Quest pointers,
  receipt history, gaps, and budget allocation;
- `contracts/QC-*.json`: append-only Quest Contracts;
- `receipts/QR-*.json`: append-only independent Verification Receipts.

Cloud continuation, bounded repair resume, and result packaging preserve this
state. Temporary team execution graphs are still regenerated for each job.

## Authority invariants

- Only one Quest may be active.
- Every dependency is bound to the exact passed milestone checkpoint.
- The Quest authority digest binds the MilestonePlan, milestone envelope,
  ToolPack digest, and approved write prefixes.
- A maker cannot be its own independent verifier.
- A Quest cannot weaken criteria, write the original repository, spend outside
  the approved project envelope, or perform a future milestone's work.
- Later Quests preserve earlier receipt IDs and passed evidence.
- Multiple repair Quests for one milestone share its existing budget allocation;
  they do not reserve the milestone budget repeatedly.
- A no-progress authentication boundary produces `needs_authorization` and
  blocks successor issuance.

## Receipt precedence

Completion is established only when all of these agree:

1. Completion Ledger is complete with latest verdict PASS;
2. final independent verification is PASS;
3. execution checkpoint is complete/PASS.

An outer transport or job-envelope error cannot erase an already written PASS
receipt. Conversely, a successful process status without the ledger and verifier
evidence cannot create a PASS receipt.

## Failure ownership

Structured phase receipts take precedence over older diagnostic prose. A newer
provider, schema, or routing-envelope exception supersedes the previously active
phase and belongs to the orchestrator. PlayMode tests, asmdefs, screenshot
harnesses, and proof artifacts belong to evidence construction. This prevents an
evidence or communication defect from authorizing unrelated product changes.

## User visibility

`GET /api/sessions/{session_id}/quests` returns the Outcome Sketch, Project
Canvas, active Quest, all referenced contracts, and all receipts. The execution
screen shows the active Quest next to the milestone skeleton and Completion
Ledger.
