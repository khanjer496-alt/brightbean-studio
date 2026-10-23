"""Telegram Bot API provider: post to a channel or group the platform bot administers.

There is no OAuth. One platform-wide bot (``PLATFORM_TELEGRAM_BOT_TOKEN``) posts
for every connected chat; the user connects a chat by adding that bot as an
administrator and naming the chat (``@channel`` or a numeric id). Connecting
verifies the bot can really post there. The account's stored "token" is the chat
id, which is not a secret; the bot token only ever comes from the environment.

API reference (checked against the current docs):

- Making requests, response shape, ``ResponseParameters`` (``retry_after``,
  ``migrate_to_chat_id``): https://core.telegram.org/bots/api#making-requests
- ``sendMessage`` (text 1-4096 characters after entity parsing, ``parse_mode``,
  ``reply_parameters``): https://core.telegram.org/bots/api#sendmessage
- ``sendPhoto`` (10 MB, caption 0-1024): https://core.telegram.org/bots/api#sendphoto
- ``sendVideo`` (caption 0-1024): https://core.telegram.org/bots/api#sendvideo
- ``sendMediaGroup`` (2-10 items): https://core.telegram.org/bots/api#sendmediagroup
- ``getChat`` / ``getChatMember``: https://core.telegram.org/bots/api#getchat
  and https://core.telegram.org/bots/api#getchatmember
- HTML formatting and escaping: https://core.telegram.org/bots/api#html-style
- Flood limits (about 1 message a second per chat, 20 a minute per group):
  https://core.telegram.org/bots/faq#my-bot-is-hitting-limits-how-do-i-avoid-this
"""

from __future__ import annotations

import html
import json
import logging
import re
from contextlib import ExitStack
from dataclasses import replace

from .base import SocialProvider
from .exceptions import APIError, ProviderError, PublishError, RateLimitError
from .types import (
    AccountProfile,
    AuthType,
    CommentResult,
    MediaRules,
    MediaType,
    PostSetting,
    PostType,
    PublishContent,
    PublishResult,
    RateLimitConfig,
    ThreadCapability,
    ThreadMode,
)

logger = logging.getLogger(__name__)

API_BASE = "https://api.telegram.org"

# https://core.telegram.org/bots/api#sendmessage
MAX_TEXT_LENGTH = 4096
# https://core.telegram.org/bots/api#sendphoto (also sendVideo)
MAX_CAPTION_LENGTH = 1024
# https://core.telegram.org/bots/api#sendmediagroup
MAX_ALBUM_ITEMS = 10

_TELEGRAM_DOCS = "https://core.telegram.org/bots/api#sending-files"
# Photos up to 10 MB, other files up to 50 MB through the hosted Bot API.
_TELEGRAM_MEDIA = MediaRules(
    source=_TELEGRAM_DOCS,
    max_items=MAX_ALBUM_ITEMS,
    media_types=frozenset({"image", "video"}),
    max_image_bytes=10 * 1024 * 1024,
    max_video_bytes=50 * 1024 * 1024,
    # sendPhoto refuses a width-to-height ratio above 20, which only matters
    # for extreme panoramas; warn rather than block.
    min_aspect_ratio=1 / 20,
    max_aspect_ratio=20,
    advisory=frozenset({"aspect_ratio"}),
)

# Tags Telegram's HTML parse mode accepts that a user may type in a caption;
# everything else is escaped so a stray "<" can't fail the whole post.
_ALLOWED_TAG = re.compile(
    r"</?(?:b|strong|i|em|u|ins|s|strike|del|code|pre|tg-spoiler|blockquote)>"
    r"|<a href=\"https?://[^\"<>]+\">|</a>",
    re.IGNORECASE,
)
_TAG_NAME = re.compile(r"</?([a-z-]+)", re.IGNORECASE)

# Telegram's own descriptions that no retry can fix, mapped to what to tell the
# user. Matched on lowercase substrings of ``description``.
_PERMANENT_REASONS = (
    ("chat not found", "Telegram can't find this chat any more. Reconnect the channel."),
    ("bot was kicked", "The bot was removed from this chat. Add it back as an administrator, then reconnect."),
    ("bot is not a member", "The bot is no longer in this chat. Add it back as an administrator, then reconnect."),
    ("not enough rights", "The bot can't post in this chat. Make it an administrator who can post messages."),
    ("have no rights to send", "The bot can't post in this chat. Make it an administrator who can post messages."),
    ("need administrator rights", "The bot can't post in this chat. Make it an administrator who can post messages."),
    ("message is too long", "This text is too long for Telegram (4096 characters)."),
    ("caption is too long", "This caption is too long for Telegram (1024 characters with media)."),
    ("can't parse entities", "Telegram couldn't read the formatting in this text."),
    ("file is too big", "A file is too large for Telegram (photos 10 MB, videos 50 MB)."),
    ("reply message not found", "The message this post should reply to no longer exists."),
)


class _RedactBotToken(logging.Filter):
    """Keep the bot token out of httpx's request log lines.

    Telegram carries the token in the URL path (``/bot<token>/method``), and
    httpx logs every request URL at INFO.
    """

    _PATTERN = re.compile(r"/bot[^/\s]+/")

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(
                self._PATTERN.sub("/bot<redacted>/", arg) if isinstance(arg, str) else arg for arg in record.args
            )
        if isinstance(record.msg, str):
            record.msg = self._PATTERN.sub("/bot<redacted>/", record.msg)
        return True


logging.getLogger("httpx").addFilter(_RedactBotToken())


def to_telegram_html(text: str) -> str:
    """Make ``text`` safe for ``parse_mode=HTML``, keeping the tags Telegram supports.

    Literal ``<``, ``>`` and ``&`` must be escaped (see the HTML-style docs). Tags
    from the supported set pass through when they are balanced; if they aren't,
    the whole text is escaped so Telegram never answers "can't parse entities".
    """
    parts = []
    stack: list[str] = []
    balanced = True
    position = 0
    for match in _ALLOWED_TAG.finditer(text):
        parts.append(html.escape(text[position : match.start()], quote=False))
        tag = match.group(0)
        name = _TAG_NAME.match(tag).group(1).lower()
        if name == "a":
            # A bare "&" in a link's query string is a parse error too.
            tag = re.sub(r"&(?!(?:amp|lt|gt|quot|#\d+);)", "&amp;", tag)
        if tag.startswith("</"):
            if not stack or stack.pop() != name:
                balanced = False
        else:
            stack.append(name)
        parts.append(tag)
        position = match.end()
    parts.append(html.escape(text[position:], quote=False))
    if balanced and not stack:
        return "".join(parts)
    return html.escape(text, quote=False)


class TelegramProvider(SocialProvider):
    """Telegram Bot API provider for channels and groups (no OAuth)."""

    media_rules = {PostType.IMAGE: _TELEGRAM_MEDIA, PostType.VIDEO: _TELEGRAM_MEDIA}

    writing_style = (
        "Conversational and direct, written for a channel's subscribers. Short paragraphs, a clear first line, "
        "light emoji. Basic formatting (bold, italic) is available; hashtags are rare."
    )

    # Each part is a message replying to the one before it (``reply_parameters``).
    thread = ThreadCapability(mode=ThreadMode.REPLY_CHAIN, max_parts=25, media_per_part=True)

    connect_url_name = "connect_telegram"
    supports_connection_link = False
    # Disconnecting can't remove the bot from the chat; the owner does that.
    revokes_grant_on_disconnect = False

    post_settings = (
        PostSetting(
            key="silent",
            label="Send silently",
            kind="checkbox",
            help="Subscribers get the message without a notification sound.",
        ),
    )

    def __init__(self, credentials: dict | None = None):
        super().__init__(credentials)
        self._bot_id: int | None = None

    # ------------------------------------------------------------------
    # Metadata
    # ------------------------------------------------------------------

    @property
    def platform_name(self) -> str:
        return "Telegram"

    @property
    def auth_type(self) -> AuthType:
        return AuthType.BOT_TOKEN

    @property
    def max_caption_length(self) -> int:
        return MAX_TEXT_LENGTH

    @property
    def supported_post_types(self) -> list[PostType]:
        return [PostType.TEXT, PostType.IMAGE, PostType.VIDEO]

    @property
    def supported_media_types(self) -> list[MediaType]:
        return [MediaType.JPEG, MediaType.PNG, MediaType.WEBP, MediaType.MP4]

    @property
    def required_scopes(self) -> list[str]:
        return []

    @property
    def rate_limits(self) -> RateLimitConfig:
        # ~1 message a second per chat, 20 a minute per group.
        return RateLimitConfig(requests_per_hour=1200, requests_per_day=20000, publish_per_day=1000)

    @classmethod
    def credentials_for_account(cls, account) -> dict:
        # The permalink of a public chat is built from its username.
        return {"chat_username": (account.account_handle or "").lstrip("@")}

    # ------------------------------------------------------------------
    # Requests
    # ------------------------------------------------------------------

    @property
    def bot_token(self) -> str:
        token = self.credentials.get("bot_token", "")
        if not token:
            raise PublishError(
                "Telegram isn't configured: the platform bot token is missing.",
                platform=self.platform_name,
                retryable=False,
            )
        return token

    def _error_for_response(self, response):
        """Telegram answers errors with ``{"ok": false, "error_code", "description", "parameters"}``."""
        body = self._safe_json(response)
        description = str(body.get("description", ""))[:300] if isinstance(body, dict) else ""
        parameters = (body.get("parameters") or {}) if isinstance(body, dict) else {}
        status = response.status_code
        if status == 429:
            retry_after = parameters.get("retry_after")
            return RateLimitError(
                f"Rate limit exceeded for {self.platform_name}: {description}",
                retry_after=int(retry_after) if retry_after else None,
                platform=self.platform_name,
                raw_response=body if isinstance(body, dict) else {},
            )
        if parameters.get("migrate_to_chat_id"):
            # A group that became a supergroup gets a new id; the stored one
            # never works again.
            return APIError(
                f"{self.platform_name} chat was migrated to a supergroup: {description}",
                status_code=status,
                platform=self.platform_name,
                raw_response=body if isinstance(body, dict) else {},
                retryable=False,
            )
        return APIError(
            f"{self.platform_name} API error {status}: {description}",
            status_code=status,
            platform=self.platform_name,
            raw_response=body if isinstance(body, dict) else {},
            # A 4xx is the request itself being wrong or refused; only a
            # server-side failure is worth another attempt.
            retryable=not (400 <= status < 500),
        )

    def _api(self, method: str, *, json_body: dict | None = None, data: dict | None = None, files=None):
        """Call a Bot API method and return its ``result``."""
        response = self._request(
            "POST",
            f"{API_BASE}/bot{self.bot_token}/{method}",
            json=json_body,
            data=data,
            files=files,
            timeout=120.0 if files else 30.0,
        )
        body = self._safe_json(response)
        if not isinstance(body, dict) or not body.get("ok"):
            raise APIError(
                f"{self.platform_name} did not accept {method}",
                status_code=response.status_code,
                platform=self.platform_name,
                raw_response=body if isinstance(body, dict) else {},
                ambiguous_write=method.startswith("send"),
            )
        return body.get("result")

    def _publish_call(self, method: str, **kwargs):
        """``_api`` for a write, turning refusals we can explain into a sentence for the user."""
        try:
            return self._api(method, **kwargs)
        except APIError as exc:
            description = str(exc.raw_response.get("description", "")).lower()
            for needle, reason in _PERMANENT_REASONS:
                if needle in description:
                    raise PublishError(
                        reason, platform=self.platform_name, raw_response=exc.raw_response, retryable=False
                    ) from exc
            raise

    # ------------------------------------------------------------------
    # Connecting
    # ------------------------------------------------------------------

    def get_auth_url(self, redirect_uri: str, state: str, code_verifier: str | None = None) -> str:
        raise NotImplementedError("Telegram has no OAuth. Use connect_telegram instead.")

    def exchange_code(self, code: str, redirect_uri: str, code_verifier: str | None = None):
        raise NotImplementedError("Telegram has no OAuth. Use connect_telegram instead.")

    @staticmethod
    def normalize_chat_reference(raw: str) -> str:
        """``@channel``, ``t.me/channel``, a channel URL or a numeric id, as ``getChat`` takes it.

        Returns "" when ``raw`` names no chat. Invite links (``t.me/+...``,
        ``joinchat``) are refused: a bot can't resolve them.
        """
        value = (raw or "").strip()
        match = re.match(r"^(?:https?://)?(?:t\.me|telegram\.me)/([A-Za-z0-9_]{4,})/?$", value, re.IGNORECASE)
        if match:
            value = match.group(1)
        if re.fullmatch(r"-?\d{5,}", value):
            return value
        value = value.lstrip("@")
        return f"@{value}" if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{3,}", value) else ""

    def get_bot_username(self) -> str:
        """The bot's ``@username``, for telling users which bot to add."""
        return self._bot().get("username", "")

    def _bot(self) -> dict:
        me = self._api("getMe")
        self._bot_id = me["id"]
        return me

    def get_profile(self, access_token: str) -> AccountProfile:
        """Verify the bot can post to the chat ``access_token`` names, and describe the chat.

        ``extra["description"]`` carries the chat's description, which is where the
        connecting user proves they administer the chat (see ``connect_telegram``).

        ``access_token`` is a chat id or ``@username`` here; the bot token comes from the environment.
        """
        chat = self._api("getChat", json_body={"chat_id": access_token})
        bot_id = self._bot_id or self._bot()["id"]
        member = self._api("getChatMember", json_body={"chat_id": chat["id"], "user_id": bot_id})
        reason = self._cannot_post_reason(chat, member)
        if reason:
            raise PublishError(reason, platform=self.platform_name, retryable=False)

        followers = 0
        try:
            followers = int(self._api("getChatMemberCount", json_body={"chat_id": chat["id"]}))
        except Exception:
            logger.info("Telegram member count unavailable for chat %s", chat["id"])
        username = chat.get("username")
        return AccountProfile(
            platform_id=str(chat["id"]),
            name=chat.get("title") or chat.get("first_name") or str(chat["id"]),
            handle=f"@{username}" if username else None,
            follower_count=followers,
            # The connect view looks for the user's one-time code here: editing a
            # chat's description takes an administrator of that chat.
            extra={"type": chat.get("type", ""), "description": chat.get("description") or ""},
        )

    @staticmethod
    def _cannot_post_reason(chat: dict, member: dict) -> str:
        """Why the bot can't post to ``chat`` given its ``getChatMember`` record, or ""."""
        chat_type = chat.get("type")
        status = member.get("status")
        if chat_type not in ("channel", "group", "supergroup"):
            return "Telegram bots post to channels and groups. Enter a channel or group, not a person."
        if status == "creator":
            return ""
        if status == "administrator":
            if chat_type == "channel" and member.get("can_post_messages") is False:
                return "The bot is an administrator but isn't allowed to post messages. Turn on 'Post Messages'."
            return ""
        if status == "member" and chat_type != "channel":
            return ""
        if status == "restricted" and chat_type != "channel" and member.get("can_send_messages"):
            return ""
        return "The bot can't post there yet. Add it to the chat as an administrator, then connect again."

    # ------------------------------------------------------------------
    # Publishing
    # ------------------------------------------------------------------

    def publish_post(self, access_token: str, content: PublishContent) -> PublishResult:
        """Send text, or text with photos and videos, to the chat."""
        chat_id = access_token
        text = to_telegram_html(content.text or "")
        media = self._media_items(content)

        if not media and not text.strip():
            raise PublishError(
                "Telegram needs some text or a photo or video to post.", platform=self.platform_name, retryable=False
            )
        if len(media) > MAX_ALBUM_ITEMS:
            raise PublishError(
                f"Telegram takes at most {MAX_ALBUM_ITEMS} items in one post.",
                platform=self.platform_name,
                retryable=False,
            )

        common = self._common_params(content)
        message_ids: list[int] = []
        if not media:
            sent = self._publish_call(
                "sendMessage", json_body={"chat_id": chat_id, "text": text, "parse_mode": "HTML", **common}
            )
            message_ids.append(sent["message_id"])
        else:
            # A caption is capped at 1024 characters; a longer text is sent as
            # its own message right after the media rather than being cut.
            caption = text if len(content.text or "") <= MAX_CAPTION_LENGTH else ""
            message_ids.extend(self._send_media(chat_id, media, caption, common))
            if text and not caption:
                try:
                    follow_up = self._publish_call(
                        "sendMessage",
                        json_body={
                            "chat_id": chat_id,
                            "text": text,
                            "parse_mode": "HTML",
                            **self._reply_params(message_ids[0], common),
                        },
                    )
                except ProviderError as exc:
                    # The media is already live: a retry would post it twice.
                    raise APIError(
                        f"{self.platform_name} posted the media but not the text that follows it: {exc}",
                        platform=self.platform_name,
                        raw_response=getattr(exc, "raw_response", {}) or {},
                        ambiguous_write=True,
                    ) from exc
                message_ids.append(follow_up["message_id"])

        first = message_ids[0]
        return PublishResult(
            platform_post_id=str(first),
            url=self._permalink(chat_id, first),
            extra={"chat_id": str(chat_id), "message_ids": message_ids},
        )

    def _media_items(self, content: PublishContent) -> list[tuple[str, str]]:
        """``(kind, local path)`` for each attachment; kind is ``photo`` or ``video``."""
        items = []
        for index, path in enumerate(content.media_files or []):
            media_type = content.media_types[index] if index < len(content.media_types) else ""
            if media_type == "gif":
                raise PublishError(
                    "Telegram can't send a GIF here. Convert it to a video or an image.",
                    platform=self.platform_name,
                    retryable=False,
                )
            items.append(("video" if content.is_video(index) else "photo", path))
        return items

    @staticmethod
    def _common_params(content: PublishContent) -> dict:
        params: dict = {}
        if content.extra.get("silent"):
            params["disable_notification"] = True
        reply_to = content.extra.get("reply_to_message_id")
        if reply_to:
            params["reply_parameters"] = {"message_id": int(reply_to), "allow_sending_without_reply": False}
        return params

    @staticmethod
    def _reply_params(message_id: int, common: dict) -> dict:
        params = {k: v for k, v in common.items() if k != "reply_parameters"}
        params["reply_parameters"] = {"message_id": message_id}
        return params

    def _send_media(self, chat_id: str, media: list[tuple[str, str]], caption: str, common: dict) -> list[int]:
        """Upload the files and send them as one message or one album; returns the message ids."""
        with ExitStack() as stack:
            if len(media) == 1:
                kind, path = media[0]
                params = {"chat_id": chat_id, **self._form_fields(common)}
                if caption:
                    params.update(caption=caption, parse_mode="HTML")
                if kind == "video":
                    params["supports_streaming"] = "true"
                file = stack.enter_context(open(path, "rb"))
                sent = self._publish_call(
                    "sendVideo" if kind == "video" else "sendPhoto",
                    data=params,
                    files={kind: (path.rsplit("/", 1)[-1], file)},
                )
                return [sent["message_id"]]

            files = {}
            group = []
            for index, (kind, path) in enumerate(media):
                files[f"file{index}"] = (path.rsplit("/", 1)[-1], stack.enter_context(open(path, "rb")))
                item = {"type": kind, "media": f"attach://file{index}"}
                if index == 0 and caption:
                    item.update(caption=caption, parse_mode="HTML")
                if kind == "video":
                    item["supports_streaming"] = True
                group.append(item)
            sent = self._publish_call(
                "sendMediaGroup",
                data={"chat_id": chat_id, "media": json.dumps(group), **self._form_fields(common)},
                files=files,
            )
            return [message["message_id"] for message in sent]

    @staticmethod
    def _form_fields(common: dict) -> dict:
        """``common`` as multipart form fields, where nested objects travel as JSON strings."""
        return {k: json.dumps(v) if isinstance(v, dict) else v for k, v in common.items()}

    def _permalink(self, chat_id: str, message_id: int) -> str | None:
        """A link to the message: ``t.me/<username>/<id>`` for a public chat, ``t.me/c/...`` for a supergroup."""
        username = self.credentials.get("chat_username")
        if username:
            return f"https://t.me/{username}/{message_id}"
        match = re.fullmatch(r"-100(\d+)", str(chat_id))
        return f"https://t.me/c/{match.group(1)}/{message_id}" if match else None

    def publish_thread_part(
        self,
        access_token: str,
        content: PublishContent,
        *,
        root: PublishResult,
        parent: PublishResult,
    ) -> PublishResult:
        """Publish a thread part as a message replying to the part before it."""
        return self.publish_post(
            access_token,
            replace(content, extra={**content.extra, "reply_to_message_id": parent.platform_post_id}),
        )

    def publish_comment(self, access_token: str, post_id: str, text: str) -> CommentResult:
        """Reply to a message (the first comment)."""
        sent = self._publish_call(
            "sendMessage",
            json_body={
                "chat_id": access_token,
                "text": to_telegram_html(text),
                "parse_mode": "HTML",
                "reply_parameters": {"message_id": int(post_id)},
            },
        )
        return CommentResult(platform_comment_id=str(sent["message_id"]), extra={"chat_id": str(access_token)})

    def delete_post(self, access_token: str, post_id: str) -> bool:
        """Delete a message the bot sent (``deleteMessage``; Telegram refuses messages older than 48 hours)."""
        return bool(self._api("deleteMessage", json_body={"chat_id": access_token, "message_id": int(post_id)}))
