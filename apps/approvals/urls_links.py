from django.urls import path

from . import views_links

app_name = "approval_links"

urlpatterns = [
    path("media/<str:token>/", views_links.email_media, name="media"),
    path("<str:token>/", views_links.confirm, name="confirm"),
    path("<str:token>/resend/", views_links.resend, name="resend"),
]
