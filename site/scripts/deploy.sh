#!/bin/sh
# Build and publish postdelegate.com to Cloudflare Pages. Refuses to run on a conflicted or dirty tree.
set -eu
cd "$(dirname "$0")/.."
if git diff --name-only --diff-filter=U | grep -q .; then echo "Unresolved merge conflicts: fix them first." >&2; exit 1; fi
if grep -rIl '^<<<<<<< \|^>>>>>>> ' src public >/dev/null 2>&1; then echo "Conflict markers in site sources." >&2; exit 1; fi
if [ -n "$(git status --porcelain -- .)" ]; then echo "site/ has uncommitted changes: commit them first." >&2; exit 1; fi
pnpm install --frozen-lockfile >/dev/null
pnpm build
npx --yes wrangler@4 pages deploy dist --project-name postdelegate --branch main \
  --commit-hash "$(git rev-parse HEAD)" --commit-message "$(git log -1 --format=%s)"
