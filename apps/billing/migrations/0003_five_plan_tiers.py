from django.db import migrations, models

# The four-plan lineup (Free, Creator, Team, Agency) became five tiers. No
# subscription was live on the old slugs; any row that still has one moves to
# the nearest new tier instead of silently dropping to Free.
_RENAMED = {"creator": "starter", "agency": "ultimate"}


def rename_plans(apps, schema_editor):
    Subscription = apps.get_model("billing", "Subscription")
    for old, new in _RENAMED.items():
        Subscription.objects.filter(plan=old).update(plan=new)


class Migration(migrations.Migration):
    dependencies = [
        ("billing", "0002_unpaid_paused_statuses"),
    ]

    operations = [
        migrations.AlterField(
            model_name="subscription",
            name="plan",
            field=models.CharField(
                choices=[
                    ("free", "Free"),
                    ("starter", "Starter"),
                    ("team", "Team"),
                    ("pro", "Pro"),
                    ("ultimate", "Ultimate"),
                ],
                default="free",
                max_length=20,
            ),
        ),
        migrations.RunPython(rename_plans, migrations.RunPython.noop),
    ]
