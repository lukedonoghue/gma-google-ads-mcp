# Grow My Ads Hosted Connector Deployment

This fork deploys Google's official read-only Google Ads MCP server to Cloud
Run for the GMA 13 Skills product. It adds the production controls missing from
the upstream sample deployment:

- encrypted Firestore persistence for OAuth clients and refresh tokens;
- a stable JWT signing key from Secret Manager;
- stateless HTTP operation so Cloud Run instances can scale safely;
- an unauthenticated health endpoint but OAuth protection on `/mcp`;
- a 1,000-row report cap and no full GAQL/customer IDs in logs;
- optional per-request manager (`login_customer_id`) context; and
- an enforced PPC Navigator manager boundary for the hosted GMA deployment;
- pinned runtime dependencies.

The server remains read-only. Its only tools are customer discovery, resource
metadata, and Google Ads reporting search. It has no mutation service.

## Decisions required before creating cloud resources

1. **Google Cloud project ID.** Use a dedicated project such as
   `gma-google-ads-skills`, with billing enabled.
2. **Region and Firestore location.** The scripts default Cloud Run to
   `europe-west1` and Firestore to the European multi-region `eur3`. This keeps
   the encrypted OAuth token store in Europe. Firestore location is a durable
   project decision; confirm it before running bootstrap.
3. **Developer token.** A production-capable Google Ads developer token is
   required. For a private alpha, the existing GMA token can be used if Luke
   explicitly accepts the shared quota. Create a dedicated product token before
   public launch.
4. **OAuth publishing status.** Private testing may use named test users. Public
   customers require the app in production and Google verification where
   required.

## 1. Install and authenticate Google Cloud CLI

On macOS:

```text
brew install --cask google-cloud-sdk
gcloud auth login
gcloud auth application-default login
```

The Cloud Run service uses its own service account; do not upload or set a local
`GOOGLE_APPLICATION_CREDENTIALS` file on Cloud Run.

## 2. Create the Google OAuth application

In the chosen Google Cloud project:

1. Enable the Google Ads API and configure Google Auth Platform branding.
2. Use **Grow My Ads Skills** as the app name and `growmyads.com` as an
   authorized domain.
3. Publish a real home page, privacy policy, and terms page before verification.
   The privacy policy must disclose that the GMA connector brokers Google Ads
   requests, stores encrypted OAuth credentials, and does not persist report
   rows.
4. Request `openid`, email/profile, and
   `https://www.googleapis.com/auth/adwords`. Google offers no narrower
   read-only Ads scope; read-only is enforced by the tools this server exposes.
5. Create an OAuth client of type **Web application**.
6. Add this exact authorized redirect URI:

```text
https://ads-mcp.growmyads.com/auth/callback
```

Keep the client secret out of the repository and chat transcripts.

## 3. Bootstrap cloud resources

Review the Firestore location, then run:

```text
./deploy/bootstrap-gcp.sh YOUR_PROJECT_ID europe-west1 eur3
```

This enables the required APIs, creates Artifact Registry, creates the Cloud
Run service identity, grants only Firestore data access, and creates a
delete-protected Firestore database.

## 4. Add secrets

Run the interactive secret loader:

```text
./deploy/create-secrets.sh YOUR_PROJECT_ID
```

It prompts without echoing for the Google Ads developer token, OAuth client ID,
and OAuth client secret. It generates the JWT and encryption keys locally and
pipes every value directly into Secret Manager. It writes no secret file.

## 5. Build and deploy

```text
./deploy/deploy-cloud-run.sh YOUR_PROJECT_ID europe-west1 \
  https://ads-mcp.growmyads.com 2073274070
```

The deployment pins specific Secret Manager versions to the Cloud Run revision.
The service is publicly reachable because MCP clients must reach its OAuth
routes; application-level OAuth still protects the MCP tools. The final
argument enforces PPC Navigator as the only `login_customer_id`; requests
cannot substitute the parent Grow My Ads MCC even though the company developer
token is owned there.

## 6. Map the domain

For the alpha, use Cloud Run's direct domain mapping:

```text
gcloud domains verify growmyads.com
./deploy/map-domain.sh YOUR_PROJECT_ID europe-west1 ads-mcp.growmyads.com
```

The first command opens Google Search Console if this Google account has not
already verified the domain.

Add the printed records in Cloudflare. Keep them **DNS only** while Google
provisions and renews the certificate, and do not place an additional redirect
or “Always Use HTTPS” rule in front of the validation path. Direct Cloud Run
domain mappings are currently a limited/preview option; before a larger launch,
move the custom hostname to Google's recommended external Application Load
Balancer and Cloud Armor without changing the public MCP URL.

## 7. Smoke test before Google Ads access

```text
./deploy/smoke-test.sh https://ads-mcp.growmyads.com
```

This confirms the health route, OAuth discovery, and rejection of an
unauthenticated MCP request. It does not touch an Ads account.

Then connect from Codex and Claude Code, complete Google OAuth, and call only:

1. `list_accessible_customers`;
2. one customer-name/settings query; and
3. one small campaign query with a limit of 5.

Only after those pass should the 14-skill acceptance suite in the plugin's
`LIVE-TEST.md` begin.

## Production checks before selling access

- OAuth consent screen is production/verified; test-mode seven-day token expiry
  is not acceptable for customers.
- A dedicated production-capable Ads developer token is in use.
- Privacy policy and retention schedule cover encrypted OAuth tokens, Firestore,
  Secret Manager, and Cloud Logging.
- Cloud Monitoring alerts cover 5xx errors, latency, instance failures, Ads API
  quota errors, and OAuth failures.
- Firestore backup/retention and secret rotation are documented and tested.
- Redirect-URI allowlisting is tightened after recording the verified Codex,
  Claude Code, and Claude.ai callback URIs.
- Cloud Run spend and Google Ads API operations per skill run have been measured.
- The external load balancer/Cloud Armor migration is completed before broad
  public distribution.
