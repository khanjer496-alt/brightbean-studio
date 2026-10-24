"""What each network will publish, as the shared preview components render it.

The composer's live preview panel and every approver surface (client portal,
approval page, emails, Slack) draw ``composer/partials/preview_card.html`` from
the same dicts, so an approver sees exactly the card the author saw. Nothing
here knows a network: the card template picks the mockup, and the thread and
settings rules come from the provider declarations.
"""

from __future__ import annotations

from providers import post_settings_for, thread_capability_for

from .services import post_media_assets, post_thread_parts, thread_blocker, thread_parts_for

POST_TYPE_LABEL = "Format"


def media_item(asset) -> dict:
    """One media asset as the preview mockups read it."""
    return {
        "id": str(asset.pk),
        "url": asset.file.url if asset.file else "",
        "is_video": asset.is_video,
        "filename": asset.filename,
    }


def thread_preview(account, parts, first_comment: str = "", counting=None) -> dict | None:
    """The thread under one account's card, or None for a single post.

    ``parts`` is ``(caption, assets)`` per part after the first post, already
    resolved for ``account`` (its overrides applied).
    """
    if not parts:
        return None
    capability = thread_capability_for(account.platform)
    limit = capability.part_max_length or account.char_limit
    measure = _measure(account, counting)
    return {
        "mode": capability.mode.value,
        "blocker": thread_blocker(account, parts, first_comment=first_comment, counting=counting),
        "parts": [
            {
                "number": number,
                "caption": caption,
                "char_count": measure(caption),
                "char_limit": limit,
                "is_over_limit": measure(caption) > limit,
                "media": [media_item(a) for a in assets],
            }
            for number, (caption, assets) in enumerate(parts, start=2)
        ],
    }


def _measure(account, counting=None):
    """How ``account`` counts a text: after escaping, and, when links will be shortened, with their short length."""
    if counting is None:
        return account.caption_wire_length
    return lambda text: account.caption_wire_length(counting(text))


def account_preview(account, *, title: str, caption: str, first_comment: str = "", thread=None, counting=None) -> dict:
    """The card context for one account: its effective text and how it measures up.

    ``counting`` rewrites a text before it is counted (links swapped for their short length).
    """
    char_limit = account.char_limit
    # Counted after escaping, so the badge matches what gets published.
    char_count = _measure(account, counting)(caption)
    return {
        "account": account,
        "title": title,
        "caption": caption,
        "first_comment": first_comment,
        "char_count": char_count,
        "char_limit": char_limit,
        "is_over_limit": char_count > char_limit,
        "truncated_caption": caption[:char_limit] if len(caption) > char_limit else caption,
        "needs_title": account.field_config["needs_title"],
        "thread": thread,
    }


def settings_summary(account, platform_extra: dict | None) -> list[dict]:
    """The per-post settings an approver should see, as ``{"label", "value"}`` rows.

    Only what the provider declares (``post_settings``) plus the chosen post
    format; the publisher's own bookkeeping in ``platform_extra`` is not shown.
    """
    extra = platform_extra or {}
    rows = []
    declared_keys = {field.key for field in post_settings_for(account.platform)}
    if extra.get("post_type") and "post_type" not in declared_keys:
        rows.append({"label": POST_TYPE_LABEL, "value": str(extra["post_type"]).replace("_", " ").title()})
    for field in post_settings_for(account.platform):
        value = extra.get(field.key)
        if field.kind == "checkbox":
            value = field.default if value is None else bool(value)
            rows.append({"label": field.label, "value": "Yes" if value else "No"})
        elif value not in (None, ""):
            labels = dict(field.choices)
            rows.append({"label": field.label, "value": labels.get(value, str(value))})
    return rows


def platform_post_preview(platform_post, *, thread_parts=None) -> dict:
    """What ``platform_post``'s account will publish: the effective text, thread and settings.

    Mirrors the publisher: the account's override when it has one, otherwise
    the post's text; thread parts with the account's per-part overrides.
    """
    account = platform_post.social_account
    first_comment = platform_post.effective_first_comment if account.supports_first_comment() else ""
    parts = [(caption, assets) for _part, caption, assets in thread_parts_for(platform_post, thread_parts)]
    preview = account_preview(
        account,
        title=platform_post.effective_title,
        caption=platform_post.effective_caption,
        first_comment=first_comment,
        thread=thread_preview(account, parts, first_comment),
    )
    preview["settings"] = settings_summary(account, platform_post.platform_extra)
    preview["platform_post"] = platform_post
    return preview


def post_previews(post, platform_posts=None) -> dict:
    """Everything the approver surfaces draw for ``post``: a card per account and the media in order."""
    if platform_posts is None:
        platform_posts = list(post.platform_posts.select_related("social_account").order_by("social_account__platform"))
    parts = post_thread_parts(post)
    return {
        "previews": [platform_post_preview(pp, thread_parts=parts) for pp in platform_posts],
        "media_items": [media_item(asset) for asset in post_media_assets(post)],
    }
