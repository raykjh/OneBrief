# Agent Cards and Versioned Workspaces

OneBrief separates reusable agent types from project-specific agent instances.

## Default registry

The registry contains nine base types: project owner, architect, investigator,
analyst, creator, maker, integrator, critic, and guardian. A project owner selects
only the types needed for a goal. Registration does not mean all nine models run.

Every `AgentCard` defines one distinct mission, activation conditions, owned
deliverables, authority, prohibited actions, and structured input/output contracts.
Every `AgentInstance` adds a project role, perspective, authority grants, temperament,
capability-pack grants, and scoped memory.

Temperament and capability packs are assigned together. Temperament is only a
tie-breaker after the project contract, authoritative sources, budget, safety, role
boundaries, and accepted handoffs.

## Goal-driven TeamPlan

An approved job begins with one budgeted `team_planning` Gemini call. The Project
Owner sees the immutable goal, deliverables, acceptance criteria, source inventory,
nine registered cards, and the allowed pack catalog. It returns the smallest
sufficient `TeamPlan`, including teams, members, APT-3 profiles, pack grants, and
one owner for each executable stage.

Code validates the plan before any downstream model work:

- exactly one Project Owner and at most one instance per base type;
- Analyst owns evidence analysis, Maker owns drafting/revision, and an independent
  Critic owns verification;
- Investigator is mandatory when public research is enabled;
- pack IDs must come from the approved catalog;
- the model cannot grant authority: each instance receives only the authority in
  its registered card.

`TeamPlanningCoordinator` then materializes every validated instance. Re-entry
loads the persisted plan instead of spending another planning call.

## Per-agent model approval

The Project Owner assigns one model and a reason to every selected instance from a
closed Gemini catalog. The deterministic Producer reprices every stage with those
choices against the immutable user approval. The resulting `ModelExecutionPolicy`
binds each stage to exactly one model; `ModelPolicyGateway` rejects any mismatch
before a provider call. The policy and model-budget decision are immutable audit
records included in the result package. Public Google Search research is restricted
to its approved grounded model.

## Agent library

`WorkspaceManager.initialize_agent_library()` creates:

```text
agent_library/
  registry.json
  <agent-type>/
    card.json
    packs/
    examples/
    evaluations/
    versions/
```

An existing changed card is never silently overwritten. A reviewed card change must
be versioned explicitly.

## Project workspace

`WorkspaceManager.create_project()` creates a Git-like document workspace without
requiring GitHub:

```text
projects/<project-id>/
  project.json
  00_contract/
  01_sources/immutable/
  01_sources/knowledge_candidates/
  01_sources/approved_knowledge/
  02_plan_and_teams/
  03_team_workspaces/
  04_review_requests/
  05_accepted_artifacts/
  06_decisions_and_evidence/
  07_budget_and_usage/
  08_execution_log/revisions/
```

Each activated instance gets private working files, submissions, and structured
handoffs under its team. Only reviewed artifacts should enter
`05_accepted_artifacts`. `WorkspaceRevision` records parentage, author, content hash,
reason, and review status so later storage adapters can implement diff, review, merge,
and rollback semantics.
