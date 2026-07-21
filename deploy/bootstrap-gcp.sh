#!/bin/bash
# Create the non-secret Google Cloud resources used by the hosted MCP service.
set -euo pipefail

project_id="${1:?Usage: $0 PROJECT_ID [REGION] [FIRESTORE_LOCATION]}"
region="${2:-europe-west1}"
firestore_location="${3:-eur3}"
repository="gma-mcp"
service_account="gma-google-ads-mcp"
service_account_email="${service_account}@${project_id}.iam.gserviceaccount.com"

command -v gcloud >/dev/null 2>&1 || {
  echo "gcloud is required. Install the Google Cloud CLI first." >&2
  exit 1
}

gcloud config set project "$project_id"

gcloud services enable \
  artifactregistry.googleapis.com \
  cloudbuild.googleapis.com \
  firestore.googleapis.com \
  googleads.googleapis.com \
  iam.googleapis.com \
  run.googleapis.com \
  secretmanager.googleapis.com

if ! gcloud artifacts repositories describe "$repository" \
  --location "$region" >/dev/null 2>&1; then
  gcloud artifacts repositories create "$repository" \
    --repository-format docker \
    --location "$region" \
    --description "Grow My Ads MCP containers"
fi

if ! gcloud iam service-accounts describe "$service_account_email" \
  >/dev/null 2>&1; then
  gcloud iam service-accounts create "$service_account" \
    --display-name "GMA Google Ads MCP Cloud Run"
fi

gcloud projects add-iam-policy-binding "$project_id" \
  --member "serviceAccount:${service_account_email}" \
  --role roles/datastore.user \
  --condition=None >/dev/null

if ! gcloud firestore databases describe --database "(default)" \
  >/dev/null 2>&1; then
  echo "Creating Firestore in ${firestore_location}. This location is a durable project choice."
  gcloud firestore databases create \
    --database "(default)" \
    --location "$firestore_location" \
    --type firestore-native \
    --delete-protection
fi

echo "Bootstrap complete for ${project_id} in ${region}."
echo "Next: deploy/create-secrets.sh ${project_id}"
