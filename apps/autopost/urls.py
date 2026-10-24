"""``/workspace/<id>/autopost/…`` (not ``feeds/``, which is the composer's Feeds tab)."""

from django.urls import path

from . import views

app_name = "autopost"

urlpatterns = [
    path("", views.home, name="home"),
    path("add/", views.feed_add, name="add"),
    path("<uuid:feed_id>/", views.detail, name="detail"),
    path("<uuid:feed_id>/save/", views.save, name="save"),
    path("<uuid:feed_id>/toggle/", views.toggle, name="toggle"),
    path("<uuid:feed_id>/check/", views.check_now, name="check"),
    path("<uuid:feed_id>/stop/", views.stop, name="stop"),
]
