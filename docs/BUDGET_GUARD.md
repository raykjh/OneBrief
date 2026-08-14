# Approval and Hard Budget Guard

## Safety contract

An execution run begins with one immutable approval tied to the SHA-256 of a specific
budget estimate. Every Gemini generation must use `BudgetedGeminiClient`.

Before the provider is called, the gateway:

1. asks Vertex AI to count the complete input tokens;
2. combines that count with the configured maximum output tokens;
3. prices the worst case with the approved versioned price card;
4. adds a 5% reservation safety margin;
5. atomically reserves that amount in the run ledger; and
6. blocks the call when actual spend plus concurrent reservations would exceed the
   approved amount.

After a successful response, provider-reported input, response, and thinking tokens
replace the reservation with actual model cost. Failed calls release their reservation.

## Non-borrowable execution-phase wallets

For software work, the immutable estimate includes separate wallets for:

- shared planning and analysis;
- product implementation and product repairs;
- evidence-harness construction and evidence repairs;
- independent/final verification; and
- an unspent reserve that normal stages cannot address.

`phase_budget_policy.json` scales these estimates to the exact approved total and binds
them to the approval ID, estimate digest, and its own SHA-256. Every call stage is routed
to one wallet before the global cap check. A call is denied if either its phase cap or
its AI-repair count is exhausted, even when another phase has money remaining. The
reserve is therefore real headroom, not implicit permission to continue any loop.

Build, test, runtime probe, and observation attempts are recorded separately in the
tamper-evident `phase_attempts.json`. Their count limit prevents a deterministic tool
loop from running indefinitely without spending model tokens. Old approvals without a
phase policy remain readable; new software approvals always carry the policy digest.

## Decision-critical model escalation

OneBrief does not upgrade the whole team. New team plans reserve
`gemini-3.1-pro-preview` only for the one-time team-planning call and the selected
project-owner, independent-critic, and guardian instances. Their executable stages are
final approval, independent verification, and policy review. Investigator, analyst,
maker, architect, creator, and integrator work remains on `gemini-3.5-flash` or
`gemini-3.5-flash-lite`.

The escalation is deterministic after the model-generated TeamPlan. If the provider
assigns Pro to a routine role, code downgrades that role to Flash. The producer then
reprices the exact stage bindings. An unaffordable or unapproved plan stops before the
provider call; it never silently substitutes another model. Gemini 3.1 Pro Preview is
global-endpoint-only, matching OneBrief's recorded `global-standard` price card.

## State transitions

```text
APPROVED -> RUNNING -> COMPLETE
                 \-> NEEDS_BUDGET
                 \-> FAILED
```

`NEEDS_BUDGET` never increases its own allowance. A new explicit approval workflow is
required. Approvals cannot be overwritten in place.

## Integrity and concurrency

- Dollar arithmetic is stored as integer microdollars.
- A file lock serializes all ledger mutations, including parallel calls.
- Every ledger revision has a canonical SHA-256 integrity value.
- Phase policies and deterministic-attempt ledgers have independent canonical hashes.
- The approval is duplicated in the ledger and compared with the standalone approval.
- Files are replaced atomically so interruption cannot leave half-written JSON.

## Scope of the guarantee

The hard cap covers Gemini model calls made through this gateway under the recorded
price-card version and token caps. It cannot cap unrelated Google Cloud services,
taxes, currency conversion, a provider price change, or calls that bypass the gateway.
Production deployment must therefore deny direct model credentials to worker code and
route all execution calls through this gateway service.
