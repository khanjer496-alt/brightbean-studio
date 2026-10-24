"""Saved account sets, mounted at ``/workspace/<id>/sets/``."""

from django.urls import path

from . import views

app_name = "account_sets"

urlpatterns = [
    path("", views.set_list, name="list"),
    path("new/", views.set_create, name="create"),
    path("picker/", views.set_picker, name="picker"),
    path("<uuid:set_id>/", views.set_edit, name="edit"),
    path("<uuid:set_id>/delete/", views.set_delete, name="delete"),
    path("<uuid:set_id>/apply/", views.set_apply, name="apply"),
]
