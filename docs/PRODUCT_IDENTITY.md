# OneBrief Product Identity

## One sentence

OneBrief manages completion criteria and evidence so AI work converges to a genuinely
completed state.

## The narrow problem inside the general-purpose product

The product is general-purpose because user goals vary. The problem it solves is narrow:
people who can chat with AI should not have to learn how to design, equip, prompt,
sequence, supervise, and repeatedly correct an agent team.

The value is not the number of agents. The value is responsibility for completion.
Agents are replaceable workers inside this system; agent activity is never proof of
completion.

## Invariants

1. The user describes the goal, authoritative information, constraints, and optional
   output form. Internal orchestration choices are not user settings.
2. Before paid execution, OneBrief creates both the completion contract and the
   capability pack required to prove it. Codex or the user may provide an address,
   repository, or authoritative source, but they do not author the pack for OneBrief.
3. The project owner chooses agents, models, knowledge, skills, tools, memory, ordering,
   and safe parallelism.
   Model choice follows a deterministic criticality boundary: routine research,
   analysis, creation, and revision stay on Flash tiers; team planning, independent
   verification, governance, and final approval may use the approved higher-reasoning
   model only after that exact cost is included in the immutable budget approval.
4. Every artifact has one accountable maker. Independent verification may reject it,
   but correctable work returns to the original maker with evidence and instructions.
5. Objective criteria use deterministic proof whenever possible. Semantic and
   subjective quality use an independent reviewer plus disclosed working defaults. An
   approved run is not interrupted for preference feedback.
   When the user does not request a visual direction, the first delivery uses an
   accessible conventional professional design. Distinctive design exploration is a
   later existing-project improvement rather than an untestable first-run objective.
6. The loop continues within the approved budget and policy: make, verify, diagnose,
   return to owner, revise, and reverify.
   For a multi-surface or otherwise medium/large deliverable, the project owner first
   decomposes the completion contract into independently verifiable vertical
   milestones. Passing milestones are immutable checkpoints; a changed upstream slice
   invalidates only its dependency descendants, while the final milestone always
   replays the accumulated candidate against the clean approved baseline and runs the
   full completion contract.
7. Before execution, OneBrief calls the user only for missing authoritative information,
   insufficient budget, or a decision beyond agent authority. After a completed result,
   user feedback starts an explicit existing-project improvement run.
8. Example projects and domain ToolPacks prove and improve the system; they do not define
   the product.
9. The primary user view is a completion ledger. Agent topology and execution traces are
   secondary diagnostic evidence.

## Keep, add, remove

### Keep

- one goal plus authoritative internal information
- automatic team, model, skill, knowledge, tool, and memory assignment
- immutable budget approval and cost circuit breaker
- isolated execution, checkpoints, evidence packages, and resumability
- independent verification and deterministic gates
- project continuity and reusable packs

### Add

- a first-class completion contract
- criterion-level proof modes: deterministic and independent review
- visible convergence history rather than an agent-count showcase
- original-maker revision for every artifact type
- one canonical goal that merges confirmed supplements while retaining source history
- convergence measurements: required criteria passed, unresolved gaps, revision reason,
  cost consumed, and why execution stopped

### Remove from the active product experience

- user selection of retry count, agent, model, or tool
- a separate generic revision role that breaks artifact ownership
- claims that OneBrief is only a long-form writer
- example-specific identity or Exchange-specific product language
- success claims based only on agents running or files being produced

## Product flow: two stages

### Stage 1 — define and authorize

1. Receive one short goal, authoritative sources, and an optional existing project or
   destination. A novice is not expected to author a professional brief.
2. SixSense finds the applicable professional standard and prepares at most five
   material choices in one Gemini pass. The browser presents them as an instant,
   one-question-at-a-time sequence with recommended defaults and no network wait between
   questions.
   Its choices resolve user preferences only. Facts or candidates that the approved
   execution is supposed to research may not be smuggled into a recommended option.
   When the goal delegates discovery or selection to OneBrief, SixSense does not return
   that delegated decision to the user; only a genuine mandatory information gap may stop intake.
3. OneBrief asks separately only for genuinely missing authoritative information that
   cannot be inferred or safely defaulted.
4. OneBrief creates the completion contract and generates and qualifies the smallest
   capability pack that can create and prove the result.
5. OneBrief presents the exact goal, completion criteria, read/write boundaries,
   validation adapters, forbidden boundaries, and minimum/recommended/maximum cost.
6. The user gives one approval for that exact hashed plan.

### Stage 2 — converge and deliver

1. OneBrief selects the team and models inside the approved plan.
2. For medium and large work, the project owner creates a `MilestonePlan` from the
   approved criteria. Every criterion has one primary milestone owner; later milestones
   include already passed dependencies as regression requirements.
3. The accountable maker produces one independently executable vertical slice.
4. Deterministic checks and an independent verifier accept or reject the milestone
   contract. A PASS writes an immutable, digest-bound checkpoint.
5. Failed work returns to the same maker with evidence until all required criteria pass.
   Each return is a criterion-scoped repair plan: passing criteria are frozen as
   regression constraints, the failed slice becomes the next bounded task, and a
   repeated failure is decomposed instead of receiving blind additional retries.
6. The final integration milestone applies the accumulated candidate to a clean copy of
   the exact approved source revision, reruns the full contract, and safely applies or
   packages only that proven result.

Stage 2 may never silently widen stage 1. Missing authoritative information,
insufficient budget, or a new permission returns an amendment request to stage 1. The
user reaches that amendment with one action from the stopped run; OneBrief preserves the
lineage and reuses only artifacts that remain compatible with the amended contract.

## Completion semantics

A run is complete only when every required criterion has the specified evidence and no
policy or authority blocker remains. Revision depth is an internal safety ceiling, not
a user setting or product success condition. If the approved budget or capability
boundary cannot support further correction, the run returns to stage 1 instead of
claiming completion or silently expanding authority.

Broad goals are executable only after they become atomic observable slices. A criterion
must be able to fail independently and name its own evidence. OneBrief may group those
slices into a coherent release, but it must not hide several independently failing
behaviors behind one vague criterion such as “the application is professional.”

Example cases should be selected after this loop works. They are test vectors for
different artifact types and evaluation modes, not the center of product design.

