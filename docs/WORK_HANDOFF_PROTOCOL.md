# Work Handoff Protocol

OneBrief agents do not exchange free-form chat transcripts. Stage-specific
Pydantic payloads remain authoritative, and repair work is wrapped in a common,
digest-bound handoff.

## Contracts

- `FailureObservationV2` assigns a stable `FailureCode`, causal layer, owner,
  evidence digest, affected paths, failed criteria, and attempt number.
- `EvidenceBinding` attaches one compile, behavior, visual, policy, or other
  observation to one explicit completion criterion. Unbound system checks may
  block release but may not silently overwrite a product criterion.
- `WorkHandoffEnvelopeV1` binds sender, recipient, project, milestone, round,
  goal digest, completion-contract digest, source revision, owned criteria,
  preserved passing criteria, edit scope, inputs, and expected output schema.
- `HandoffReceipt` proves that the exact recipient accepted the exact handoff
  digest. A changed, stale, replayed for another recipient, or scope-expanded
  handoff is rejected before another model turn.

Stage-specific objects such as `AnalysisPackage`, `ProjectCodeChangeSet`,
`VerificationReport`, and `RepairContract` are not replaced. The envelope makes
their responsibility and authority explicit.

## Unity evidence rule

Unity completion proof has three independent dimensions:

1. compile evidence;
2. runtime behavior evidence;
3. rendered visual evidence.

A missing screenshot can revise the visual dimension without erasing an already
bound compile or behavior pass. The failure code
`UNITY_SCREENSHOT_NOT_MATERIALIZED` is evidence-owned and can grant only the
`evidence_construction` phase.

The disposable Unity clone receives a trusted `OneBriefAtomicScreenshot`
helper. It renders the real camera and measured canvases to a `RenderTexture`,
reads pixels synchronously, writes and flushes a temporary PNG, verifies its
decoded dimensions, atomically publishes it, and only then permits atomic
publication of `runtime-evidence.json`. The helper is validator-owned evidence
infrastructure and is never applied to the user's product repository.
