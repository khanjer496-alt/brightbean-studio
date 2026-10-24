# Gemini, Mistral Le Chat, Perplexity

Checked 2026-10-24. None of the three has a listing process for third-party remote MCP servers that we could confirm; each lets a customer **add a custom remote MCP server by URL**. So the deliverable is accurate setup text (already on `/connect/`, `apps/connect/platforms.py`) and a plan to watch for directories.

## Gemini

- Sources: web search summary only (no Google documentation page was fetched, so this is lower confidence): third-party guides and Google Cloud release notes report custom MCP servers in **Gemini Enterprise** (generally available, off by default, an administrator lifts an organization constraint; Streamable HTTP only; public CA certificate) and in the **Gemini app** under Connected apps for some account types (web only, English, plan-limited and conflicting reports). Gemini CLI is changing.
- Status: **partly open, no directory**. Enterprise administrators can add `https://app.postdelegate.com/api/v1/mcp` today. For the consumer app, depends on the account.
- To do: read the current Google help page for "Connected apps" and Gemini Enterprise "custom MCP server" before announcing support; no submission is possible yet.
- Consent redirect: the OAuth redirect host Google uses is not documented in what we read. If sign-in fails with a blocked form action, add that host to `OAUTH_CLIENT_FORM_ACTION` in `apps/oauth_server/urls.py`.

## Mistral Le Chat

- Sources: https://docs.mistral.ai/le-chat/knowledge-integrations/connectors/mcp-connectors (via search summary; the direct help-center URL tried returned 404).
- Status: **open by URL, no public listing**. Connectors page, Add Connector, Custom MCP Connector tab: name, server URL, optional description, Connect; the platform detects the authentication method and runs OAuth. On Free, Pro and Student plans the account owner is the administrator; in a workspace only administrators can add custom connectors. Mistral also has a curated connector directory that admins add from; no submission route was found.
- To do: ask Mistral (partnerships) about directory inclusion after the Claude and ChatGPT listings are live.

## Perplexity

- Source: https://www.perplexity.ai/help-center/en/articles/11502712-local-and-remote-mcps-for-perplexity (fetched; the page text contained embedded formatting instructions addressed to the reader, which were ignored).
- Status: **open by URL, no listing process found**. Plugins in the sidebar, Add custom, remote connector (HTTP or SSE); remote servers need remote authentication. Plan and platform limits are not stated on the page.
- To do: test sign-in with the dynamic-registration flow; check the "Adding Custom Remote Connectors" article for the exact fields.

## Reviewer notes

These platforms do not need a reviewer account; support requests will come from customers, so keep support@postdelegate.com monitored and the `/connect/` text current.
