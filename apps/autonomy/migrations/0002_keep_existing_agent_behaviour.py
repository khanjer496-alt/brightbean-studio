"""Workspaces that already run agents keep working as they do today.

New workspaces start conservative (agents draft, people approve). A workspace
that already has an Agent API key has agents publishing and replying on their
own, so switching it to "draft only" overnight would silently stop that. Those
workspaces get a policy at "Full auto" with no undo delay, which changes
nothing about how they behave; an owner can tighten it from Settings.
"""

from django.db import migrations


def keep_existing_behaviour(apps, schema_editor):
    ApiKey = apps.get_model("api_keys", "ApiKey")
    AutonomyPolicy = apps.get_model("autonomy", "AutonomyPolicy")
    workspace_ids = ApiKey.objects.filter(revoked_at__isnull=True).values_list("workspace_id", flat=True).distinct()
    AutonomyPolicy.objects.bulk_create(
        [
            AutonomyPolicy(
                workspace_id=workspace_id,
                draft_level="full_auto",
                publish_level="full_auto",
                reply_level="full_auto",
                undo_delay_minutes=0,
            )
            for workspace_id in workspace_ids
        ],
        ignore_conflicts=True,
    )


class Migration(migrations.Migration):
    dependencies = [
        ("autonomy", "0001_initial"),
        ("api_keys", "0001_initial"),
    ]

    operations = [migrations.RunPython(keep_existing_behaviour, migrations.RunPython.noop)]
