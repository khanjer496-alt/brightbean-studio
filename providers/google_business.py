"""Google Business Profile API provider implementation."""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta
from urllib.parse import urlencode, urlparse

from .base import SocialProvider
from .exceptions import OAuthError, PublishError
from .types import (
    AccountProfile,
    AuthType,
    MediaRules,
    MediaType,
    OAuthTokens,
    PostMetrics,
    PostSetting,
    PostType,
    PublishContent,
    PublishResult,
    ReplyResult,
    ReviewPage,
)
from .types import InboxMessage as ProviderInboxMessage

logger = logging.getLogger(__name__)

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
ACCOUNTS_API = "https://mybusinessaccountmanagement.googleapis.com/v1"
BUSINESS_INFO_API = "https://mybusinessbusinessinformation.googleapis.com/v1"
POSTS_API = "https://mybusiness.googleapis.com/v4"
# Reviews still live only on the v4 surface (``accounts.locations.reviews``);
# the newer Business Information / Account Management APIs have no reviews
# resource. Verified against
# https://developers.google.com/my-business/reference/rest/v4/accounts.locations.reviews
#   list:        GET    {parent=accounts/*/locations/*}/reviews  (pageSize <= 50, orderBy "updateTime desc")
#   updateReply: PUT    {name=accounts/*/locations/*/reviews/*}/reply   (body: ReviewReply{comment})
#   deleteReply: DELETE {name=accounts/*/locations/*/reviews/*}/reply
REVIEWS_API = POSTS_API
REVIEWS_PAGE_SIZE = 50
# A first import walks this many pages (1,000 reviews) so the history analytics
# reads is complete for nearly every location; later polls stop at ``since``.
REVIEWS_FIRST_IMPORT_PAGES = 20
# The reconcile that finds reviews deleted on Google reads only ids, but still
# stops here (5,000 reviews) rather than walking a location without end.
REVIEW_IDS_MAX_PAGES = 100
# Accounts searched for the connected location when a login manages several.
REVIEWS_OWNER_ACCOUNTS_MAX = 20
# A reply or edit made on Google can move updateTime a little after we last
# looked, and clocks differ; walk this far back past ``since`` to catch them.
REVIEWS_LOOKBACK = timedelta(days=1)
# ReviewReply.comment is capped at 4096 bytes.
REVIEW_REPLY_MAX_BYTES = 4096
STAR_RATINGS = {"ONE": 1, "TWO": 2, "THREE": 3, "FOUR": 4, "FIVE": 5}


def _parse_rfc3339(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


# What a Business Profile post's photo may be: 10 KB to 5 MB, at least 250x250.
# https://support.google.com/business/answer/6103862
GOOGLE_BUSINESS_MEDIA_RULES = {
    PostType.IMAGE: MediaRules(
        source="https://support.google.com/business/answer/6103862",
        min_width=250,
        min_height=250,
        max_image_bytes=5 * 1024 * 1024,
    ),
}


# LocalPost fields, from
# https://developers.google.com/my-business/reference/rest/v4/accounts.locations.localPosts (checked 2026-10-24):
# ``callToAction`` {actionType: BOOK|ORDER|SHOP|LEARN_MORE|SIGN_UP|CALL, url (left unset for CALL); ignored for
# OFFER}, ``event`` {title, schedule{startDate,startTime,endDate,endTime}} (required for EVENT and OFFER, and the
# schedule "must contain all fields"), ``offer`` {couponCode, redeemOnlineUrl, termsConditions}. GET_OFFER is
# deprecated in favour of the OFFER topic type, and ALERT (COVID-19) is not offered.
CTA_ACTION_TYPES = (
    ("LEARN_MORE", "Learn more"),
    ("BOOK", "Book"),
    ("ORDER", "Order online"),
    ("SHOP", "Buy"),
    ("SIGN_UP", "Sign up"),
    ("CALL", "Call now"),
)
GOOGLE_BUSINESS_POST_SETTINGS = (
    PostSetting(
        key="topic_type",
        label="Post type",
        kind="select",
        placeholder="Update",
        choices=(("EVENT", "Event"), ("OFFER", "Offer")),
        help="An event or offer needs a title and a start and end date and time.",
    ),
    PostSetting(
        key="cta_type",
        label="Button",
        kind="select",
        placeholder="No button",
        choices=CTA_ACTION_TYPES,
        show_when="topic_type=|EVENT",
        help="Adds a call-to-action button. Not available on offers; Google ignores it there.",
    ),
    PostSetting(
        key="cta_url",
        label="Button link",
        kind="text",
        max_length=2048,
        placeholder="https://example.com/book",
        show_when="topic_type=|EVENT",
        help="Where the button leads. Leave empty for Call now, which dials the business phone number.",
    ),
    PostSetting(
        key="event_title", label="Event or offer title", kind="text", max_length=58, show_when="topic_type=EVENT|OFFER"
    ),
    PostSetting(key="event_start_date", label="Starts on", kind="date", show_when="topic_type=EVENT|OFFER"),
    PostSetting(key="event_start_time", label="Starts at", kind="time", show_when="topic_type=EVENT|OFFER"),
    PostSetting(key="event_end_date", label="Ends on", kind="date", show_when="topic_type=EVENT|OFFER"),
    PostSetting(key="event_end_time", label="Ends at", kind="time", show_when="topic_type=EVENT|OFFER"),
    PostSetting(
        key="offer_coupon_code",
        label="Coupon code",
        kind="text",
        max_length=100,
        show_when="topic_type=OFFER",
    ),
    PostSetting(
        key="offer_redeem_url",
        label="Redeem online link",
        kind="text",
        max_length=2048,
        placeholder="https://example.com/offer",
        show_when="topic_type=OFFER",
    ),
    PostSetting(
        key="offer_terms",
        label="Terms and conditions",
        kind="text",
        max_length=1000,
        show_when="topic_type=OFFER",
    ),
)


def _valid_web_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def _schedule_part(settings: dict, name: str) -> datetime | None:
    try:
        day = date.fromisoformat(str(settings.get(f"event_{name}_date") or ""))
        clock = time.fromisoformat(str(settings.get(f"event_{name}_time") or ""))
    except ValueError:
        return None
    return datetime.combine(day, clock)


class GoogleBusinessProvider(SocialProvider):
    """Google Business Profile provider.

    Uses Google OAuth 2.0.  The ``credentials`` dict must contain:

    - ``client_id``
    - ``client_secret``

    Optional:
    - ``account_id`` – Google Business account ID
    - ``location_id`` – Google Business location ID
    """

    media_rules = GOOGLE_BUSINESS_MEDIA_RULES
    post_settings = GOOGLE_BUSINESS_POST_SETTINGS
    supports_reviews = True

    # Publishes from hosted URLs only — the platform fetches the media
    # itself, so ``PublishContent.media_files`` is never read and the engine
    # can skip downloading the asset to local disk entirely.
    needs_local_media = False

    writing_style = (
        "A local-business update: friendly, direct and useful, ending with a clear call to action. No hashtags."
    )

    # ------------------------------------------------------------------
    # Metadata
    # ------------------------------------------------------------------

    @property
    def platform_name(self) -> str:
        return "Google Business"

    @property
    def auth_type(self) -> AuthType:
        return AuthType.OAUTH2

    @property
    def max_caption_length(self) -> int:
        return 1500

    @property
    def supported_post_types(self) -> list[PostType]:
        return [PostType.TEXT, PostType.IMAGE]

    @property
    def supported_media_types(self) -> list[MediaType]:
        return [MediaType.JPEG, MediaType.PNG]

    @property
    def required_scopes(self) -> list[str]:
        return ["https://www.googleapis.com/auth/business.manage"]

    # ------------------------------------------------------------------
    # OAuth
    # ------------------------------------------------------------------

    def get_auth_url(self, redirect_uri: str, state: str, code_verifier: str | None = None) -> str:
        """Build the Google OAuth 2.0 authorization URL."""
        params = {
            "client_id": self.credentials["client_id"],
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(self.required_scopes),
            "state": state,
            "access_type": "offline",
            "prompt": "consent",
        }
        return f"{AUTH_URL}?{urlencode(params)}"

    def exchange_code(self, code: str, redirect_uri: str, code_verifier: str | None = None) -> OAuthTokens:
        """Exchange an authorization code for Google access/refresh tokens."""
        resp = self._request(
            "POST",
            TOKEN_URL,
            data={
                "client_id": self.credentials["client_id"],
                "client_secret": self.credentials["client_secret"],
                "code": code,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            },
        )
        data = resp.json()
        if "error" in data:
            raise OAuthError(
                f"Token exchange failed: {data.get('error_description', data['error'])}",
                platform=self.platform_name,
                raw_response=data,
            )
        return OAuthTokens(
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token"),
            expires_in=data.get("expires_in"),
            token_type=data.get("token_type", "Bearer"),
            scope=data.get("scope"),
            raw_response=data,
        )

    def refresh_token(self, refresh_token: str) -> OAuthTokens:
        """Refresh an expired Google access token."""
        resp = self._request(
            "POST",
            TOKEN_URL,
            data={
                "client_id": self.credentials["client_id"],
                "client_secret": self.credentials["client_secret"],
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
        )
        data = resp.json()
        if "error" in data:
            raise OAuthError(
                f"Token refresh failed: {data.get('error_description', data['error'])}",
                platform=self.platform_name,
                raw_response=data,
            )
        return OAuthTokens(
            access_token=data["access_token"],
            refresh_token=refresh_token,  # Google doesn't rotate refresh tokens
            expires_in=data.get("expires_in"),
            token_type=data.get("token_type", "Bearer"),
            scope=data.get("scope"),
            raw_response=data,
        )

    def revoke_token(self, access_token: str) -> bool:
        """Revoke a Google OAuth token."""
        try:
            self._request(
                "POST",
                REVOKE_URL,
                params={"token": access_token},
            )
            return True
        except Exception:
            logger.exception("Failed to revoke Google token")
            return False

    # ------------------------------------------------------------------
    # Account / location helpers
    # ------------------------------------------------------------------

    def _get_account_id(self, access_token: str) -> str:
        """Return the account ID from credentials or by listing accounts."""
        if self.credentials.get("account_id"):
            return self.credentials["account_id"]

        resp = self._request(
            "GET",
            f"{ACCOUNTS_API}/accounts",
            access_token=access_token,
        )
        data = resp.json()
        accounts = data.get("accounts", [])
        if not accounts:
            raise PublishError(
                "No Google Business accounts found",
                platform=self.platform_name,
            )
        # Return the first account's resource name (e.g. "accounts/123")
        return accounts[0]["name"]

    def _get_location_id(self, access_token: str, account_id: str) -> str:
        """Return the location ID from credentials or by listing locations."""
        if self.credentials.get("location_id"):
            return self.credentials["location_id"]

        resp = self._request(
            "GET",
            f"{BUSINESS_INFO_API}/{account_id}/locations",
            access_token=access_token,
        )
        data = resp.json()
        locations = data.get("locations", [])
        if not locations:
            raise PublishError(
                "No locations found for Google Business account",
                platform=self.platform_name,
            )
        return locations[0]["name"]

    @classmethod
    def credentials_for_account(cls, account) -> dict:
        # The connected location ("locations/123"), which the reviews endpoints
        # address under the owning account. Kept apart from ``location_id`` so
        # publishing keeps resolving exactly as before.
        return {"review_location": account.account_platform_id}

    # ------------------------------------------------------------------
    # Profile
    # ------------------------------------------------------------------

    def get_profile(self, access_token: str) -> AccountProfile:
        """Fetch the Google Business account and primary location info."""
        account_id = self._get_account_id(access_token)

        resp = self._request(
            "GET",
            f"{BUSINESS_INFO_API}/{account_id}/locations",
            access_token=access_token,
        )
        data = resp.json()
        locations = data.get("locations", [])

        if locations:
            loc = locations[0]
            name = loc.get("title", loc.get("name", ""))
            address_obj = loc.get("storefrontAddress", {})
            address_lines = address_obj.get("addressLines", [])
            address = ", ".join(address_lines) if address_lines else ""
            phone = loc.get("phoneNumbers", {}).get("primaryPhone", "")
            return AccountProfile(
                platform_id=loc.get("name", account_id),
                name=name,
                handle=None,
                extra={"address": address, "phone": phone},
            )

        return AccountProfile(
            platform_id=account_id,
            name=account_id,
        )

    # ------------------------------------------------------------------
    # Publishing
    # ------------------------------------------------------------------

    @classmethod
    def validate_post_settings(cls, settings: dict) -> str:
        settings = settings or {}
        error = super().validate_post_settings(settings)
        if error:
            return error
        topic = settings.get("topic_type") or "STANDARD"
        if topic not in ("STANDARD", "EVENT", "OFFER"):
            return "Post type has an unsupported value."
        cta = settings.get("cta_type") or ""
        url = str(settings.get("cta_url") or "").strip()
        if cta and topic == "OFFER":
            return "Google ignores the button on offers; remove it or post an update or event."
        if cta == "CALL" and url:
            return "A Call now button dials the business phone number; leave its link empty."
        if cta and cta != "CALL" and not url:
            return "The button needs a link."
        if url and not cta:
            return "Choose a button for the link, or clear the link."
        if url and not _valid_web_url(url):
            return "The button link must start with http:// or https://."
        if topic in ("EVENT", "OFFER"):
            if not str(settings.get("event_title") or "").strip():
                return "An event or offer needs a title."
            start, end = _schedule_part(settings, "start"), _schedule_part(settings, "end")
            if start is None or end is None:
                return "An event or offer needs a start and end date and time."
            if end < start:
                return "The end must not be before the start."
        redeem = str(settings.get("offer_redeem_url") or "").strip()
        if topic == "OFFER" and redeem and not _valid_web_url(redeem):
            return "The redeem link must start with http:// or https://."
        return ""

    @staticmethod
    def _event_body(settings: dict) -> dict:
        start, end = _schedule_part(settings, "start"), _schedule_part(settings, "end")

        def _date(value: datetime) -> dict:
            return {"year": value.year, "month": value.month, "day": value.day}

        def _time(value: datetime) -> dict:
            return {"hours": value.hour, "minutes": value.minute, "seconds": 0, "nanos": 0}

        return {
            "title": str(settings["event_title"]).strip(),
            "schedule": {
                "startDate": _date(start),
                "startTime": _time(start),
                "endDate": _date(end),
                "endTime": _time(end),
            },
        }

    def publish_post(self, access_token: str, content: PublishContent) -> PublishResult:
        """Publish a local post to Google Business Profile."""
        error = self.validate_post_settings(content.extra)
        if error:
            raise PublishError(error, platform=self.platform_name, retryable=False)
        if content.text and len(content.text) > self.max_caption_length:
            raise PublishError(
                f"Post text exceeds {self.max_caption_length} characters (got {len(content.text)})",
                platform=self.platform_name,
                retryable=False,
            )

        account_id = self._get_account_id(access_token)
        location_id = self._get_location_id(access_token, account_id)

        # Determine topic type
        topic_type = content.extra.get("topic_type", "STANDARD")

        body: dict = {
            "languageCode": content.extra.get("language_code", "en"),
            "summary": content.text or "",
            "topicType": topic_type,
        }

        # Attach media
        if content.media_urls:
            body["media"] = [{"mediaFormat": "PHOTO", "sourceUrl": url} for url in content.media_urls]

        # EVENT and OFFER posts carry a title and schedule; an OFFER also its coupon details.
        if topic_type in ("EVENT", "OFFER"):
            if content.extra.get("event_title"):
                body["event"] = self._event_body(content.extra)
            elif content.extra.get("event"):
                body["event"] = content.extra["event"]
        if topic_type == "OFFER":
            offer = {
                key: str(content.extra[setting]).strip()
                for key, setting in (
                    ("couponCode", "offer_coupon_code"),
                    ("redeemOnlineUrl", "offer_redeem_url"),
                    ("termsConditions", "offer_terms"),
                )
                if str(content.extra.get(setting) or "").strip()
            }
            if offer:
                body["offer"] = offer
            elif content.extra.get("offer"):
                body["offer"] = content.extra["offer"]

        # The button; Google ignores it on offers (validate_post_settings refuses the combination).
        cta = content.extra.get("cta_type")
        if cta:
            call_to_action: dict = {"actionType": cta}
            if cta != "CALL":
                call_to_action["url"] = str(content.extra["cta_url"]).strip()
            body["callToAction"] = call_to_action

        resp = self._request(
            "POST",
            f"{POSTS_API}/{location_id}/localPosts",
            access_token=access_token,
            json=body,
        )
        data = resp.json()

        post_name = data.get("name", "")
        return PublishResult(
            platform_post_id=post_name,
            url=data.get("searchUrl"),
            extra=data,
        )

    # ------------------------------------------------------------------
    # Analytics
    # ------------------------------------------------------------------

    def get_post_metrics(self, access_token: str, post_id: str) -> PostMetrics:
        """Fetch metrics for a Google Business local post."""
        resp = self._request(
            "GET",
            f"{POSTS_API}/{post_id}",
            access_token=access_token,
        )
        data = resp.json()
        search_views = 0
        maps_views = 0
        for metric in data.get("searchActionMetrics", []):
            if metric.get("metricType") == "QUERIES_DIRECT":
                search_views += metric.get("value", 0)
        return PostMetrics(
            impressions=search_views + maps_views,
            extra={
                "search_views": search_views,
                "maps_views": maps_views,
                "raw": data,
            },
        )

    # ------------------------------------------------------------------
    # Reviews
    # ------------------------------------------------------------------

    def _review_parent(self, access_token: str) -> str:
        """``accounts/{a}/locations/{l}`` for the connected location."""
        location = (self.credentials.get("review_location") or "").strip("/")
        if not location:
            raise PublishError("No Google Business location is connected", platform=self.platform_name)
        if location.startswith("accounts/"):
            return location
        location = location if location.startswith("locations/") else f"locations/{location}"
        account = self._location_owner(access_token, location)
        account = account if account.startswith("accounts/") else f"accounts/{account}"
        return f"{account}/{location}"

    def _location_owner(self, access_token: str, location: str) -> str:
        """The account (``accounts/1``) the connected location belongs to.

        A login can manage several Business Profile accounts (an agency's own
        plus each client's), so "the first account" is only safe when there is
        exactly one; otherwise each account's locations are listed until the
        connected one turns up.
        """
        if self.credentials.get("account_id"):
            return self.credentials["account_id"]
        accounts = (
            self._request("GET", f"{ACCOUNTS_API}/accounts", access_token=access_token).json().get("accounts", [])
        )
        names = [a["name"] for a in accounts if a.get("name")]
        if len(names) == 1:
            return names[0]
        for account in names[:REVIEWS_OWNER_ACCOUNTS_MAX]:
            data = self._request(
                "GET",
                f"{BUSINESS_INFO_API}/{account}/locations",
                access_token=access_token,
                params={"readMask": "name", "pageSize": 100},
            ).json()
            if any(loc.get("name") == location for loc in data.get("locations", [])):
                return account
        raise PublishError(
            "The connected Google Business location is no longer in any account this login manages",
            platform=self.platform_name,
            retryable=False,
        )

    def _review_message(self, review: dict) -> ProviderInboxMessage:
        reviewer = review.get("reviewer") or {}
        created = _parse_rfc3339(review.get("createTime"))
        updated = _parse_rfc3339(review.get("updateTime")) or created
        reply = review.get("reviewReply") or {}
        extra: dict = {
            "rating": STAR_RATINGS.get(review.get("starRating", ""), 0) or None,
            "sender_avatar_url": reviewer.get("profilePhotoUrl", "") if not reviewer.get("isAnonymous") else "",
            "create_time": review.get("createTime", ""),
            "update_time": review.get("updateTime", ""),
        }
        if reply.get("comment"):
            extra["review_reply"] = {
                "comment": reply["comment"],
                "update_time": reply.get("updateTime", ""),
                "state": reply.get("reviewReplyState", ""),
            }
        return ProviderInboxMessage(
            platform_message_id=review["name"],
            sender_id=review.get("reviewId", review["name"]),
            sender_name=reviewer.get("displayName") or "A Google user",
            text=review.get("comment", "") or "",
            timestamp=updated,
            message_type="review",
            extra=extra,
        )

    def list_reviews(
        self, access_token: str, *, since: datetime | None = None, max_pages: int = 5, page_token: str | None = None
    ) -> ReviewPage:
        parent = self._review_parent(access_token)
        cutoff = since - REVIEWS_LOOKBACK if since else None
        messages: list = []
        average = total = None
        for _ in range(max(1, max_pages)):
            params: dict = {"pageSize": REVIEWS_PAGE_SIZE, "orderBy": "updateTime desc"}
            if page_token:
                params["pageToken"] = page_token
            data = self._request(
                "GET", f"{REVIEWS_API}/{parent}/reviews", access_token=access_token, params=params
            ).json()
            average = data.get("averageRating", average)
            total = data.get("totalReviewCount", total)
            reached_old = False
            for review in data.get("reviews", []):
                if "name" not in review:
                    continue
                msg = self._review_message(review)
                # Newest activity first, so the first old row ends the walk.
                if cutoff is not None and msg.timestamp is not None and msg.timestamp < cutoff:
                    reached_old = True
                    break
                messages.append(msg)
            page_token = data.get("nextPageToken")
            if reached_old or not page_token:
                page_token = None if reached_old else page_token
                break
        return ReviewPage(messages=messages, average_rating=average, total_count=total, next_page_token=page_token)

    def list_review_ids(self, access_token: str) -> set[str] | None:
        parent = self._review_parent(access_token)
        ids: set[str] = set()
        page_token = None
        for _ in range(REVIEW_IDS_MAX_PAGES):
            params: dict = {"pageSize": REVIEWS_PAGE_SIZE, "fields": "reviews(name),nextPageToken"}
            if page_token:
                params["pageToken"] = page_token
            data = self._request(
                "GET", f"{REVIEWS_API}/{parent}/reviews", access_token=access_token, params=params
            ).json()
            ids.update(review["name"] for review in data.get("reviews", []) if "name" in review)
            page_token = data.get("nextPageToken")
            if not page_token:
                return ids
        return None

    def get_messages(self, access_token: str, since: datetime | None = None) -> list[ProviderInboxMessage]:
        pages = 5 if since else REVIEWS_FIRST_IMPORT_PAGES
        return self.list_reviews(access_token, since=since, max_pages=pages).messages

    def reply_to_review(self, access_token: str, review_id: str, text: str) -> ReplyResult:
        if len(text.encode()) > REVIEW_REPLY_MAX_BYTES:
            raise PublishError(
                f"A review reply can be at most {REVIEW_REPLY_MAX_BYTES} bytes",
                platform=self.platform_name,
                retryable=False,
            )
        data = self._request(
            "PUT", f"{REVIEWS_API}/{review_id}/reply", access_token=access_token, json={"comment": text}
        ).json()
        return ReplyResult(platform_message_id=review_id, extra=data)

    def delete_review_reply(self, access_token: str, review_id: str) -> bool:
        self._request("DELETE", f"{REVIEWS_API}/{review_id}/reply", access_token=access_token)
        return True
