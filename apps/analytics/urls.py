"""URLs for the analytics page."""

from django.urls import path

from . import views

app_name = "analytics"

urlpatterns = [
    path("<uuid:account_id>/status/", views.analytics_status, name="status"),
    path("", views.analytics_index, name="index"),
    path("accounts/", views.analytics_accounts, name="accounts"),
    path("compare/", views.analytics_compare, name="compare"),
    path("export/<slug:kind>.csv", views.analytics_export, name="export"),
    path("post/<uuid:post_id>/", views.post_detail, name="post_detail"),
    path("<uuid:account_id>/", views.analytics_account, name="account"),
]
