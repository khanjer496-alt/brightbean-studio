"""Copy and actions for the empty states shared by the main pages.

Every page that can be empty renders the same component
(``onboarding/partials/_empty_state.html``) through the ``empty_state`` template
tag, so the wording and the primary action live here, in one place.
"""

from __future__ import annotations

from django.urls import reverse

# SECURITY: icon paths are rendered with ``|safe``; keep them constants.
_ICONS = {
    "calendar": '<rect x="3" y="4" width="18" height="18" rx="2"/><path d="M16 2v4M8 2v4M3 10h18"/>',
    "inbox": '<path d="M21 15a2 2 0 01-2 2H7l-4 4V5a2 2 0 012-2h14a2 2 0 012 2z"/>',
    "analytics": '<path d="M3 3v18h18"/><path d="M7 12l4-4 4 4 6-6"/>',
    "media": (
        '<rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/>'
        '<path d="M21 15l-5-5L5 21"/>'
    ),
    "evergreen": '<path d="M17 1l4 4-4 4"/><path d="M3 11V9a4 4 0 014-4h14M7 23l-4-4 4-4"/><path d="M21 13v2a4 4 0 01-4 4H3"/>',
    "reports": '<path d="M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z"/><path d="M14 2v6h6M16 13H8M16 17H8"/>',
    "connect": '<path d="M13.828 10.172a4 4 0 00-5.656 0l-4 4a4 4 0 105.656 5.656l1.102-1.101"/><path d="M10.172 13.828a4 4 0 005.656 0l4-4a4 4 0 00-5.656-5.656l-1.1 1.1"/>',
}

# key -> (icon, title, body, action label, url name, alpine click handler)
# The channel-dependent pages fall back to CONNECT when nothing is connected.
_STATES = {
    "calendar": (
        "calendar",
        "Nothing scheduled yet",
        "Posts you schedule appear here on the day they go out.",
        "Create a post",
        "composer:compose",
        "",
    ),
    "inbox": (
        "inbox",
        "Your inbox is empty",
        "Comments, mentions and messages from your channels land here.",
        "Create a post",
        "composer:compose",
        "",
    ),
    "analytics": (
        "analytics",
        "No analytics yet",
        "Numbers show up within a day or two of your first published post.",
        "Create a post",
        "composer:compose",
        "",
    ),
    "media": (
        "media",
        "No media yet",
        "Upload images and videos once, then reuse them in any post.",
        "Upload media",
        "",
        "$refs.fileInput.click()",
    ),
    "evergreen": (
        "evergreen",
        "No evergreen posts yet",
        "Add a post that did well and it fills empty queue slots again later.",
        "Pick from sent posts",
        "calendar:calendar",
        "",
    ),
    "reports": (
        "reports",
        "No reports yet",
        "Send clients a branded summary every week or month.",
        "Create your first report",
        "reports:create",
        "",
    ),
}

_NEEDS_CHANNEL = {"calendar", "inbox", "analytics", "evergreen"}

# Query strings for actions that open a page on a specific tab.
_ACTION_QUERIES = {"evergreen": "?mode=list&tab=sent"}

_CONNECT = (
    "connect",
    "Connect your first channel",
    "Link a network to start scheduling posts.",
    "Connect a channel",
    "social_accounts:connect",
    "",
)


def empty_state(key, workspace, *, has_channels=None, offer_sample=False):
    """Template context for the empty state ``key`` in ``workspace``.

    ``has_channels`` skips the lookup when the page already knows whether the
    workspace has a connected channel. ``offer_sample`` adds a secondary
    "Show me an example" action to the calendar's connect-a-channel state.
    """
    if key not in _STATES:
        raise KeyError(f"No empty state called {key!r}")
    state = _STATES[key]
    if key in _NEEDS_CHANNEL:
        if has_channels is None:
            from apps.social_accounts.models import SocialAccount

            has_channels = (
                SocialAccount.objects.for_workspace(workspace.id)
                .filter(connection_status=SocialAccount.ConnectionStatus.CONNECTED)
                .exists()
            )
        if not has_channels:
            state = _CONNECT
    icon, title, body, label, url_name, click = state
    action_url = ""
    if url_name:
        query = "" if state is _CONNECT else _ACTION_QUERIES.get(key, "")
        action_url = reverse(url_name, kwargs={"workspace_id": workspace.id}) + query
    sample_url = ""
    if offer_sample and key == "calendar" and state is _CONNECT:
        sample_url = reverse("onboarding:sample_create", kwargs={"workspace_id": workspace.id})
    return {
        "sample_url": sample_url,
        "key": key,
        "icon_svg": _ICONS[icon],
        "title": title,
        "body": body,
        "action_label": label,
        "action_url": action_url,
        "action_click": click,
    }
