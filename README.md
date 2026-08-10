# OneBrief

OneBrief manages completion criteria and their evidence so AI work converges to a
genuinely completed state. A non-expert describes a goal once; OneBrief defines what
"done" means, assigns AI workers, rejects unsupported results, returns failures to the
accountable maker, and releases only evidence-backed work.

The product contract is defined in `docs/PRODUCT_IDENTITY.md`. Example projects and
domain ToolPacks are validation cases; they do not define the product.

The user experience has two stages: OneBrief first creates the completion criteria,
capability pack, permission manifest, and cost envelope for one exact approval; it then
works autonomously until the approved criteria are proven. See
`docs/TWO_STAGE_WORKFLOW.md`.

The stage-one intake includes **SixSense**: one Gemini inspection pre-generates at most
five material choices, then the browser presents them one at a time with no network wait
between questions. Recommended defaults are preselected, so a novice can turn a short
goal into a complete work contract in about 30 seconds. See `docs/SIXSENSE.md`.

## Implemented milestones

- Requirements Analyst built with Google ADK and Gemini 3.5 Flash
- SixSense rapid sequential intake with one-pass question generation and recommended defaults
- ADK-native Accountable Maker -> Independent Verifier -> original-maker revision loop
- criterion-level completion ledger showing pending, failed, revised, and proven work
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

See `docs/COMPLETION_CONVERGENCE.md` for the fixed product scope, system boundary, and
the evidence-driven convergence state machine.

See `docs/JOB_SYSTEM.md` for job creation, detached execution, status polling,
and result package structure.

See `docs/CLOUD_RUN_JOBS.md` for cloud deployment, submission, observation, and
result retrieval.

See `docs/PARALLEL_TEST_CAMPAIGNS.md` for the frozen-baseline architecture that runs
three evidence-only test lanes and permits only one integration lane to patch common
code and promote a fully revalidated candidate.
