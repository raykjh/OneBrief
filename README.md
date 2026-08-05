# OneBrief

OneBrief turns one goal and an authoritative source package into a validated
long-form deliverable with minimal user interruption.

## Current milestone

The first agent, **Requirements Analyst**, is implemented with Google ADK and
Gemini 3.5 Flash on Vertex AI. It:

- normalizes the goal;
- separates missing mandatory information from optional improvements;
- produces one consolidated set of questions;
- defines deliverables and measurable acceptance criteria; and
- blocks budget estimation until mandatory gaps are resolved.

## Local setup

Use Python 3.12 and Application Default Credentials for the Google account that
can access `onebrief-agent-20260805`.

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
$env:GOOGLE_GENAI_USE_VERTEXAI="TRUE"
$env:GOOGLE_CLOUD_PROJECT="onebrief-agent-20260805"
$env:GOOGLE_CLOUD_LOCATION="global"
.venv\Scripts\onebrief analyze samples\intake_missing_information.json
```

The example intentionally omits a company's private hiring rules and candidate
materials. A correct run must request those mandatory inputs in one batch and
set `ready_for_estimate` to `false`.

