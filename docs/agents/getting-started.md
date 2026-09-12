# Connect an AI agent to PostDelegate

PostDelegate's BrightBean workspace exposes MCP and REST over the same workspace permissions. The public website is not the API. Until the full app is deployed, use a local client with the local preview; a cloud-hosted assistant cannot reach your computer's loopback address.

## Connect

Open **Settings → API Keys** for the deployment's MCP URL and API reference. The endpoint is `<workspace-origin>/api/v1/mcp` (no trailing slash). A remote OAuth-capable MCP client can sign in and request authorization. OAuth acts with the signed-in user's permissions; it is not automatically a limited draft-only account.

For a Bearer-token client, issue an API key for the intended workspace and connected-account allowlist. Begin with `create_posts`; add `upload_media` or `view_analytics` only when needed. Leaving `publish_directly` disabled prevents the key from scheduling posts. Store the token in the client's credential settings, not in the brief or shared chat.

The app supports these protocols; exact connector setup depends on the client. A public connector directory listing or certification is not claimed.

## First useful workflow

1. Ask the agent to call `list_accounts` and inspect the permitted accounts and capabilities. Select a real connected account; no accounts means the owner needs to connect one in the web app.
2. Give a brief and ask for `create_draft`. The REST create endpoint accepts a stable idempotency key. MCP create_draft does not accept that field; do not send it. Check the returned post ID and the draft in the web workspace.
3. Complete the configured review in the web app. To schedule that same existing post, call `schedule_draft` with its ID using an actor allowed to publish. `schedule_post` creates a new scheduled post; it is not an alias for scheduling an existing draft.
4. Poll `get_post` for per-platform results. A scheduled response is not evidence of publication. Review failed or uncertain outcomes in the workspace before trying again.

Starter prompt:

> List the accounts I can access and their capabilities. Ask which one to use, then turn my brief into a draft. Do not schedule or publish it. Return the post ID so I can review it in PostDelegate.

After review:

> Schedule the existing approved draft with this post ID for the time and timezone I specify. Do not create another post. If permissions or approvals prevent scheduling, explain the error and stop.

## Media and retries

Use existing media IDs, small MCP `upload_media` payloads, or the `request_media_upload` → upload bytes to the returned URL → `finalize_media_upload` flow. Follow the returned limits and server validation. Do not substitute an arbitrary external URL for a MediaAsset ID.

Retry a timed-out REST create request with the same idempotency key and identical payload. MCP create_draft is not idempotent: if its response is lost, inspect list_posts before creating another draft. Do not create a new key merely because a response was lost. Respect `Retry-After` for rate limits. Authentication errors require reconnecting/reissuing credentials; permission errors require the owner to change the grant, not repeated attempts.

An uncertain social-platform publication is distinct from a retriable API call: the external post may already exist. Reconcile it before any new publishing attempt.

## Current limits

Provider approval and live publishing verification are still launch requirements. AI text/image/video generation is not bundled or enabled merely by connecting MCP; an external agent may create content using its own tools. The app currently supports REST/MCP; no dedicated PostDelegate CLI is shipped.

CSV imports currently support text/schedule data. Media URL columns are rejected explicitly until safe media ingestion is implemented; use the media upload workflow and attach media to drafts instead.
