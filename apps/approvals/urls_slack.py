from django.urls import path

from . import views_slack

app_name = "approval_slack"

urlpatterns = [
    path("interactions/", views_slack.interactions, name="interactions"),
    path("callback/", views_slack.callback, name="callback"),
]
