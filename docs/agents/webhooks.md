# Outbound webhooks

PostDelegate can call your server when something happens in a workspace. Set endpoints up in
**Workspace settings > Webhooks**, or with the REST API (`/api/v1/webhooks/`) and the MCP tools
(`list_webhook_endpoints`, `create_webhook_endpoint`, `update_webhook_endpoint`,
`delete_webhook_endpoint`, `rotate_webhook_secret`, `send_webhook_test`, `list_webhook_deliveries`,
`resend_webhook_delivery`). Managing webhooks needs `manage_workspace_settings` or
`manage_social_accounts` (workspace owners and managers by default). An API key must carry one of them
and be allowed to act on every connected account in the workspace, because events report on all of
them.

## Events

| Type | Sent when |
| --- | --- |
| `post.scheduled` | A post is scheduled for an account |
| `post.published` | A post is live on an account |
| `post.failed` | A post failed for good, after its retries (including when the platform may have accepted it but we could not confirm or record it; `error` says so) |
| `post.partially_published` | A thread stopped part-way: the first post is live, later parts are not |
| `post.approved` | A post got its final approval |
| `post.changes_requested` | A reviewer asked for changes |
| `comment.created` | Someone commented on a post (internal and client comments; see `visibility`) |
| `inbox.message.received` | A new comment, mention, DM or review reached the inbox |
| `review.received` | A new customer review (e.g. Google Business Profile) reached the inbox: `rating` (1-5), `reviewer_name`, `body`, `has_reply`. Sent in addition to `inbox.message.received` |
| `account.disconnected` | A social account lost its connection, or a member removed it (`removed: true`). Sent once per break: removing an account that had already lost its connection sends nothing more |
| `agent.action.executed` | The autonomy policy let an agent or automation act (publish, reply, recycle). `release_at` is when it can first go out |
| `agent.action.held` | The policy held an agent's or automation's action as a draft for review; `reasons` says why |
| `agent.action.blocked` | The policy refused an agent's or automation's action; `reasons` says why |
| `webhook.test` | You pressed "Send test event" (never part of a subscription) |

The `post.*` events (except approvals) are sent **once per account a post targets**: a post going to
three accounts sends three `post.published` events, each with its own `platform_post_id` and
`social_account`. `post.approved` and `post.changes_requested` are sent once per decision and list the
targets that moved in `platform_post_ids` and `social_accounts`.

## The request

`POST` with a JSON body:

```json
{
  "id": "6f1c2f4e-9a3b-4d8e-8f0a-2b7f4c1d9e55",
  "type": "post.published",
  "api_version": "2026-10-01",
  "created_at": "2026-10-23T14:03:11.482913+00:00",
  "workspace": {"id": "...", "name": "Acme"},
  "data": {
    "post_id": "...",
    "platform_post_id": "...",
    "status": "published",
    "social_account": {"id": "...", "platform": "bluesky", "account_name": "...", "account_handle": "..."},
    "scheduled_at": "...",
    "published_at": "...",
    "external_id": "at://did:plc:.../app.bsky.feed.post/...",
    "permalink": "https://bsky.app/profile/.../post/..."
  }
}
```

`external_id` is the post's id on the platform. `permalink` is its public URL when the platform returned
one at publish time, otherwise `null`. `api_version` names the shape of the envelope and `data`; new
fields can appear without notice, but a field is never removed or changed in meaning without a new
`api_version`.

Headers:

- `PostDelegate-Signature: t=<unix seconds>,v1=<hex>` where `v1` is the HMAC-SHA256 of `"<t>.<raw body>"`
  keyed with your endpoint secret.
- `PostDelegate-Event`: the event type.
- `PostDelegate-Delivery`: the delivery id. It stays the same across retries and resends, so use it (or
  the body's `id`) to ignore duplicates.

Answer with any `2xx` within 10 seconds. Redirects are not followed. Anything else is a failure.

## Verifying the signature

Check the signature against the **raw** body, before parsing it, and reject old timestamps.

```python
import hashlib, hmac, time


def verify(secret: str, raw_body: bytes, header: str, tolerance: int = 300) -> bool:
    parts = dict(item.strip().split("=", 1) for item in header.split(","))
    timestamp = int(parts["t"])
    if abs(time.time() - timestamp) > tolerance:
        return False
    expected = hmac.new(secret.encode(), f"{timestamp}.".encode() + raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected.encode(), parts.get("v1", "").encode())
```

```js
import crypto from "node:crypto";

export function verify(secret, rawBody, header, tolerance = 300) {
  const parts = Object.fromEntries(header.split(",").map((p) => p.split("=")));
  if (Math.abs(Date.now() / 1000 - Number(parts.t)) > tolerance) return false;
  const expected = crypto.createHmac("sha256", secret).update(`${parts.t}.`).update(rawBody).digest("hex");
  const a = Buffer.from(expected), b = Buffer.from(parts.v1 ?? "");
  return a.length === b.length && crypto.timingSafeEqual(a, b);
}
```

## Retries and shut-off

A failed delivery is retried after 1 minute, 5 minutes, 30 minutes, 2 hours and 12 hours (six attempts
in all). After 15 failed attempts in a row an endpoint is turned off and the workspace owners are
notified; turn it back on from the settings page or with `PATCH /webhooks/{id}` (`"is_enabled": true`).
The delivery log keeps the last 100 deliveries per endpoint on screen and is pruned after 30 days.

A resend sends the same body under the same delivery id and starts the retry schedule again. It is
refused while the delivery is being sent or is due for a retry within the next two minutes.

## URL rules

Endpoints must be public `http(s)` URLs. Addresses that resolve to private, loopback or link-local
ranges are refused when you save the endpoint and again at send time, and the request is sent to the
address that was checked (the TLS certificate is still verified against your hostname).

## Secrets

The signing secret (`whsec_...`) is returned once, when you create the endpoint or rotate the secret.
Rotating replaces it immediately; update your receiver first if you cannot tolerate failures.
