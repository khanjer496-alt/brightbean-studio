# Scoped agent tokens

A token for an outside agent (Claude, ChatGPT, n8n, your own code) can do exactly what you intend and no more. Three things bound it, and they stack:

1. **Workspace permissions**: the same permissions your team has. A token never has more than the person who issued it.
2. **Capabilities**: groups of tools in plain language ("Read posts & calendar", "Draft posts", "Schedule & publish"...). Anything outside the token's capabilities is closed, on REST and on MCP.
3. **Limits and guards**: daily budgets, an expiry date, allowed IP ranges, and a mode that keeps the token to drafts until a person approves.

## Create one

Open **Settings -> API Keys** and choose **Issue new token**. Pick a workspace and the accounts it may touch, then a starting point:

| Preset | Capabilities | Guards |
|---|---|---|
| Read-only analyst | Read posts & calendar, Analytics read, Settings read | 2,000 calls a day |
| Drafting assistant (needs approval) | Read posts & calendar, Draft posts, Media upload, AI writing assistant, Settings read | Drafts only until approved; 2,000 calls and 100 AI calls a day |
| Inbox helper (drafts replies) | Read posts & calendar, Inbox read, Inbox reply, Settings read | Drafts only until approved; 2,000 calls a day |
| Full agent (guardrails) | Everything above that writes, plus Schedule & publish and Inbox read/reply | Your autonomy policy applies; 5,000 calls, 200 AI calls and 10 scheduled posts a day |

A preset only fills in the form. Change anything you like, and edit the token again later; changes apply on the token's next request. "Approvals" and "Settings write" are in no preset: tick them deliberately.

The token is shown once. **Rotate** gives the same token (same scope, limits and history) a new secret and ends the old one immediately. **Revoke** ends it for good. Both take effect on the very next request on every server, because the key is read from the database each time rather than from a cache.

## What each capability opens

| Capability | Group | Risk | What it covers | Workspace permissions it uses | REST routes | MCP tools |
|---|---|---|---|---|---|---|
| **Read posts & calendar** (`read_calendar`) | Content | low | See posts, drafts, queues, posting slots, ideas, templates, tags, events, team members, connected accounts, engagement automations and followed feeds. | `create_posts` (ideas, templates, evergreen, automations and feeds are read with it) | 27 | 25 |
| **Draft posts** (`draft_posts`) | Content | medium | Write and edit drafts, duplicate posts, keep ideas, templates, tags, categories and calendar events, and save repurposing outputs as drafts. | `create_posts` | 20 | 20 |
| **Schedule & publish** (`schedule_posts`) | Content | high | Schedule posts, cancel or delete them, manage queues, posting slots, evergreen recycling, engagement automations (repost or reply when a post takes off) and RSS auto-posting. | `create_posts`, `publish_directly`, `manage_social_accounts` (posting slots) | 20 | 21 |
| **Media upload** (`upload_media`) | Content | medium | Browse the media library and upload images and video. | `upload_media` | 3 | 5 |
| **AI writing assistant** (`use_ai_assistant`) | Content | medium | Ask the workspace's AI assistant to write or adapt captions and repurpose articles, PDFs and videos. Counts toward the AI budget. | `create_posts` | 5 | 5 |
| **Inbox read** (`read_inbox`) | Inbox | low | Read comments and direct messages from connected accounts. | `use_inbox` | 2 | 2 |
| **Inbox reply** (`reply_inbox`) | Inbox | high | Write, edit and send replies to comments and messages. The autonomy policy decides what is sent. | `use_inbox`, `reply_from_inbox` | 4 | 4 |
| **Analytics read** (`read_analytics`) | Insights | low | Read analytics, scheduled reports, competitor comparisons and AI visibility. | `view_analytics` | 9 | 9 |
| **Approvals** (`manage_approvals`) | Review | high | See posts waiting for review, approve, reject or request changes, and comment. | `approve_posts` | 6 | 4 |
| **Settings read** (`read_settings`) | Workspace | low | Read the brand voice, brand kit, autonomy policy, activity log, webhooks and link-in-bio pages. | `create_posts`, `view_analytics`; webhooks also need `manage_workspace_settings` or `manage_social_accounts`, granted separately | 9 | 8 |
| **Settings write** (`write_settings`) | Workspace | high | Change the brand voice, brand kit, autonomy policy, webhooks, link-in-bio pages and tracked competitors, and run reports (which can email them). | `manage_workspace_settings`, `manage_social_accounts`, `view_analytics` (reports) | 16 | 16 |

The exact route and tool lists live in one place, `apps/api_keys/capabilities.py`, and a test fails if a route or tool is added without a capability.

When a token calls something outside its capabilities:

- REST answers `403` with `{"error": "forbidden", "detail": "Missing capability: read_analytics"}`.
- MCP hides the tool in `tools/list`, and `tools/call` answers with the same `Missing capability: ...` message.

A capability also withdraws the permissions that would widen a request another capability allows: without *Schedule & publish* a token loses `publish_directly` (so `POST /posts/` with `action=schedule`, re-timing a post or starting a repeat are refused), without *Approvals* it loses `approve_posts` (no `pre_approved` evergreen items), and without *Inbox reply* it loses `reply_from_inbox`. Other permissions only matter on routes their own capability already gates, so they are left alone and reads keep working.

## Budgets, expiry and IP ranges

| Limit | Behaviour |
|---|---|
| Calls a day | REST and MCP together. Over it: `429`, `tier` `token_daily_requests`. A value above your plan's daily allowance is lowered to it when you save. |
| AI calls a day | `POST /ai/caption`, `POST /ai/adapt`, `generate_caption`, `adapt_caption` spend one each. A repurposing job (`POST /repurpose/jobs`, `create_repurpose_job`) reserves every AI request it may make up front, and is refused with `429` before it starts when that is more than is left. Over it: `429` / a tool error, `tier` `token_daily_ai`. `0` means no AI. |
| Scheduled posts a day | Reserved, one per post (a post to three networks is three), at the moment the autonomy policy lets it go out on its own, with one conditional database update so neither a multi-network request nor concurrent requests can pass the budget. A request that would pass it is refused as a whole, gets back what it reserved, and the refusal is in Activity. Posts held for a person don't count. |
| Expires on | After the end of that day the token answers `401`. |
| Allowed IP ranges | One CIDR range or address per line. Other addresses get `403`. The address is the one the trusted-proxy logic derives (`BB_TRUSTED_PROXIES`), so a forged `X-Forwarded-For` does not help. |

A day is a UTC day, like the other call caps, so budgets reset at 00:00 UTC. The list shows today's usage against each budget.

## Read-only until approved

Tick this and autonomy is forced to **Draft only** for the token whatever the workspace allows: a schedule becomes a draft waiting in Activity, a reply becomes a draft in the inbox. It can only tighten: if the workspace has an action set to Off, it stays Off. See [autonomy.md](autonomy.md).

## Claude Desktop

**Connector (OAuth).** Add the MCP URL from **Settings -> API Keys** as a custom connector and sign in. The consent screen lists what the app asks for in plain language, with a checkbox for each. Untick anything you do not want to allow, then authorize. The app gets only what stays ticked, and the choice carries through token refreshes. Capabilities that publish or change settings start unticked. An app can ask for specific capabilities with `cap:<name>` scopes (for example `scope=mcp cap:read_calendar cap:draft_posts`); the screen then offers only those.

**Limits on a connected app.** An approval belongs to the organization you approve it in: its tokens carry an `org:` scope, act only in that organization's workspaces (even when you switch to another one in the app), and stop working if you leave it. A client can't ask for an `org:` scope itself.

- **Admins** (`manage_api_keys`) see every app connected to their organization under **Connected apps** on the API keys page, with what it may do and today's usage. **Edit** sets a ceiling (capabilities, drafts-only mode, daily budgets, expiry): the app uses what the person approved *within* it, and approving the app again can't go past it. **Revoke** deletes the app's tokens and revokes its refresh tokens on the spot, and sticks: the consent screen tells the person an admin disconnected the app until an admin chooses **Allow again**.
- **Everyone** sees their own connected apps under **Settings -> My connected apps**, can untick capabilities (only narrowing; allowing more goes through the consent screen) and can disconnect an app. Their own disconnect is undone by approving the app again.

Refreshing with fewer scopes never widens a grant: the stored choice and the admins' ceiling are intersected with the token's scopes, and a token that lost its `org:` scope is refused. After an expiry, the app's tokens are refused and it can't refresh.

**Token (local config).** In `claude_desktop_config.json`, with a token from the form above:

```json
{
  "mcpServers": {
    "postdelegate": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "https://YOUR-WORKSPACE-HOST/api/v1/mcp", "--header", "Authorization:${POSTDELEGATE_AUTH}"],
      "env": { "POSTDELEGATE_AUTH": "Bearer YOUR_TOKEN" }
    }
  }
}
```

## Claude Code

```bash
claude mcp add --transport http postdelegate https://YOUR-WORKSPACE-HOST/api/v1/mcp \
  --header "Authorization: Bearer $POSTDELEGATE_TOKEN"
```

or in a project's `.mcp.json` (keep the token in your environment, not in the file):

```json
{
  "mcpServers": {
    "postdelegate": {
      "type": "http",
      "url": "https://YOUR-WORKSPACE-HOST/api/v1/mcp",
      "headers": { "Authorization": "Bearer ${POSTDELEGATE_TOKEN}" }
    }
  }
}
```

Then ask the agent to call `list_accounts`. A tool the token cannot use will not appear in its tool list.

## Who can manage tokens

Issuing, editing, rotating and revoking need the organization permission `manage_api_keys` (owners and admins), and you can only grant permissions you hold in the workspace. Every action an agent takes is in the workspace's Activity page; each token row links to it, filtered to that token.

## Keys from before scoped tokens

Keys issued before this feature were given the **Full agent (guardrails)** preset when it shipped (migration `api_keys.0005`). That preset never includes Approvals or Settings write, and it schedules only as far as the workspace's autonomy policy allows, which starts at Draft only.
