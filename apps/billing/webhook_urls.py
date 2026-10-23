from django.urls import path

from .views import stripe_webhook

urlpatterns = [
    path("stripe/", stripe_webhook, name="stripe_webhook"),
]
