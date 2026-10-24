from django.urls import path

from . import views

app_name = "support"

urlpatterns = [
    path("staff/view-as/stop/", views.impersonation_stop, name="impersonation_stop"),
    path("staff/view-as/<uuid:user_id>/", views.impersonate, name="impersonate"),
    path("staff/platform-errors/", views.platform_errors, name="platform_errors"),
    path("announcements/<uuid:announcement_id>/dismiss/", views.dismiss_announcement, name="dismiss_announcement"),
]
