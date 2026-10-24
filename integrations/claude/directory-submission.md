# Claude directory submission content

Sources (read 2026-10-24): https://code.claude.com/docs/en/plugins/publish.md (section "Submit to Anthropic's directory"), https://code.claude.com/docs/en/plugins-reference (manifest fields), https://code.claude.com/docs/en/mcp.md (remote servers and OAuth). The connector submission guide at https://support.claude.com/en/articles/12922490 returned 404 when fetched; the portal is https://claude.ai/directory/manage. A search summary of that guide (not the guide itself) said every tool needs a read-only or destructive annotation and that a test account with sample data is required. **Re-read the portal's current form before submitting and adjust this file.**

Who submits: an Owner of a paid claude.ai plan (Pro, Max, Team, Enterprise) or an Enterprise member with the Directory permission. Needs the **owner's claude.ai login**; an agent cannot do it.

## Listing

| Field | Value |
| --- | --- |
| Name | PostDelegate |
| Tagline (short) | Plan, draft, schedule and review social posts with Claude. |
| Short description (under 160) | Draft and schedule posts across your social channels, find the best time to post, triage the inbox and read analytics, within limits you control. |
| Long description | PostDelegate is a social media scheduler for teams and agencies. Connect it to Claude and ask for a week of posts, a thread, or a reply to every unanswered comment. Claude writes in your saved brand voice, checks each network's length limits, picks times that suit each account, and schedules posts or sends them to a person for approval. Your workspace's autonomy setting decides what Claude may do alone: by default it writes drafts and a person approves everything. You choose what Claude can do when you connect it, and you can narrow or revoke access at any time. Includes analytics, reports, link-in-bio pages, evergreen recycling and an inbox. |
| Categories | Marketing, Social media, Productivity |
| Developer | Nasida Apps LLC |
| Website | https://postdelegate.com |
| Support | support@postdelegate.com |
| Documentation | https://app.postdelegate.com/connect/ |
| Privacy policy | https://app.postdelegate.com/legal/privacy/ |
| Terms | https://app.postdelegate.com/legal/terms/ |
| Icon | `../assets/icon-512.png` (also `icon-1024.png`, `icon.svg`) |

## Server

| Field | Value |
| --- | --- |
| MCP URL | `https://app.postdelegate.com/api/v1/mcp` (no trailing slash) |
| Transport | Streamable HTTP (single POST endpoint, JSON responses) |
| Authentication | OAuth 2.1, authorization code with PKCE (S256 only), public client |
| Discovery | `/.well-known/oauth-protected-resource/api/v1/mcp` (RFC 9728), `/.well-known/oauth-authorization-server` (RFC 8414); a 401 carries the `WWW-Authenticate` pointer |
| Client registration | Dynamic client registration (RFC 7591) at `https://app.postdelegate.com/oauth/register`; redirect URIs must be https or http on a loopback address |
| Callback URLs seen | Claude: `https://claude.ai/api/mcp/auth_callback`, `https://claude.com/api/mcp/auth_callback`. Claude Code: loopback `http://localhost:<port>/callback` |
| Scopes | `mcp`, plus `cap:<capability>` scopes the consent screen adds for the narrowed set the person approves |
| Tool annotations | Every tool has `title` and `annotations` (`readOnlyHint`, `destructiveHint`, `idempotentHint`, `openWorldHint`), derived from the capability each tool belongs to. See `connector-tools.md`. |
| Rate limits | Per token and per IP; documented at `/api/v1/docs` |
| Source IPs | Not allowlisted; server is public |

## Example prompts

1. "Draft a LinkedIn post and an X thread announcing our spring sale, in our brand voice. Save them as drafts."
2. "What is the best time to post to our Instagram this week? Show me the next three open slots."
3. "Turn this article into three LinkedIn posts and schedule them across next week, held for my approval: <url>"
4. "Show me the comments we have not replied to and draft answers."
5. "How did our channels do over the last 30 days compared with the 30 before? What should we post more of?"

## Screenshots to capture (1280x800 or larger PNG)

1. Claude drafting two network-specific versions of a post, with the saved draft ids.
2. A held-for-review response: Claude telling the user a post is waiting for approval.
3. The PostDelegate consent screen listing capabilities with the high-risk ones unticked.
4. The week calendar widget (ChatGPT) or the PostDelegate calendar showing the scheduled posts.
5. Inbox triage table with drafted replies.
6. An analytics summary.

Demo video (60 to 90 seconds): connect, ask for a week of posts, show the held-for-review path, approve in PostDelegate.

## Tools

Full table with annotations: `connector-tools.md` (128 tools in 11 capability groups). Group summary for the form:

| Capability (shown on the consent screen) | Tools |
| --- | --- |
| Read posts and calendar | list and get posts, accounts, queues, slots, ideas, templates, tags, events, members, usage |
| Draft posts | create and edit drafts, ideas, templates, tags, categories, short links |
| Schedule and publish | schedule, cancel, queue, posting slots, evergreen, engagement rules, RSS feeds |
| Media upload | upload and search media |
| AI writing assistant | generate and adapt captions, repurpose articles and videos |
| Inbox read, Inbox reply | list messages; draft, edit and send replies |
| Analytics read | analytics, reports, competitors, AI visibility, link stats |
| Approvals | list, approve, reject, comment |
| Settings read and write | brand voice, brand kit, autonomy policy, webhooks, link-in-bio, tracked competitors |

## Test account for reviewers (fill in before submitting; never commit real credentials)

- Sign-in URL: https://app.postdelegate.com/accounts/login/
- Email: `<REVIEWER_EMAIL>`  Password: `<REVIEWER_PASSWORD>` (no MFA, no email code)
- The account is an owner of workspace "Reviewer Demo" with: two connected demo channels, six drafts, three scheduled posts, two posts waiting for approval, two inbox comments, analytics history, a brand voice and a link-in-bio page.
- Autonomy level is Draft only, so schedule requests come back "held for review": this is intended. To test scheduling, the reviewer can raise "Schedule and publish" to Auto with guardrails under Settings, Autonomy.
- Demo channels cannot publish to a real network, so reviewing cannot post publicly.

Preparation checklist: create the account in production; seed it with a script that is not in the repo history; verify sign-in with no MFA; rotate the password after review.
