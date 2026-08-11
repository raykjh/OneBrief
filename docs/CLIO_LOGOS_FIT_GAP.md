# CLIO/LOGOS concepts: OneBrief fit-gap review

Status: read-only review of frozen reference documents against OneBrief commit
`3fce3f832cd06ce07f8b7fab02468b894ec70b5b`. The frozen systems are historical
design evidence, not implementation dependencies or a new product scope.

## Current pilot evidence frozen before this review

- Public-data candidate revalidation reused the existing drafts and made no maker
  call. One independent verification plus final approval cost `$0.025835`. The run
  stopped as `partial / UNVERIFIABLE`: the artifact described an implementation,
  but did not provide runnable HTTP/browser evidence. This is a correct safe
  rejection, not a completed app.
- Six bounded Exchange continuations cost `$1.286340` in their worker ledgers.
  They made real incremental translations and repeatedly reached build/server/web
  observation, but the independent observer still found Korean text in the English
  page. The last exact-search patch also failed to match reliably. OneBrief stopped
  without applying the package to the source repository.
- The two follow-up paths therefore cost `$1.312175` in recorded Gemini worker
  ledgers. Agent Platform routing overhead is not represented by those worker
  ledgers and must not be presented as part of that exact figure.
- The stable branch and Cloud worker both use commit `3fce3f8`; the deployed image
  digest is `sha256:7394162dc6c1cfbd92d604821de1440ea4e9ba096ebe6c23efcdda855f6c72f2`.
- Regression evidence for the common fixes is `303 passed` (five warnings). The
  targeted dynamic-anchor suite passed 32 tests after the final change.

## Fit-gap table

| Candidate | State in OneBrief | Evidence in current implementation | Decision |
|---|---|---|---|
| Authority Envelope | **Partly present** | `PreparationPlan.authorization_sha256` binds canonical goal, completion contract, ToolPack permission manifest, and budget. Model policy and source snapshot are separately enforced. It does not yet expose actor, executor, expiry, risk ceiling, and source revision as one reviewable record. | Do not import the LOGOS envelope. Later extend the existing preparation contract with only actor, expiry, base source revision, and risk class if the UI and worker both enforce them. |
| Attention Gate / Decision Request | **Partly present** | Requirements, budget, authorization, recovery, and safe-apply gates already stop for the user. `RecoveryDecision` classifies responsibility and bounded retry. The user-facing stop reason is not one consistent structured object. | Highest-value small gap. Add one `DecisionRequest` projection over existing gates; do not add a second workflow engine. |
| Digest-bound approval | **Strongly partly present** | Run approval checks `authorization_sha256`; imported projects separately check ToolPack SHA; budget approval is immutable; safe apply checks package hash and unchanged source HEAD. Actor, expiry, single-use receipt, and one combined action digest are absent. | Strengthen the current approval path rather than create a parallel approval subsystem. First prove changed digest, expired approval, stale HEAD, and replay are rejected. |
| Unified stop reasons | **Partly present** | `PipelineStatus`, `JobStatus`, `ErrorClass`, verifier verdicts, and safe-apply errors exist, but stale revision, conflict, authority expansion, missing evidence, and budget are not one cross-layer vocabulary. | Add a small enum/mapping only when it drives UI, API, and evaluation consistently. Avoid replacing the detailed existing classes. |
| Durable lineage and idempotent receipt | **Already present for the hackathon path** | Immutable job packages, GCS generation claims, continuation manifests, checkpoints, cost ledgers, result hashes, and idempotent safe-apply receipts already cover the demonstrated run. | Do not reimplement. Remaining useful work is lineage-wide aggregation and a regression test for response loss/retry. |
| L0 Resume Capsule | **Partly present** | Project continuation state records goal, completed/pending/failed work, source HEAD, previous run, and result. Continuation manifests preserve parent-child context. There is no compact deterministic L0 artifact with prohibitions, unresolved conflicts, approval state, and next action. | Useful after the demo path is stable, but not required to fix current convergence. Implement only as a projection from existing state, never as another source of truth. |
| Candidate to approved playbook | **Already present** | `ProjectClosureManager` creates hashed `ReuseCandidate` objects; privacy, provenance, and independent reviews are required; promotion requires two successful project uses; model output is not auto-promoted. | No change. Add real cases and tests instead of a new registry. |
| CLIO/LOGOS core, memory repository, device credentials, encrypted storage, Mem0 | **Not needed** | These solve cross-device canonical memory and institutional trust, not OneBrief's completion-and-evidence loop. | Explicitly out of scope. Importing them would be hackathon overdesign and enlarge the security surface. |

## Implemented minimal layer

One read-only `DecisionRequest` record is now generated from existing blockers. It contains:

- a stable reason code (`missing_information`, `additional_budget_required`,
  `authority_expansion_required`, `stale_revision`, `conflict_detected`,
  `evidence_unavailable`, `external_side_effect_approval`);
- the action or decision required from the user;
- the current authorization digest, affected scope, base revision when relevant,
  additional budget when relevant, and expiry;
- safe alternatives such as reduce scope, refresh inspection, or stop;
- a machine-readable indication that no autonomous retry is allowed.

This record is a projection over `PreparationPlan`, `RecoveryDecision`, job
status, and safe-apply preconditions. It must not own state, create approvals, or
replace the existing budget, ToolPack, continuation, and safe-apply stores.

The execution authorization now binds actor, executor, completion-contract digest,
ToolPack hash, source revision, read/write scope, prohibited actions, risk ceiling,
budget, and expiry. The consumed action is retained as an `AuthorizationReceipt`.
External safe apply uses a separate `DecisionRequest` bound to the run authorization,
result manifest, base source revision, and project.

Continuation manifests now carry an ancestor aggregate and each terminal job emits a
deduplicated lineage summary for cost, calls, tokens, revisions, and elapsed time. Each
valid terminal job also emits a deterministic L0 Resume Capsule projected from its
goal, completion criteria, checkpoint position, prohibitions, source commit, and budget.
Projection failure is advisory and cannot mask the authoritative job outcome.

## Risks

- A second authority store would create disagreement with the current immutable
  budget, ToolPack approval, and safe-apply receipts.
- A broad envelope can become ceremony without improving a single user decision.
- Treating a Resume Capsule or model summary as canonical would reintroduce stale
  context instead of preventing it.
- New stop states can break automatic resume unless every existing terminal path is
  mapped and backwards-compatible.
- Expiry and actor binding need a real identity and clock policy; placeholder fields
  would look secure without being enforceable.

## Acceptance tests before implementation is considered complete

1. The same blocker produces the same `DecisionRequest` digest after restart.
2. Missing information, additional budget, stale source, authority expansion,
   conflict, unavailable evidence, and external apply each produce distinct reason
   codes and never trigger an unapproved model/tool retry.
3. An approval is rejected when its action digest, completion contract, ToolPack
   hash, base source revision, actor, or expiry differs.
4. Replaying an already-consumed external-apply approval returns the existing receipt
   and does not apply twice.
5. Parent and continuation jobs expose one lineage without double-counting cost or
   model calls.
6. Existing requirements, budget, checkpoint, continuation, safe-apply, and parallel
   campaign tests remain green.
7. The user UI asks one concrete decision and shows why autonomy stopped, without
   exposing internal agent or retry choices.

## Reference documents

- [Project Continuity Architecture](https://docs.google.com/document/d/16wW7C2CaMqhV580-8WV6_0hmWhts4QrK-Npc6lyDEGA/edit)
- [Project Continuity Data Model](https://docs.google.com/document/d/1oxNoZPjc7OG98q05QS2RLL-ZRe-0BQ8F1bK7nW0wGnM/edit)
- [Governance and Security](https://docs.google.com/document/d/1rzu7WzpcJUTBfPzu9i7TEjXmiR7weP072K-Mc-ErW5M/edit)
- [Resume Pack](https://docs.google.com/document/d/1nVz5kPUDs-PcsyOE2J0l9IVn_nQpeGyZMuCoo3zijm0/edit)
- [Checkpoint and Restore](https://docs.google.com/document/d/1V7OFx2XuT94HO5tiT9DqFy9Vmiam11vdL3EFGo11PsQ/edit)
