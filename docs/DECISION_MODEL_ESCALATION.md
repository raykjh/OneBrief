# Decision-Critical Model Escalation

## Purpose

OneBrief spends more model capability only where one judgment can change the whole run.
This is a quality policy, not an invitation to upgrade every worker.

## Deterministic assignment

| Work | Approved model tier |
|---|---|
| Team planning | `gemini-3.1-pro-preview` |
| Project-owner final approval | `gemini-3.1-pro-preview` |
| Independent verification | `gemini-3.1-pro-preview` |
| Guardian policy review | `gemini-3.1-pro-preview` |
| Public grounded research | `gemini-3.5-flash` |
| Architecture, analysis, creation, integration, revision | `gemini-3.5-flash` or `gemini-3.5-flash-lite` |

The Project Owner still selects the smallest team. After normalization, code binds Pro
to owner, critic, and guardian instances and removes Pro from routine roles. Model
selection never changes authority or allows a maker to approve its own work.

## Budget and runtime enforcement

The versioned price card records standard global prices per million tokens:

- Gemini 3.1 Pro Preview: input `$2.00`, output and reasoning `$12.00`.
- Gemini 3.5 Flash: input `$1.50`, output and reasoning `$9.00`.
- Gemini 3.5 Flash-Lite: input `$0.30`, output and reasoning `$2.50`.

The producer reprices the selected TeamPlan before execution. The immutable model policy
binds every stage to one exact model, and the budget gateway blocks a different model or
an insufficient hard cap before any provider call.

## Platform boundary

`gemini-3.1-pro-preview` is a preview model and is available only on the global endpoint.
OneBrief already uses the global Vertex AI endpoint for Gemini calls. A production plan
that forbids preview models can omit this catalog entry and retain the same stage policy
with the highest approved GA model.

Official sources:

- https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/gemini/3-1-pro
- https://cloud.google.com/gemini-enterprise-agent-platform/generative-ai/pricing
