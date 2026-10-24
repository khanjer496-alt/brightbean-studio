# Postiz and BrightBean edge cases vs PostDelegate

Research date: 2026-10-24. Read-only on code. Not committed.

## How to read this

**Evidence references**
- `PZ#1234` is Postiz issue 1234: `https://github.com/gitroomhq/postiz-app/issues/1234`
- `PZ@abcd1234` is a Postiz commit: `https://github.com/gitroomhq/postiz-app/commit/abcd1234`
- `BB#12` and `BB@abcd123` are the same for `brightbeanxyz/brightbean-studio`.

**Status**
- handled
- partial
- NOT handled
- n/a

**Paths** are relative to `/Users/naserkhanjar/Projects/postdelegate-studio` and cite file:line as of this checkout.

**Sources and caveats**
- The local Postiz checkout at `/Users/naserkhanjar/Projects/PostDelegate` has only 19 commits. Its history was squashed, so it is no use for `git log --follow`.
- I mined a blobless clone of upstream `gitroomhq/postiz-app` (3,476 commits) and `gh issue list` output. That is 859 issues, 319 labelled `type: bug`.
- Postiz's error model is `refresh-token | bad-body | retry | disconnect`, set per provider in `handleErrors()`. Most Postiz "fixes" are new entries in those tables. I used that as the checklist for PostDelegate's equivalents.
- Postiz runs on Temporal. Some of its bugs are workflow-engine bugs (PZ#1321, PZ#1259, PZ#1213, PZ#1340, PZ#1172). PostDelegate publishes from a DB-polled engine, so these are marked n/a or handled by design, with the reason given.
- I read the code paths cited. I did not run live API calls. Anything marked "NOT handled" means I grepped and found no handling.

## BrightBean upstream sync

- `git fetch upstream` gives 645 upstream commits.
- `git log HEAD..upstream/main` is empty. Every upstream commit is already in our history, and we are 334 commits ahead.
- Cherry-pick list: **none needed**. The 239 upstream fix-like commits are already merged, including `91cbb9a` (Mastodon media processing wait), `0d15d53` (concurrent credential refresh), `4ca3647` (TikTok stranding and memory), `0aec94a` (publish fan-out bounded by the Postgres connection budget), `df313f2` (Bluesky session refresh), `14555d1` (Threads long-lived token refresh) and `8107cf2` (LinkedIn reserved-character escaping).
- Upstream open issues worth watching are in the "BrightBean upstream" section below.

---

## Instagram (Facebook Login and Instagram Login)

| Edge case | Evidence | PostDelegate status | Suggested fix |
|---|---|---|---|
| Meta Graph errors arrive as HTTP 400 with `error.code`/`error_subcode`. Expired or revoked token is code 190 (also 463/467), and the session-invalid/checkpoint codes (459/490) mean disconnect. | PZ@28730a0c, PZ@c693a97e, PZ@186d3370, PZ#757, PZ#623 | **NOT handled.** `providers/base.py:926-955` maps only 429 to `RateLimitError`; everything else is a generic `APIError`. `apps/social_accounts/error_messages.py:99-112` (`_has_expired_token_code`) only matches a string `error`, and Graph sends a dict. A 400/190 is classed "rejected" and retried 3 times (`apps/publisher/engine.py:154`, `1373`). The account is never flagged. | Override `_error_for_response` in a shared Meta mixin. Map 190/463/467/459/490 to `TokenExpiredError`, which also makes `_refused_a_live_token` (`engine.py:1692`) fail fast. |
| Container-creation errors have subcodes with different retry semantics. 2207052 media fetch failed is retryable; 2207082 silent video, 2207085 and 2207003 timeout are non-retryable with a readable message. | PZ@b95e1e2c, PZ@f4bd43f2, PZ@48997091, PZ#1472, PZ#1004, PZ#1126 | **NOT handled.** Container `ERROR` status raises a plain retryable `PublishError` carrying the raw status text (`providers/instagram.py:533-539`). It retries blindly 3 times and re-uploads each time. A non-retryable format error is retried, and a retryable fetch error gets no hint about the public URL. | Parse `error_subcode` and the status text into retryable/non-retryable plus a curated sentence, mirroring Postiz's tables. |
| GET on a freshly created container or just-published media returns code 100 subcode 33 (a Meta server bug), which breaks status polling and the permalink read. | PZ#1562 | **Partial.** The permalink read is best-effort (`instagram.py:563-581`). The container status poll (`instagram.py:521-546`) is not: any `APIError` on that GET aborts the publish. | Treat code 100/33 on the status GET as "keep polling" for a bounded number of tries. |
| Reels and video processing can take over 2 minutes. | PZ@d48e2a74, BB@1ed0fd4 | **Partial.** `CONTAINER_POLL_TIMEOUT = 120` (`instagram.py:101`, `instagram_login.py:95`). On timeout the post is retried and re-creates a container each time (no reuse). | Raise the timeout to about 5 minutes for video. Persist the container id via `content.upload_cache` (already used by X and Mastodon) and resume polling on retry. |
| Single video must publish as a Reel; the carousel and image type is chosen from the sniffed media type, not the URL suffix. | BB@26eb8be, BB@fc0b0cf, BB@c266995 | handled: `engine.py:1011-1053`, `1201-1258`, `instagram.py:414-430`. | none |
| Media Meta can't fetch (`localhost`, private URL, non-public `APP_URL`) gives 9004/2207052. | PZ#1472, PZ#1004 | **FIXED (media-pipeline).** `apps/publisher/media_preflight.py` runs before publishing for every provider with `fetches_media_by_url` (Instagram, Threads, Facebook, Pinterest, GMB): public-host check (`is_safe_url`), a one-byte ranged GET (not HEAD: presigned links are GET-only), content-type check and signed-URL lifetime check, with a plain-language failure. Was: **Partial.** `_public_media_url` (`engine.py:449-455`) uses `APP_URL` or a presigned URL. No preflight that the URL is publicly reachable. | Add a startup/ops check that `APP_URL` is public HTTPS and a one-time fetch probe from the worker. |
| Image formats: Instagram officially takes JPEG only. PNG, WebP and HEIC from phones fail with "Unsupported image format". | PZ@e3b3b82f (error map), PZ#1108 | **FIXED for PNG/WebP (media-pipeline):** `MediaRules.image_formats={"jpeg"}` makes the engine publish a derived JPEG copy (`apps/media_library/derivatives.py`), original untouched. HEIC is not an accepted upload type (no `pillow-heif`), so it never reaches this point. Was: **NOT handled.** The rule is only advisory (`instagram.py:119-127`, `advisory=media_types`) and there is no conversion. `grep` for heic/webp finds nothing in the upload path. | Convert non-JPEG stills to JPEG on upload or at publish (Pillow is already in the stack). |
| Aspect ratio 4:5 to 1.91:1; 8 MB images. | PZ#885 (ratio), error map "Aspect ratio not supported" | handled: block at schedule time and at publish (`instagram.py:119-127`, `engine.py:757-775`). | none |
| Collaborator handles (strip `@`, URL-encode, not allowed on carousels). | PZ@1de15370, PZ@0a8c28fb, PZ@a9aced7d, PZ@c1fd5cff | partial. `providers/instagram_options.py:80-90` parses collaborators. I did not verify the carousel restriction or the untaggable-collaborator message. | Check `container_options` for carousel and add a readable message for the untaggable case. |
| Duplicate first comments, and the comment-rejected error 1772107. | BB@28611c2, PZ@454553c4 | handled: `instagram.py:584-621` (`publish_comment`, `find_own_comment`) and `engine.py:1744-1770`. | none |
| Daily publishing cap (about 100 posts per 24h) and the Page posting block. | PZ@d934731c, PZ@3387b3c9 | partial. `rate_limits()` declares `publish_per_day=100` (`instagram.py:258-266`), but the live `content_publishing_limit` endpoint is not read. | Optional: read the quota endpoint before bulk schedules. |
| Posts stuck "scheduled" with no error. | BB#204, BB#199, PZ#818 | handled upstream (BB@4414d52) and in this fork: publish capacity reserved in the worker, plus the stale/confirm sweeps (`engine.py:183-215`, `1477-1660`). | none |
| Duplicate and loop posting after a failed status write (account flagged for bot behaviour). | PZ#832, PZ#1172, PZ#1321 | handled by design. The claim to `publishing` is a conditional UPDATE (`engine.py:543-590`, `1419-1470`), a success write never re-queues (`engine.py:915-950`), and an ambiguous write is parked as failed. | none |

## Facebook

| Edge case | Evidence | PostDelegate status | Suggested fix |
|---|---|---|---|
| Expired or revoked token (190), "(#200) permission", page-publishing authorization, and a user token stored where a page token is required. | PZ@fa69f42e, PZ@8bcb2f54, PZ@cc8ca985, PZ#1346, PZ#1126 | **NOT handled.** There is no Graph error-code mapping (see the Instagram table). `providers/meta_insights.py:13` and `meta_messaging.py:121-123` know codes 10/190/200, but only for analytics and messaging. | Share one `meta_errors` classifier across Facebook, Instagram and Threads. |
| Temporary posting-rate block (368 / 1390008) is retryable; "posting too fast". | PZ@d934731c, PZ#1041 | **NOT handled.** It comes back as HTTP 400, so it isn't a `RateLimitError`. It is retried by default, but with generic copy and the 1/5/30 minute ladder. | Map 368/1390008 to `RateLimitError` with a long `retry_after`. |
| Deprecated scopes (`read_insights`) and metrics break connect and analytics. | PZ#1346, PZ#1609, PZ@ce32dccb, PZ@3fab214f | partial. Scopes are current (`facebook.py`, BB@ad2cf16 made partial grants visible). Analytics errors are surfaced, not swallowed (BB@fc6cd91). I did not verify the Graph API version pin against Meta's sunset schedule. | Put the Graph version in one constant with a calendar reminder (Postiz moved v20 to v25). |
| Single image published as a "photo" rather than a post. | PZ#1593 | n/a / by design: `facebook.py:415-435` publishes photos deliberately. | Optional: expose a "post as feed post" toggle. |
| Multi-photo (up to 10), photos staged unpublished then attached, then cleaned up. | PZ#552, BB@758a5dd | handled: `facebook.py:437-515`. | none |
| Video and Reel `upload_complete` status polling. | PZ@7edeadd5 | handled: `facebook.py:555-660`. | none |
| Reconnect with no page token. | PZ@cc8ca985 | partial. `facebook.py:342-370` reads the page token, but I did not see a hard failure when it is empty. | Fail the reconnect cleanly if `access_token` is empty. |
| Comments and first comment after a Reel (async id). | PZ#1319 | handled: `facebook.py:740-818` (page-scoped id lookup, `find_own_comment`). | none |

## Threads

| Edge case | Evidence | PostDelegate status | Suggested fix |
|---|---|---|---|
| Publishing before the container is FINISHED fails with "media not found" (4279009). | PZ@5536d8a7, PZ#1364 | handled: `providers/threads.py:378-415` (polls IN_PROGRESS/FINISHED/ERROR/EXPIRED). | none |
| `UNKNOWN` container error and 2207052 on media. | PZ@e6251b25, PZ#1364 | **NOT handled.** An ERROR status is a generic retryable `PublishError` (`threads.py:398-405`), with no subcode parsing. | Same Meta classifier. |
| More than 3 URLs in a post gives `bad_body`. | PZ#1203 | **NOT handled.** There is no link-count rule. | Add a post-settings validation (warn or block above the real limit; verify the limit first). |
| Character counter wrong for non-ASCII (Cyrillic counted double). | PZ#2218 | partial. The server counts code points, so it is correct. The composer counts UTF-16 units (`templates/composer/compose.html:2423-2433`), which is wrong for emoji and other astral characters. | See the cross-cutting "caption counting" row. |
| Long-lived token 60-day refresh. | BB@14555d1 | handled: `threads.py:244-262`, `apps/social_accounts/tasks.py:18` (expiry bootstrap). | none |
| Images in comments and thread replies. | PZ@7c265e5f | handled: reply-chain threads (`threads.py:325`, `engine.py:2207-2330`). | none |

## X (Twitter)

| Edge case | Evidence | PostDelegate status | Suggested fix |
|---|---|---|---|
| 402 "credits depleted" / usage cap: the UI says success, then "Unknown Error". | PZ#2231, PZ#1513 | handled: `providers/x.py:158-162`, `x.py:438-455`. It fails permanently with a specific message. | none |
| Duplicate-content rejection, and the cashtag, 4-media and URL-not-allowed rules. | PZ@f477777e, PZ@0458cffc, PZ@0d98fc02 | handled for duplicate (`x.py:477`). The 4xx path surfaces X's own sentence (`x.py:481-490`). | none |
| 429 reset from `x-rate-limit-reset`; retries exhausted give a readable message. | PZ@1ae4a3b7, PZ@a3047d18 | handled: `x.py:455-470` (`QuotaExceededError` with `resets_at`, honoured by `engine.py:1373-1415`). | none |
| Refresh token rotates on every use, so concurrent refreshes burn it. | PZ#1354, PZ#1178, PZ@935c80a8 | handled: `x.py:575-590` and the row lock in `apps/social_accounts/models.py:383-460`. `invalid_grant` becomes a reconnect (`x.py:522`). | none |
| Weighted length: URL=23, emoji=2, CJK=2. Premium 25,000 limit. | PZ#1149 | handled: `x.py:222-260`, mirrored in `compose.html:2434-2470`. | none |
| Images capped at 1000px / forced compression. | PZ#738, PZ#1037 | n/a: we never resize. Rules block oversize files instead (`x.py` media rules). | none |
| Thread-finisher HTML in tweets. | PZ#1013, PZ@4c3e002a | handled: plain-text threads (`x.py:689-762`). | none |
| Chunked video: STATUS polling and the upload cache. | PZ@0d98fc02 | handled: `x.py:886-975`. | none |
| Alt text on media. | PZ#948 (Mastodon), PZ#695 (Bluesky) | **FIXED (media-pipeline):** `POST /2/media/metadata` after upload for images and GIFs (`x.py` `_set_alt_text`). | none |

## LinkedIn (personal and company page)

| Edge case | Evidence | PostDelegate status | Suggested fix |
|---|---|---|---|
| `LinkedIn-Version` header sunsets after about 12 months. A dead version gives 426 NONEXISTENT_VERSION and every post fails. | PZ#2005, PZ#1187, BB#37 | partial. Pinned at `202604` (`providers/linkedin.py:55-59`). There is no 426 handling or alert. When the version sunsets, every post fails with a generic error. | Map 426 to a clear operator-facing error, alert on it, and put a calendar reminder at about month 10. |
| OAuth `prompt=none` skips newly added scopes, so the connect fails with "Not enough scopes". | PZ#1580, PZ#1582, BB#26 | handled / n/a. `get_auth_url` (`linkedin.py:~250`) sends no `prompt=none`. Personal scopes are the self-serve set. | none |
| Reserved characters in commentary truncate or corrupt the post. | BB@8107cf2 | handled: `linkedin.py:74-100`, and the composer counts escapes (`providers/__init__.py:89-105`). | none |
| Images over 36 MP, forced to 1000px. | PZ@5daee690 | partial. The decode ceiling is 30 MP (`apps/media_library/services.py:412`) and we don't resize. I did not find a LinkedIn pixel rule. | Add `max_width`/`max_height` or an area rule to `LINKEDIN_MEDIA_RULES`. |
| Animated GIFs turned into PNGs. | PZ#825, PZ@d0562250 | handled: GIF is passed through as a first-class media type (`linkedin.py:217`). | none |
| PDF carousel, with PNG slides blank. | PZ#1179, PZ@029ddb29 | handled: the user supplies the PDF through the Documents API (`linkedin.py:447-548`, 100 MB and 300 pages). We don't build PDFs from PNGs, so the blank-slide bug doesn't apply. The repurpose pipeline builds the PDF (`document_carousel = True`). | none |
| Video: chunked upload, wait for AVAILABLE, PROCESSING_FAILED. | PZ@84bee82b, PZ@e18d4a59 | handled: `linkedin.py:549-640`, `1079-1112`. | none |
| Company page: Content Admin roles, ACL elements missing `organizationalTarget~`, a page hijacking the personal channel. | PZ@db1f84ff, PZ@671946b5, PZ@03bea3de | partial. A separate `linkedin_company` provider (`providers/linkedin_company.py`) avoids the channel-id collision by design. I did not verify role filtering (Content Admin vs Super Admin). | Check the org-ACL role list covers CONTENT_ADMIN. |
| Link preview missing. | PZ#1594 | **NOT handled** (low): no article/link card is attached to LinkedIn text posts. | Optional. |
| Access token 60 days with no refresh token for most apps. | PZ#1580 | partial. The expiring-soon status exists (`models.py:370`), and the health check sets TOKEN_EXPIRING only when refresh fails. | Notify at 14/7/1 days before a non-refreshable token expires. |

## TikTok

| Edge case | Evidence | PostDelegate status | Suggested fix |
|---|---|---|---|
| Unaudited apps can only post `SELF_ONLY`; the privacy options come from `creator_info`. | BB@99545be, BB@c0d5c76, PZ@e19c855d | handled: `providers/tiktok.py:45-70`, `364-493` (`_check_creator_constraints`). | none |
| `creator_info` outage should not block the publish. | PZ#1300 | handled: it logs and proceeds (`tiktok.py:452-460`). | none |
| FILE_UPLOAD single chunk is capped at 64 MB; there is no multi-chunk path. | PZ#814, PZ#946, PZ@a15f6ea3 | **FIXED at schedule time (media-pipeline):** `max_video_bytes=64_000_000` is in `TIKTOK_MEDIA_RULES`. TikTok's docs allow 4 GB as 5-64 MB chunks, so multi-chunk upload remains a follow-up. Was: partial. `MAX_SINGLE_CHUNK_SIZE = 64_000_000` (`tiktok.py:83-85`) is enforced only at publish (`tiktok.py:641-650`). It is not in `TIKTOK_MEDIA_RULES` (`tiktok.py:128-160`), so the user finds out late. | Add `max_video_bytes=64_000_000` to the media rule, or implement chunked upload. |
| Spam-risk "too many pending shares" and the 5 drafts/day cap (draft mode). | PZ@e19c855d, PZ#1300 | handled: `tiktok.py:59-61`, and the inbox/draft option (`tiktok.py:167-170`). | none |
| Description/caption missing on draft posts. | PZ#1507 | handled: `title` is the caption (`tiktok.py:516`), and draft init is separate (`tiktok.py:498`, `532`). | none |
| Photo posts: PULL_FROM_URL only, domain verification, image size pre-check. | PZ@a15f6ea3, PZ@96e46cfe, PZ@009bd365 | partial. The photo rules cap the item count only (`tiktok.py:134-139`). There is no size/format pre-check, and a URL-ownership failure is a mapped permanent error (`tiktok.py:59`). | Add a photo dimension/size rule. |
| `NotEnoughScopes` on connect. | PZ#1773, PZ#515 | partial. `get_granted_scopes` exists on Meta only; I did not verify TikTok's scope check on connect. | Show which scope is missing. |
| Publish ID versus public post ID (analytics). | PZ@bcbc1195, PZ@634c6da1 | handled by design: the async confirm sweep (`engine.py:1477-1660`) settles status, and BB@4ca3647 fixed the stranding. | none |
| `video_cover_timestamp_ms` must be sent. | PZ@881ac768, PZ@148cc961 | handled: `tiktok.py:523-530`. | none |
| PKCE on all OAuth flows. | BB@5fd5d36 | handled: `apps/social_accounts/oauth_pkce.py`. | none |

## YouTube

| Edge case | Evidence | PostDelegate status | Suggested fix |
|---|---|---|---|
| The upload is a single streamed PUT. A dropped connection mid-upload, or a chunk-level transient error, loses the whole upload. The resumable protocol allows `Content-Range: bytes */*` to ask for the offset and resume. | PZ@d1765d40 (chunk transient errors), PZ#860 (AggregateError), PZ#2011, PZ#946 | **FIXED (media-pipeline):** 8 MB chunks, status query on interruption, session URI kept in `upload_cache` for 24 h so an engine retry resumes; a 404 starts a new session; chunk/status steps are not ambiguous writes. Was: **NOT handled.** `providers/youtube.py:489-523` makes one PUT with a 300 s timeout. A transport error is marked `ambiguous_write` (`providers/base.py:876-883`), so a retry never happens and the post is parked failed even though the video may exist. | Implement resume (query offset, then PUT the remaining range) with 3 attempts. Persist the upload URI in `upload_cache`. |
| Daily quota (10,000 units, `videos.insert` costs 1,600) comes back as 403, not 429. | PZ@30e1f83c, BB@e5ac2d4, BB@5782333 | handled: `youtube.py:819-873` (`QuotaExceededError` with the Pacific-midnight reset, scoped breaker). | none |
| Refresh token is not returned on refresh, so it must be preserved. Daily re-auth otherwise. | PZ@943293dc, PZ#1351, PZ#1595, PZ@49aa92ac | handled: `models.py:430-432` keeps the old refresh token unless a new one is returned. Permanent `invalid_grant` purges the connection (`models.py:462-470`, `youtube_compliance.py`). | none |
| `userinfo.get` silently fails, so the id is empty and connect fails with "Invalid API key". | PZ#1585 | partial. I did not verify that profile fetch failures at connect time are surfaced rather than swallowed. | Make the connect path raise when `channel id` is empty. |
| Thumbnail failure should not fail the post, and the video id and URL must be kept. | PZ@23beddcd, PZ@b2897ff7 | handled: the thumbnail error is logged and swallowed (`youtube.py:525-547`). However, no message tells the user "channel not verified" when thumbnails fail. | Surface a post-publish warning on the PlatformPost. |
| Brand accounts, channel selection. | PZ#689 | partial. Not verified. | Test with a brand account. |
| Tags length (500 chars total) and title 100. | PZ#936 | partial. `PLATFORM_FIELD_CONFIG` has `title_max_length=100` (`models.py:540`). I did not find a tags-total-length rule (`youtube.py:474`). | Validate the 500-char tags total. |
| "Couldn't confirm it was published" with no video id. | PZ#2011 | **FIXED (media-pipeline):** an empty video id is now an ambiguous `APIError`, not an empty-id success. Was: partial. The confirm sweep exists, but YouTube doesn't use async confirm. A response without an `id` produces an empty-id success (`youtube.py:521-523`). | Treat an empty `video_id` as a failure, not a success. |

## Pinterest

| Edge case | Evidence | PostDelegate status | Suggested fix |
|---|---|---|---|
| Transient "Something went wrong on our end" is retryable. | PZ@18a312b6, PZ@ba8cb481 | **NOT handled** (see the cross-cutting ambiguous-write row). A 5xx on `POST /pins` is parked as failed. | Add a Pinterest classifier. |
| Board id is numeric and must be mapped; boards:write scope. | PZ@002a341b, PZ@538f3e6f | handled: scope list `pinterest.py:112-115`; board required (`pinterest.py:226-231`, composer `services.py:~890`). | none |
| Video pins must upload the mp4, not the first media item. | PZ@be413ec2 | partial. `_publish_video_pin` is routed from `is_video` in `extra` (`pinterest.py:250-253`), not from the sniffed type. | Route on the sniffed media type like Instagram. |
| Token refresh fails and the channel can't be refreshed from the UI. | PZ#1598 | partial. Refresh keeps the old refresh token if none is returned (`pinterest.py:195`). `refresh_token_expires_in` isn't tracked. | Track the refresh token expiry. |
| Local file upload not supported; hosted URL only. | n/a | handled with a clear permanent error (`pinterest.py:256-263`). The engine passes URLs. | none |

## Reddit

| Edge case | Evidence | PostDelegate status | Suggested fix |
|---|---|---|---|
| `RATELIMIT` is a 200 response with `json.errors`; retry only when it is the sole error. | PZ@6fb5df41, PZ@9fbaf550 | handled: `providers/reddit.py:305-322` (`RateLimitError` with `retry_after`). | none |
| Subreddit with slashes or `r/` prefix. | PZ@3000fddd, PZ@b52cd598, PZ@12136dcc | handled: `reddit.py:95-100`. | none |
| Required flair, NSFW, link vs self restrictions. Mapped permanent errors. | PZ#191, PZ#867, PZ#981 | handled: `reddit.py:73-92`. | none |
| Media posts unsupported by the API. | PZ#617 | handled / n/a: media refused at schedule time (`reddit.py:1-10`). | none |
| Multi-subreddit spam. | n/a | handled: capped and spaced (`reddit.py:66-72`, `117-130`). | none |

## Bluesky

| Edge case | Evidence | PostDelegate status | Suggested fix |
|---|---|---|---|
| The 300 limit is in graphemes. Emoji, ZWJ sequences and flags count as one. | PZ#388 | **Partial.** The server uses `len()`, which counts code points (`providers/bluesky.py:502`, `449`). The composer uses UTF-16 `.length` (`compose.html:2427`). A family emoji is 1 grapheme but 5+ code points and 8 UTF-16 units. It is rejected early, or a legitimate post is blocked. | Use a grapheme segmenter on both sides (Python `regex` `\X`; JS `Intl.Segmenter`). |
| Images need `aspectRatio` or the preview frame is wrong. | PZ#885, PZ@81342042 | **FIXED (media-pipeline):** `aspectRatio` on images and video from the measured (or derived) size. Was: **NOT handled.** The image embed sends only `alt` and `image` (`bluesky.py:672-673`). | Send `aspectRatio{width,height}` from the asset metadata (video too). |
| Images over 1 MB are rejected (blob limit); Postiz compressed, we block. | PZ#443, PZ#412 | **FIXED (media-pipeline):** `compress_images=True`; the composer warns and the engine publishes a recompressed/shrunk JPEG copy (an unfittable image fails with a clear message). Was: partial. The rule blocks at schedule time (`bluesky.py:70`), so the user must shrink the file. No auto-resize. | Offer auto-recompress to under 1 MB (Pillow). |
| Link facets: trailing punctuation, and hashtag/mention byte offsets. | PZ@fab0b524, PZ#952 | partial. UTF-8 byte offsets are correct (`bluesky.py:579-611`). The URL regex `https?://[^\s\)\]>]+` keeps a trailing `.` or `,` in the link. The hashtag `\w+` accepts pure numbers. | Strip trailing punctuation. |
| Link preview (card) embed. | PZ#394 | **NOT handled.** Only images and video embeds exist (`bluesky.py:656-679`). | Optional. |
| One alt text for all images; alt text lost. | PZ#695 | **FIXED (media-pipeline):** per-image `alt` from `PublishContent.media_alt` (video `alt` too). Was: partial. `content.extra["alt_text"]` is a single string (`bluesky.py:672`), not the per-asset alt stored in `MediaAsset.alt_text` (`apps/media_library/models.py:138`). | Pass per-asset alt in `PublishContent`. |
| Video through the video service (job status polling). | PZ#879 | partial. `app.bsky.embed.video` is posted with a blob uploaded to the PDS (`bluesky.py:661-668`) rather than through `video.bsky.app` with job polling. This may fail above the PDS blob limit. | Verify with a 50-100 MB video. |
| Thread order and reply `cid`. | PZ#1123, PZ@aa12f100 | handled: reply-chain threads with `find_thread_part` and `getPosts` for the cid (`bluesky.py:430-445`). | none |
| Session refresh before publish; mark disconnected only on 4xx login failure. | PZ@0b26c98e, BB@df313f2 | handled: `bluesky.py:233-246`, `apps/social_accounts/tasks.py:18`. | none |
| 2FA and custom PDS. | PZ#988, PZ#507 | handled for custom PDS (SSRF-checked, `engine.py:115-121`). 2FA is n/a with app passwords. | none |
| First comment. | BB#137 | n/a / known gap: `supports_first_comment: False` for Bluesky (`models.py:556-558`). Thread replies are the equivalent. | none |

## Mastodon

| Edge case | Evidence | PostDelegate status | Suggested fix |
|---|---|---|---|
| Media attachment limit (4), described and processed asynchronously (206 until done, 422 when failed). | PZ#1105, PZ#1811, BB@91cbb9a | handled: `providers/mastodon.py:50-79`, `381-400`, `540-620`. The upload is reused across retries. | none |
| Alt text is not sent. | PZ#948, PZ@09f42742 | **FIXED (media-pipeline):** `description` on the media upload (cut to 1,500). | none |
| Limit read from the instance (`max_characters`). | PZ#1083 | handled: `mastodon.py:321`. | none |
| URLs count 23 characters; remote mentions count the username only. | PZ#1149 | **NOT handled.** There is no `caption_length` override for Mastodon (`providers/base.py:437` default `len`). The composer over-counts, which blocks valid posts. | Add `caption_length` for Mastodon. |
| Thread posts: only the first is published. | PZ#1525 | handled: the engine's thread parts (`engine.py:2207-2330`). | none |
| Instance picker and `scope` vs `scopes`. | PZ#1054, PZ#1141, BB@9fd2201 | handled. | none |

## Telegram, Discord, Slack, Lemmy, Farcaster, Nostr, Dribbble, VK, GMB

| Edge case | Evidence | PostDelegate status | Suggested fix |
|---|---|---|---|
| Telegram caption limit is 1024 with media versus 4096 for text. | PZ#1171, PZ#618, PZ#747 | handled: `providers/telegram.py:56-58`, `418-433` (a long text is sent as a separate message). | none |
| Telegram 400 rejections are non-retryable; HTML escaping; forum topics. | PZ@225aebca, PZ#909, PZ#1062 | handled: `telegram.py:77-153`, `271`. Forum topics were not verified. | none |
| Discord content is capped at 2000; embed suppression; the 50001/50013 permission errors. | PZ#378, PZ#915, PZ@d6bc6eb0 | handled: `providers/discord.py:65`, `98-99`, `129`, `197-210`. | none |
| Slack `chat.postMessage` returns `ok:false` with HTTP 200, so failures are marked published. Video is rejected. The old 280-char cap. | PZ@4e83569e, PZ@678acd4a, PZ#880, PZ#1172 | handled: `providers/slack.py:209-240` (error code mapping, rate limits). | none |
| Lemmy errors surface in the response instead of crashing; Nostr key as bytes; VK errors marked completed. | PZ@0a2fc960, PZ@f9e0d010, PZ@96bca725 | handled: `lemmy.py:188-203`, `nostr.py:421`, and the base ambiguity guard. | none |
| GMB: the post can be created in REJECTED state, or return a non-JSON body. The release URL is `searchUrl`. | PZ@f700b8a1, PZ@1d965049, PZ@6345ca57 | **Partial.** `providers/google_business.py:511-521` treats any 2xx JSON as success and returns `searchUrl`. It does not check `state == REJECTED`. | After create, read `state` and fail on REJECTED. |
| GMB localPosts URL malformed, and SERVICE_DISABLED. | PZ#1964, PZ#1692 | partial. The URL is built from `location_id` (`google_business.py:511`). I did not verify account-prefix handling. | Add a unit test against the documented path. |
| WordPress needs a real User-Agent. Connect errors. | PZ@b0103f73, PZ@3c7a6863 | partial. SSRF-checked site URL (`engine.py:147`), but I did not verify the User-Agent on WordPress requests. | Send a UA header. |
| Hashnode endpoint and schema changed. | PZ@12978bef, PZ@0ffdbc97 | partial. `providers/hashnode.py` was not diffed against the live schema. | Smoke-test publish. |

---

## Cross-cutting

### Retries, errors and ambiguity

| Edge case | Evidence | PostDelegate status | Suggested fix |
|---|---|---|---|
| **Ambiguous-write rule is too broad.** Every mutating request that times out or returns 5xx is flagged `ambiguous_write`, and the engine then fails the post permanently. This also hits non-committing steps: container creation, upload init, chunk PUT, token POST. | PZ@18a312b6, PZ@b95e1e2c, PZ@d1765d40, PZ#1306 | **NOT handled.** `providers/base.py:876-892` sets it for all providers. Only X (`x.py:896`) and Slack (`slack.py:441`) reset it, and `engine.py:930-940` parks it as failed. Instagram, Facebook, Threads, LinkedIn, TikTok, Pinterest, YouTube, Bluesky and Mastodon have no reset. A routine Meta 500 on container creation costs the user the post. | Add `request(..., commit=False)` (or an `idempotent=True` kwarg) so only the final commit call is ambiguous. Container create, upload init and chunk calls become retryable. |
| Auth failures. Credential rejection before the retry budget. | PZ@935c80a8, PZ@fc349942, PZ#1595 | partial. `_refused_a_live_token` (`engine.py:1692-1725`) fails fast only when the stored token isn't expired. A refresh failure at publish is swallowed (`engine.py:345-364`), so a dead refresh token wastes 3 retries (1, 5, 30 minutes) before "reconnect". Nothing at publish time sets the account to `needs_reconnect`. | On `invalid_grant` or a 401 after refresh, set `connection_status=TOKEN_EXPIRING/DISCONNECTED`, notify, and fail fast. |
| Health check turns any transient failure into a permanent ERROR. | PZ#1598, PZ#757 | **NOT handled.** `apps/social_accounts/tasks.py:155-158` sets `ConnectionStatus.ERROR` on any exception, including 5xx, timeouts and 429. ERROR drops the account from `schedule_all_health_checks` (`tasks.py:186-200`, CONNECTED/TOKEN_EXPIRING only), and `apps/composer/services.py:164-170` then refuses new scheduling. Only a manual reconnect recovers it. | Set ERROR only for credential rejections. Leave status unchanged (with the message) for 5xx, timeouts and 429, and re-include ERROR in the sweep. |
| Retry ladder is 1/5/30 minutes, 3 tries. | PZ@7b8fa7fb, PZ@6859f0d0 | partial. `engine.py:78`, `154`. Provider-supplied reset times override it (`engine.py:1373-1415`). Meta 4/17/32/613/368 don't produce `retry_after` (see Meta). | Meta classifier. |
| Raw provider bodies must not reach users, and failure emails must not storm. | BB@14f6159, PZ#2231 | handled: `error_messages.py:213-257`, `engine.py:1260-1370`. | none |
| Worker killed mid-publish. | PZ#1259, PZ#862 | handled: `confirm_pending_publishes` / `PUBLISH_INTERRUPTED_MESSAGE` (`engine.py:1477-1660`). | none |
| Deleted or edited scheduled posts still publish. | PZ#1213, PZ#1340 | n/a / handled by design: no external workflow. Status is re-checked under a row lock (`engine.py:543-590`). | none |
| Overdue backlog after an outage floods the platforms at once (no maximum lateness). | PZ#1259 (related) | NOT handled (low). `_get_due_platform_posts` (`engine.py:513-541`) has no lower bound. Per-platform concurrency caps limit the blast. | Optional: warn or hold posts more than N hours late. |

### Scheduling and timezones

| Edge case | Evidence | PostDelegate status | Suggested fix |
|---|---|---|---|
| Recurring posts drift across DST. | PZ#1145 | handled: series expand in the workspace zone (`apps/calendar/tasks.py:120-176`). Slots use wall-clock time (`apps/calendar/services.py:83-101`). | none |
| "Post now" date offset (~2h in the past); client timezone. | PZ#1176 | handled: UTC storage, `USE_TZ=True` (`config/settings/base.py:276-278`), and a conversion with the workspace zone (`apps/composer/views.py:1477`). Overdue rows publish at once. | none |
| Nonexistent local times (spring-forward gap) and ambiguous times (fall-back) from `replace(tzinfo=ZoneInfo)`. | PZ#1145 | partial. `calendar/services.py:101`, `composer/views.py:1478`, `4660`. `zoneinfo` resolves both silently (gap rolls forward, ambiguity picks the first). No user-visible warning. | Optional: warn when the chosen local time is in a DST gap. |
| Dragging a post to another date keeps its time. | PZ#1052, PZ#471 | not verified. | Add a regression test. |
| Whitespace-only content is accepted and fails later. | PZ#1498 | handled: `apps/composer/services.py:1169`. | none |
| Scheduling against an errored or disconnected account is refused, to avoid burning quota. | n/a | handled: `composer/services.py:164-170` (but see the health-check row above). | none |

### Token refresh

| Edge case | Evidence | PostDelegate status | Suggested fix |
|---|---|---|---|
| Concurrent refreshes burn a rotating refresh token. | PZ@943acec8, PZ@49aa92ac, BB@0d15d53 | handled: row lock plus a stale-credential check (`apps/social_accounts/models.py:383-470`, `token_refresh.py`). | none |
| Refresh window and unknown expiry. | PZ@fc349942, PZ@dac6e8e1 | handled: a 7-day window before publish (`engine.py:342`), expiry bootstrap for Bluesky and Threads (`tasks.py:18`, `72-83`). | none |
| Refresh succeeded but the status stays stale. | n/a | handled: `models.py:430-445` sets CONNECTED. | none |
| Platforms whose refresh token can also expire (Pinterest 60 days, LinkedIn). | PZ#1598 | partial. No refresh-token expiry field. | Store `refresh_token_expires_at` where the platform returns it, and warn. |

### Media pipeline

| Edge case | Evidence | PostDelegate status | Suggested fix |
|---|---|---|---|
| Per-platform media rules checked at schedule time and at publish. | PZ@d6bc6eb0 and others | handled: `providers/types.py:218-258`, `apps/composer/services.py:665-790`, `engine.py:757-775`. This is better than Postiz. | none |
| No normalisation or transcoding: HEIC, WebP, PNG for JPEG-only networks, oversized stills, video codec/audio/fps. | PZ#1108, PZ#462, PZ#1129, PZ#946, PZ#814 | **FIXED for stills (media-pipeline):** publish-time derived JPEG (Instagram) and size-fitted JPEG (Bluesky) via Pillow, driven by `MediaRules.image_formats` / `compress_images`; original never mutated, copies deleted with the asset. Video transcoding and HEIC intake remain open. Was: **NOT handled.** `ffmpeg` is used only for probing, thumbnails and trim (`apps/media_library/services.py:512-730`). The validators accept the formats, and the platform rejects later. | Add a normalise-on-upload step (HEIC/WebP to JPEG; optional H.264/AAC remux) behind a setting. |
| Unmeasured video (ffprobe fails) skips the duration, size and aspect rules silently. | BB#204 (0 s and 0x0 video) | **FIXED (media-pipeline):** `MediaAsset.metadata_unverified` (no schema change) drives a composer warning on networks with length/shape rules, the API exposes `metadata_verified`, byte limits still apply, and the worker logs the probe failure. `ffmpeg` (with `ffprobe`) is already in the Dockerfile. Was: partial. `extract_video_metadata` returns `{}` on failure (`media_library/services.py:539-551`), and `composer/services.py:771` skips the rule when duration is 0. | Mark the asset "unprobed", and block or warn before it can be scheduled to a rule-bound network. |
| Alt text is stored per post media (`apps/composer/models.py:821`, `892`; `media_library/models.py:138`) but never sent. `PublishContent` (`providers/types.py`) has no alt field. Only Pinterest (`pinterest.py:247`) and Bluesky (single string) use it. | PZ#948, PZ#695, BB#165 (related) | **FIXED (media-pipeline):** `PublishContent.media_alt` (post's own alt, else the library asset's), `SocialProvider.supports_alt_text`; sent for X, Bluesky, Mastodon, LinkedIn, Pinterest, Facebook, Instagram (images), Threads, Tumblr, Lemmy. Was: **NOT handled.** X, Mastodon, Threads, Instagram, Facebook and LinkedIn all support alt text. This is also an accessibility gap. | Add `media_alt: list[str]` to `PublishContent`, and wire the supported providers. |
| Huge images exhaust memory. | BB@3f98205, BB@76b9735 | handled: bounded decode (`media_library/services.py:370-430`). | none |
| Large uploads behind Cloudflare (100 MB request cap). | BB#129 (open) | handled when object storage is on: presigned direct upload (`media_library/services.py:137`). Local storage still posts the whole file. | Require S3/R2 in production. |
| Remote media fetched by URL has stalled reads. | PZ@b6ead58b, BB@daff36f | handled: bounded remote fetch (`linkedin.py:988`, BB@daff36f). | none |
| Streamed uploads, no whole-file buffering. | PZ@9980825a | handled: BB@04f7f64 (`bluesky.py:560-590`, `youtube.py:508-523`). | none |
| Media deleted from the library should be deleted from storage. | PZ#996, PZ#1217 | not verified here. | Check `MediaAsset` delete signals vs the R2 object. |

### Character counting

| Edge case | Evidence | PostDelegate status | Suggested fix |
|---|---|---|---|
| Composer counts UTF-16 units (`text.length`) while the server counts code points. They disagree on every emoji outside the BMP. | PZ#2218, PZ#1149 | **Partial.** `compose.html:2423-2433` vs `providers/base.py:437-440` and `providers/__init__.py:89-105`. | Count code points (`[...text].length`) client-side, and graphemes for Bluesky. |
| URL weighting: X (23) is done. Mastodon (23), Bluesky (link text counts in full, parameters included) and Threads are not. | PZ#1149 | partial. Only X has a `caption_length`. | Add per-provider `caption_length`. |
| Silent truncation. Pinterest descriptions are cut with `[: self.max_caption_length]` (`pinterest.py:236`), and TikTok's title is cut (`tiktok.py:516`). | PZ#273, PZ#442, PZ#483 (truncated text) | partial. The composer enforces `PLATFORM_CHAR_LIMITS` (`models.py:~500`), but the provider cuts silently if a limit slips through. | Raise `PublishError` instead of truncating. |

### Webhooks and platform deprecations

| Edge case | Evidence | PostDelegate status | Suggested fix |
|---|---|---|---|
| Webhook subscription failures degrade to polling and retry at most 5 times. | PZ#1216, BB@a304d5a | handled: `apps/social_accounts/webhooks.py:223` with `MAX_AUTOMATIC_RETRIES`. | none |
| API version sunsets: Meta Graph (v20 to v25), LinkedIn versions, Hashnode GraphQL, VK domains, Facebook metrics. | PZ@3fab214f, PZ#2005, PZ@12978bef, PZ@5a5da1ae, PZ@ce32dccb | partial. Versions are pinned (LinkedIn 202604; Graph version not centrally verified). No deprecation monitor. | Keep a "versions to rotate" table with dates, and alert on 426/400-version errors. |
| Meta dev-mode / App Review blocks connect ("Feature Unavailable"). | BB#155, BB#124, BB#183 | n/a to code. This is a launch blocker, tracked in `CODEX-platform-applications.md`. | Finish App Review first. |

---

## BrightBean upstream

### Upstream fixes we are missing

**None.** `git log HEAD..upstream/main` is empty. Nothing to cherry-pick.

For the record, the fixes that matter for launch are all in our history:

| Upstream commit | What it fixes | Local status |
|---|---|---|
| `91cbb9a` | Mastodon media processing wait | in tree (`providers/mastodon.py:50-79`) |
| `0d15d53` | Protect concurrent credential refreshes | in tree |
| `4414d52` | Reserve publishing capacity in the shared worker (BB#204, BB#199: posts stuck "scheduled") | in tree |
| `ee9d04c` | Stop scheduling posts that can only fail, and say why | in tree (`composer/services.py:164-170`, `media_rules_blocker`) |
| `4ca3647` | TikTok stranding and memory exhaustion | in tree |
| `0aec94a` | Bound publish fan-out to the Postgres connection budget | in tree (`engine.py:227-246`) |
| `14f6159` | Failure-email storms exhausting the Resend quota (BB#179) | in tree |
| `14555d1` | Threads App ID and long-lived token refresh (BB#45) | in tree |
| `df313f2` | Bluesky session refresh before publish | in tree |
| `8107cf2` | LinkedIn reserved-character escaping (BB#121) | in tree |
| `1ed0fd4`, `26eb8be`, `fc0b0cf` | Instagram single video as a Reel; route on media type | in tree |
| `c266995` | Route media on the sniffed type, not the URL suffix | in tree |
| `04f7f64`, `3f98205`, `76b9735` | Streamed uploads and a bounded image decode | in tree |

### Upstream issues relevant to us

| Edge case | Evidence | PostDelegate status | Suggested fix |
|---|---|---|---|
| Posts stuck "scheduled" with no error; videos with 0 s duration and 0x0 size. | BB#204, BB#199, BB#112 | handled for the scheduling cause (BB@4414d52). The 0x0 probe issue remains: see "Unmeasured video" above. | Block or flag unprobed video. |
| Meta connect: "Feature Unavailable" while the app is in development mode; "No Instagram Business accounts found". | BB#155, BB#124, BB#183 | n/a (App Review and config). The "no Business accounts" copy exists (`meta_accounts.py`). | Complete App Review and Instagram Login before launch. |
| LinkedIn API version sunset (202401 broke posting). | BB#37 | partial: now `202604`, but there is no 426 alerting. | See LinkedIn. |
| LinkedIn Personal requires restricted scopes that self-serve apps can't get. | BB#26 | handled: personal uses `w_member_social` only (`linkedin_personal.py`). | none |
| Media 404 in production with `DEBUG=False` and filesystem storage. | BB#130 | handled: BB@2444903, scoped public media route. Needs a deploy smoke test. | Add the check to the deploy checklist. |
| Chunk uploading behind Cloudflare. | BB#129 (open) | partial: presigned direct upload with S3/R2. | Require R2 in production. |
| "Cannot save draft": `POST /compose/save/` returns 204 and the draft isn't listed. | BB#164 (open) | not verified. | Reproduce on the current build. |
| Additional post parameters (Reels vs video, visibility, AI-disclosure) via API. | BB#203 (open), BB#165 | partial. TikTok has `is_aigc` and privacy (`tiktok.py:79-80`), and Facebook Reels are supported. | Check the API/MCP schemas expose `platform_extra`. |
| Mastodon quick-start publish failing (setup step). | BB#112 | handled upstream; deploy docs only. | none |
| Bluesky shown as "credentials not configured"; first comment for Bluesky. | BB#18, BB#137 | handled / known: Bluesky is session auth; no first comment. | none |
| Recurring single posts. | BB#159 | handled: `apps/calendar/tasks.py`, `test_recurrence.py`. | none |
| Draft publish button stays enabled after publish. | BB#116 | handled upstream (BB@14552b8). | none |

---

## Prioritised top 20 gaps (likelihood x impact)

1. **Ambiguous-write flag on every mutating 5xx or timeout fails posts permanently.** It covers non-committing steps on 9 of 12 providers (`providers/base.py:876-892`, `engine.py:930-940`). Any routine Meta, LinkedIn or Pinterest hiccup loses the post. High likelihood, high impact. Fix: only mark the commit call ambiguous.
2. **No Meta Graph error taxonomy** (Instagram, Facebook, Threads): 190/463/467 expired token, 368/4/17/32/613 rate limits, 2207xxx container errors, 459/490 checkpoint. Wrong retries, no `retry_after`, and no reconnect prompt. `providers/base.py:926`, `error_messages.py:99`. Meta is the biggest user segment.
3. **Health check marks an account ERROR on any transient failure, and ERROR accounts are never rechecked or schedulable.** `apps/social_accounts/tasks.py:155-158`, `186-200`, `apps/composer/services.py:164-170`. A single Meta 503 breaks a customer's account until they reconnect by hand.
4. **A publish-time auth failure does not flag the account for reconnect.** A dead refresh token wastes 3 retries (about 36 minutes), and later scheduled posts each fail separately. `engine.py:345-364`, `1692-1725`.
5. **[FIXED in media-pipeline]** **YouTube upload is a single PUT with no resume.** It fails on large or flaky uploads, and the failure is flagged ambiguous. `providers/youtube.py:489-523`.
6. **[FIXED in media-pipeline]** **Alt text is stored but never published.** There is no field in `PublishContent`, and Bluesky takes one string for all images. This is an accessibility and quality gap on every network that supports alt text.
7. **[stills FIXED in media-pipeline; video transcoding open]** **No image/video normalisation.** HEIC, WebP and PNG for JPEG-only Instagram, and video codec/audio. Phone uploads fail at the platform. `apps/media_library/services.py`, `instagram.py:119-127`.
8. **[FIXED in media-pipeline]** **Unmeasured video (ffprobe failure) silently bypasses all duration, size and ratio rules.** `media_library/services.py:539-551`, `composer/services.py:771`. Related to BB#204.
9. **Instagram container flow:** a 120 s timeout, no container reuse on retry, and the code 100/33 status-GET bug. `instagram.py:101`, `521-546`.
10. **Character counting mismatch.** The composer uses UTF-16, the server uses code points, and Bluesky needs graphemes. Mastodon URLs count 23. `compose.html:2423-2433`, `bluesky.py:502`. It either blocks valid posts or lets over-limit ones through, and it hits emoji-heavy users every day.
11. **[aspectRatio, 1 MB compress, alt FIXED in media-pipeline; link card and video service open]** **Bluesky:** no `aspectRatio` on images and video, a 1 MB image block with no auto-compress, no link card, and video through `uploadBlob` rather than the video service. `bluesky.py:656-679`.
12. **[FIXED in media-pipeline]** **TikTok 64 MB single-chunk limit is not in the schedule-time media rule.** Users find out when publishing fails. `tiktok.py:83-85`, `128-160`, `641-650`.
13. **LinkedIn API version sunset (`202604`) has no 426 detection or alert.** When it expires, every LinkedIn post fails at once. `linkedin.py:55-59`.
14. **GMB `REJECTED` state is not checked.** The post reports success while nothing is live. `google_business.py:511-521`.
15. **Pinterest transient 5xx and the video-pin routing:** `pinterest.py:250-253`. This falls under #1 and the routing should use the sniffed type.
16. **Threads link-count rule (`bad_body` above the limit) and the UNKNOWN container error.** `threads.py:398`.
17. **[FIXED in media-pipeline]** **Media fetch preflight.** Verify `APP_URL` is public HTTPS and reachable by Meta, or the 2207052 and 9004 errors are all you get. `engine.py:449`.
18. **No refresh-token expiry tracking** (Pinterest 60 days, LinkedIn). The account dies silently after the window. `models.py:430-445`.
19. **Silent truncation in providers** (Pinterest description, TikTok title) rather than a validation error. `pinterest.py:236`, `tiktok.py:516`.
20. **Overdue backlog after an outage publishes at once with no maximum lateness.** `engine.py:513-541`. Lower likelihood, but there is real reputational risk (a three-day-old "flash sale" post going out).

### Verify before launch (cheap)
- Reproduce BB#164, "Save Draft" returning 204 with no draft listed.
- Brand-account YouTube connect (PZ#689).
- Hashnode and WordPress live smoke tests (PZ@12978bef, PZ@b0103f73).
- TikTok `NotEnoughScopes` copy (PZ#1773).
- Media delete also deleting the stored object (PZ#996).
