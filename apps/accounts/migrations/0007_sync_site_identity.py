from django.conf import settings
from django.db import migrations


def set_site(apps, schema_editor):
    """Replace 0004's hard-coded studio.brightbean.xyz Site with this deployment's.

    Read from settings (APP_URL / BRAND_NAME) rather than hard-coded, so a
    PostDelegate deploy gets its own domain and name and a self-hosted one
    gets whatever it configures. AccountsConfig re-syncs after every migrate.
    """
    Site = apps.get_model("sites", "Site")
    Site.objects.update_or_create(
        id=settings.SITE_ID,
        defaults={"domain": settings.SITE_DOMAIN, "name": settings.SITE_NAME},
    )


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0006_verify_emails_of_existing_logins"),
        ("sites", "0002_alter_domain_unique"),
    ]
    operations = [migrations.RunPython(set_site, migrations.RunPython.noop)]
