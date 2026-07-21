#!/bin/bash
# Verify health, OAuth discovery, and protection without accessing Ads data.
set -euo pipefail

base_url="${1:-https://ads-mcp.growmyads.com}"

curl --fail --silent --show-error "${base_url}/healthz" | \
  python3 -m json.tool

curl --fail --silent --show-error \
  "${base_url}/.well-known/oauth-authorization-server" | \
  python3 -m json.tool >/dev/null

status="$(curl --silent --output /dev/null --write-out '%{http_code}' \
  --request POST \
  --header 'Content-Type: application/json' \
  --header 'Accept: application/json, text/event-stream' \
  --data '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"gma-smoke","version":"1"}}}' \
  "${base_url}/mcp")"

case "$status" in
  401|403)
    echo "PASS: unauthenticated MCP request was rejected (${status})."
    ;;
  *)
    echo "FAIL: expected 401/403 from unauthenticated MCP request, got ${status}." >&2
    exit 1
    ;;
esac

echo "PASS: health, OAuth discovery, and access protection are working."
