"""Final approval policy used by both approval services and the publisher.

The editorial status alone is not sufficient proof that the exact bytes about to
be sent were approved: a bug or direct status update could put an unreviewed row
back into ``scheduled``.  Final approval therefore records a fingerprint of the
publishable payload.  The worker recomputes it immediately before dispatch.
"""

from __future__ import annotations

import hashlib
import json

APPROVAL_REQUIRED_MODES = frozenset({"required_internal", "required_internal_and_client"})


def publish_fingerprint(platform_post) -> str:
    """Return a deterministic SHA-256 fingerprint of publishable post content."""
    post = platform_post.post
    media = list(
        post.media_attachments.select_related("media_asset")
        .order_by("position", "id")
        .values(
            "media_asset_id",
            "media_asset__file",
            "position",
            "alt_text",
            "platform_overrides",
        )
    )
    payload = {
        "post": {
            "title": post.title,
            "caption": post.caption,
            "first_comment": post.first_comment,
            "tags": post.tags,
        },
        "platform": {
            "social_account_id": str(platform_post.social_account_id),
            "title": platform_post.platform_specific_title,
            "caption": platform_post.platform_specific_caption,
            "media": platform_post.platform_specific_media,
            "first_comment": platform_post.platform_specific_first_comment,
            "extra": platform_post.platform_extra,
        },
        "media": media,
    }
    # A thread's later parts are publishable content too. Added only when
    # there are any, so a single post hashes exactly as it did before threads
    # existed and its recorded approval stays valid.
    parts = list(post.thread_parts.order_by("position", "created_at"))
    if parts:
        overrides = dict(
            platform_post.part_states.exclude(platform_specific_caption__isnull=True).values_list(
                "part_id", "platform_specific_caption"
            )
        )
        payload["parts"] = [
            {
                "caption": part.caption,
                "caption_override": overrides.get(part.id),
                "delay_seconds": part.delay_seconds,
                "is_finisher": part.is_finisher,
                "media": list(
                    part.media_attachments.order_by("position", "id").values(
                        "media_asset_id", "media_asset__file", "position", "alt_text"
                    )
                ),
            }
            for part in parts
        ]
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def mark_final_approval(platform_post) -> None:
    """Persist proof that the current publishable payload received final approval."""
    from django.utils import timezone

    platform_post.approval_completed_at = timezone.now()
    platform_post.approval_fingerprint = publish_fingerprint(platform_post)
    platform_post.save(update_fields=["approval_completed_at", "approval_fingerprint", "updated_at"])


def clear_final_approval(platform_post) -> None:
    """Invalidate approval proof when a post re-enters an editorial review state."""
    if platform_post.approval_completed_at is None and not platform_post.approval_fingerprint:
        return
    platform_post.approval_completed_at = None
    platform_post.approval_fingerprint = ""
    platform_post.save(update_fields=["approval_completed_at", "approval_fingerprint", "updated_at"])


def approval_allows_publish(platform_post) -> bool:
    """Fail closed when an approval-required workspace lacks exact payload proof."""
    mode = getattr(platform_post.post.workspace, "approval_workflow_mode", "none")
    if mode not in APPROVAL_REQUIRED_MODES:
        return True
    if platform_post.approval_completed_at is None or not platform_post.approval_fingerprint:
        return False
    return platform_post.approval_fingerprint == publish_fingerprint(platform_post)


def targets_fingerprint(platform_posts) -> str:
    """One fingerprint over several platform posts (``""`` when there are none).

    What a bundled decision covers: each target's own ``publish_fingerprint``,
    in a stable order, so a change to any account's text, media, thread or
    settings changes the whole.
    """
    parts = sorted(f"{pp.pk}:{publish_fingerprint(pp)}" for pp in platform_posts)
    if not parts:
        return ""
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def review_fingerprint(post, statuses) -> str:
    """The fingerprint of what a reviewer in the stage(s) ``statuses`` is being asked to decide.

    Computed over the post's platform posts currently in those statuses. A
    decision link or a Slack button carries the value from when the request was
    built; if the value now differs, the post changed and must be looked at again.
    """
    targets = post.platform_posts.filter(status__in=statuses).select_related("post", "social_account")
    return targets_fingerprint(targets)
