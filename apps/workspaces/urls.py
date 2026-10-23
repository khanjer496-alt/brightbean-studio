from django.urls import path

from . import views

app_name = "workspaces"

urlpatterns = [
    path("", views.workspace_list, name="list"),
    path("create/", views.workspace_create, name="create"),
    path("<uuid:workspace_id>/settings/", views.workspace_settings, name="settings"),
    path("<uuid:workspace_id>/settings/approvals/", views.approvals_settings, name="approvals_settings"),
    path("<uuid:workspace_id>/settings/brand-voice/", views.brand_voice_settings, name="brand_voice_settings"),
    path("<uuid:workspace_id>/settings/hashtags/", views.hashtag_sets_settings, name="hashtag_sets_settings"),
    path("<uuid:workspace_id>/settings/signatures/", views.signatures_settings, name="signatures_settings"),
]
