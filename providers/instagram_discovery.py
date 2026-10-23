"""Business Discovery: public numbers of another Instagram professional account.

Shared by both Instagram providers, which differ only in the Graph host.
Reference: https://developers.facebook.com/docs/instagram-platform/instagram-api-with-facebook-login/business-discovery
(checked 2026-10-24). The query is made as the customer's own connected Instagram
professional account (``GET /<ig-user-id>?fields=business_discovery.username(<name>){...}``).
It returns only professional (business or creator) accounts that are not age-gated;
a personal account, or one that does not exist, comes back as an error and is reported
as not found. Counts of likes may be hidden by the owner and then read as 0, so the
engagement average is only a guide.
"""

from __future__ import annotations

from datetime import datetime

from .base import SocialProvider
from .exceptions import APIError, PublicProfileNotFound
from .types import PublicProfileMetrics

SAMPLE_POSTS = 25
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
        # Graph answers 400 (code 110 / 24) for a username it cannot discover.
        if exc.status_code == 400:
            raise PublicProfileNotFound(
                f"{handle} is not an Instagram business or creator account that can be read.",
                platform=provider.platform_name,
            ) from exc
        raise
    found = data.get("business_discovery") or {}
    if not found:
        raise PublicProfileNotFound(f"No Instagram professional account found for {handle}.", platform="Instagram")
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
