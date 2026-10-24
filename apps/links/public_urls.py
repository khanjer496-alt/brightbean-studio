"""Public short links: ``/s/<code>``. No login."""

from django.urls import path

from . import public_views

app_name = "links_public"

urlpatterns = [
    path("<str:code>", public_views.follow, name="follow"),
]
