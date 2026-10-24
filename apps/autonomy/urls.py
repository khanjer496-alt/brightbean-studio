from django.urls import path

from . import views

app_name = "autonomy"

urlpatterns = [
    path("", views.activity, name="activity"),
    path("tray/", views.tray, name="tray"),
    path("settings/", views.autonomy_settings, name="settings"),
    path("crisis-pause/", views.crisis_toggle, name="crisis_toggle"),
    path("<uuid:action_id>/", views.action_detail, name="action_detail"),
    path("<uuid:action_id>/<str:verb>/", views.action_run, name="action_run"),
]
