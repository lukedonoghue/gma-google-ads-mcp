#!/bin/bash
# Add secret values without placing them in shell history or repository files.
set -euo pipefail

project_id="${1:?Usage: $0 PROJECT_ID}"
service_account_email="gma-google-ads-mcp@${project_id}.iam.gserviceaccount.com"

command -v gcloud >/dev/null 2>&1 || {
  echo "gcloud is required." >&2
  exit 1
}

gcloud config set project "$project_id"

ensure_secret() {
  local secret_name="$1"
  if ! gcloud secrets describe "$secret_name" >/dev/null 2>&1; then
    gcloud secrets create "$secret_name" --replication-policy automatic
  fi
  gcloud secrets add-iam-policy-binding "$secret_name" \
    --member "serviceAccount:${service_account_email}" \
    --role roles/secretmanager.secretAccessor >/dev/null
}

prompt_secret() {
  local secret_name="$1"
  local prompt="$2"
  local secret_value
  ensure_secret "$secret_name"
  read -r -s -p "$prompt: " secret_value
  echo
  if [ -z "$secret_value" ]; then
    echo "No value entered for ${secret_name}; stopping." >&2
    exit 1
  fi
  printf '%s' "$secret_value" | \
    gcloud secrets versions add "$secret_name" --data-file=- >/dev/null
  unset secret_value
  echo "Added a new version of ${secret_name}."
}

prompt_secret "gma-google-ads-developer-token" \
  "Google Ads developer token"
prompt_secret "gma-google-oauth-client-id" \
  "Google OAuth web client ID"
prompt_secret "gma-google-oauth-client-secret" \
  "Google OAuth web client secret"

ensure_secret "gma-mcp-jwt-signing-key"
python3 -c 'import secrets; print(secrets.token_urlsafe(64), end="")' | \
  gcloud secrets versions add gma-mcp-jwt-signing-key --data-file=- >/dev/null

ensure_secret "gma-mcp-storage-encryption-key"
python3 -c 'import base64,os; print(base64.urlsafe_b64encode(os.urandom(32)).decode(), end="")' | \
  gcloud secrets versions add gma-mcp-storage-encryption-key --data-file=- >/dev/null

echo "Secrets are ready. Values were not written to disk."
echo "Next: deploy/deploy-cloud-run.sh ${project_id}"
