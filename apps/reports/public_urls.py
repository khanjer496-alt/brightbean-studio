"""Shareable report links: ``/reports/share/<token>/``. No login; the signed token is the credential."""

from django.urls import path

from . import views

app_name = "reports_public"

urlpatterns = [
    path("<str:token>/", views.public_report, name="view"),
    path("<str:token>/pdf/", views.public_pdf, name="pdf"),
]
