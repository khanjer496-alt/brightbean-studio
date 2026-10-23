"""Editor URLs: ``/workspace/<id>/bio/…``."""

from django.urls import path

from . import views

app_name = "linkinbio"

urlpatterns = [
    path("", views.page_list, name="list"),
    path("new/", views.page_create, name="create"),
    path("<uuid:page_id>/", views.page_edit, name="edit"),
    path("<uuid:page_id>/save/", views.page_save, name="save"),
    path("<uuid:page_id>/delete/", views.page_delete, name="delete"),
    path("<uuid:page_id>/links/add/", views.link_add, name="link_add"),
    path("<uuid:page_id>/links/<uuid:link_id>/", views.link_update, name="link_update"),
    path("<uuid:page_id>/links/<uuid:link_id>/delete/", views.link_delete, name="link_delete"),
    path("<uuid:page_id>/links/<uuid:link_id>/move/", views.link_move, name="link_move"),
    path("<uuid:page_id>/posts/<uuid:post_id>/", views.post_link_save, name="post_link"),
]
