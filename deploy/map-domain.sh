#!/bin/bash
# Create the alpha custom-domain mapping and print the DNS records to add.
set -euo pipefail

project_id="${1:?Usage: $0 PROJECT_ID [REGION] [DOMAIN]}"
region="${2:-europe-west1}"
domain="${3:-ads-mcp.growmyads.com}"
service="gma-google-ads-mcp"

gcloud config set project "$project_id"

if ! gcloud beta run domain-mappings describe \
  --domain "$domain" \
  --region "$region" >/dev/null 2>&1; then
  gcloud beta run domain-mappings create \
    --service "$service" \
    --domain "$domain" \
    --region "$region"
fi

echo "Add the following records in Cloudflare as DNS-only while Google provisions TLS:"
gcloud beta run domain-mappings describe \
  --domain "$domain" \
  --region "$region" \
  --format 'table(status.resourceRecords[].type,status.resourceRecords[].rrdata)'

echo "After DNS and TLS are ready: deploy/smoke-test.sh https://${domain}"
