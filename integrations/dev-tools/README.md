# Developer tools

Sources (read 2026-10-24): https://code.claude.com/docs/en/mcp.md (`claude mcp add`, `.mcp.json`, OAuth), https://cursor.com/docs/context/mcp and https://cursor.com/docs/context/mcp/install-links (deeplink format, OAuth, redirect URLs), https://code.visualstudio.com/docs/copilot/reference/mcp-configuration (`mcp.json`, `code --add-mcp`). The `vscode:mcp/install` URL is not on the VS Code pages fetched; it is the form VS Code's own install buttons and community generators use (search results, 2026-10-24). Raycast: https://manual.raycast.com/model-context-protocol (via search; its documented deeplink covers local servers only).

Server URL for all of them: `https://app.postdelegate.com/api/v1/mcp`. The same links are built by `apps/connect/platforms.py` and shown on `/connect/`.

## Claude Code

    claude mcp add --transport http postdelegate https://app.postdelegate.com/api/v1/mcp

Then run `/mcp` and sign in. Project-shared config (`.mcp.json`):

    { "mcpServers": { "postdelegate": { "type": "http", "url": "https://app.postdelegate.com/api/v1/mcp" } } }

Claude Code receives the OAuth code on a loopback port; dynamic client registration accepts `http://localhost:<port>` redirects. Plugin (adds the skill): see `../claude/README.md`.

## Cursor

One click (build with the exact JSON `{"url":"https://app.postdelegate.com/api/v1/mcp"}`, base64):

    cursor://anysphere.cursor-deeplink/mcp/install?name=postdelegate&config=eyJ1cmwiOiJodHRwczovL2FwcC5wb3N0ZGVsZWdhdGUuY29tL2FwaS92MS9tY3AifQ%3D%3D

`.cursor/mcp.json`:

    { "mcpServers": { "postdelegate": { "url": "https://app.postdelegate.com/api/v1/mcp" } } }

Cursor's desktop callback is `http://localhost:8787/callback`; web and cloud agents use `https://www.cursor.com/agents/mcp/oauth/callback`. Both register through dynamic registration.

## VS Code (GitHub Copilot agent mode)

    code --add-mcp '{"name":"postdelegate","type":"http","url":"https://app.postdelegate.com/api/v1/mcp"}'

Install link: `vscode:mcp/install?%7B%22name%22%3A%22postdelegate%22%2C%22type%22%3A%22http%22%2C%22url%22%3A%22https%3A%2F%2Fapp.postdelegate.com%2Fapi%2Fv1%2Fmcp%22%7D`. GitHub Markdown will not link `vscode:` URLs; wrap it as `https://insiders.vscode.dev/redirect?url=<encoded link>` in READMEs. `.vscode/mcp.json`:

    { "servers": { "postdelegate": { "type": "http", "url": "https://app.postdelegate.com/api/v1/mcp" } } }

## Raycast

Install MCP Server command, name `PostDelegate`, URL above, transport HTTP, OAuth type dynamic. Raycast's documented install deeplink (`raycast://mcp/install?<json>`) is documented for local (stdio) servers; do not publish a deeplink for the remote server until Raycast documents one.

## Token clients

Clients that cannot do OAuth: issue a token (Organization, API keys), start from the Drafting assistant preset, and send it as `Authorization: Bearer <token>`.
