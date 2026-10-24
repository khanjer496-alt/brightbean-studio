# Claude

| Item | Where |
| --- | --- |
| Plugin (1 connector, 1 skill) | `plugins/postdelegate/` |
| Connector config | `plugins/postdelegate/.mcp.json` (remote HTTP, OAuth with dynamic client registration; no client id needed) |
| Skill | `plugins/postdelegate/skills/postdelegate/SKILL.md` and `references/` |
| Marketplace for a dedicated public repo | `.claude-plugin/marketplace.json` in this folder (the repo root is `integrations/claude/`) |
| Marketplace for this monorepo | `/.claude-plugin/marketplace.json` at the repository root |
| Directory listing text | `directory-submission.md` |
| Tool list | `connector-tools.md` |

## Test locally

    claude plugin validate integrations/claude/plugins/postdelegate --strict
    claude plugin validate integrations/claude
    claude --plugin-dir integrations/claude/plugins/postdelegate

## Publish the marketplace

Claude installs from a GitHub repo whose root holds `.claude-plugin/marketplace.json`. Two ways:

1. **Dedicated public repo (recommended).** Create `postdelegate-ai` (public), copy the contents of `integrations/claude/` to its root, push. Users run `claude plugin marketplace add <owner>/postdelegate-ai`. Then set `AI_PLUGIN_MARKETPLACE_REPO=<owner>/postdelegate-ai` in the app so `/connect/` shows the command.
2. **This repo.** If the application repo is public, `claude plugin marketplace add <owner>/<repo>` works as is, using the root `/.claude-plugin/marketplace.json`.

Bump `version` in `plugin.json` on every release; users only receive an update when it changes.

## Submit to the directory

See `directory-submission.md`.
