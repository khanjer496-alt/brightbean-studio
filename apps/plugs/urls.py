"""``/workspace/<id>/automations/…``."""

from django.urls import path

from . import views

app_name = "plugs"

urlpatterns = [
    path("", views.home, name="home"),
    path("rules/add/", views.rule_add, name="rule_add"),
    path("rules/<uuid:rule_id>/toggle/", views.rule_toggle, name="rule_toggle"),
    path("rules/<uuid:rule_id>/delete/", views.rule_delete, name="rule_delete"),
    path("post/<uuid:post_id>/", views.composer_panel, name="composer_panel"),
]
