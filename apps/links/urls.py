"""Workspace short-link and UTM settings, mounted at ``/workspace/<id>/links/``."""

from django.urls import path

from . import views

app_name = "links"

urlpatterns = [
    path("", views.link_list, name="list"),
    path("settings/", views.settings_page, name="settings"),
    path("settings/utm-preview/", views.utm_preview, name="utm_preview"),
    path("new/", views.link_create, name="create"),
    path("should-shorten/", views.should_shorten, name="should_shorten"),
    path("<uuid:link_id>/toggle/", views.link_toggle, name="toggle"),
]
