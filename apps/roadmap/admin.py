from django.contrib import admin

from .models import RoadmapItem


@admin.register(RoadmapItem)
class RoadmapItemAdmin(admin.ModelAdmin):
    list_display = ("title", "status", "order", "updated_at")
    list_filter = ("status",)
    list_editable = ("status", "order")
    search_fields = ("title", "description")
