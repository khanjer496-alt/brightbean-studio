# PostDelegate compared with Post Bridge and Postiz

Reviewed 12 September 2026. PostDelegate means the BrightBean checkout in this repository, including current local changes. The accepted product direction is AI-agent-first social publishing, closer to Postiz in scope, with web oversight. The owner has authorized agents to fix product gaps. Feature sequencing below remains a prioritization recommendation; no pricing decision is implied.

## Evidence and confidence

Competitors: official product/pricing/help pages; advertised capabilities, not independent hands-on verification. PostDelegate: current source and prior local browser/test evidence. Implemented does not mean production-qualified. No verified revenue, retention or causal explanation of competitor success was established. “Not confirmed” is not “absent.”

## Feature comparison

| Area | Post Bridge | Postiz | PostDelegate today |
| --- | --- | --- | --- |
| Networks | 10 advertised | 30+ advertised | 11 networks represented by 13 provider adapters; no X or Reddit. Production permissions/publishing unverified. |
| Drafts, scheduling, platform customization | Advertised | Advertised | Implemented; local UI checked. Real scheduled publishing remains a launch gate. |
| Bulk content | Bulk video workflow | Not confirmed in this review | CSV text/schedule import exists; unsupported media URL imports are now explicitly rejected. No dedicated bulk-video-to-post flow found. |
| Repeated content | Queue slots | Repeated posts, RSS auto-post | Recurrence generation and queue exist; a separate evergreen recycling flow not found. RSS reading is not proof of automatic publishing. |
| Creative tools | Video content studio | AI text/image/video and image editor | Image crop/resize/rotate and video trim/thumbnail extraction. Optional Intelligence integration is unconfigured; no comparable enabled studio. |
| Collaboration | Team invitations on higher tiers | Team collaboration/customer groups | Workspace roles, internal/client approval stages, client portal, holds and change requests implemented. Competitors' equivalent client approval depth not established. |
| Analytics | Beta analytics | Analytics advertised | Dashboard/API exist; platform coverage varies. No CSV/PDF export or best-time recommendations found. |
| Developer/AI-agent access | API/MCP | API, agent CLI, automation integrations | REST API/MCP, scoped keys and OAuth implemented; customer setup needs documentation and end-to-end qualification. |
| Inbox | Depth not confirmed | Equivalent depth not confirmed | Threads, replies, assignment, notes and saved replies implemented; platform coverage varies. |
| Paid subscriptions | Available | Available | Optional Intelligence billing is not general publishing-plan billing. Customer plans/checkout/entitlements remain incomplete. |

Sources for competitor columns: [Post Bridge product](https://www.post-bridge.com/), [Post Bridge scheduling](https://www.post-bridge.com/social-media-scheduler), [Post Bridge pricing](https://www.post-bridge.com/pricing), [Postiz product](https://postiz.com/), [Postiz pricing](https://postiz.com/pricing).

## Concrete local findings

- `providers/__init__.py`: Facebook, Instagram, LinkedIn, TikTok, YouTube, Pinterest, Threads, Bluesky, Google Business, Mastodon and DEV.to. Instagram authentication modes and LinkedIn personal/company adapters are not additional networks.
- `apps/composer/views.py`, `csv_confirm_import`: the earlier silent media-loss issue is fixed by rejecting unsupported media URL imports and removing that mapping. Supported rows commit atomically. Safe media ingestion and a dedicated bulk-video flow remain future work.
- `apps/calendar/models.py` and `tasks.py`: recurrence rules and generation are implemented. Do not rebuild recurrence as a supposedly missing feature.
- `apps/publisher/engine.py`: first-comment delivery and retry/reconciliation exist. Provider-specific limits still apply.
- `apps/approvals/services.py` and `apps/client_portal/`: internal/client review, hold, reject and request-change workflows exist.
- `apps/analytics/`: dashboard/API exist. No report export or posting-time recommendation implementation found.
- `apps/media_library/services.py`: basic image operations, trimming, frame and thumbnail extraction exist; no full studio/subtitle workflow found.
- `apps/intelligence/`: optional external-service integration and its subscriptions must not be represented as enabled native publishing SaaS billing or an included AI product.
- `apps/inbox/` and social-account webhooks: engagement functionality exists. Incoming provider hooks and notification delivery are not by themselves a complete developer automation onboarding experience.

## Recommended sequence

### 1. Prove the agent publishing foundation

Deploy the reviewed app when hosting is available. Qualify connect/reconnect, token expiry, immediate and scheduled publication, media uploads and failures on a small initial set of channels. Verify ownership/tenant boundaries, email delivery, backup restore, support and recovery procedures. Implement clear publishing plans, usage limits, checkout, cancellation and entitlements before charging customers.

Acceptance exercises: a user connects an AI client through REST or MCP, prepares a draft, completes required approval in the web workspace and schedules the post; a scheduled post publishes while nobody has the app open; a failed connection produces a useful reconnect path; an uncertain remote publish never silently triggers duplicate publication. Agree measurable targets after obtaining a baseline; test counts are not customer reliability measurements.

### 2. Close the most valuable parity gaps

First make scoped REST/MCP setup, draft creation, approval handoff, scheduling and publishing-status recovery reliable and documented. Fix CSV media import and provide a batch video workflow: upload multiple videos, edit each caption/platform/time, validate requirements, preview the batch, submit, then report each result. Add X if customer interviews confirm the founder/creator audience requires it, after checking API access and unit costs. Give the existing API/MCP copyable, tested setup guides and example workflows. Improve failure notifications and recovery visibility alongside these features.

### 3. Build around the accepted AI-agent-first proposition

Accepted direction: social publishing infrastructure for AI agents, with scoped REST/MCP access, approval controls, scheduling and human oversight in the web workspace. Aim closer to Postiz in publishing and automation scope; small-team or client-approval positioning is not the primary proposition. Retain collaboration, client review and inbox capabilities as supporting workflows. Validate the agent connection, draft-to-review handoff and publishing recovery with agent builders and operators. This is the chosen position, not proof of a competitive advantage.

Add report export, reusable content tools or a lightweight creative studio only when observed customer work supports the priority. Avoid attempting every long-tail channel or a complete video editor before reliable publishing is proven.

## Pricing and acquisition

Both competitors currently advertise entry plans at $29/month for five connected accounts/channels. Old $9 testimonials should not set our comparison. Postiz's homepage and pricing page disagree about the Standard post cap, so do not claim exact parity on that cap without confirming it. Sources: the pricing pages linked above.

Do not pick our price from server cost alone: support, media, platform APIs, AI, payments and reliability work matter. Test packaging with users before committing to an “unlimited” promise.

Observed positioning suggests lessons, not proven causes of success: Post Bridge emphasizes quick cross-posting, human support and creator workflows; Postiz emphasizes platform breadth, open source and automation. MCP itself is already competitive parity. For PostDelegate, pair a first successful agent-created, reviewed and scheduled post with practical REST/MCP setup guides, working examples and genuine customer evidence. Track activation, successful scheduled publishing, repeat use, paid conversion, support burden and retention. Feature duplication cannot guarantee their commercial results.
