# Hackathon Development and Prior-Work Disclosure

## New hackathon work

KHALINOS, developed under the internal codename OneBrief, was created on 2026-08-05
during the All Things Agentic Hackathon submission period. The KHALINOS orchestration
system, ADK agents, budget gateway, ToolPack lifecycle,
isolated execution workflow, verification gates, web interface, Cloud Run job transport,
and result packaging are hackathon-period work.

## Development assistance

OpenAI Codex was used as an AI coding assistant for implementation, review, testing, and
documentation. Product goals, agent responsibilities, authority boundaries, convergence
policy, budget policy, and final technical decisions were directed by the entrant. The
submitted runtime uses Gemini through Vertex AI and Google ADK; Codex is not a model or
agent dependency of the submitted application.

The runtime uses only Google Gemini models. Routine work uses Gemini 3.5 Flash tiers;
bounded decision-critical stages may use Gemini 3.1 Pro Preview. The latter is a Google
Cloud preview model and is disclosed as such. Every selected model is included in the
budget estimate and enforced by the same immutable execution gateway.

## Pre-existing demonstration projects

Exchange and JULPAE are pre-existing projects owned by the entrant. They are not submitted
as new hackathon products. When used in a demonstration, they serve only as existing-project
inputs that prove KHALINOS can inspect an approved repository, generate a bounded ToolPack,
edit an isolated copy, run allowlisted verification, and return a reviewable result package.
All pre-existing project code remains clearly separated from the new KHALINOS repository.

## Third-party and open-source components

KHALINOS uses Google ADK, Google GenAI/Vertex AI, Google Cloud Run, Google Cloud Storage,
FastAPI, Pydantic, Uvicorn, Filelock, XlsxWriter, and development-only Pytest/HTTPX under
their respective licenses and terms. These components provide standard infrastructure;
the submitted contribution is the KHALINOS workflow and its safety, budget, evidence, and
quality-convergence layers.
