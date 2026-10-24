"""Saved views: the calendar / Publish-page filters under a name, personal or shared with the workspace.

A saved view is only the query string the page already understands, cleaned to
a whitelist, so opening one is a plain link and the page treats it exactly like
filters picked by hand. The date being looked at is not part of it (a saved
"Week" opens on the current week), nor is the display timezone, which is each
person's own setting and would otherwise travel with a shared view.
"""

import uuid
from urllib.parse import urlencode

from django.db import IntegrityError, transaction
from django.db.models import Q
from django.urls import reverse

from .models import SavedView, SavedViewDefault

MAX_VALUES = 25
MAX_VALUE_LENGTH = 100
NAME_MAX_LENGTH = 80

# Single-value parameters and the values the page accepts for them.
_CHOICES = {
    "mode": {"calendar", "list"},
    "view": {"month", "week", "day", "list"},
    "tab": {"queue", "drafts", "approvals", "sent"},
    "holidays": {"1"},
}
# Parameters that may repeat; the calendar and its list tabs read these.
_ID_PARAMS = ("channel", "author", "category")
_TEXT_PARAMS = ("status", "platform", "tag")


def _valid_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
    except (TypeError, ValueError, AttributeError):
        return False
    return True


def clean_filters(params) -> dict[str, list[str]]:
    """The saveable filters from a ``QueryDict`` (or any mapping with ``getlist``), dropping everything else."""
    out: dict[str, list[str]] = {}
    for key, allowed in _CHOICES.items():
        value = (params.get(key) or "").strip()
        if value in allowed:
            out[key] = [value]
    for key in _ID_PARAMS:
        values = [v.strip() for v in params.getlist(key) if _valid_uuid(v.strip())]
        if values:
            out[key] = list(dict.fromkeys(values))[:MAX_VALUES]
    for key in _TEXT_PARAMS:
        values = [v.strip()[:MAX_VALUE_LENGTH] for v in params.getlist(key) if v.strip()]
        if values:
            out[key] = list(dict.fromkeys(values))[:MAX_VALUES]
    return out


def view_url(saved: SavedView) -> str:
    """The page address that opens ``saved``."""
    pairs = [(key, value) for key, values in saved.filters.items() for value in values]
    query = urlencode(pairs)
    base = reverse("calendar:calendar", kwargs={"workspace_id": saved.workspace_id})
    return f"{base}?{query}" if query else f"{base}?mode={saved.filters.get('mode', ['calendar'])[0]}"


def visible_views(workspace, user):
    """Views ``user`` can open in ``workspace``: their own and the ones shared with everyone."""
    return (
        SavedView.objects.for_workspace(workspace.id)
        .filter(Q(owner=user) | Q(is_shared=True))
        .select_related("owner")
        .order_by("-is_shared", "name")
    )


def default_view(workspace, user) -> SavedView | None:
    """The view ``user`` opens by default, if they set one and can still see it."""
    link = (
        SavedViewDefault.objects.filter(workspace=workspace, user=user)
        .filter(Q(view__owner=user) | Q(view__is_shared=True))
        .select_related("view")
        .first()
    )
    return link.view if link else None


def default_view_id(workspace, user):
    return SavedViewDefault.objects.filter(workspace=workspace, user=user).values_list("view_id", flat=True).first()


def can_share(membership) -> bool:
    """Sharing with the workspace is for people who work on posts, not clients or viewers."""
    return bool(membership and membership.effective_permissions.get("create_posts", False))


def can_delete(saved: SavedView, user, membership) -> bool:
    if saved.owner_id == user.id:
        return True
    return bool(saved.is_shared and membership and membership.effective_permissions.get("edit_others_posts", False))


def create_view(workspace, owner, membership, *, name, filters, shared=False) -> SavedView:
    """Save ``filters`` as ``name``. Raises ``ValueError`` with a message for the person saving."""
    name = (name or "").strip()
    if not name:
        raise ValueError("Give the view a name.")
    if len(name) > NAME_MAX_LENGTH:
        raise ValueError(f"A name can be at most {NAME_MAX_LENGTH} characters.")
    if not filters:
        raise ValueError("There are no filters to save.")
    if shared and not can_share(membership):
        raise ValueError("You can't share views with the workspace.")
    try:
        # A savepoint, so the duplicate-name error doesn't poison the request's own transaction.
        with transaction.atomic():
            return SavedView.objects.create(
                workspace=workspace, owner=owner, name=name, filters=filters, is_shared=bool(shared)
            )
    except IntegrityError as exc:
        raise ValueError("You already have a view with that name.") from exc


def delete_view(saved: SavedView) -> None:
    saved.delete()


def set_default(workspace, user, saved: SavedView | None) -> None:
    """Make ``saved`` the person's default view, or clear it with ``None``."""
    if saved is None:
        SavedViewDefault.objects.filter(workspace=workspace, user=user).delete()
        return
    SavedViewDefault.objects.update_or_create(workspace=workspace, user=user, defaults={"view": saved})
