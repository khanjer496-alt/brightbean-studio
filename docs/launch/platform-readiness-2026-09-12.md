# PostDelegate platform-readiness checkpoint

Checked 12 September 2026. Marketing origin returned HTTP 200; `https://app.postdelegate.com/health/` returned HTTP 530. Hosting was previously deferred. No platform approval is verified by this task, and no application was submitted here.

## Engineering before platform submissions

- Complete production build/type checks, reviewed release, live HTTPS origin, database/worker and R2 verification.
- Publish current operator/privacy/terms/deletion/support pages; verify working business email and deletion/disconnection flows.
- Register exact per-provider callback URLs from the BrightBean routes, not old Postiz callback examples.
- Configure developer apps and test accounts, then demonstrate actual OAuth and a real publish for every permission requested.
- Prepare reviewer access, per-permission screencasts, clear use-case descriptions, retention/security answers and minimum required scopes.
- Verify retries, token refresh/reconnect, user consent, post status, media validation and separate tenant access on the deployed build.

## Specific code risks identified

The TikTok provider currently prefers FILE_UPLOAD (`providers/tiktok.py`, around line 296). TikTok's current guidelines say server-stored media should use PULL_FROM_URL with a verified source domain or URL prefix. R2-backed media publishing therefore needs an explicit transport/domain review before submission. Do not assume current transport selection is audit-ready.

Agent-first publishing must preserve platform-specific consent and UI requirements. TikTok requires creator information, user-selected privacy, commercial disclosures, preview and express upload consent; a generic instruction to an agent is not itself proof that this flow complies.

Instagram Login currently requests publishing, comments, messaging and insights scopes. If the initial release is publishing-only, review this requested bundle and feature gating before applying; do not ask reviewers to approve functionality we cannot demonstrate.

## Platform routes

- Meta Facebook/Instagram/Threads: developer app(s), correct login route and account types, requested publishing permissions, review/advanced access and business verification where required. Exact current gates must be confirmed in the actual Meta dashboard; official documentation fetches were blocked in this check.
- TikTok: registration and Content Posting access plus Direct Post audit; unaudited use is visibility-limited. Verify required UX and source-domain ownership.
- YouTube: Google OAuth verification where sensitive scopes require it and a separate YouTube API compliance audit for public-upload restrictions. OAuth approval is not the same as upload-audit approval.
- LinkedIn Community Management: registered organization, verified business email/website, associated company Page verification, Development access and Standard-tier review with test credentials/screencast. Personal-only posting follows different product permissions; do not conflate the paths.
- Pinterest: business account/app registration, Trial access, then Standard request with real OAuth/action video. Trial-created Pins/Boards are visible only to the creator.
- Google Business Profile: API access request and eligibility, including a managed verified profile active for at least 60 days and its business website.
- Other implemented providers: qualify their actual authentication and posting rules with test accounts. No claim of live approval or universal availability follows from having provider code.

## Owner inputs

Nasida Apps LLC and postdelegate.com are already chosen. Still needed: hosting account/access when ready, business/domain email, requested company verification documents, developer-account ownership, and suitable test social accounts/Pages/channels. Supply secrets through secure configuration, not chat. A company name alone is not completed business verification.

## Feature backlog, separate from approval gates

General publishing billing/entitlements; safe CSV media ingestion and bulk-video workflow; X (and optionally Reddit) provider coverage; packaged/tested client, n8n and Make workflows; MCP draft-creation retry protection; optional enabled AI creation tools; report export. Recurring posts, first comments, approvals, inbox and analytics already have implementations; prioritize validation over rebuilding them.

## Sources

- https://developers.tiktok.com/docs/en/content-sharing-guidelines
- https://developers.tiktok.com/docs/en/content-posting-api-get-started
- https://developers.google.com/youtube/v3/docs/videos
- https://developers.google.com/identity/protocols/oauth2/production-readiness/sensitive-scope-verification
- https://learn.microsoft.com/en-us/linkedin/marketing/community-management-app-review?view=li-lms-2026-06
- https://developers.pinterest.com/docs/key-concepts/access-tiers/
- https://developers.google.com/my-business/content/prereqs

These requirements do not guarantee approval or establish a fixed turnaround time. Start with a narrow, real working integration and request only the permissions it needs.
