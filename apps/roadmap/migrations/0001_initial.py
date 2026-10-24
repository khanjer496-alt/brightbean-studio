from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name="RoadmapItem",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("title", models.CharField(max_length=120)),
                ("description", models.CharField(blank=True, max_length=300)),
                (
                    "status",
                    models.CharField(
                        choices=[("now", "Now"), ("next", "Next"), ("later", "Later")], default="later", max_length=10
                    ),
                ),
                (
                    "order",
                    models.PositiveIntegerField(default=0, help_text="Lower numbers show first within a column."),
                ),
                ("link_url", models.URLField(blank=True, help_text="Optional page with more detail.")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={"db_table": "roadmap_item", "ordering": ["order", "id"]},
        ),
    ]
