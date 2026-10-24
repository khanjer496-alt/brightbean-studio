# Official MCP Registry

Sources (read 2026-10-24): https://raw.githubusercontent.com/modelcontextprotocol/registry/main/docs/modelcontextprotocol-io/quickstart.mdx (publish flow), `.../authentication.mdx` (DNS and HTTP verification, namespace rules), `.../docs/reference/server-json/generic-server-json.md` (schema; latest dated schema in the examples is 2025-12-11). The `title` field is not shown in those pages; if `mcp-publisher publish` rejects it, remove it. Validate against the schema URL in `$schema` before publishing.

`server.json` here is a remote-only entry: no npm or PyPI package, so no `mcpName` marker. The remote URL must be publicly reachable. `description` is limited to 100 characters (it is 91).

## Namespace and ownership

Domain authentication allows names under the reverse of the domain: `com.postdelegate/<name>`. Owning the DNS for `postdelegate.com` proves it. Needs the **domain owner's DNS access** (Cloudflare or registrar) and a machine with OpenSSL 3 (on macOS the system LibreSSL lacks Ed25519: `brew install openssl@3`).

### 1. Generate a key and the TXT record

    export MY_DOMAIN="postdelegate.com"
    openssl genpkey -algorithm Ed25519 -out key.pem
    PUBLIC_KEY="$(openssl pkey -in key.pem -pubout -outform DER | tail -c 32 | base64)"
    echo "${MY_DOMAIN}. IN TXT \"v=MCPv1; k=ed25519; p=${PUBLIC_KEY}\""

Keep `key.pem` secret (password manager, not the repo).

### 2. The exact DNS record

| Field | Value |
| --- | --- |
| Type | TXT |
| Name | `postdelegate.com` (the apex, `@` in most DNS panels; not a subdomain such as `_mcp-auth`) |
| Value | `v=MCPv1; k=ed25519; p=<PUBLIC_KEY from step 1>` |
| TTL | 300 while testing |

If you rotate keys, delete the old TXT first: a stale record is tried first and breaks verification. If the apex already holds other TXT records (SPF), add this as a separate record.

HTTP alternative, if the DNS cannot be changed: serve `v=MCPv1; k=ed25519; p=<PUBLIC_KEY>` at `https://postdelegate.com/.well-known/mcp-registry-auth` and use `mcp-publisher login http`.

### 3. Publish

    brew install mcp-publisher
    cd integrations/mcp-registry
    PRIVATE_KEY="$(openssl pkey -in key.pem -noout -text | grep -A3 "priv:" | tail -n +2 | tr -d ' :\n')"
    mcp-publisher login dns --domain postdelegate.com --private-key "$PRIVATE_KEY"
    mcp-publisher publish
    curl "https://registry.modelcontextprotocol.io/v0.1/servers?search=com.postdelegate/postdelegate"

Wait a few minutes after adding the record before logging in.

### Updating

Bump `version` in `server.json` (semver) for each release and run `mcp-publisher publish` again. Downstream catalogs (PulseMCP, mcp.so and others) pick up registry entries.

Status: ready to publish once https://app.postdelegate.com/api/v1/mcp is live.
