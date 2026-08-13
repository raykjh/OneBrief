# Completion Convergence

## Fixed scope

OneBrief is **a system that manages completion criteria and evidence so AI work reaches
an actually completed state**.

It is not a general chatbot, an agent builder, an agent-count demonstration, or a claim
that one prompt can solve every task. Agents are workers. OneBrief is the control system
that defines, measures, rejects, revises, and proves their work.

## Two stages and two product responsibilities

### Stage 1. Establish and authorize the completion system

Before paid execution, OneBrief converts the goal and authoritative sources into:

- an observable target state;
- deliverables in a usable output form;
- required quality criteria with stable `Qnn` identifiers;
- the evidence required for every criterion;
- a proof mode: deterministic verification or independent review;
- authority, safety, and budget boundaries;
- a OneBrief-generated capability pack with exact read/write prefixes and evidence
  adapters.

The contract, capability permissions, and cost envelope are canonicalized into one
authorization hash. Approval is valid only for that exact plan. External development
assistants may prepare a clean repository, URL, or source material; OneBrief remains
responsible for discovering, generating, qualifying, and proposing its own ToolPack.

Only missing authoritative information, insufficient budget, or a decision outside AI
authority may interrupt execution. Implementation preferences with safe defaults do not.

### Stage 2. Converge work until the contract is proven

```text
completion contract
  -> accountable maker produces work
  -> deterministic evidence collection
  -> independent verifier checks each Qnn criterion
       -> PASS: record evidence
       -> REVISE: record failure and exact correction, return to same maker
       -> NEEDS_INFORMATION: stop only at the authority boundary
       -> UNVERIFIABLE: do not present the result as complete
  -> repeat within approved budget
  -> safe delivery/application only when all required criteria pass
```

`REVISE` now creates `repair_plan_r*_n*.json`. Each task binds one failed `Qnn`
criterion to its evidence, exact revision instructions, a normalized failure
fingerprint, and the already passing criteria that must not regress. The first failure
receives a bounded repair. The second identical fingerprint requires a smaller,
independently verifiable change. A third identical fingerprint stops blind retries and
reports the unresolved slice rather than consuming the remaining budget on the same
approach.

Duplicate blocking is necessary but not sufficient. Software runs additionally persist
`convergence_ledger.json` and an evidence-bound `repair_contract.json`. A trusted failure
is classified at the structured-output, source-binding, build, runtime,
evidence-integrity, semantic-product, authority, or provider boundary. The contract then
records one causal hypothesis, its cheapest discriminating probe, the permitted repair
surface, predicted evidence, and a progressive verification ladder. The same causal
failure plus the same strategy is `no_progress` and is blocked before another maker
call. One materially different hypothesis may be tested; two different repairs reaching
the same boundary stop for diagnosis or an explicit decision. This turns the loop from
"reject and retry" into "observe, hypothesize, probe, repair, and learn."

Semantic failures are compared by stable symptom atoms (for example, a specific mobile
surface remaining clipped, overlapping, unreadable, or missing glyphs), not by the
verifier's full prose or by whichever file the last repair touched. A smaller symptom
set is measurable progress. Rephrasing the same symptoms or moving the same idea to a
different file is not. The runtime also enforces the repair contract's permitted paths;
the prompt alone is never the security or convergence boundary.

### Product work and proof work are separate authorities

Software convergence no longer treats every failed check as the same kind of coding
task. Before the first maker call, OneBrief freezes `evidence_specification.json` from
the completion contract and immutable acceptance inputs. Trusted failures are then
assigned to one of four owners:

- **product**: shipped source is wrong; only product source may change;
- **evidence**: the executable test or observation harness is missing or wrong; only
  tests and evidence harnesses may change;
- **environment**: provider, license, or worker infrastructure failed; do not buy a
  model edit;
- **contract**: authority or completion criteria must change; return for a decision.

The current phase is passed through ADK state and binds the maker call stage. If one
proposal crosses the product/evidence boundary, out-of-phase paths are deferred and
recorded; a proposal containing no authorized path is rejected. The verifier remains a
separate `final_verification` authority.

Each approved software run also receives non-borrowable phase wallets and attempt
limits. Product implementation, evidence construction, final verification, shared
context, and reserve are all inside the user's original total cap. Exhausting one phase
does not silently consume another phase's money. Deterministic build/test attempts have
their own bounded ledger, so repeated tools cannot hide behind a remaining model-dollar
balance.

If a paid proposal failed only at trusted source promotion, the valid structured proposal
is preserved as `development_pending_promotion.json`. A continuation first re-promotes
that exact proposal through the current path, hash, and anchor catalog. It buys no new
maker call unless deterministic re-promotion still cannot bind safely.

### Medium and large work converges through dependency-bound milestones

OneBrief does not force a medium software goal through one all-or-nothing candidate.
After authorization, a deterministic project-owner tool maps the existing `Qnn`
criteria into a `MilestonePlan` of independently executable vertical slices. This plan
cannot invent scope or weaken a criterion:

- every overall criterion has exactly one primary milestone owner;
- coarse end-to-end criteria may be preceded by explicit derived slice criteria
  (for example Login, Lobby, then Settings) so partial work is executable without
  pretending the parent criterion has already passed;
- each later slice revalidates the passed criteria on which it depends;
- every PASS receipt binds the milestone contract, source revisions, dependency
  checkpoint IDs, candidate digest, and evidence digests;
- checkpoint receipts are append-only and idempotent;
- changing an upstream candidate invalidates only descendant checkpoints;
- the final integration milestone must include every overall criterion and uses full
  regression rather than a targeted check.

Functional milestones advance only an isolated integration repository. That repository
is transient and is neither shipped nor uploaded. On continuation, OneBrief reconstructs
it from the original approved snapshot and the bounded, hash-checked milestone change
sets. The final accumulated candidate is then tested once more against the clean
baseline, preventing incremental drift from being mislabeled as completion.

The approval quote expands model-call counts, phase wallets, repair-call limits, and
time ranges for every planned vertical slice plus the final clean-baseline integration
pass. A legacy one-pass estimate therefore cannot silently starve a later milestone.

Checkpoint durability crosses execution locations. Cloud-capable work remains on the
managed worker. A desktop-only approved adapter such as Unity is handed to the local
Capability Runner through the same immutable job digest and approval, and each PASS is
uploaded before the next dependent milestone starts. A worker interruption can lose at
most the active, unverified slice—not an earlier verified checkpoint.

The user still approves one hard minimum-to-maximum project envelope. Milestone weights
are internal planning allocations, not independent approvals; the global circuit breaker
remains authoritative and the reserve is available only inside that envelope.

There is no silent authority expansion. `NEEDS_INFORMATION`, `NEEDS_BUDGET`, and
`NEEDS_AUTHORIZATION` are structured returns to stage 1. The user approves an amended
plan; OneBrief then resumes from persisted evidence instead of pretending the first run
completed.

## Completion ledger

`completion_ledger.json` is the primary product state. For each criterion it stores:

- stable criterion ID and description;
- required proof and proof mode;
- current status: pending, passed, revise, needs information, or unverifiable;
- evidence from every verification attempt;
- failure reasons and revision instructions;
- the number of revision rounds.

The agent graph remains available as diagnostics, but it cannot declare success. A run
with ten completed agents and one failed required criterion is incomplete.

## Non-negotiable release rule

A required criterion passes only with its specified evidence. A file existing, a model
saying “PASS,” or an agent completing its turn is insufficient. Deterministic gates may
overrule the verifier. Failed work returns to the accountable maker. Safe application to
an existing project is allowed only after the completion ledger is complete and policy
checks pass.

## Near-term product direction

1. Make the criterion ledger the main run screen and agent activity secondary.
2. Require verifiers to reference exact `Qnn` IDs and preserve attempt history.
3. Expand evidence adapters for documents, spreadsheets, web apps, and software builds.
4. Use a disclosed conventional design baseline for first delivery; reserve user taste
   questions and distinctive visual exploration for an explicit follow-up improvement.
5. Measure convergence: criteria passed, revisions required, stop reason, time, and cost.
6. Demonstrate one small end-to-end case where a real first attempt may fail,
   same-maker repair produces new evidence, and only PASS enables safe application.

## Deliberate exclusions

- marketing based on nine agents or dynamic team size;
- user-facing model, agent, retry, or ToolPack selection;
- adding domain-specific agents before a reusable evidence adapter is justified;
- treating partial output or exhausted revision limits as success;
- claiming subjective user satisfaction without user confirmation.
