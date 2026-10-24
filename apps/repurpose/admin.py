from django.contrib import admin

from .models import RepurposeJob


@admin.register(RepurposeJob)
class RepurposeJobAdmin(admin.ModelAdmin):
    list_display = ("id", "workspace", "source_type", "status", "calls_made", "created_at")
    list_filter = ("status", "source_type")
    readonly_fields = [f.name for f in RepurposeJob._meta.fields if f.name != "id"]
    exclude = ("source_text",)
