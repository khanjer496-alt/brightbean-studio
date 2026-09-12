# PostDelegate: BrightBean on Hetzner + Cloudflare

Prepared 12 September 2026. This is an operator runbook, not evidence of a deployed
service. The application is the **BrightBean/Django** fork at
<https://github.com/khanjer496-alt/brightbean-studio>. Do not use the separate
Postiz/Temporal checkout or its deployment instructions.

## Target and current gates

Use one **CX33, Ubuntu 24.04 LTS, x86-64** server with a primary IPv4 address.
Hetzner lists CX33 as 4 shared vCPU, 8 GB RAM and 80 GB NVMe. Confirm availability
and the total including IPv4, tax and any optional backup before ordering; the
public listing currently reports limited/unavailable stock. Hetzner account setup was explicitly deferred by the owner on 12 September 2026.
No account/host access was configured during this audit. A stopped server continues to be billed.
See [Hetzner's listing](https://www.hetzner.com/cloud/cost-optimized/).

One Compose project runs Caddy, Gunicorn, the background worker and PostgreSQL.
R2 stores media. No Redis, Temporal, Elasticsearch, managed database, permanent
staging server, hosted AI or billing provider is required for this launch shape.
Capacity is a starting choice; record memory, disk and CPU during actual video
uploads and scheduling before inviting customers.

**Do not reuse `postdelegate-app:optimized`.** The existing local image contains
`/app/.cloudflared-preview-token` (existence verified without reading the token).
New ignore rules exclude it. Build a fresh reviewed release from clean source;
never upload the old image or copy the local spike environment to the server.
The tunnel credential's invalidation/replacement remains an operator action.

Before customer access, still prove:

- reviewed release commit, dependency/CI checks and fresh Linux image;
- authorized live host, real domain/TLS, closed registration and owner login;
- authenticated transactional email and monitored support/privacy addresses;
- private R2 upload/download, backup encryption and an isolated restore;
- at least one approved provider's connect, publish, schedule, failure and reconnect;
- deployed tenant/role/approval behavior and realistic memory headroom.

Source tests do not establish these production outcomes.

## 1. Server, firewall and source

In Hetzner Console, create the approved CX33 with Ubuntu 24.04 and an SSH public
key. Use a Hetzner firewall: TCP 22 from the operator's address; TCP 80 and 443
public; UDP 443 optional for HTTP/3. Do not expose 5432 or 8000. The current
standalone Compose file publishes only Caddy's ports. Keep key-based SSH access
working before tightening SSH login policy. Docker access is root-equivalent.

Install Docker Engine, Buildx and the Compose plugin using
[Docker's Ubuntu repository instructions](https://docs.docker.com/engine/install/ubuntu/).
Use the official apt repository, not an unreviewed convenience script. Install
`git`, `curl`, `ca-certificates`, `openssl`, `age`, and an AWS CLI for backup work.
Then verify `docker version` and `docker compose version`.

Commands below run in a root administrative shell on the **new server**, not the
Mac or existing spike containers. Replace the release placeholder with the exact
reviewed commit after it is available on the fork:

```bash
export RELEASE_REV='REPLACE_WITH_REVIEWED_40_CHARACTER_COMMIT'
git clone https://github.com/khanjer496-alt/brightbean-studio.git /opt/postdelegate
cd /opt/postdelegate
git checkout --detach "$RELEASE_REV"
test "$(git rev-parse HEAD)" = "$RELEASE_REV"
test -z "$(git status --porcelain)"
install -d -m 700 /etc/postdelegate
install -m 600 .env.postdelegate.example /etc/postdelegate/runtime.env
```

Never embed Git credentials in the clone URL. If the fork is private, arrange
read-only repository access. Provide network users the corresponding source for
the deployed revision, including local modifications, under the project's AGPL
source obligations.

## 2. Runtime configuration and R2

Edit `/etc/postdelegate/runtime.env` privately. Do not `cat` it into logs, source
it as shell code, commit it or put it inside the Docker build directory. Set:

| Field | Required value |
| --- | --- |
| `POSTDELEGATE_IMAGE_TAG` | `release-<same 40-character commit>` |
| `POSTDELEGATE_ENV_FILE` | `/etc/postdelegate/runtime.env` |
| `SECRET_KEY` | A newly generated, permanent value, e.g. `openssl rand -base64 48` |
| `ENCRYPTION_KEY_SALT` | A different newly generated permanent value |
| `POSTGRES_PASSWORD` | Newly generated URL-safe value, e.g. `openssl rand -hex 32` |
| `DATABASE_URL` | `postgres://postdelegate:<same password>@postgres:5432/postdelegate` |
| `APP_URL` | `https://app.postdelegate.com` |
| `ALLOWED_HOSTS` | `app.postdelegate.com` |
| `POSTDELEGATE_DOMAIN` | `app.postdelegate.com` |
| `POSTDELEGATE_HTTP_PORT` / `POSTDELEGATE_HTTPS_PORT` | `80` / `443` |
| `REGISTRATION_OPEN` | `false` |
| `STORAGE_BACKEND` | `s3` |
| `S3_ENDPOINT_URL` | The account's `https://<account-id>.r2.cloudflarestorage.com` |
| `S3_BUCKET_NAME` | Verified private media bucket belonging to this deployment |
| `S3_ACCESS_KEY_ID` / `S3_SECRET_ACCESS_KEY` | Credentials scoped to that media bucket |
| `S3_REGION_NAME` | `auto` |
| `S3_CUSTOM_DOMAIN` | Empty for private signed URLs |

The example's `nashr-production-media` is a candidate, not confirmation of bucket
ownership or safe reuse. Verify its existing contents and ownership before use.
Keep R2 public access disabled. The app uses private ACLs and one-hour signed
URLs; a signed URL is a bearer link until expiry. Verify multipart upload and
provider media fetches. Configure bucket CORS only for the exact app origin and
required upload methods/headers if the deployed browser upload route needs it.
See [R2 S3 setup](https://developers.cloudflare.com/r2/get-started/s3/).

The existing encrypted model fields derive their key from **both** `SECRET_KEY`
and `ENCRYPTION_KEY_SALT`. Preserve both off-host with recovery materials.
Changing either blindly makes existing social/platform/2FA secrets unreadable.
Production now rejects missing, short or known template values. This guard does
not prove entropy or replace secret custody.

Keep all platform credentials empty initially. Set SMTP host, authenticated
sender, TLS settings and verified `DEFAULT_FROM_EMAIL`; test password reset and
invitation delivery before opening the beta. Set support/privacy links and
addresses to the verified operator's endpoints. Cloudflare Email Routing alone
does not provide authenticated outbound mail. Keep Intelligence/hosted-AI
variables empty. Never copy another deployment's provider credentials.

## 3. Build, validate and bootstrap before exposing the app

Use only the standalone `docker-compose.postdelegate.yml`; do not combine it with
the upstream development Compose override. This explicit helper fixes the project
name so future checkouts cannot accidentally select another set of volumes:

```bash
cd /opt/postdelegate
export POSTDELEGATE_ENV_FILE=/etc/postdelegate/runtime.env
dc() {
  docker compose --project-name postdelegate-prod \
    --env-file /etc/postdelegate/runtime.env \
    -f /opt/postdelegate/docker-compose.postdelegate.yml "$@"
}
dc config --quiet
docker build --pull --platform linux/amd64 \
  --tag "postdelegate-app:release-${RELEASE_REV}" .
docker image inspect "postdelegate-app:release-${RELEASE_REV}" --format '{{.Id}} {{.Config.User}}'
docker run --rm --network none --entrypoint sh "postdelegate-app:release-${RELEASE_REV}" \
  -c 'test ! -e /app/.cloudflared-preview-token && test ! -e /app/.env && test ! -e /app/.env.postdelegate.local'
dc up -d postgres
dc run --rm --no-deps migrate
dc run --rm --no-deps migrate python manage.py check --deploy
dc run --rm --no-deps migrate python manage.py createsuperuser
dc up -d app worker
dc ps
```

Enter the owner email/password interactively; do not place the password in shell
history. `createsuperuser` provisions the initial organization and workspace via
the existing user signal. Confirm owner memberships, rename the workspace and
configure approval requirements during the first login. Leave registration
closed. A superuser is an operator account; do not issue it to customers.

Record the commit, image ID, dependency check result and UTC deployment time. The
current Compose uses local tags, so keep release tags unique and never retag an
old release. A registry-based release must additionally record its immutable
registry digest. Do not build from `spike` or float production on an unreviewed
branch. Check the fresh image's user is `app`, not root.

## 4. Real origin and Cloudflare TLS

In the `postdelegate.com` Cloudflare zone, replace the obsolete `app` tunnel
routing with an A record pointing to the approved Hetzner IPv4. Remove conflicting
CNAME/AAAA entries. Do not alter the marketing site's apex or `www` routes.
Start with DNS-only while Caddy obtains and validates its public certificate:

```bash
dc up -d caddy
dc logs --tail=80 caddy
curl --fail --show-error https://app.postdelegate.com/health/
```

Once origin HTTPS works, enable the Cloudflare proxy for `app` and use
[Full (strict)](https://developers.cloudflare.com/ssl/origin-configuration/ssl-modes/full-strict/).
Do not use Flexible mode. Add no cache-everything rule for authenticated pages,
API routes, callbacks or signed media URLs. Verify login/CSRF, logout, owner
permissions and the invite-only screen through the public hostname. Provider
callbacks must use this origin; see each provider's configured slug rather than
assuming one universal callback (TikTok uses `social1`).

The app health endpoint is a process/HTTP check, not a database or real-publish
check. Worker health requires a completed publish cycle within 20 minutes.
Docker marks a stale worker unhealthy; `restart: unless-stopped` alone does not
restart an unhealthy-but-running process. Arrange an operator alert and diagnose
before restarting. After a crash, `publishing` rows have an unknown outcome: do
not bulk reschedule them. Reconcile with the provider first to avoid duplicates.

## 5. Backups and a restore drill before customer #1

Required recovery set: PostgreSQL, media objects, the exact source/image revision,
runtime configuration and both encryption derivation values. A database dump
alone cannot restore media; a server snapshot alone is not the off-host backup.
The application media credential must not access the private backup bucket.

On a trusted recovery machine, create an age identity, store its private key in
the owner's recovery vault, and give the server only its public recipient. Keep a
separate AWS CLI profile `postdelegate-backup` with minimum required access to the
private backup bucket. The [R2 AWS CLI guide](https://developers.cloudflare.com/r2/examples/aws/aws-cli/)
describes the account endpoint/profile setup. Backup credentials stay root-owned
outside the application environment.

A manual database backup, with operator-supplied non-secret target values:

```bash
set -euo pipefail
umask 077
: "${BACKUP_RECIPIENT:?Set the age public recipient}"
: "${BACKUP_BUCKET:?Set the verified private backup bucket}"
: "${R2_ENDPOINT:?Set the R2 S3 endpoint}"
install -d -m 700 /var/backups/postdelegate
BACKUP_FILE="/var/backups/postdelegate/db-$(date -u +%Y%m%dT%H%M%SZ).dump.age"
dc exec -T postgres pg_dump -U postdelegate -d postdelegate -Fc \
  | age -r "$BACKUP_RECIPIENT" -o "${BACKUP_FILE}.partial"
mv "${BACKUP_FILE}.partial" "$BACKUP_FILE"
sha256sum "$BACKUP_FILE" > "${BACKUP_FILE}.sha256"
aws --profile postdelegate-backup --endpoint-url "$R2_ENDPOINT" s3 cp \
  "$BACKUP_FILE" "s3://${BACKUP_BUCKET}/database/"
aws --profile postdelegate-backup --endpoint-url "$R2_ENDPOINT" s3 cp \
  "${BACKUP_FILE}.sha256" "s3://${BACKUP_BUCKET}/database/"
```

Save the shell helper/config into a root-owned scheduled backup script before
scheduling it; shell functions do not persist across cron/systemd runs. Implement
and prove nightly execution, failure alerts and a retention policy (initial
proposal: 7 daily and 4 weekly copies). No backup timer or alert has been installed
by this runbook. Keep failed `.partial` output private and investigate the failure.
Also preserve media snapshots/copies in a separate private recovery location;
never mirror deletes automatically. Test recovery of a deliberately removed test
object. Set retention to match the published deletion policy and budget.

For the restore drill, download the encrypted dump and checksum with a separate
read credential to an isolated machine. Verify `sha256sum -c`, decrypt with the
vault identity, and restore into a new PostgreSQL 16 database. One possible
throwaway container has no network access:

```bash
: "${RESTORE_FILE:?Set downloaded .dump.age path}"
: "${RESTORE_IDENTITY:?Set protected age identity path on recovery machine}"
docker run -d --name postdelegate-restore-check --network none \
  -e POSTGRES_HOST_AUTH_METHOD=trust postgres:16-alpine
# Wait until this succeeds before continuing:
docker exec postdelegate-restore-check pg_isready -U postgres
docker exec postdelegate-restore-check createdb -U postgres postdelegate_restore
set -o pipefail
age -d -i "$RESTORE_IDENTITY" "$RESTORE_FILE" \
  | docker exec -i postdelegate-restore-check pg_restore -U postgres \
      -d postdelegate_restore --no-owner --no-privileges --exit-on-error
```

Verify row counts and migrations, then rehearse application decryption with the
matching SECRET_KEY/salt and recovery media in an isolated environment. Do not
start a restored worker or permit SMTP/provider egress: restored due posts and
background tasks can send real messages. Record backup age, restore duration,
media/decryption checks and the operator. Explicitly remove only the named drill
container and its disposable volume after reviewing the result. Do not run
`compose down -v` or a system-wide prune on production.

## 6. Release acceptance and upgrades

Exercise owner login, legitimate invitation, mismatched/expired/reused invitation,
workspace isolation, API-token revocation, distinct internal/client approvals,
upload ownership, a scheduled post and handled failure. Real publishing and mail
tests require the owner's intended test accounts and authorization; merely
populating credentials is not platform approval. Record the evidence and tested
provider identifiers.

Before every upgrade: record the old image ID, take a verified off-host backup,
stop app/worker writes in a maintenance window, install the reviewed revision,
build a fresh image, run migration and checks, then restart and recheck health.
A destructive database migration cannot be rolled back by changing the image
tag alone. Keep the old image and matching encrypted backup until the new release
is accepted. Monitor worker heartbeat, failed/ambiguous posts, disk growth,
PostgreSQL size and backup freshness. Do not treat a green `/health/` response as
proof that scheduling, storage or recovery works.

## Local verification — 12 September 2026

Launch preparation is on `codex/brightbean-launch-20260912` in the BrightBean
checkout. These results apply to the local changes, not a deployed release:

- Full PostgreSQL-backed Python suite: **1,522 passed**, 9 subtests passed,
  586 deprecation warnings (296.25 seconds).
- Frontend inline-handler tests: **9 passed**.
- Ruff 0.15.9 lint, format check and Git whitespace checks: passed.
- Installed Python dependency audit: **80 packages, no known vulnerabilities**.
  Django is now 5.2.17; cryptography 50.0.1; Pillow 12.3.0.
- Production image build: **not completed**. The local build was stopped when
  free host disk fell below 1 GB; no new production image was published.
- Type checking: **not completed**. Local mypy stalled importing its compiled
  modules; a diagnostic traceback stopped at `from mypy import api`. Rerun in
  clean CI before merging or releasing.
- Browser verification of rebuilt production CSS, live hosting, email,
  provider credentials/publishing and backup restore remain outstanding.

The owner deferred Hetzner account setup. No server was purchased or provisioned,
no domain origin was changed, and no production deployment was performed.
