from django.urls import path

from . import views

app_name = "billing"

urlpatterns = [
    path("", views.overview, name="overview"),
    path("checkout/", views.checkout, name="checkout"),
    path("portal/", views.portal, name="portal"),
]
