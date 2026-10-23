"""Staff URLs: ``/workspace/<id>/analytics/reports/…``."""

from django.urls import path

from . import views

app_name = "reports"

urlpatterns = [
    path("", views.report_list, name="list"),
    path("new/", views.report_create, name="create"),
    path("preview/", views.report_preview, name="preview"),
    path("runs/<uuid:run_id>/", views.run_detail, name="run_detail"),
    path("runs/<uuid:run_id>/pdf/", views.run_pdf, name="run_pdf"),
    path("runs/<uuid:run_id>/share/", views.run_share, name="run_share"),
    path("<uuid:report_id>/", views.report_edit, name="edit"),
    path("<uuid:report_id>/delete/", views.report_delete, name="delete"),
    path("<uuid:report_id>/run/", views.report_run, name="run"),
    path("<uuid:report_id>/history/", views.report_history, name="history"),
]
