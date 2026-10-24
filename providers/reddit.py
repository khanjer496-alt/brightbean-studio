"""Reddit provider: submit text and link posts, and comments, as the connected user.

An account is a Reddit user (OAuth2 with a permanent grant); the subreddit,
flair and kind of post are chosen per post. Reddit's image and video upload is
an undocumented, asynchronous flow (a signed S3 upload and a websocket that
reports the post), so media is not supported: the media rules refuse
attachments instead of publishing them wrongly.

API reference (checked against the current docs):

- OAuth2 (authorize with ``duration=permanent``, token endpoint with HTTP Basic
  client auth, refresh, ``revoke_token``, access tokens last an hour):
  https://github.com/reddit-archive/reddit/wiki/OAuth2
- API rules (the ``User-Agent`` format, rate-limit headers):
  https://github.com/reddit-archive/reddit/wiki/API
- Endpoint reference (``/api/submit``, ``/api/comment``, ``/api/del``,
  ``/r/{subreddit}/api/link_flair_v2``, ``/api/v1/me``) and scopes
  (``identity submit read mysubreddits flair``): https://www.reddit.com/dev/api
  (the reference page itself refused automated fetches, so its field names were
  checked against the reference TypeScript provider and Reddit's OAuth2 wiki).
"""

from __future__ import annotations

import base64
import logging
import re
import time
from urllib.parse import urlencode, urlparse

from django.conf import settings

from .base import SocialProvider
from .exceptions import APIError, OAuthError, PublishError, RateLimitError
from .types import (
    AccountProfile,
    AuthType,
    CommentResult,
    MediaType,
    OAuthTokens,
    PostSetting,
    PostType,
    PublishContent,
    PublishResult,
    RateLimitConfig,
    ThreadCapability,
    ThreadMode,
)

logger = logging.getLogger(__name__)

AUTH_URL = "https://www.reddit.com/api/v1/authorize"
TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
REVOKE_URL = "https://www.reddit.com/api/v1/revoke_token"
API_BASE = "https://oauth.reddit.com"

SCOPES = ["identity", "submit", "read", "mysubreddits", "flair"]

# A title is at most 300 characters; a text post body 40,000; a comment 10,000.
MAX_TITLE_LENGTH = 300
MAX_TEXT_LENGTH = 40000
MAX_COMMENT_LENGTH = 10000

SUBREDDIT_PATTERN = re.compile(r"[A-Za-z0-9_]{2,21}")

# One post can go to several subreddits (the first plus ``extra_subreddits``), submitted one after another. The cap
# and the pause between submissions are ours, not Reddit's: reddit.com rate-limits submissions per account and per
# subreddit ("RATELIMIT") and treats the same post pushed to many communities as spam, so a handful, spaced out,
# is the most we will send in one go.
MAX_SUBREDDITS_PER_POST = 5
SUBREDDIT_SPACING_SECONDS = 15

# Reddit's ``json.errors`` codes that no retry can fix, mapped to what to tell the user.
_PERMANENT_ERRORS = {
    "SUBREDDIT_NOEXIST": "That subreddit doesn't exist. Check the name.",
    "SUBREDDIT_NOTALLOWED": "You aren't allowed to post in that subreddit.",
    "SUBREDDIT_REQUIRED": "Choose a subreddit to post to.",
    "NO_SELFS": "That subreddit doesn't allow text posts. Use a link post.",
    "NO_LINKS": "That subreddit doesn't allow link posts. Use a text post.",
    "ALREADY_SUB": "That link was already submitted to this subreddit.",
    "BAD_URL": "Reddit doesn't accept that link.",
    "TOO_LONG": "The title or text is too long for Reddit.",
    "NO_TEXT": "Reddit needs some text for this post.",
    "BAD_FLAIR_TARGET": "That flair can't be used on this kind of post.",
    "SUBMIT_VALIDATION_FLAIR_REQUIRED": "This subreddit requires a flair. Choose one for the post.",
    "SUBMIT_VALIDATION_REQUIRED_FIELD_MISSING": "This subreddit requires a flair. Choose one for the post.",
    "USER_REQUIRED": "Reddit no longer accepts the connected login. Reconnect the account.",
    "SUBREDDIT_NOTALLOWED_BANNED": "You are banned from that subreddit.",
    "IN_TIMEOUT": "Reddit has put the account in a timeout.",
    "DOMAIN_BANNED": "That subreddit doesn't accept links to this site.",
    "NSFW_WARNING_REQUIRED": "That subreddit needs the post marked NSFW.",
}


def normalize_subreddit(value: str | None) -> str:
    """``r/Python``, ``/r/python`` or ``python`` as the bare name, or "" when it isn't one."""
    name = (value or "").strip()
    name = re.sub(r"^/?r/", "", name, flags=re.IGNORECASE).strip("/")
    return name if SUBREDDIT_PATTERN.fullmatch(name) else ""


def parse_extra_subreddits(raw) -> list[tuple[str, str]]:
    """``(subreddit, flair name)`` pairs from "a | Flair, b": names normalized, order kept, blanks skipped.

    An unreadable name stays as typed (with its flair) so validation can name it.
    """
    pairs: list[tuple[str, str]] = []
    for entry in str(raw or "").split(","):
        name, _bar, flair = entry.partition("|")
        if name.strip():
            pairs.append((normalize_subreddit(name) or name.strip(), flair.strip()))
    return pairs


def default_user_agent() -> str:
    """``<platform>:<app ID>:<version> (by /u/<username>)``, the form Reddit's API rules require."""
    slug = re.sub(r"[^a-z0-9]+", "", settings.BRAND_NAME.lower()) or "app"
    return f"web:com.{slug}.studio:v1.0 (by /u/{slug})"


class RedditProvider(SocialProvider):
    """Reddit provider: text and link posts, flair, NSFW/spoiler marks, comment threads."""

    profile_url_template = "https://www.reddit.com/user/{handle}"

    # No media: see the module docstring. A post with any is refused before it is
    # scheduled (``accepts_media``) rather than published without it.
    accepts_media = False

    writing_style = (
        "Plain and genuine, written for the specific community. A clear descriptive title carries the post; "
        "the body adds context in Markdown. No hashtags, no sales tone."
    )

    # Later parts are comments on the first post.
    thread = ThreadCapability(
        mode=ThreadMode.COMMENTS, max_parts=10, part_max_length=MAX_COMMENT_LENGTH, media_per_part=False
    )

    revokes_grant_on_disconnect = True
    revocation_uses_refresh_token = True

    post_settings = (
        PostSetting(
            key="subreddit",
            label="Subreddit",
            kind="text",
            required=True,
            max_length=24,
            placeholder="e.g. python",
            help="Without the r/. The account must be allowed to post there.",
        ),
        PostSetting(
            key="post_kind",
            label="Post type",
            kind="select",
            choices=(("self", "Text post"), ("link", "Link post")),
            help=(
                "A text post uses the caption as its body; a link post shares the link below. Image and video posts "
                "aren't offered: Reddit doesn't document a media upload for apps."
            ),
        ),
        PostSetting(
            key="link_url",
            label="Link",
            kind="text",
            max_length=2048,
            placeholder="https://example.com/article",
            help="Required for a link post; ignored for a text post.",
        ),
        PostSetting(
            key="flair_id",
            label="Flair",
            kind="select",
            options_from_provider=True,
            depends_on="subreddit",
            help="Some subreddits require one.",
        ),
        PostSetting(
            key="extra_subreddits",
            label="Also post to",
            kind="text",
            max_length=300,
            placeholder="learnpython | Question, flask",
            help=(
                f"Up to {MAX_SUBREDDITS_PER_POST - 1} more subreddits, separated by commas, each posted "
                f"{SUBREDDIT_SPACING_SECONDS} seconds apart. Add | and a flair name to set that subreddit's flair. "
                "Posting the same thing to many communities can be treated as spam; keep it relevant to each."
            ),
        ),
        PostSetting(key="nsfw", label="Mark as NSFW", kind="checkbox"),
        PostSetting(key="spoiler", label="Mark as spoiler", kind="checkbox"),
    )

    # ------------------------------------------------------------------
    # Metadata
    # ------------------------------------------------------------------

    @property
    def platform_name(self) -> str:
        return "Reddit"

    @property
    def auth_type(self) -> AuthType:
        return AuthType.OAUTH2

    @property
    def max_caption_length(self) -> int:
        return MAX_TEXT_LENGTH

    @property
    def supported_post_types(self) -> list[PostType]:
        return [PostType.TEXT, PostType.LINK]

    @property
    def supported_media_types(self) -> list[MediaType]:
        return []

    @property
    def required_scopes(self) -> list[str]:
        return list(SCOPES)

    @property
    def rate_limits(self) -> RateLimitConfig:
        # OAuth clients get a request budget per minute; new accounts are also
        # limited by Reddit's own per-subreddit posting cool-down (RATELIMIT).
        return RateLimitConfig(requests_per_hour=3600, requests_per_day=50000, publish_per_day=50)

    # ------------------------------------------------------------------
    # Requests
    # ------------------------------------------------------------------

    @property
    def user_agent(self) -> str:
        return self.credentials.get("user_agent") or default_user_agent()

    def _client_auth(self) -> dict:
        raw = f"{self.credentials['client_id']}:{self.credentials['client_secret']}".encode()
        return {"Authorization": f"Basic {base64.b64encode(raw).decode()}", "User-Agent": self.user_agent}

    def _api_request(self, method: str, path: str, access_token: str, **kwargs):
        headers = {"User-Agent": self.user_agent, **(kwargs.pop("headers", None) or {})}
        return self._request(method, f"{API_BASE}{path}", access_token=access_token, headers=headers, **kwargs)

    def _error_for_response(self, response):
        """Map an HTTP failure: 429 (with Reddit's reset time), other 4xx permanent, 5xx retryable."""
        body = self._safe_json(response)
        body = body if isinstance(body, dict) else {}
        status = response.status_code
        if status == 429:
            wait = response.headers.get("Retry-After") or response.headers.get("X-Ratelimit-Reset")
            try:
                retry_after = int(float(wait)) if wait is not None else None
            except ValueError:
                retry_after = None
            return RateLimitError(
                f"Rate limit exceeded for {self.platform_name}",
                retry_after=retry_after,
                platform=self.platform_name,
                raw_response=body,
            )
        # 401 keeps its retries: an expired access token is refreshed before the next attempt, and the
        # publish engine stops retrying a refusal of a token it knows is live.
        return APIError(
            f"{self.platform_name} API error {status}: {str(body.get('message') or body.get('error') or '')[:200]}",
            status_code=status,
            platform=self.platform_name,
            raw_response=body,
            retryable=status == 401 or not (400 <= status < 500),
        )

    def _raise_for_json_errors(self, data: dict, *, subreddit: str = "") -> None:
        """Reddit reports most rejections as HTTP 200 with ``json.errors``; turn them into exceptions."""
        errors = (data.get("json") or {}).get("errors") or []
        if not errors:
            return
        code, message = str(errors[0][0]), str(errors[0][1] if len(errors[0]) > 1 else "")
        if code == "RATELIMIT":
            seconds = (data.get("json") or {}).get("ratelimit")
            raise RateLimitError(
                f"Reddit asked us to slow down: {message[:200]}",
                retry_after=int(float(seconds)) if seconds else self._wait_from_message(message),
                platform=self.platform_name,
                raw_response=data,
            )
        reason = _PERMANENT_ERRORS.get(code)
        if reason is None:
            where = f" to r/{subreddit}" if subreddit else ""
            reason = f"Reddit rejected the post{where}: {message[:200]}".strip()
        raise PublishError(reason, platform=self.platform_name, raw_response=data, retryable=False)

    @staticmethod
    def _wait_from_message(message: str) -> int | None:
        """Seconds from "try again in 9 minutes" / "in 45 seconds", the only wait some responses give."""
        match = re.search(r"(\d+)\s*(second|minute)", message)
        if not match:
            return None
        return int(match.group(1)) * (60 if match.group(2) == "minute" else 1)

    # ------------------------------------------------------------------
    # OAuth
    # ------------------------------------------------------------------

    def get_auth_url(self, redirect_uri: str, state: str, code_verifier: str | None = None) -> str:
        params = {
            "client_id": self.credentials["client_id"],
            "response_type": "code",
            "state": state,
            "redirect_uri": redirect_uri,
            # permanent is what returns a refresh token; temporary grants end after an hour.
            "duration": "permanent",
            "scope": " ".join(self.required_scopes),
        }
        return f"{AUTH_URL}?{urlencode(params)}"

    def _token_request(self, data: dict) -> dict:
        body = self._request("POST", TOKEN_URL, headers=self._client_auth(), data=data).json()
        if "access_token" not in body:
            raise OAuthError(
                f"Reddit token request failed ({body.get('error', 'unknown')})",
                platform=self.platform_name,
                raw_response=body,
            )
        return body

    def exchange_code(self, code: str, redirect_uri: str, code_verifier: str | None = None) -> OAuthTokens:
        data = self._token_request({"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri})
        if not data.get("refresh_token"):
            raise OAuthError(
                "Reddit didn't return a refresh token, so the connection would end within an hour. Try again.",
                platform=self.platform_name,
                raw_response=data,
            )
        granted = set((data.get("scope") or "").split())
        missing = [scope for scope in SCOPES if scope not in granted]
        if missing:
            raise OAuthError(
                f"Reddit didn't grant: {', '.join(missing)}. Reconnect and approve every permission.",
                platform=self.platform_name,
                raw_response=data,
            )
        return OAuthTokens(
            access_token=data["access_token"],
            refresh_token=data["refresh_token"],
            expires_in=data.get("expires_in"),
            scope=data.get("scope"),
            raw_response=data,
        )

    def refresh_token(self, refresh_token: str) -> OAuthTokens:
        """Trade the permanent refresh token for a new hour-long access token."""
        data = self._token_request({"grant_type": "refresh_token", "refresh_token": refresh_token})
        return OAuthTokens(
            access_token=data["access_token"],
            # Reddit keeps the same refresh token and doesn't send it back.
            refresh_token=data.get("refresh_token") or refresh_token,
            expires_in=data.get("expires_in"),
            scope=data.get("scope"),
            raw_response=data,
        )

    def revoke_token(self, access_token: str) -> bool:
        """Revoke a token (the refresh token, from disconnect, also ends its access tokens)."""
        try:
            self._request("POST", REVOKE_URL, headers=self._client_auth(), data={"token": access_token})
            return True
        except Exception:
            logger.warning("Failed to revoke Reddit token", exc_info=True)
            return False

    # ------------------------------------------------------------------
    # Profile
    # ------------------------------------------------------------------

    def get_profile(self, access_token: str) -> AccountProfile:
        me = self._api_request("GET", "/api/v1/me", access_token).json()
        icon = (me.get("icon_img") or "").split("?")[0]
        return AccountProfile(
            platform_id=str(me["id"]),
            name=f"u/{me['name']}",
            handle=me["name"],
            avatar_url=icon or None,
            follower_count=int(me.get("total_karma") or 0),
        )

    # ------------------------------------------------------------------
    # Post settings
    # ------------------------------------------------------------------

    @classmethod
    def validate_post_settings(cls, settings: dict) -> str:
        settings = settings or {}
        error = super().validate_post_settings(settings)
        if error:
            return error
        if not normalize_subreddit(settings.get("subreddit")):
            return "Subreddit must be a name like python (letters, numbers and underscores, 2-21 characters)."
        extras = parse_extra_subreddits(settings.get("extra_subreddits"))
        names = [normalize_subreddit(settings.get("subreddit")).lower(), *(name.lower() for name, _flair in extras)]
        bad = next((name for name, _flair in extras if not normalize_subreddit(name)), None)
        if bad:
            return f"{bad!r} isn't a subreddit name (letters, numbers and underscores, 2-21 characters)."
        if len(names) > MAX_SUBREDDITS_PER_POST:
            return f"Post to at most {MAX_SUBREDDITS_PER_POST} subreddits at a time."
        if len(set(names)) != len(names):
            return "Each subreddit can be listed only once."
        if settings.get("post_kind") == "link":
            url = str(settings.get("link_url") or "").strip()
            parsed = urlparse(url)
            if parsed.scheme not in ("http", "https") or not parsed.netloc:
                return "A link post needs a valid link starting with http:// or https://."
        return ""

    def get_setting_options(self, access_token: str, key: str, depends_value: str = "") -> list[dict]:
        """Flairs a post to the subreddit ``depends_value`` can use."""
        if key != "flair_id":
            raise NotImplementedError(f"Reddit has no live options for {key}")
        subreddit = normalize_subreddit(depends_value)
        if not subreddit:
            return []
        try:
            flairs = self._api_request("GET", f"/r/{subreddit}/api/link_flair_v2", access_token).json()
        except APIError as exc:
            # A subreddit without flair, or one that hides it, answers 4xx: no options, not an error.
            if exc.status_code and 400 <= exc.status_code < 500:
                return []
            raise
        return [{"id": flair["id"], "name": flair.get("text") or flair["id"]} for flair in flairs if flair.get("id")]

    # ------------------------------------------------------------------
    # Publishing
    # ------------------------------------------------------------------

    def publish_post(self, access_token: str, content: PublishContent) -> PublishResult:
        """Submit a text or link post to the subreddit named in the post's settings."""
        extra = content.extra
        subreddit = normalize_subreddit(extra.get("subreddit"))
        title = (content.title or "").strip()
        if content.media_files or content.media_urls:
            raise PublishError(
                "Reddit posts here are text or links. Remove the media from this post.",
                platform=self.platform_name,
                retryable=False,
            )
        if not subreddit:
            raise PublishError(
                "Reddit needs a subreddit. Set one for this post in the composer.",
                platform=self.platform_name,
                retryable=False,
            )
        if not title:
            raise PublishError(
                "Reddit requires a title. Set the post title before publishing.",
                platform=self.platform_name,
                retryable=False,
            )
        if len(title) > MAX_TITLE_LENGTH:
            raise PublishError(
                f"Reddit titles are limited to {MAX_TITLE_LENGTH} characters.",
                platform=self.platform_name,
                retryable=False,
            )
        error = self.validate_post_settings({**extra, "link_url": content.link_url or extra.get("link_url")})
        if error:
            raise PublishError(error, platform=self.platform_name, retryable=False)

        targets = [(subreddit, str(extra.get("flair_id") or ""))]
        for name, flair_name in parse_extra_subreddits(extra.get("extra_subreddits")):
            targets.append((name, self._flair_id_by_name(access_token, name, flair_name) if flair_name else ""))
        link = content.link_url or extra.get("link_url")
        if extra.get("post_kind") != "link" and len(content.text or "") > MAX_TEXT_LENGTH:
            raise PublishError(
                f"Reddit text posts are limited to {MAX_TEXT_LENGTH} characters.",
                platform=self.platform_name,
                retryable=False,
            )

        done: list[dict] = []
        for index, (name, flair_id) in enumerate(targets):
            if index:
                self._pause_between_submissions(content)
            try:
                done.append(self._submit(access_token, content, name, flair_id, link))
            except Exception as exc:
                if not done:
                    raise
                # Earlier subreddits already have the post; a retry would repeat it there.
                raise PublishError(
                    f"Posted to {', '.join('r/' + d['subreddit'] for d in done)}, but not to r/{name}: {exc}",
                    platform=self.platform_name,
                    retryable=False,
                ) from exc
        first = done[0]
        extra_out = {"subreddit": first["subreddit"], "fullname": first["fullname"]}
        if len(done) > 1:
            extra_out["also_posted"] = [
                {"subreddit": d["subreddit"], "fullname": d["fullname"], "url": d["url"]} for d in done[1:]
            ]
        return PublishResult(platform_post_id=first["fullname"], url=first["url"], extra=extra_out)

    def _pause_between_submissions(self, content: PublishContent) -> None:
        """Wait out the spacing between two subreddits, giving up early when the worker is stopping."""
        deadline = time.monotonic() + SUBREDDIT_SPACING_SECONDS
        while time.monotonic() < deadline:
            if content.should_stop is not None and content.should_stop():
                raise PublishError(
                    "Publishing was interrupted between subreddits.", platform=self.platform_name, retryable=False
                )
            time.sleep(min(1.0, max(0.0, deadline - time.monotonic())))

    def _flair_id_by_name(self, access_token: str, subreddit: str, flair_name: str) -> str:
        """The id of the flair called ``flair_name`` in ``subreddit``, or a permanent error naming the choices."""
        options = self.get_setting_options(access_token, "flair_id", subreddit)
        for option in options:
            if option["name"].strip().lower() == flair_name.strip().lower():
                return option["id"]
        known = ", ".join(option["name"] for option in options[:8]) or "none"
        raise PublishError(
            f"r/{subreddit} has no flair called {flair_name!r} (available: {known}).",
            platform=self.platform_name,
            retryable=False,
        )

    def _submit(self, access_token: str, content: PublishContent, subreddit: str, flair_id: str, link) -> dict:
        """One ``/api/submit`` to ``subreddit``: ``{"subreddit", "fullname", "url"}``."""
        extra = content.extra
        title = (content.title or "").strip()
        is_link = extra.get("post_kind") == "link"
        form = {
            "api_type": "json",
            "kind": "link" if is_link else "self",
            "sr": subreddit,
            "title": title,
            "nsfw": "true" if extra.get("nsfw") else "false",
            "spoiler": "true" if extra.get("spoiler") else "false",
            "sendreplies": "true",
        }
        if is_link:
            form["url"] = link
        else:
            form["text"] = content.text or ""
        if flair_id:
            form["flair_id"] = flair_id

        data = self._api_request("POST", "/api/submit", access_token, data=form).json()
        self._raise_for_json_errors(data, subreddit=subreddit)
        result = (data.get("json") or {}).get("data") or {}
        fullname = result.get("name") or (f"t3_{result['id']}" if result.get("id") else "")
        if not fullname:
            # Accepted without telling us what it created: do not submit it again.
            raise APIError(
                "Reddit accepted the post but returned no id",
                platform=self.platform_name,
                raw_response=data,
                ambiguous_write=True,
            )
        return {"subreddit": subreddit, "fullname": fullname, "url": result.get("url") or None}

    def publish_comment(self, access_token: str, post_id: str, text: str) -> CommentResult:
        """Comment on a post the account made (also how later thread parts are posted)."""
        if len(text) > MAX_COMMENT_LENGTH:
            raise PublishError(
                f"Reddit comments are limited to {MAX_COMMENT_LENGTH} characters.",
                platform=self.platform_name,
                retryable=False,
            )
        thing_id = post_id if post_id.startswith("t3_") else f"t3_{post_id}"
        data = self._api_request(
            "POST", "/api/comment", access_token, data={"api_type": "json", "thing_id": thing_id, "text": text}
        ).json()
        self._raise_for_json_errors(data)
        things = ((data.get("json") or {}).get("data") or {}).get("things") or []
        if not things:
            raise APIError(
                "Reddit accepted the comment but returned no id",
                platform=self.platform_name,
                raw_response=data,
                ambiguous_write=True,
            )
        comment = things[0].get("data") or {}
        permalink = comment.get("permalink")
        return CommentResult(
            platform_comment_id=comment.get("name") or f"t1_{comment.get('id', '')}",
            extra={"url": f"https://www.reddit.com{permalink}" if permalink else ""},
        )

    def delete_post(self, access_token: str, post_id: str) -> bool:
        """Delete one of the account's posts or comments (``/api/del``)."""
        self._api_request("POST", "/api/del", access_token, data={"id": post_id})
        return True
