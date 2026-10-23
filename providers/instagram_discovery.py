"""Business Discovery: public numbers of another Instagram professional account.

Only the Facebook Login provider can use it: Meta documents Business Discovery
for the Instagram API with Facebook Login, not with Instagram Login
(https://developers.facebook.com/docs/instagram-platform/instagram-api-with-facebook-login/business-discovery,
checked 2026-10-24). The query is made as the customer's own connected Instagram
professional account (``GET /<ig-user-id>?fields=business_discovery.username(<name>){...}``).
It returns only professional (business or creator) accounts that are not age-gated;
a personal account, or one that does not exist, comes back as an error and is reported
as not found. Counts of likes may be hidden by the owner and then read as 0, so the
engagement average is only a guide.

Rate limits: each query counts against the customer's own Instagram account's
Business Use Case allowance, which scales with that account's reach. One read per
competitor per day is a negligible share of it, and a throttled answer (Graph error
codes 4, 17, 32, 613 or 80002) is raised as a rate limit so the day's remaining
reads on that account stop rather than retry.
"""

from __future__ import annotations

from datetime import datetime

from .base import SocialProvider
from .exceptions import APIError, PublicProfileNotFoundError, RateLimitError, TokenExpiredError
from .types import PublicProfileMetrics

SAMPLE_POSTS = 25
# Graph error codes for a throttled app or account, and for a token that no longer works.
_THROTTLED = {4, 17, 32, 613, 80002}
_TOKEN_INVALID = {102, 190}
_FIELDS = (
    "business_discovery.username({username}){{username,name,followers_count,media_count,"
    "media.limit(" + str(SAMPLE_POSTS) + "){{like_count,comments_count,timestamp}}}}"
)


def business_discovery_metrics(
    provider: SocialProvider, *, base_url: str, ig_user_id: str, access_token: str, handle: str
) -> PublicProfileMetrics:
    try:
        data = provider._request(
            "GET",
            f"{base_url}/{ig_user_id or 'me'}",
            access_token=access_token,
            params={"fields": _FIELDS.format(username=handle)},
        ).json()
    except APIError as exc:
        code = (exc.raw_response.get("error") or {}).get("code")
        if code in _THROTTLED:
            raise RateLimitError(str(exc), platform=provider.platform_name) from exc
        if code in _TOKEN_INVALID:
            raise TokenExpiredError(str(exc), status_code=exc.status_code, platform=provider.platform_name) from exc
        # Anything else Graph refuses with a 400 is a username it cannot discover
        # (missing, personal, or age-gated).
        if exc.status_code == 400:
            raise PublicProfileNotFoundError(
                f"{handle} is not an Instagram business or creator account that can be read.",
                platform=provider.platform_name,
            ) from exc
        raise
    found = data.get("business_discovery") or {}
    if not found:
        raise PublicProfileNotFoundError(
            f"No Instagram professional account found for {handle}.", platform=provider.platform_name
        )
    media = (found.get("media") or {}).get("data", [])
    stamps = []
    for item in media:
        try:
            stamps.append(datetime.fromisoformat(str(item.get("timestamp")).replace("+0000", "+00:00")))
        except ValueError:
            continue
    engagement = [int(m.get("like_count") or 0) + int(m.get("comments_count") or 0) for m in media]
    return PublicProfileMetrics(
        handle=found.get("username") or handle,
        display_name=found.get("name") or "",
        followers=found.get("followers_count"),
        posts_count=found.get("media_count"),
        avg_engagement=round(sum(engagement) / len(engagement), 2) if engagement else None,
        posts_per_week=provider._posts_per_week(stamps),
        sampled_posts=len(engagement),
        profile_url=f"https://www.instagram.com/{found.get('username') or handle}/",
    )
