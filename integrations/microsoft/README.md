# Microsoft Copilot Studio and Power Platform

Sources (read 2026-10-24): https://learn.microsoft.com/en-us/microsoft-copilot-studio/agent-extend-action-mcp (MCP support: tools and resources, generative orchestration required), https://learn.microsoft.com/en-us/microsoft-copilot-studio/mcp-add-existing-server-to-agent (wizard, transports, OAuth types, swagger example with `x-ms-agentic-protocol: mcp-streamable-1.0`), https://learn.microsoft.com/en-us/microsoft-copilot-studio/mcp-create-new-server (auth, callback URL), https://learn.microsoft.com/en-us/connectors/custom-connectors/certification-submission (file requirements, title and summary rules, packaging, Partner Center).

## What works today

Copilot Studio supports **MCP over Streamable HTTP** (SSE was dropped after August 2025). A customer can connect PostDelegate with no files from us:

1. Agent, Tools, Add a tool, New tool, Model Context Protocol.
2. Server name `PostDelegate`, description, URL `https://app.postdelegate.com/api/v1/mcp`.
3. Authentication: OAuth 2.0, type **Dynamic discovery** (PostDelegate supports dynamic client registration and RFC 9728 discovery).
4. Create, create a connection (sign-in), Add to agent. Generative orchestration must be on.

Make sure the consent redirect is allowed: the authorize page lists `global.consent.azure-apim.net` in its form-action policy (`apps/oauth_server/urls.py`). Verify live once deployed.

## Files in this folder (generated)

    python integrations/microsoft/generate_swagger.py          # rewrite
    python integrations/microsoft/generate_swagger.py --check  # CI (tests/test_integrations_microsoft.py also checks)

| File | Purpose |
| --- | --- |
| `postdelegate-mcp.swagger.json` | MCP connector: one `POST /api/v1/mcp` operation with `x-ms-agentic-protocol: mcp-streamable-1.0` and an OAuth 2.0 (`accessCode`) security definition. Import it in Power Apps, Custom connectors, Import an OpenAPI file. Use this for the agent store and certification. |
| `postdelegate-rest.swagger.json` | The REST API as OpenAPI 2.0 (operations for Power Automate flows). Security is an **API key in the `Authorization` header**: the REST API accepts a PostDelegate token ("Bearer bb_studio_...", created under Organization, API keys), not an OAuth access token; only the MCP route takes OAuth. |
| `apiProperties.json` | Connector properties: brand colour `#7357FF`, OAuth settings placeholder. |

Conversion notes: summaries are cleaned to Power Platform's rule (80 characters, letters, digits, spaces, parentheses), nullable `anyOf` becomes `x-nullable`, multi-shape unions become an untyped value with a description, only the 200 response is described. Run Solution Checker and fix its findings before certification.

## Publishing the connector for other tenants (Microsoft certification)

Needs: a Microsoft Partner Center account for **Nasida Apps LLC** (verified publisher) and an Entra ID to sign the SAS URL.

1. Create the custom connector from `postdelegate-mcp.swagger.json` inside a solution. Title: `PostDelegate` (max 30 characters; must not contain "API", "Connector" or product names). Description 30 to 500 characters, no Power Platform product names.
2. Icon: 1:1, 100 to 230 px, non-white background matching the brand colour, logo under 70 percent of the frame, PNG named `icon.png`. Make it from `../assets/icon.svg`: the mark sits on a dark tile, which satisfies the non-white background rule; set `iconBrandColor` in `apiProperties.json` to match the tile you submit.
3. OAuth: certification asks for a static client id and secret in Partner Center. Register a **confidential client** for the connector's redirect URL (with `redirectMode: GlobalPerConnector` it is `https://global.consent.azure-apim.net/redirect/<connector id>`; copy it from the connector's Security tab):

       python manage.py oauth_client create --name "Microsoft Copilot Studio" --redirect-uri https://global.consent.azure-apim.net/redirect/<connector id>
       python manage.py oauth_client rotate --client-id <id>   # new secret; the old one stops working at once
       python manage.py oauth_client list

   The secret is printed once and stored hashed. Put the client id in `apiProperties.json` and the secret in Partner Center only. The client goes through the same consent screen as every other (shown as verified), may omit PKCE (the secret authenticates it at the token endpoint), and must use S256 if it sends PKCE. A redirect host other than `global.consent.azure-apim.net` must also be added to `OAUTH_CLIENT_FORM_ACTION` in `apps/oauth_server/urls.py`.
4. Run Solution Checker; make at least 10 successful test calls per operation; write `intro.md` (what it does, how to get credentials, known limits).
5. Export connector and a test flow solution, build the package with Package Deployer, zip with `intro.md`, upload to a blob, generate a SAS URL valid 15+ days, validate with `ConnectorPackageValidator.ps1`, submit in Partner Center. Production host only: no staging URLs.

Microsoft agent store: the docs fetched describe certification of connectors for Copilot Studio and Power Platform; they did not describe a separate agent-store listing for an MCP server. Treat the certified connector as the route and recheck Partner Center for an agent-store option when submitting. Also see "Bring your own MCP server" (Agents 365 CLI and Microsoft 365 admin center) mentioned on the add-existing-server page for tenant-level registration.

Status: usable now by URL; certification ready to start after the server is live.
