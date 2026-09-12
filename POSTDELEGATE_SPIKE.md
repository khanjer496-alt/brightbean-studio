# PostDelegate on BrightBean Studio — migration spike

Upstream baseline: `d85fce192e687d20e8fd7e9449a40ad7952ec7c3` from
`brightbeanxyz/brightbean-studio`, licensed AGPL-3.0. BrightBean is the selected PostDelegate application base. The separate Postiz-derived
repository now supplies the public website; its legacy application is not the launch target.

## Goal

Prepare the selected Django/PostgreSQL application for launch on a small Hetzner
server with Cloudflare and R2. Hosting setup remains deferred by the owner.

## Safety additions in this spike

- Deployment branding and legal/public URLs are environment-configurable.
- Public registration can be disabled while invitation signup remains available.
- MCP identifies the deployment as `postdelegate` when configured.
- A mutating provider request that times out or returns 5xx is considered an
  ambiguous remote write. It is not automatically retried, because the social
  platform may already have accepted it and a retry could duplicate a post.
- Social OAuth tokens remain in BrightBean's AES-256-GCM encrypted fields; API
  keys remain HMAC-hashed at rest.

## Lean production shape

One VPS: Caddy + Django/Gunicorn web + database-backed worker + PostgreSQL. R2 is
used through BrightBean's S3-compatible storage backend. No Redis, Temporal or
Elasticsearch is required by this spike.

Use `.env.postdelegate.example` only as a template. Real R2/social/email secrets
must never enter Git. `docker-compose.postdelegate.yml` is standalone rather than
inheriting the upstream development Compose file: only Caddy publishes ports,
PostgreSQL stays private, and signup is closed.

## Gates before switching bases

1. Branded public/auth/OAuth/MCP surfaces render PostDelegate and point to the
   owned PostDelegate policy URLs.
2. R2 configuration creates private, signed object URLs using a bucket-scoped
   credential; an end-to-end real R2 upload remains an external credential test.
3. User/workspace/API-key/MCP tests pass against PostgreSQL.
4. Approval-required workspaces cannot publish through web/API/MCP before the
   required approval state.
5. Ambiguous remote writes are parked for operator review and never automatically
   retried; provider success/failure crash boundaries still need a real-platform
   fault-injection test before customers.
