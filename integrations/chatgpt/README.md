# ChatGPT (Apps SDK / MCP Apps)

PostDelegate's MCP server is the ChatGPT app: ChatGPT connects to `https://app.postdelegate.com/api/v1/mcp` over OAuth. This folder holds the widgets and the submission text.

Sources (read 2026-10-24): https://developers.openai.com/apps-sdk/build/mcp-server (annotations, result shape), https://developers.openai.com/plugins/build/chatgpt-ui (tool and resource `_meta`, MIME type `text/html;profile=mcp-app`, CSP fields), https://developers.openai.com/apps-sdk/build/chatgpt-ui (the `ui/*` postMessage bridge and `window.openai`), https://developers.openai.com/apps-sdk/deploy/submission (submission steps, assets, test account, domain verification; the page is titled for plugins).

## What the server does for ChatGPT

- Every tool carries `title` and `annotations` (`readOnlyHint`, `destructiveHint`, `idempotentHint`, `openWorldHint`) derived from its capability (`apps/api_keys/capabilities.py`).
- Tool results include `structuredContent` (the same object as the text block).
- `resources/list` and `resources/read` serve three widgets, MIME type `text/html;profile=mcp-app`, URIs `ui://widget/<name>-v1.html`:

| Widget | Rendered by | Shows |
| --- | --- | --- |
| Post preview | `get_post`, `create_draft`, `schedule_post` | caption, targets, status, schedule, "held for review" notice |
| Week calendar | `list_posts` | posts laid out across a week |
| Approval list | `list_approvals` | posts waiting for a decision; "Review this" sends a follow-up message in the chat |

  Those tools carry `_meta.ui.resourceUri` and the compatibility alias `_meta["openai/outputTemplate"]`, plus the invoking and invoked status text. Resources carry `ui.prefersBorder` and an empty CSP allowlist (`connectDomains` and `resourceDomains` are empty: the widgets load nothing and call nothing).
- Widgets never write: the approval list asks the model to review in chat rather than calling `review_post` itself. All tool-result text reaches the DOM through `textContent`.
- `MCP_WIDGET_DOMAIN` (optional) adds `ui.domain` to the resources. ChatGPT's submission asks for a dedicated widget origin; decide it with the portal and set the variable.
- `/.well-known/openai-apps-challenge` serves `OPENAI_APPS_CHALLENGE` as plain text for domain verification (404 while unset).

## Build the widgets

Sources are in `widgets/src` (shared bridge and CSS, one HTML template per widget). The build has no dependencies:

    node integrations/chatgpt/build.mjs          # writes apps/mcp/widgets/*.html (committed)
    node integrations/chatgpt/build.mjs --check  # CI: fails when the committed bundles are stale
    node --test tests/frontend/chatgpt-widgets.test.cjs

When a widget changes in a way ChatGPT must not serve from cache, bump `version` in `apps/mcp/widgets.py` (the URI is the cache key).

## Try it before submitting

ChatGPT developer mode: Settings, Apps (or Connectors), enable developer mode, create an app with the MCP URL and OAuth. Call `list_posts` and check the calendar renders. Test light and dark and a narrow width.

## Submission

See `submission.md`.
