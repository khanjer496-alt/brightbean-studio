from django.db import migrations


def verify_emails_of_existing_logins(apps, schema_editor):
    """Mark the address of every user who has signed in before as verified.

    ACCOUNT_EMAIL_VERIFICATION goes from "none" to "mandatory" alongside this,
    and allauth refuses to sign in anyone whose address is unverified. Until
    now nothing ever verified a password signup, so without this every
    existing email/password user would be stopped at their next login and
    sent a confirmation email — a lockout wherever mail is not configured.
    Users who never signed in are left alone: their address has proven
    nothing yet, and they confirm it on their first login like a new signup.

    An address another user has already verified is skipped rather than
    violating allauth's unique_verified_email constraint.
    """
    User = apps.get_model("accounts", "User")
    EmailAddress = apps.get_model("account", "EmailAddress")

    for user in User.objects.filter(last_login__isnull=False).only("pk", "email").iterator():
        email = user.email.lower()
        if EmailAddress.objects.filter(email__iexact=email, verified=True).exclude(user_id=user.pk).exists():
            continue
        address = EmailAddress.objects.filter(user_id=user.pk, email__iexact=email).first()
        if address is None:
            EmailAddress.objects.create(
                user_id=user.pk,
                email=email,
                verified=True,
                primary=not EmailAddress.objects.filter(user_id=user.pk, primary=True).exists(),
            )
        elif not address.verified:
            address.verified = True
            address.save(update_fields=["verified"])


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0005_add_review_banner_dismissed_at"),
        ("account", "0009_emailaddress_unique_primary_email"),
    ]

    operations = [
        migrations.RunPython(verify_emails_of_existing_logins, migrations.RunPython.noop),
    ]
