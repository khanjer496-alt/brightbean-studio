"""Per-post options Instagram's content publishing API takes, shared by both Instagram providers.

Every field here is a parameter of ``POST /{ig-user-id}/media``, checked against
https://developers.facebook.com/docs/instagram-platform/instagram-graph-api/reference/ig-user/media
(2026-10-24): ``media_type`` (STORIES, REELS), ``collaborators`` (at most 3 usernames; feed images,
Reels and carousels, not Stories), ``trial_params.graduation_strategy`` (MANUAL or SS_PERFORMANCE;
Reels only), ``share_to_feed`` (Reels only) and ``thumb_offset`` (milliseconds into a Reel).

Deliberately NOT offered, because the API does not expose them to third-party apps: picking music
from Instagram's library for a Reel (``audio_name`` only renames the Reel's own audio) and Story
stickers (polls, questions, countdowns, links). The composer says so next to the post type.
"""

from __future__ import annotations

import re

from .exceptions import PublishError
from .types import PostSetting, PostType

MAX_COLLABORATORS = 3
_USERNAME = re.compile(r"^[A-Za-z0-9._]{1,30}$")
GRADUATION_STRATEGIES = ("MANUAL", "SS_PERFORMANCE")

INSTAGRAM_POST_SETTINGS = (
    PostSetting(
        key="post_type",
        label="Post format",
        kind="select",
        placeholder="Post (a lone video becomes a Reel)",
        choices=(("story", "Story"), ("reel", "Reel")),
        help=(
            "A Story lasts 24 hours and takes one photo or video. Instagram's API does not let apps add music "
            "from its library or Story stickers (polls, questions, links); add those in the Instagram app."
        ),
    ),
    PostSetting(
        key="collaborators",
        label="Collaborators",
        kind="text",
        max_length=200,
        placeholder="username1, username2",
        show_when="post_type=|reel",
        help=f"Invite up to {MAX_COLLABORATORS} Instagram usernames to co-author this post. Not available for Stories.",
    ),
    PostSetting(
        key="trial_reel",
        label="Trial Reel",
        kind="select",
        placeholder="Share with everyone",
        choices=(
            ("MANUAL", "Trial: I graduate it manually in the app"),
            ("SS_PERFORMANCE", "Trial: graduate it automatically if it performs"),
        ),
        show_when="post_type=|reel",
        help="A trial Reel is shown to people who don't follow you first. Applies when the post is a Reel.",
    ),
    PostSetting(
        key="share_to_feed",
        label="Reel placement",
        kind="select",
        placeholder="Instagram's default",
        choices=(("true", "Reels tab and profile feed"), ("false", "Reels tab only")),
        show_when="post_type=|reel",
        help="Neither choice guarantees placement; Instagram still decides eligibility.",
    ),
    PostSetting(
        key="thumb_offset",
        label="Cover frame (ms)",
        kind="text",
        max_length=8,
        placeholder="0",
        show_when="post_type=|reel",
        help="Milliseconds into the Reel for its cover frame. Default 0 is the first frame.",
    ),
)


def parse_collaborators(raw) -> list[str]:
    """Usernames in a comma/space separated string (a leading @ is dropped), in order, without repeats."""
    names: list[str] = []
    for part in re.split(r"[,\s]+", str(raw or "")):
        name = part.strip().lstrip("@")
        if name and name.lower() not in {n.lower() for n in names}:
            names.append(name)
    return names


def validate_instagram_settings(settings: dict) -> str:
    """Why these Instagram settings can't publish, as a sentence, or ""."""
    settings = settings or {}
    post_type = settings.get("post_type") or ""
    if post_type not in ("", "story", "reel"):
        return "Post format must be Post, Story or Reel."
    names = parse_collaborators(settings.get("collaborators"))
    if names:
        if post_type == "story":
            return "Instagram doesn't allow collaborators on Stories."
        if len(names) > MAX_COLLABORATORS:
            return f"Instagram allows at most {MAX_COLLABORATORS} collaborators."
        bad = next((n for n in names if not _USERNAME.match(n)), None)
        if bad:
            return f"{bad!r} isn't a valid Instagram username."
    trial = settings.get("trial_reel") or ""
    if trial and trial not in GRADUATION_STRATEGIES:
        return "Trial Reel has an unsupported value."
    if post_type == "story" and (trial or settings.get("share_to_feed") or settings.get("thumb_offset")):
        return "Trial, placement and cover frame apply to Reels only."
    offset = str(settings.get("thumb_offset") or "").strip()
    if offset and not offset.isdigit():
        return "Cover frame must be a whole number of milliseconds."
    share = settings.get("share_to_feed") or ""
    if share not in ("", "true", "false"):
        return "Reel placement has an unsupported value."
    return ""


def container_options(extra: dict, post_type: PostType, *, is_video: bool, platform: str) -> dict:
    """Parameters to add to a single-media or carousel container for the chosen options.

    ``post_type`` is the type the engine resolved. Options that the resolved type can't carry fail the
    publish (non-retryable) rather than being dropped.
    """
    is_story = post_type is PostType.STORY
    is_reel = not is_story and (post_type is PostType.REEL or is_video)
    resolved = "story" if is_story else ("reel" if is_reel else "")
    error = validate_instagram_settings({**extra, "post_type": resolved})
    if error:
        raise PublishError(error, platform=platform, retryable=False)
    params: dict = {}
    names = parse_collaborators(extra.get("collaborators"))
    if names:
        params["collaborators"] = names
    if is_reel:
        if extra.get("trial_reel"):
            params["trial_params"] = {"graduation_strategy": extra["trial_reel"]}
        if extra.get("share_to_feed") in ("true", "false"):
            params["share_to_feed"] = extra["share_to_feed"] == "true"
        if str(extra.get("thumb_offset") or "").strip():
            params["thumb_offset"] = int(str(extra["thumb_offset"]).strip())
    elif extra.get("trial_reel") or extra.get("share_to_feed") or str(extra.get("thumb_offset") or "").strip():
        raise PublishError(
            "Trial Reel, Reel placement and cover frame apply to a Reel; this post isn't one.",
            platform=platform,
            retryable=False,
        )
    return params
