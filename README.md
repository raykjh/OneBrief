# OneBrief

OneBrief lets a non-expert describe a goal once, then automatically assembles and
supervises an agent team until the result meets an explicit definition of done. It is a
general-purpose, quality-convergence operating system rather than a long-form writer or
an agent-count showcase.

The product contract is defined in `docs/PRODUCT_IDENTITY.md`. Example projects and
domain ToolPacks are validation cases; they do not define the product.

## Implemented milestones

- Requirements Analyst built with Google ADK and Gemini 3.5 Flash
- ADK-native Accountable Maker -> Independent Verifier -> original-maker revision loop
- software convergence with isolated ToolPack build/test execution before independent review
- deterministic grounding, completion-evidence, and reality gates that can overrule model PASS
- mandatory versus optional information classification
- local text-source upload with SHA-256 source manifests
- semantic reinspection after upload
- hard gate preventing cost estimation while mandatory information is missing
- deterministic Producer with versioned Gemini pricing and bounded estimates
- durable work orders with snapshotted inputs and immutable budget approval
- detached background workers with exclusive job claiming
- versioned result packages containing artifacts, evidence manifests, and cost audit data
- asynchronous Cloud Run Job execution with a dedicated service identity
- Cloud Storage job transport and verified result-package delivery
- one bounded Gemini 3.5 Flash Google Search grounding step with preserved source URLs
- deterministic Markdown-table to `.xlsx` export with a separate public-source sheet
- fixed grounded-search fee reservation inside the same immutable user budget

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

See `docs/JOB_SYSTEM.md` for job creation, detached execution, status polling,
and result package structure.

See `docs/CLOUD_RUN_JOBS.md` for cloud deployment, submission, observation, and
result retrieval.
