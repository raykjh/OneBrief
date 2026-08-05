# Cloud Run Jobs and Cloud Storage

OneBrief can submit a locally approved durable job to Cloud Storage, execute it
as a Cloud Run Job, and publish the immutable result package back to the same GCS
job prefix.

## Deployed resources

- Project: `onebrief-agent-20260805`
- Region: `asia-northeast3` (Seoul)
- Bucket: `gs://onebrief-agent-20260805-jobs`
- Artifact Registry: `asia-northeast3-docker.pkg.dev/onebrief-agent-20260805/onebrief`
- Cloud Run Job: `onebrief-worker`
- Service identity: `onebrief-worker@onebrief-agent-20260805.iam.gserviceaccount.com`

The service identity has only `roles/storage.objectUser` on the job bucket and
`roles/aiplatform.user` on the project. No service-account key file is created or
placed in the container. Cloud Run supplies Application Default Credentials.

## Data flow

```text
Local job-create
  -> GCS jobs/<job-id>/
  -> Cloud Run execution with ONEBRIEF_JOB_URI override
  -> generation-match claim object
  -> ephemeral /tmp download
  -> analyst/writer/verifier/reviser through budget gateway
  -> GCS job state + work artifacts + result-vNNN
  -> local cloud-status / cloud-download
```

The claim object uses an object-generation precondition. Two Cloud Run executions
cannot process the same GCS job prefix successfully. Cloud Run retries are set to
zero because the OneBrief pipeline, rather than infrastructure retries, owns its
budget and resume policy.

## Deploy

From the repository root:

```powershell
.\scripts\deploy_cloud_run_job.ps1
```

The script enables required APIs, creates missing resources, applies the two
worker permissions, builds the container in Cloud Build, and deploys the job.

## Submit

Create a normal queued job first. Then upload it and start Cloud Run without
waiting for completion:

```powershell
.venv\Scripts\onebrief cloud-submit `
  output\jobs\<job-id> `
  --bucket onebrief-agent-20260805-jobs `
  --project onebrief-agent-20260805 `
  --region asia-northeast3 `
  --cloud-run-job onebrief-worker
```

The returned receipt contains the GCS job URI and Cloud Run operation name.

## Observe and retrieve

Read durable state directly from Cloud Storage:

```powershell
.venv\Scripts\onebrief cloud-status `
  gs://onebrief-agent-20260805-jobs/jobs/<job-id>
```

Download a completed result package:

```powershell
.venv\Scripts\onebrief cloud-download `
  gs://onebrief-agent-20260805-jobs/jobs/<job-id> `
  --output-dir output\downloads\<job-id>
```

The downloaded `package_manifest.json` hash must equal
`result_manifest_sha256` in the remote `job.json`.

## Remote layout

```text
jobs/<job-id>/
  job.json
  inputs/
  run/
  work/
  packages/result-v001/
  control/
    claim.json
    completion.json
```

Raw source contents remain under the private `inputs/` prefix and are not copied
into the result package. The result contains only a source manifest with names,
requirement keys, sizes, media types, and SHA-256 hashes.

## Verified cloud smoke run

- Job ID: `3584c0c3-43ca-4ddd-b35b-22054bf1d242`
- Cloud Run execution: `onebrief-worker-92q2n`
- Tasks: 1 successful
- Cloud Run execution time: 18.14 seconds
- Package: `packages/result-v001`
- Manifest SHA-256:
  `cbe8028e76531d6c8b051bd9db37898d2bdb1e03cf9e24c64b4c866a7b2385ae`
- Gemini calls: 0 because completed checkpoints were intentionally supplied
- Model cost ledger: `$0`

This smoke run proves GCS upload, asynchronous Cloud Run execution, service
identity access, checkpoint reuse, result publication, package download, and
end-to-end manifest integrity without spending model budget.
