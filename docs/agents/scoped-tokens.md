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
| **Read posts & calendar** (`read_calendar`) | Content | low | See posts, drafts, queues, posting slots, ideas, templates, tags, events, team members and connected accounts. | none beyond reading | 23 | 21 |
| **Draft posts** (`draft_posts`) | Content | medium | Write and edit drafts, duplicate posts, and keep ideas, templates, tags, categories and calendar events. | `create_posts` | 19 | 19 |
| **Schedule & publish** (`schedule_posts`) | Content | high | Schedule posts, cancel or delete them, manage queues, posting slots and evergreen recycling. | `create_posts`, `publish_directly` | 13 | 14 |
| **Media upload** (`upload_media`) | Content | medium | Browse the media library and upload images and video. | `upload_media` | 3 | 5 |
| **AI writing assistant** (`use_ai_assistant`) | Content | medium | Ask the workspace's AI assistant to write or adapt captions. Counts toward the AI budget. | `create_posts` | 2 | 2 |
| **Inbox read** (`read_inbox`) | Inbox | low | Read comments and direct messages from connected accounts. | `use_inbox` | 2 | 2 |
| **Inbox reply** (`reply_inbox`) | Inbox | high | Write, edit and send replies to comments and messages. The autonomy policy decides what is sent. | `use_inbox`, `reply_from_inbox` | 4 | 4 |
| **Analytics read** (`read_analytics`) | Insights | low | Read analytics, scheduled reports, competitor comparisons and AI visibility. | `view_analytics` | 10 | 10 |
| **Approvals** (`manage_approvals`) | Review | high | See posts waiting for review, approve, reject or request changes, and comment. | `approve_posts` | 6 | 4 |
| **Settings read** (`read_settings`) | Workspace | low | Read the brand voice, brand kit, autonomy policy, activity log, webhooks and link-in-bio pages. | none beyond reading | 9 | 8 |
| **Settings write** (`write_settings`) | Workspace | high | Change the brand voice, brand kit, autonomy policy, webhooks, link-in-bio pages and tracked competitors. | `manage_workspace_settings` | 15 | 15 |

The exact route and tool lists live in one place, `apps/api_keys/capabilities.py`, and a test fails if a route or tool is added without a capability.

When a token calls something outside its capabilities:

- REST answers `403` with `{"error": "forbidden", "detail": "Missing capability: read_analytics"}`.
- MCP hides the tool in `tools/list`, and `tools/call` answers with the same `Missing capability: ...` message.

A capability also withdraws the workspace permissions only it would use (a token without *Schedule & publish* loses `publish_directly`), so a request that is otherwise allowed cannot reach scheduling by another route.

## Budgets, expiry and IP ranges

| Limit | Behaviour |
|---|---|
| Calls a day | REST and MCP together. Over it: `429`, `tier` `token_daily_requests`. Must be within your plan's daily allowance. |
| AI calls a day | `POST /ai/caption`, `POST /ai/adapt`, `generate_caption`, `adapt_caption`. Over it: `429` / a tool error, `tier` `token_daily_ai`. `0` means no AI. |
| Scheduled posts a day | Counted when the autonomy policy lets a post go out on its own. Over it the schedule is refused with a reason the agent can read, and the refusal is in Activity. |
| Expires on | After the end of that day the token answers `401`. |
| Allowed IP ranges | One CIDR range or address per line. Other addresses get `403`. The address is the one the trusted-proxy logic derives (`BB_TRUSTED_PROXIES`), so a forged `X-Forwarded-For` does not help. |

A day is a UTC day, like the other call caps, so budgets reset at 00:00 UTC. The list shows today's usage against each budget.

## Read-only until approved

Tick this and autonomy is forced to **Draft only** for the token whatever the workspace allows: a schedule becomes a draft waiting in Activity, a reply becomes a draft in the inbox. It can only tighten: if the workspace has an action set to Off, it stays Off. See [autonomy.md](autonomy.md).

## Claude Desktop

**Connector (OAuth).** Add the MCP URL from **Settings -> API Keys** as a custom connector and sign in. The consent screen lists what the app asks for in plain language, with a checkbox for each. Untick anything you do not want to allow, then authorize. The app gets only what stays ticked, and the choice carries through token refreshes. Capabilities that publish or change settings start unticked. An app can ask for specific capabilities with `cap:<name>` scopes (for example `scope=mcp cap:read_calendar cap:draft_posts`); the screen then offers only those.

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
