from django.urls import path

from . import views

app_name = "social_accounts"

urlpatterns = [
    # Workspace-scoped views
    path(
        "<uuid:workspace_id>/",
        views.account_list,
        name="list",
    ),
    path(
        "<uuid:workspace_id>/connect/",
        views.connect_platform,
        name="connect",
    ),
    path(
        "<uuid:workspace_id>/connect/bluesky/",
        views.connect_bluesky,
        name="connect_bluesky",
    ),
    path(
        "<uuid:workspace_id>/connect/mastodon/",
        views.connect_mastodon,
        name="connect_mastodon",
    ),
    path(
        "<uuid:workspace_id>/connect/devto/",
        views.connect_devto,
        name="connect_devto",
    ),
    path(
        "<uuid:workspace_id>/connect/telegram/",
        views.connect_telegram,
        name="connect_telegram",
    ),
    path(
        "<uuid:workspace_id>/connect/medium/",
        views.connect_medium,
        name="connect_medium",
    ),
    path(
        "<uuid:workspace_id>/connect/wordpress/",
        views.connect_wordpress,
        name="connect_wordpress",
    ),
    path(
        "<uuid:workspace_id>/connect/hashnode/",
        views.connect_hashnode,
        name="connect_hashnode",
    ),
    path(
        "<uuid:workspace_id>/connect/listmonk/",
        views.connect_listmonk,
        name="connect_listmonk",
    ),
    path(
        "<uuid:workspace_id>/connect/x/",
        views.connect_x,
        name="connect_x",
    ),
    path(
        "<uuid:workspace_id>/connect/lemmy/",
        views.connect_lemmy,
        name="connect_lemmy",
    ),
    path(
        "<uuid:workspace_id>/connect/nostr/",
        views.connect_nostr,
        name="connect_nostr",
    ),
    path(
        "<uuid:workspace_id>/connect/farcaster/",
        views.connect_farcaster,
        name="connect_farcaster",
    ),
    # OAuth callback (not workspace-scoped - platform redirects here)
    path(
        "callback/<str:platform>/",
        views.oauth_callback,
        name="oauth_callback",
    ),
    # Account selection (Facebook multi-page)
    path(
        "select-account/",
        views.select_account,
        name="select_account",
    ),
    # Per-account actions
    path(
        "<uuid:workspace_id>/<uuid:account_id>/reconnect/",
        views.reconnect,
        name="reconnect",
    ),
    path(
        "<uuid:workspace_id>/<uuid:account_id>/retry-webhooks/",
        views.retry_webhooks,
        name="retry_webhooks",
    ),
    path(
        "<uuid:workspace_id>/<uuid:account_id>/options/",
        views.update_options,
        name="update_options",
    ),
    path(
        "<uuid:workspace_id>/<uuid:account_id>/paused/",
        views.set_paused,
        name="set_paused",
    ),
    path(
        "<uuid:workspace_id>/<uuid:account_id>/disconnect/",
        views.disconnect,
        name="disconnect",
    ),
]
