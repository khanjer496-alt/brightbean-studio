"""Onboarding checklist evaluation logic.

Computes the "Get started" checklist for a workspace: up to seven items, each
with a live completion status. Items that do not apply are left out: the AI
writer only when it is configured, "connect a client" only for agencies.
Used by the checklist widget that ``base.html`` renders for workspace owners.
"""

from django.urls import reverse

from apps.onboarding.flow import has_scheduled_post
from apps.onboarding.models import OnboardingChecklist, OnboardingFlow

# Seconds a slot may differ between its creation and last update before it
# counts as edited by a person (the defaults are written in one go).
_SLOT_EDIT_GRACE_SECONDS = 2


def _icon(path):
    return f'<path d="{path}"/>'


# SECURITY: ``icon_svg`` is rendered with ``|safe`` in the checklist template,
# so these must remain server-side constants. Never build them from input.
_ICONS = {
    "connect_accounts": _icon("M13.828 10.172a4 4 0 00-5.656 0l-4 4a4 4 0 105.656 5.656l1.102-1.101")
    + _icon("M10.172 13.828a4 4 0 005.656 0l4-4a4 4 0 00-5.656-5.656l-1.1 1.1"),
    "schedule_post": _icon(
        "M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z"
    ),
    "brand_voice": _icon("M12 1a3 3 0 00-3 3v8a3 3 0 006 0V4a3 3 0 00-3-3z")
    + _icon("M19 10v2a7 7 0 01-14 0v-2M12 19v4M8 23h8"),
    "invite_members": _icon("M16 21v-2a4 4 0 00-4-4H5a4 4 0 00-4 4v2")
    + '<circle cx="8.5" cy="7" r="4"/>'
    + _icon("M20 8v6m3-3h-6"),
    "posting_times": '<circle cx="12" cy="12" r="9"/>' + _icon("M12 7v5l3 3"),
    "try_ai": _icon("M13 2L3 14h9l-1 8 10-12h-9l1-8z"),
    "connect_client": _icon("M17 21v-2a4 4 0 00-4-4H5a4 4 0 00-4 4v2")
    + '<circle cx="9" cy="7" r="4"/>'
    + _icon("M23 21v-2a4 4 0 00-3-3.87M16 3.13a4 4 0 010 7.75"),
}


def _posting_times_reviewed(workspace_id, confirmed):
    """Whether the posting times were looked at: confirmed by hand or changed."""
    from django.db.models import F

    from apps.calendar.models import PostingSlot

    if "posting_times" in confirmed:
        return True
    slots = PostingSlot.objects.filter(social_account__workspace_id=workspace_id)
    # Added later than the channel's defaults, or edited after they were made.
    edited = slots.filter(updated_at__gt=F("created_at") + _grace())
    added_later = slots.filter(created_at__gt=F("social_account__created_at") + _grace(60))
    return edited.exists() or added_later.exists()


def _grace(seconds=_SLOT_EDIT_GRACE_SECONDS):
    from datetime import timedelta

    return timedelta(seconds=seconds)


def get_checklist_items(workspace, user=None):
    """Return a list of checklist item dicts with dynamic completion status.

    Each item has: key, title, description, completed (bool), url, icon_svg

    ``user`` is whose checklist this is: it decides which hand-confirmed items
    count and whether the workspace is an agency's.

    SECURITY: `icon_svg` is rendered with `|safe` in the checklist template.
    It MUST remain a server-side constant string. Never populate it from user
    input, model fields, or any other untrusted source.
    """
    from apps.ai_writer.client import ai_available
    from apps.ai_writer.models import AIRequestLog
    from apps.members.models import Invitation, WorkspaceMembership
    from apps.social_accounts.models import SocialAccount

    workspace_id = workspace.id
    flow = OnboardingFlow.objects.filter(user=user, workspace_id=workspace_id).first() if user else None
    confirmed = []
    if user:
        row = OnboardingChecklist.objects.filter(user=user, workspace_id=workspace_id).first()
        confirmed = (row.confirmed_items if row else []) or []

    memberships = WorkspaceMembership.objects.filter(workspace_id=workspace_id)
    teammate_joined = memberships.exclude(user=user).exists() if user else memberships.count() > 1
    # An invitation that is still out counts too: the owner did their part.
    teammate_invited = Invitation.objects.filter(
        organization_id=workspace.organization_id, accepted_at__isnull=True
    ).exists()

    brand_voice_set = bool(
        workspace.brand_voice_tone
        or workspace.brand_voice_words_to_use
        or workspace.brand_voice_words_to_avoid
        or any(workspace.brand_voice_examples or [])
    )

    items = [
        {
            "key": "connect_accounts",
            "title": "Connect a channel",
            "description": "Link the network you post on most",
            "completed": SocialAccount.objects.for_workspace(workspace_id).exists(),
            "url": reverse("social_accounts:connect", kwargs={"workspace_id": workspace_id}),
        },
        {
            "key": "schedule_post",
            "title": "Schedule a post",
            "description": "Write something and pick when it goes out",
            "completed": has_scheduled_post(workspace_id),
            "url": reverse("composer:compose", kwargs={"workspace_id": workspace_id}),
        },
        {
            "key": "brand_voice",
            "title": "Set your brand voice",
            "description": "Tell the writing assistant how you sound",
            "completed": brand_voice_set,
            "url": reverse("workspaces:brand_voice_settings", kwargs={"workspace_id": workspace_id}),
        },
        {
            "key": "invite_members",
            "title": "Invite a teammate",
            "description": "Add anyone who helps with your content, in any role",
            "completed": teammate_joined or teammate_invited,
            "url": reverse("members:list"),
        },
        {
            "key": "posting_times",
            "title": "Set your posting times",
            "description": "Check the suggested best times or choose your own",
            "completed": _posting_times_reviewed(workspace_id, confirmed),
            "url": reverse("calendar:posting_slots", kwargs={"workspace_id": workspace_id}) + "?onboarding=1",
        },
    ]

    if ai_available(workspace.organization):
        items.append(
            {
                "key": "try_ai",
                "title": "Try the AI writer",
                "description": "Let it draft a caption from a few words",
                "completed": AIRequestLog.objects.filter(
                    workspace_id=workspace_id, status=AIRequestLog.Status.OK
                ).exists(),
                "url": reverse("composer:compose", kwargs={"workspace_id": workspace_id}),
            }
        )

    if flow and flow.is_agency:
        items.append(
            {
                "key": "connect_client",
                "title": "Connect a client",
                "description": "Invite a client to approve posts in their portal",
                "completed": memberships.filter(workspace_role=WorkspaceMembership.WorkspaceRole.CLIENT).exists(),
                "url": reverse("client_portal_admin:client_list", kwargs={"workspace_id": workspace_id}),
            }
        )

    for item in items:
        item["icon_svg"] = _ICONS[item["key"]]
    return items


def confirm_item(user, workspace, key):
    """Record a hand-confirmed item (one of the keys the checklist computes)."""
    row, _created = OnboardingChecklist.objects.get_or_create(user=user, workspace=workspace)
    if key not in row.confirmed_items:
        row.confirmed_items = [*row.confirmed_items, key]
        row.save(update_fields=["confirmed_items"])
