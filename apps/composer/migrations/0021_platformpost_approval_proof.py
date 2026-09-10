from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("composer", "0020_platformpost_first_comment_state")]

    operations = [
        migrations.AddField(
            model_name="platformpost",
            name="approval_completed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="platformpost",
            name="approval_fingerprint",
            field=models.CharField(blank=True, default="", max_length=64),
        ),
    ]
