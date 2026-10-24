# Connect an AI agent to PostDelegate

PostDelegate's BrightBean workspace exposes MCP and REST over the same workspace permissions. The public website is not the API. Until the full app is deployed, use a local client with the local preview; a cloud-hosted assistant cannot reach your computer's loopback address.

## Connect

Open **Settings → API Keys** for the deployment's MCP URL and API reference. The endpoint is `<workspace-origin>/api/v1/mcp` (no trailing slash). A remote OAuth-capable MCP client can sign in and request authorization. OAuth acts with the signed-in user's permissions, narrowed to the capabilities the user leaves ticked on the consent screen; it is not automatically a limited draft-only account.

For a Bearer-token client, issue a token for the intended workspace and connected-account allowlist and start from the **Drafting assistant** preset: it can read, write drafts and use the AI assistant, and a person approves everything. Add capabilities (media, analytics, scheduling) only when needed; the token cannot use a tool outside its capabilities. See [scoped-tokens.md](scoped-tokens.md) for presets, daily budgets, expiry, IP ranges and example client config. Store the token in the client's credential settings, not in the brief or shared chat.

The public page `/connect/` lists setup for each AI app and developer tool (Claude, ChatGPT, Copilot Studio, Gemini, Mistral, Perplexity, Cursor, VS Code, Raycast). Directory submission material is under `integrations/`. A public connector directory listing or certification is not claimed until a directory approves it.

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

## Threads

`create_draft`, `schedule_post`, and REST `POST`/`PATCH /posts` accept an optional `parts` list: the later parts of a thread, in order (the caption is part 1). Each part takes `caption`, `media_asset_ids`, `delay_seconds` (wait after the previous part, up to 24 hours), and `is_finisher` (a closing part, always last). Check each account's `thread_mode` in `list_accounts` first: `reply_chain` publishes each part as a reply to the one before, `comments` publishes later parts as comments on the first post (text only), and `none` cannot take a thread, so scheduling one there is refused. `thread_max_parts` and `thread_part_max_length` are enforced when scheduling.

If a thread stops part-way, `get_post` shows the platform post's `thread_status` as `partial` and each part's status. The parts already live are never re-sent. `resume_thread` (REST `POST /posts/{id}/resume-thread`) restarts at the stopped part. If that part is `unconfirmed` the platform may already have published it: check the platform first, then pass `confirm_unconfirmed=true` (a query parameter on REST), because resuming sends it again. Without it the resume is refused.

On a `comments` account a thread can't also carry a `first_comment` — both are comments on the first post and could interleave — so scheduling one is refused; make the first comment a part instead.

## Reviewing, queues and planning

Beyond drafting and scheduling, an agent can run the review loop (`list_approvals`, `review_post`, comments, `list_members` for @mentions), keep an account on its rhythm (`list_queues`, `add_post_to_queue`, `find_next_free_slot`, posting slots), plan content (ideas, templates, tags, categories, calendar events), read or set the brand voice, and check plan usage. Each tool states the permission it needs. The full list, with the REST path for each, is in [capabilities.md](capabilities.md). Keys limited to some accounts only see and act on posts, queues, slots and templates for those accounts. Create-like tools take an optional `idempotency_key` (REST: the `Idempotency-Key` header) so a retry after a timeout returns the first result instead of a duplicate.

`DELETE /posts/{id}` (MCP `delete_post`) removes a draft, approved or scheduled post; a post that is publishing or already published is kept as a record and refused with `409`.

## Webhooks

To be told when posts publish or fail, approvals are decided or messages arrive, add an outbound webhook endpoint (needs `manage_workspace_settings` or `manage_social_accounts`, and a key covering every account in the workspace). See [webhooks.md](webhooks.md).

## Current limits

Provider approval and live publishing verification are still launch requirements. AI text/image/video generation is not bundled or enabled merely by connecting MCP; an external agent may create content using its own tools. The app currently supports REST/MCP; no dedicated PostDelegate CLI is shipped.

CSV imports currently support text/schedule data. Media URL columns are rejected explicitly until safe media ingestion is implemented; use the media upload workflow and attach media to drafts instead.
