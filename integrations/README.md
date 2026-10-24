# AI distribution kit

Everything needed to list PostDelegate as an app or connector on the major AI platforms. The server these all point at is `https://app.postdelegate.com/api/v1/mcp` (Streamable HTTP, OAuth 2.1 with PKCE and dynamic client registration).

Docs read for this kit on **2026-10-24**; each folder's README lists its URLs. Platforms change fast: re-read the vendor page before submitting.

**Everything is "ready to submit after the server is live"** unless a row says otherwise. Nothing has been submitted.

## Before any submission (one-time, owner)

1. Production is live at https://app.postdelegate.com with HTTPS, `MCP_PUBLIC_BASE_URL` and `MCP_OAUTH_ISSUER_URL` set to that origin, and `BRAND_NAME=PostDelegate`, `MCP_SERVER_NAME=postdelegate` (the default server name is still `brightbean-studio`).
2. Check the three discovery URLs return JSON: `/.well-known/oauth-protected-resource/api/v1/mcp`, `/.well-known/oauth-authorization-server`, and that `POST /api/v1/mcp` without a token returns 401 with a `WWW-Authenticate` header.
3. Create the **reviewer account** (see `claude/directory-submission.md`): owner of a seeded demo workspace, no MFA, Draft only autonomy, demo channels that cannot publish publicly. Used by Claude, ChatGPT and Microsoft reviewers.
4. Capture the **assets** below.
5. Walk through each platform's sign-in once yourself; the consent screen's form-action allowlist (`apps/oauth_server/urls.py`) covers claude.ai, claude.com, chatgpt.com, the Power Platform consent host and loopback; add any host a client redirects to that is not listed.

## Status and who does what

| Platform | Status | Route | Who must do it | Folder |
| --- | --- | --- | --- | --- |
| Claude (claude.ai, Desktop, Cowork, Claude Code) | Add by URL works now. Directory: ready after live | Plugin (1 connector, 1 skill) + connectors directory | Owner of a paid claude.ai plan, at https://claude.ai/directory/manage | `claude/` |
| ChatGPT | Developer mode by URL works now. Directory: ready after live | Apps SDK app on the MCP server with widgets | OpenAI organization Owner with verified identity | `chatgpt/` |
| Official MCP Registry | Ready after live | `mcp-publisher` with DNS verification | Whoever controls postdelegate.com DNS | `mcp-registry/` |
| Microsoft Copilot Studio | Works now via the MCP wizard (dynamic discovery). Certification: ready after live, one open question on OAuth client credentials | Custom connector swagger + Partner Center | Partner Center owner for Nasida Apps LLC | `microsoft/` |
| Gemini | Partly open (Enterprise admins; some consumer accounts). No listing process | Document only | none yet | `other-assistants/` |
| Mistral Le Chat | Open by URL. No listing process found | Document only | customer admins | `other-assistants/` |
| Perplexity | Open by URL. No listing process found | Document only | customers | `other-assistants/` |
| Claude Code, Cursor, VS Code, Raycast | Works now | Install links and config | none | `dev-tools/` |
| Smithery, Glama, mcp.so | Ready after live | Web submit | any maintainer account | `directories/` |
| PulseMCP | Submissions closed; follows the official registry | none | none | `directories/` |

In the app: public page `/connect/` lists all of these with install links and is linked from Organization, API keys. `/.well-known/mcp/server-card.json` describes the server for registries; `/.well-known/openai-apps-challenge` serves the ChatGPT domain token.

## Assets checklist

- [x] Icon: `assets/icon-512.png`, `assets/icon-1024.png`, `assets/icon.svg` (the PostDelegate mark).
- [ ] Screenshots, 1280x800 or larger PNG, light theme, demo workspace data: (1) two network-specific drafts in Claude, (2) held-for-review message, (3) OAuth consent screen with capability list, (4) week calendar widget, (5) approval list widget, (6) post preview widget, (7) inbox triage with draft replies, (8) analytics summary, (9) `/connect/` page.
- [ ] Demo video, 60 to 90 seconds, captions, public or unlisted URL: connect, ask for a week of posts, show held-for-review, approve in PostDelegate.
- [ ] Reviewer account credentials (kept in a password manager, entered only in vendor portals).
- [ ] Release notes for 1.0.

## Reviewer instructions (same for every portal)

Sign in at https://app.postdelegate.com/accounts/login/ with the reviewer account. Connect the platform to `https://app.postdelegate.com/api/v1/mcp`, approve the consent screen (leave the pre-ticked capabilities as they are; tick "Schedule and publish" only to test scheduling). The workspace is at autonomy level **Draft only**, so a request to schedule returns "held for review": that is the designed safe behavior, and the held post is visible under Activity. Demo channels cannot publish to a real network.

## Maintaining the kit

- New tool: add it to `apps/api_keys/capabilities.py` (a test fails until you do); annotations follow from its capability and name. Regenerate `claude/connector-tools.md`.
- Widgets: edit `chatgpt/widgets/src`, run `node integrations/chatgpt/build.mjs`.
- Microsoft files: `python integrations/microsoft/generate_swagger.py`.
- Version bumps: `claude/plugins/postdelegate/.claude-plugin/plugin.json`, `mcp-registry/server.json`.
