"""URLs under ``/workspace/<id>/analytics/competitors/``."""

from django.urls import path

from . import views

app_name = "competitors"

urlpatterns = [
    path("", views.competitors_index, name="index"),
    path("add/", views.competitor_add, name="add"),
    path("export/<slug:kind>.csv", views.competitors_export, name="export"),
    path("<uuid:competitor_id>/remove/", views.competitor_remove, name="remove"),
]
