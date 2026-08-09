# OneBrief Two-Stage Workflow

## Why the boundary exists

OneBrief should do the agent engineering, not ask a non-expert to design agents or
ToolPacks. At the same time, an autonomous system must not invent authority. The product
therefore separates preparation and human authorization from autonomous convergence.

## Stage 1 — definition and authorization

OneBrief performs the work required to propose an executable plan:

1. normalize the goal and preserve supplied authoritative information;
2. define observable completion criteria and required evidence;
3. inspect the destination or existing project;
4. generate and qualify the smallest suitable capability pack;
5. calculate minimum, recommended, and maximum cost;
6. present exact read/write permissions, validation tools, and forbidden boundaries;
7. bind the canonical goal, output form, completion contract, permission manifest, and
   budget envelope to one SHA-256 authorization identity.

The user approves the goal, permissions, and amount once. The user does not choose
agents, models, tools, retry counts, or pack internals.

## Stage 2 — autonomous completion

After approval, OneBrief:

1. selects workers and models within the approved cost;
2. creates work in an isolated snapshot;
3. collects deterministic and semantic evidence;
4. returns rejected work to its accountable maker;
5. repeats until every required completion criterion passes;
6. applies or packages only the verified result.

The stage-1 authorization is immutable during this work.

## Returning to stage 1

Stage 2 can return one of three amendment requests:

- `needs_information`: authoritative input is missing and cannot be safely inferred;
- `needs_budget`: the approved amount cannot fund the next justified action;
- `needs_authorization`: a required read, write, tool, or external side effect lies
  outside the approved permission manifest.

These are not generic failures. OneBrief preserves completed work and evidence, explains
the smallest required amendment, and waits for a new exact authorization. It never
silently broadens access or spending.

The run screen exposes one `Return to stage 1` action for these states. A budget stop
reopens the cost envelope immediately and may reuse compatible completed artifacts.
Information and authorization stops first request one consolidated authoritative answer;
work based on the incomplete premise is not blindly copied into the amended run. The
amended session retains its parent session and stop reason, receives a new authorization
hash, and resumes through the normal guarded execution gateway.

## Responsibility boundary

Codex or another development assistant may prepare infrastructure that exists outside
the run—such as a clean repository, deployment address, credentials entered by the
user, or source documents. OneBrief must inspect that foundation and create its own
completion contract and capability pack. This makes the demonstration evidence of
OneBrief's capability rather than evidence of a hidden human-authored ToolPack.
