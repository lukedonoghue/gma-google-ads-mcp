#!/bin/bash
# Build a pinned image and deploy OAuth MCP with live changes fail-closed.
set -euo pipefail

project_id="${1:?Usage: $0 PROJECT_ID [REGION] [PUBLIC_BASE_URL] [LOGIN_CUSTOMER_ID] [ACCESS_ROOT_CUSTOMER_ID]}"
region="${2:-europe-west1}"
public_base_url="${3:-https://ads-mcp.growmyads.com}"
login_customer_id="${4:-5294823448}"
access_root_customer_id="${5:-2073274070}"
repository="gma-mcp"
service="gma-google-ads-mcp"
service_account_email="${service}@${project_id}.iam.gserviceaccount.com"
image_tag="$(git rev-parse --short=12 HEAD)"
image="${region}-docker.pkg.dev/${project_id}/${repository}/${service}:${image_tag}"

if ! [[ "$login_customer_id" =~ ^[0-9]{10}$ ]]; then
  echo "LOGIN_CUSTOMER_ID must be a 10-digit Google Ads manager ID." >&2
  exit 1
fi

if ! [[ "$access_root_customer_id" =~ ^[0-9]{10}$ ]]; then
  echo "ACCESS_ROOT_CUSTOMER_ID must be a 10-digit Google Ads manager ID." >&2
  exit 1
fi

if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "Refusing to deploy a dirty working tree. Commit and test the exact revision first." >&2
  exit 1
fi

command -v gcloud >/dev/null 2>&1 || {
  echo "gcloud is required." >&2
  exit 1
}

gcloud config set project "$project_id"

latest_enabled_version() {
  local secret_name="$1"
  local version
  version="$(gcloud secrets versions list "$secret_name" \
    --filter 'state=ENABLED' \
    --sort-by '~createTime' \
    --limit 1 \
    --format 'value(name.basename())')"
  if [ -z "$version" ]; then
    echo "Secret ${secret_name} has no enabled version." >&2
    exit 1
  fi
  printf '%s' "$version"
}

developer_token_version="$(latest_enabled_version gma-google-ads-developer-token)"
oauth_client_id_version="$(latest_enabled_version gma-google-oauth-client-id)"
oauth_client_secret_version="$(latest_enabled_version gma-google-oauth-client-secret)"
jwt_signing_key_version="$(latest_enabled_version gma-mcp-jwt-signing-key)"
storage_key_version="$(latest_enabled_version gma-mcp-storage-encryption-key)"

gcloud builds submit --tag "$image" .

gcloud run deploy "$service" \
  --image "$image" \
  --region "$region" \
  --platform managed \
  --execution-environment gen2 \
  --service-account "$service_account_email" \
  --allow-unauthenticated \
  --cpu 1 \
  --memory 1Gi \
  --concurrency 20 \
  --timeout 300 \
  --min-instances 0 \
  --max-instances 5 \
  --set-env-vars "GMA_MCP_ENV=production,GMA_MCP_OAUTH_STORAGE=firestore,GMA_MCP_CHANGESET_STORAGE=firestore,GMA_MCP_SEARCH_MAX_ROWS=1000,GMA_ENABLE_CHANGESETS=1,GMA_ENABLE_CHANGESET_VALIDATION=0,GMA_ENABLE_MUTATIONS=0,GMA_ALLOW_LIVE_MUTATIONS=0,GMA_DEVELOPER_TOKEN_AD_MANAGEMENT_CONFIRMED=0,GMA_MCP_ENFORCED_LOGIN_CUSTOMER_ID=${login_customer_id},GMA_MCP_ACCESS_ROOT_CUSTOMER_ID=${access_root_customer_id},GOOGLE_PROJECT_ID=${project_id},GOOGLE_ADS_MCP_BASE_URL=${public_base_url}" \
  --set-secrets "GOOGLE_ADS_DEVELOPER_TOKEN=gma-google-ads-developer-token:${developer_token_version},GOOGLE_ADS_MCP_OAUTH_CLIENT_ID=gma-google-oauth-client-id:${oauth_client_id_version},GOOGLE_ADS_MCP_OAUTH_CLIENT_SECRET=gma-google-oauth-client-secret:${oauth_client_secret_version},GMA_MCP_JWT_SIGNING_KEY=gma-mcp-jwt-signing-key:${jwt_signing_key_version},GMA_MCP_STORAGE_ENCRYPTION_KEY=gma-mcp-storage-encryption-key:${storage_key_version}" \
  --labels "product=gma-13-skills,component=google-ads-mcp"

service_url="$(gcloud run services describe "$service" \
  --region "$region" \
  --format 'value(status.url)')"

echo "Cloud Run service: ${service_url}"
echo "OAuth base URL: ${public_base_url}"
echo "Next: deploy/map-domain.sh ${project_id} ${region}"
