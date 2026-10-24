from django.urls import path

from . import views

app_name = "legal"

urlpatterns = [
    path("terms/", views.terms, name="terms"),
    path("privacy/", views.privacy, name="privacy"),
    path("data-deletion/", views.data_deletion, name="data_deletion"),
    path("data-deletion/meta/", views.meta_data_deletion_callback, name="meta_data_deletion"),
    path("data-deletion/status/<str:code>/", views.data_deletion_status, name="data_deletion_status"),
    path("security/", views.security, name="security"),
    path("dpa/", views.dpa, name="dpa"),
    path("licenses/", views.licenses, name="licenses"),
    path("subprocessors/", views.subprocessors_list, name="subprocessors"),
]
