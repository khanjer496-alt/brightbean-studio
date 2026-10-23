from django.urls import path

from . import views

app_name = "ai_writer_org"

urlpatterns = [
    path("", views.org_settings, name="settings"),
]
