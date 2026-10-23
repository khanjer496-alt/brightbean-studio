from django.urls import path

from . import views

app_name = "organizations"

urlpatterns = [
    path("settings/", views.settings_view, name="settings"),
    path("brand-kit/", views.brand_kit_view, name="brand_kit"),
    path("workspaces/", views.workspaces_view, name="workspaces"),
    path("calendar/", views.cross_workspace_calendar, name="cross_workspace_calendar"),
]
