"""Workspace webhook settings, mounted at ``/workspace/<id>/webhooks/``."""

from django.urls import path

from . import views

app_name = "webhooks"

urlpatterns = [
    path("", views.endpoint_list, name="list"),
    path("new/", views.endpoint_create, name="create"),
    path("<uuid:endpoint_id>/", views.endpoint_detail, name="detail"),
    path("<uuid:endpoint_id>/update/", views.endpoint_update, name="update"),
    path("<uuid:endpoint_id>/rotate-secret/", views.endpoint_rotate_secret, name="rotate_secret"),
    path("<uuid:endpoint_id>/test/", views.endpoint_test, name="test"),
    path("<uuid:endpoint_id>/delete/", views.endpoint_delete, name="delete"),
    path("<uuid:endpoint_id>/deliveries/<uuid:delivery_id>/resend/", views.delivery_resend, name="resend"),
]
