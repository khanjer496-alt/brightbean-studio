"""URLs under ``/workspace/<id>/analytics/ai-visibility/``."""

from django.urls import path

from . import views

app_name = "ai_visibility"

urlpatterns = [
    path("", views.visibility_index, name="index"),
    path("save/", views.visibility_save, name="save"),
    path("run/", views.visibility_run, name="run"),
    path("runs/<uuid:run_id>/save-idea/", views.visibility_save_idea, name="save_idea"),
]
