# Community directories and registries

Checked 2026-10-24. None needs code beyond the registry entry; all need a human with an account.

## Smithery

Source: https://smithery.ai/docs/build/publish. For a remote server there is **no `smithery.yaml`**: go to https://smithery.ai/new, enter `https://app.postdelegate.com/api/v1/mcp`, or run `smithery mcp publish "https://app.postdelegate.com/api/v1/mcp" -n @<org>/postdelegate`. Smithery scans the server; an OAuth server prompts the publisher to sign in. If the scan cannot finish, it reads a static card: the app serves one at `https://app.postdelegate.com/.well-known/mcp/server-card.json` (server info, OAuth, all tools with annotations). Do not block `SmitheryBot/1.0` in a WAF, and return 401 to unauthenticated requests (the server does). Then finish the vendor checklist under the server's Settings, Verification. Needs a Smithery account (GitHub login).

## Glama

Source: https://glama.ai/mcp/servers (the page shows an Add Server button and a Claimed filter; it did not document steps). Click Add Server, submit the repository or URL, then claim the listing with the owner's GitHub account. `glama.json` (claim file) is in this folder; its format was not verified against Glama's docs, so check the claim screen first.

## mcp.so

Source: https://mcp.so. Use Submit (https://mcp.so/submit), paste the repository or server URL, review the fetched name, description, config and tools, and submit; listings go live after review. Fields to have ready are in `listing-text.md`.

## PulseMCP

Source: https://www.pulsemcp.com/submit. **Submissions are closed right now** and the page says the official MCP Registry entry is the best first step, which PulseMCP picks up automatically once submissions resume. So: publish to the official registry (`../mcp-registry/`) and do nothing else.

## Official MCP Registry

See `../mcp-registry/`.
