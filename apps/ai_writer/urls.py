from django.urls import path

from . import views

app_name = "ai_writer"

urlpatterns = [
    # Composer AI panel (HTMX endpoints)
    path("write/", views.write, name="write"),
    path("improve/", views.improve, name="improve"),
    path("adapt/", views.adapt, name="adapt"),
    path("hashtags/", views.hashtags, name="hashtags"),
    path("link/", views.link, name="link"),
    path("link/save/", views.link_save, name="link_save"),
    # JSON: long text → numbered thread parts (for the thread composer)
    path("thread/", views.thread, name="thread"),
]
