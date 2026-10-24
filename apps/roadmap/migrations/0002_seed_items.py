from django.db import migrations

ITEMS = [
    ("now", "More networks", "Adding more social and chat networks to publish to, one at a time as each platform allows."),
    ("now", "Arabic and right-to-left layout", "The layout already mirrors for Arabic. Translating the interface itself is the next step."),
    ("next", "Mobile app (PWA)", "An installable mobile version for approving posts and replying on the go."),
    ("next", "Report builder", "Build your own client reports from the analytics you choose."),
    ("later", "White-label for agencies", "Your logo, colors and domain on the client portal and reports."),
    ("later", "More interface languages", "Further languages beyond English and Arabic."),
]


def seed(apps, schema_editor):
    RoadmapItem = apps.get_model("roadmap", "RoadmapItem")
    if RoadmapItem.objects.exists():
        return
    for order, (status, title, description) in enumerate(ITEMS, start=1):
        RoadmapItem.objects.create(status=status, title=title, description=description, order=order * 10)


class Migration(migrations.Migration):
    dependencies = [("roadmap", "0001_initial")]

    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
