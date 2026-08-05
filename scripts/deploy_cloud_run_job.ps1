param(
    [string]$ProjectId = "onebrief-agent-20260805",
    [string]$Region = "asia-northeast3",
    [string]$Bucket = "onebrief-agent-20260805-jobs",
    [string]$Repository = "onebrief",
    [string]$JobName = "onebrief-worker",
    [string]$ImageTag = "cloud-v2"
)

$ErrorActionPreference = "Stop"
$serviceAccount = "$JobName@$ProjectId.iam.gserviceaccount.com"
$image = "$Region-docker.pkg.dev/$ProjectId/$Repository/worker`:$ImageTag"

gcloud services enable `
    run.googleapis.com `
    cloudbuild.googleapis.com `
    artifactregistry.googleapis.com `
    storage.googleapis.com `
    aiplatform.googleapis.com `
    --project=$ProjectId `
    --quiet
if ($LASTEXITCODE -ne 0) { throw "API activation failed" }

gcloud storage buckets describe "gs://$Bucket" --project=$ProjectId --format="value(name)" 2>$null
if ($LASTEXITCODE -ne 0) {
    gcloud storage buckets create "gs://$Bucket" `
        --project=$ProjectId `
        --location=$Region `
        --uniform-bucket-level-access `
        --public-access-prevention
    if ($LASTEXITCODE -ne 0) { throw "Bucket creation failed" }
}

gcloud iam service-accounts describe $serviceAccount --project=$ProjectId --format="value(email)" 2>$null
if ($LASTEXITCODE -ne 0) {
    gcloud iam service-accounts create $JobName `
        --project=$ProjectId `
        --display-name="OneBrief Cloud Run worker"
    if ($LASTEXITCODE -ne 0) { throw "Service account creation failed" }
}

gcloud artifacts repositories describe $Repository `
    --project=$ProjectId `
    --location=$Region `
    --format="value(name)" 2>$null
if ($LASTEXITCODE -ne 0) {
    gcloud artifacts repositories create $Repository `
        --project=$ProjectId `
        --location=$Region `
        --repository-format=docker `
        --description="OneBrief Cloud Run worker images"
    if ($LASTEXITCODE -ne 0) { throw "Artifact Registry creation failed" }
}

gcloud storage buckets add-iam-policy-binding "gs://$Bucket" `
    --member="serviceAccount:$serviceAccount" `
    --role="roles/storage.objectUser" `
    --project=$ProjectId `
    --quiet
if ($LASTEXITCODE -ne 0) { throw "Bucket IAM update failed" }

gcloud projects add-iam-policy-binding $ProjectId `
    --member="serviceAccount:$serviceAccount" `
    --role="roles/aiplatform.user" `
    --condition=None `
    --quiet
if ($LASTEXITCODE -ne 0) { throw "Vertex AI IAM update failed" }

gcloud builds submit `
    --project=$ProjectId `
    --region=$Region `
    --tag=$image `
    . `
    --quiet
if ($LASTEXITCODE -ne 0) { throw "Container build failed" }

gcloud run jobs deploy $JobName `
    --project=$ProjectId `
    --region=$Region `
    --image=$image `
    --service-account=$serviceAccount `
    --tasks=1 `
    --parallelism=1 `
    --max-retries=0 `
    --task-timeout=3600s `
    --cpu=1 `
    --memory=1Gi `
    --set-env-vars="GOOGLE_GENAI_USE_VERTEXAI=TRUE,GOOGLE_CLOUD_PROJECT=$ProjectId,GOOGLE_CLOUD_LOCATION=global" `
    --quiet
if ($LASTEXITCODE -ne 0) { throw "Cloud Run Job deployment failed" }

Write-Host "Cloud Run Job deployed: $JobName"
Write-Host "Image: $image"
Write-Host "Bucket: gs://$Bucket"
