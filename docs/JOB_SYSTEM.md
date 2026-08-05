# Durable Job System

OneBrief can turn an approved brief into a durable work order, return control to
the user immediately, and publish a versioned result package when the worker
finishes.

## Lifecycle

1. `job-create` validates the ready requirements, snapshots authoritative source
   content, and creates the immutable budget approval.
2. `job-start` launches a detached worker process and returns without waiting for
   model execution.
3. The worker atomically claims the queued job. A second worker cannot claim the
   same job.
4. The existing checkpointed pipeline runs analyst, writer, verifier, and revision
   calls through the shared budget gateway.
5. The worker publishes a new immutable result package and updates `job.json`.

## Commands

Create a queued job:

```powershell
.venv\Scripts\onebrief job-create `
  samples\intake.json `
  output\prepared\requirements_reinspection.json `
  samples\uploads.json `
  output\prepared\budget_estimate.json `
  --jobs-dir output\jobs `
  --recommended
```

The command prints the new job directory. Start it in the background:

```powershell
.venv\Scripts\onebrief job-start output\jobs\<job-id>
```

Check progress without opening a chat session:

```powershell
.venv\Scripts\onebrief job-status output\jobs\<job-id>
```

`job-worker` runs the same work in the current process and is useful for local
diagnostics or a managed container entry point:

```powershell
.venv\Scripts\onebrief job-worker output\jobs\<job-id>
```

## Job layout

```text
<job-id>/
  job.json
  inputs/
    intake.json
    requirements.json
    sources.json
    source_manifest.json
    budget_estimate.json
    input_manifest.json
  run/
    approval.json
    cost_ledger.json
  work/
    execution_checkpoint.json
    analysis.json
    draft_r0.json
    verification_r0.json
    final.md
  logs/
    worker.stdout.log
    worker.stderr.log
  packages/
    result-v001/
      package_manifest.json
      artifacts/
      audit/
      evidence/
```

The private source contents stay in `inputs/sources.json`. Result packages receive
only the source manifest, which contains names, sizes, media types, requirement
keys, and hashes but not source contents.

The worker verifies the input manifest before any agent or model call and fails
closed if an approved input was added, removed, or changed.

## Durable states

- `queued`: input and approval snapshot is complete
- `running`: one worker has claimed the job
- `complete`: independent verification passed
- `partial`: the revision limit was reached
- `needs_information`: the verifier needs authoritative user information
- `needs_budget`: the budget gateway blocked a call before provider invocation
- `failed`: an unexpected execution or packaging error occurred

Every JSON state update uses a temporary file followed by an atomic replace.
Result directories are published only after every included file has been copied
and hashed. Each package therefore provides its own SHA-256 manifest plus the
tamper-evident model cost ledger.
