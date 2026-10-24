"""Team-only notes on posts.

A note is for the people who work on a post, never for a client and never for
the network: it lives in its own table (:class:`PostNote`) that no client-facing
view or template reads. Everything here goes through :func:`membership_for`, so
the one rule (an active team member holding ``use_internal_notes``, and never a
client) is decided in one place.
"""

from django.db.models import Count, IntegerField, OuterRef, Subquery, Value
from django.db.models.functions import Coalesce
from django.utils import timezone

from apps.members.models import WorkspaceMembership

from .comments import MENTION_REGEX, _notify_mentions
from .models import PostNote

MAX_NOTE_LENGTH = 5_000
PERMISSION = "use_internal_notes"


def membership_for(user, workspace):
    """The member's team membership when they may read and write notes in ``workspace``, else ``None``."""
    membership = (
        WorkspaceMembership.objects.select_related("custom_role")
        .filter(user=user, workspace=workspace, user__is_active=True, workspace__is_archived=False)
        .first()
    )
    if membership is None or membership.workspace_role == WorkspaceMembership.WorkspaceRole.CLIENT:
        return None
    return membership if membership.can(PERMISSION) else None


def can_use_notes(membership):
    """Whether a membership already in hand (a request's) may see notes."""
    return bool(
        membership
        and membership.workspace_role != WorkspaceMembership.WorkspaceRole.CLIENT
        and membership.can(PERMISSION)
    )


def notes_for_post(post):
    """The post's live notes, oldest first, with their authors."""
    return PostNote.objects.filter(post=post, deleted_at__isnull=True).select_related("author").order_by("created_at")


def create_note(post, author, body):
    """Add a note to ``post`` and tell any @mentioned teammate who may read notes.

    Raises ``ValueError`` for an empty or over-long body.
    """
    body = (body or "").strip()
    if not body:
        raise ValueError("A note can't be empty.")
    if len(body) > MAX_NOTE_LENGTH:
        raise ValueError(f"A note can be at most {MAX_NOTE_LENGTH} characters.")
    note = PostNote.objects.create(post=post, author=author, body=body)
    mentions = MENTION_REGEX.findall(body)
    if mentions:
        _notify_mentions(mentions, post, author, post.workspace, body, noun="note", only_with=PERMISSION)
    return note


def delete_note(note_id, user, workspace):
    """Soft-delete a note. Its author can; so can someone who edits other people's posts."""
    note = PostNote.objects.filter(id=note_id, post__workspace=workspace, deleted_at__isnull=True).first()
    if note is None:
        raise ValueError("Note not found.")
    membership = membership_for(user, workspace)
    if membership is None:
        raise PermissionError("You don't have access to notes.")
    if note.author_id != user.id and not membership.effective_permissions.get("edit_others_posts", False):
        raise PermissionError("You can only delete your own notes.")
    note.deleted_at = timezone.now()
    note.save(update_fields=["deleted_at", "updated_at"])
    return note


def annotate_note_counts(qs, membership, *, post_ref="post_id"):
    """Add ``note_count`` to every row of ``qs`` in the same query (no per-row lookups).

    ``post_ref`` is how the rows point at their post (``"post_id"`` on a
    PlatformPost, ``"pk"`` on a Post). Someone who may not read notes (a client)
    gets a flat 0, so the count can't hint that a note exists.
    """
    zero = Value(0, output_field=IntegerField())
    if not can_use_notes(membership):
        return qs.annotate(note_count=zero)
    live = PostNote.objects.filter(post_id=OuterRef(post_ref), deleted_at__isnull=True).order_by().values("post_id")
    count = live.annotate(n=Count("pk")).values("n")
    return qs.annotate(note_count=Coalesce(Subquery(count, output_field=IntegerField()), zero))
