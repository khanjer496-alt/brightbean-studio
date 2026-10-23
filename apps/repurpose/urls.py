"""``/workspace/<id>/repurpose/…``."""

from django.urls import path

from . import views

app_name = "repurpose"

urlpatterns = [
    path("", views.home, name="home"),
    path("new/", views.job_create, name="create"),
    path("<uuid:job_id>/", views.job_detail, name="detail"),
    path("<uuid:job_id>/status/", views.job_status, name="status"),
    path("<uuid:job_id>/cancel/", views.job_cancel, name="cancel"),
    path("<uuid:job_id>/accept/", views.job_accept, name="accept"),
    path("<uuid:job_id>/outputs/<uuid:output_id>/discard/", views.output_discard, name="discard"),
    path("<uuid:job_id>/outputs/<uuid:output_id>/pdf/", views.output_pdf, name="pdf"),
]
