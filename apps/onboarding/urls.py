from django.urls import path

from . import views, views_welcome

app_name = "onboarding"

urlpatterns = [
    # Management (authenticated, workspace-scoped)
    path(
        "<uuid:workspace_id>/links/create/",
        views.create_link,
        name="create_link",
    ),
    path(
        "<uuid:workspace_id>/links/<uuid:link_id>/revoke/",
        views.revoke_link,
        name="revoke_link",
    ),
    path(
        "<uuid:workspace_id>/links/<uuid:link_id>/send-email/",
        views.send_link_email,
        name="send_link_email",
    ),
    path(
        "<uuid:workspace_id>/checklist/",
        views.checklist_partial,
        name="checklist_partial",
    ),
    path(
        "<uuid:workspace_id>/checklist/dismiss/",
        views.dismiss_checklist,
        name="dismiss_checklist",
    ),
    path(
        "<uuid:workspace_id>/checklist/confirm/<str:key>/",
        views.confirm_checklist_item,
        name="confirm_checklist_item",
    ),
    # Welcome flow (owner of a new workspace)
    path("<uuid:workspace_id>/welcome/", views_welcome.welcome, name="welcome"),
    path("<uuid:workspace_id>/welcome/workspace/", views_welcome.welcome_workspace, name="welcome_workspace"),
    path("<uuid:workspace_id>/welcome/channel/", views_welcome.welcome_channel, name="welcome_channel"),
    path("<uuid:workspace_id>/welcome/channel/next/", views_welcome.welcome_channel_next, name="welcome_channel_next"),
    path("<uuid:workspace_id>/welcome/post/", views_welcome.welcome_post, name="welcome_post"),
    path("<uuid:workspace_id>/welcome/ai/", views_welcome.welcome_ai_write, name="welcome_ai_write"),
    path("<uuid:workspace_id>/welcome/done/", views_welcome.welcome_done, name="welcome_done"),
    path("<uuid:workspace_id>/welcome/skip/", views_welcome.welcome_skip, name="welcome_skip"),
    # Example workspace
    path("<uuid:workspace_id>/sample/create/", views_welcome.sample_create, name="sample_create"),
    path("<uuid:workspace_id>/sample/remove/", views_welcome.sample_remove, name="sample_remove"),
    # First-visit hints
    path("hints/<str:key>/dismiss/", views_welcome.dismiss_hint, name="dismiss_hint"),
    # Public connection link page (NO auth required)
    path(
        "connect/<str:token>/",
        views.connection_page,
        name="connection_page",
    ),
    path(
        "connect/<str:token>/oauth/start/",
        views.connection_oauth_start,
        name="connection_oauth_start",
    ),
    path(
        "connect/<str:token>/done/",
        views.connection_done,
        name="connection_done",
    ),
    path(
        "connect/<str:token>/bluesky/",
        views.connection_bluesky_connect,
        name="connection_bluesky",
    ),
    path(
        "connect/<str:token>/mastodon/",
        views.connection_mastodon_start,
        name="connection_mastodon",
    ),
    # OAuth callback for connection link flow (NO auth, token in state)
    path(
        "connect/callback/<str:platform>/",
        views.connection_oauth_callback,
        name="oauth_callback",
    ),
]
