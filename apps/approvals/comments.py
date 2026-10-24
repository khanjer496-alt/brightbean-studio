"""Post comment business logic.

Handles creation, updating, deletion, and @mention parsing for post comments.
"""

import re

from django.db.models import Prefetch
from django.utils import timezone

from apps.members.models import WorkspaceMembership
from apps.notifications.engine import notify
from apps.notifications.models import EventType
from apps.webhooks import events as webhook_events

from .models import ApprovalAction, Channel, PostComment

# Match @mentions but not email addresses (require whitespace or start-of-string before @)
MENTION_REGEX = re.compile(r"(?:^|(?<=\s))@(\w[\w.]*)")
MAX_ATTACHMENT_SIZE = 5 * 1024 * 1024  # 5MB


def create_comment(post, author, body, visibility, parent_id=None, attachment=None, *, channel=Channel.WEB):
    """Create a comment on a post, record it in the post's activity, and notify people.

    A comment is the "comment only" review action: it leaves the post where it
    is. The post's author is told (unless they wrote it), and @mentioned
    teammates are told once each, so someone both mentioned and the author gets
    one notification, not two.

    Args:
        post: The Post instance.
        author: The User who wrote the comment.
        body: Comment text.
        visibility: "internal" or "external".
        parent_id: UUID of parent comment for threading (optional).
        attachment: UploadedFile for image attachment (optional).
        channel: The surface it was written on (web, portal, API), kept on the activity entry.

    Returns:
        The created PostComment.
    """
    if attachment and attachment.size > MAX_ATTACHMENT_SIZE:
        raise ValueError("Attachment exceeds 5MB limit.")

    parent_comment = None
    if parent_id:
        parent_comment = PostComment.objects.filter(
            id=parent_id,
            post=post,
            deleted_at__isnull=True,
        ).first()

    comment = PostComment.objects.create(
        post=post,
        author=author,
        parent_comment=parent_comment,
        body=body,
        visibility=visibility,
        attachment=attachment,
    )

    webhook_events.comment_created(comment)

    # The post's activity (approval history) shows who weighed in, without a decision.
    ApprovalAction.objects.create(
        post=post,
        user=author,
        action=ApprovalAction.ActionType.COMMENTED,
        comment=body,
        channel=channel,
    )

    # Parse @mentions and notify
    workspace = post.workspace
    mentions = MENTION_REGEX.findall(body)
    notified = _notify_mentions(mentions, post, author, workspace, body) if mentions else set()
    _notify_author(post, author, workspace, body, visibility, already_notified=notified)

    return comment


def _notify_author(post, commenter, workspace, body, visibility, *, already_notified):
    """Tell the post's author about a new comment on it (never the commenter, never twice)."""
    recipient = post.author
    if recipient is None or recipient.id == commenter.id or recipient.id in already_notified:
        return
    if visibility == PostComment.Visibility.INTERNAL and is_client(recipient, workspace):
        return
    notify(
        user=recipient,
        event_type=EventType.POST_COMMENT,
        title=f"{commenter.display_name} commented on your post",
        body=f'On "{post.caption_snippet}": "{body[:100]}"',
        data={
            "post_id": str(post.id),
            "workspace_id": str(workspace.id),
        },
    )


def is_client(user, workspace):
    """Whether ``user`` is a client member of ``workspace`` (they only ever see and write external comments)."""
    return WorkspaceMembership.objects.filter(
        user=user, workspace=workspace, workspace_role=WorkspaceMembership.WorkspaceRole.CLIENT
    ).exists()


def can_comment(user, workspace):
    """Whether ``user`` may comment on posts in ``workspace`` (active member holding ``comment_on_posts``)."""
    membership = (
        WorkspaceMembership.objects.select_related("custom_role")
        .filter(user=user, workspace=workspace, user__is_active=True, workspace__is_archived=False)
        .first()
    )
    return bool(membership and membership.can("comment_on_posts"))


def update_comment(comment_id, user, body, *, workspace=None):
    """Update a comment's body. Only the author can edit.

    When `workspace` is provided, the comment's post is required to belong to
    that workspace — defends against cross-workspace IDOR via a forged
    post_id/comment_id pair.
    """
    qs = PostComment.objects.filter(
        id=comment_id,
        deleted_at__isnull=True,
    )
    if workspace is not None:
        qs = qs.filter(post__workspace=workspace)
    comment = qs.first()

    if not comment:
        raise ValueError("Comment not found.")
    if comment.author_id != user.id:
        raise PermissionError("Only the comment author can edit.")

    comment.body = body
    comment.save(update_fields=["body", "updated_at"])
    return comment


def delete_comment(comment_id, user, workspace):
    """Soft-delete a comment. Authors and managers can delete."""
    comment = PostComment.objects.filter(
        id=comment_id,
        deleted_at__isnull=True,
    ).first()

    if not comment:
        raise ValueError("Comment not found.")

    # Check permission: author or user with approve_posts (manager+)
    membership = (
        WorkspaceMembership.objects.filter(
            user=user,
            workspace=workspace,
        )
        .select_related("custom_role")
        .first()
    )

    is_author = comment.author_id == user.id
    can_moderate = membership and membership.effective_permissions.get("approve_posts", False)

    if not is_author and not can_moderate:
        raise PermissionError("You don't have permission to delete this comment.")

    comment.deleted_at = timezone.now()
    comment.save(update_fields=["deleted_at", "updated_at"])
    return comment


def get_comments_for_post(post, user):
    """Get comments for a post, filtered by user's visibility access.

    Clients only see external comments. Team members see all.
    """
    workspace = post.workspace
    membership = WorkspaceMembership.objects.filter(
        user=user,
        workspace=workspace,
    ).first()

    is_client = bool(membership and membership.workspace_role == WorkspaceMembership.WorkspaceRole.CLIENT)

    active_replies = PostComment.objects.filter(
        deleted_at__isnull=True,
    ).select_related("author")
    if is_client:
        # A team member's internal reply under an external comment is still internal.
        active_replies = active_replies.filter(visibility=PostComment.Visibility.EXTERNAL)

    qs = (
        PostComment.objects.filter(
            post=post,
            deleted_at__isnull=True,
            parent_comment__isnull=True,  # Top-level only; replies are prefetched
        )
        .select_related("author")
        .prefetch_related(
            Prefetch("replies", queryset=active_replies),
        )
        .order_by("created_at")
    )

    # Clients only see external comments
    if is_client:
        qs = qs.filter(visibility=PostComment.Visibility.EXTERNAL)

    return qs


def _notify_mentions(mentions, post, author, workspace, body, *, noun="comment", only_with=None):
    """Send notifications to @mentioned workspace members; returns the ids of those notified.

    ``noun`` words the notification ("comment" / "note"). ``only_with`` names a
    permission the mentioned person must hold, so the text of a team-only note is
    never sent to someone who may not read it.
    """
    # Build lookup of display names / email prefixes → user
    memberships = WorkspaceMembership.objects.filter(
        workspace=workspace,
        user__is_active=True,
    ).select_related("user", "custom_role")

    name_to_user = {}
    for m in memberships:
        if only_with and not m.can(only_with):
            continue
        user = m.user
        # Match by display name (spaces replaced with dots/underscores)
        display = user.display_name.lower().replace(" ", "")
        name_to_user[display] = user
        # Also match by email prefix
        email_prefix = user.email.split("@")[0].lower()
        name_to_user[email_prefix] = user

    notified = set()
    for mention in mentions:
        mention_lower = mention.lower()
        target_user = name_to_user.get(mention_lower)
        if target_user and target_user.id != author.id and target_user.id not in notified:
            notify(
                user=target_user,
                event_type=EventType.COMMENT_MENTION,
                title=f"{author.display_name} mentioned you",
                body=f'In a {noun} on a post: "{body[:100]}"',
                data={
                    "post_id": str(post.id),
                    "workspace_id": str(workspace.id),
                },
            )
            notified.add(target_user.id)
    return notified
