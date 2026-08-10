# Agent Platform runtime and A/B evaluation

OneBrief's managed project owner runs as a Google ADK application on Gemini
Enterprise Agent Platform.  Its only mutating capability is the existing custom
IAM role `onebriefJobRunner`, containing `run.jobs.run` and
`run.jobs.runWithOverrides`. A separate `onebriefAgentModelInvoker` role contains
only `aiplatform.endpoints.predict` for Gemini inference. It has no Cloud Storage
read permission. The web
tier creates an immutable, budget-approved work order; Agent Platform starts the
fixed Cloud Run worker; the worker verifies input hashes, model policy, ToolPack
permissions, and the hard cost breaker before acting.

The deployed runtime is
`projects/1077683695702/locations/asia-northeast3/reasoningEngines/665354068385857536`.
It uses an ephemeral one-shot ADK session because routing an immutable work order
needs no conversational memory. This avoids granting managed-session or Storage
permissions. Durable identity is instead the work-order URI, Agent Platform
dispatch ID, Cloud Run operation, and immutable OneBrief audit ledger.

Cloud Trace records the managed project-owner model and tool spans.  Each worker
result also contains `evaluation_metrics.json` with completion rate, revision
rounds, model calls, token usage, actual and approved cost, ADK event authors,
elapsed time, and user supplements.  These artifacts are copied into the
immutable result package and exposed by the session status response together
with the Agent Platform and Cloud Run execution trace.

The first flagship comparison is defined in
`benchmark_cases/exchange_release_candidate.json`.  Both variants start from
Exchange commit `5363adb`, use the same goal and completion criteria, and receive
no mid-run user feedback except for a genuine information, permission, or budget
blocker.  The user's dirty `C:\exchange` worktree is not an input and is never
modified.  Two disposable local clones hold the baseline and OneBrief variants.

The comparison is not presented as proof that another coding agent is unable to
finish software.  It measures the product claim that OneBrief can obtain a
verified completion with fewer user interventions, bounded cost, durable failure
and revision evidence, and guarded application.

## 2026-08-10 managed-runtime evidence

- Agent Platform runtime: `projects/1077683695702/locations/asia-northeast3/reasoningEngines/665354068385857536`
- Agent Platform identity: `onebrief-agent-runtime@onebrief-agent-20260805.iam.gserviceaccount.com`
- Worker identity: `onebrief-worker@onebrief-agent-20260805.iam.gserviceaccount.com`
- Cloud Run service: `https://onebrief-web-1077683695702.asia-northeast3.run.app`
- Dispatches returned an Agent Platform session, Cloud Run operation, and event
  count. A duplicate dispatch was independently rejected by the Storage
  generation-match claim before any second model call, demonstrating idempotent
  delivery rather than relying on best-effort routing.
- The worker publishes evaluation metrics for every terminal state, including
  failures and budget/authorization stops. Failed work also receives an immutable
  result package instead of disappearing behind a log message.

Cloud Trace captures ADK and tool spans; OneBrief's own immutable evidence joins
the Agent Platform session, Cloud Run operation, work-order URI, model-call
ledger, completion criteria, revision rounds, and result package. The durable
OneBrief identifiers are necessary because an ephemeral one-shot ADK session is
intentionally used for least privilege.

## Exchange A/B result under the original approval

Both variants used Exchange revision `5363adb`, the same goal and acceptance
criteria, and zero mid-run user supplements. The baseline made one Gemini call,
spent `$0.096543`, and failed after `33.719` seconds because its proposed changes
did not satisfy the executable change schema.

The OneBrief lineage made multiple budget-preserving resumptions through Agent
Platform. It spent `$2.644001` of the immutable `$3.343300` approval before the
remaining `$0.699299` fell below the approved minimum for another full agent
round. It produced and repeatedly revised an executable Exchange candidate,
passed dependency restoration, lint, build, and repository tests, and reached
independent HTTP/browser observation. No run modified `C:\exchange`; every
attempt used the committed snapshot in an isolated clone.

This first cold-start comparison is deliberately recorded as **not completed**,
not converted into a success after the fact. During the run it exposed and fixed
real platform defects: lock restoration, transient package installation,
large-file exact edits, canonical TeamPlan reuse, ancestor artifact recovery,
Cloud Run memory sizing, and framework-independent browser observation. After
the observer was corrected locally, it rejected the candidate for the substantive
remaining criterion: the page had no operable Korean/English language control.
That is the intended fail-closed behavior, but the flagship completion claim is
not yet proven. A new explicit model-budget approval is required to repair that
criterion and execute a final managed revalidation.

The correct A/B interpretation is therefore:

- baseline: cheaper and faster, but no executable candidate or verification;
- OneBrief: materially farther, auditable and safely rejected, but slower and
  more expensive and not complete within this first approval;
- product evidence gained: managed routing, bounded spend, resumable artifacts,
  deterministic checks, semantic rejection, and honest budget escalation;
- evidence still required: one managed run that repairs the language criterion,
  passes the independent observer, packages the result, and completes safe apply.
