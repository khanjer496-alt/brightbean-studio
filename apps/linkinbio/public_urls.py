"""Public pages: ``/l/<slug>/``. No login."""

from django.urls import path

from . import public_views

app_name = "linkinbio_public"

urlpatterns = [
    path("<slug:slug>/", public_views.public_page, name="page"),
    path("<slug:slug>/theme.css", public_views.theme_css, name="theme"),
    path("<slug:slug>/click/", public_views.click, name="click"),
]
