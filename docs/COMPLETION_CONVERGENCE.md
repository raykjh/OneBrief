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
