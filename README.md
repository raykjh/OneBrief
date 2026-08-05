# OneBrief

OneBrief turns one goal and an authoritative source package into a validated
long-form deliverable with minimal user interruption.

## Implemented milestones

- Requirements Analyst built with Google ADK and Gemini 3.5 Flash
- mandatory versus optional information classification
- local text-source upload with SHA-256 source manifests
- semantic reinspection after upload
- hard gate preventing cost estimation while mandatory information is missing
- deterministic Producer with versioned Gemini pricing and bounded estimates

## Local setup

Use Python 3.12 and Application Default Credentials for the Google account that can
access `onebrief-agent-20260805`.

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
$env:GOOGLE_GENAI_USE_VERTEXAI="TRUE"
$env:GOOGLE_CLOUD_PROJECT="onebrief-agent-20260805"
$env:GOOGLE_CLOUD_LOCATION="global"
.venv\Scripts\onebrief analyze samples\intake_missing_information.json
```

After supplying the requested sources:

```powershell
.venv\Scripts\onebrief prepare `
  samples\intake_missing_information.json `
  output\requirements_analysis_ko.json `
  samples\hiring_uploads.json `
  --output-dir output\prepared_hiring
```

See `docs/WORKFLOW.md` for the gate behavior and generated artifacts.

