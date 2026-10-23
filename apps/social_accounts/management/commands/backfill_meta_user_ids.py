"""Record the Meta user behind connections made before ``meta_user_id`` existed.

Meta's data deletion callback names the app-scoped *user*, while a Facebook
Page or Instagram professional account is stored under its own id, so older
connections cannot be matched to a deletion request. ``/debug_token`` reports
which user granted a token, Page tokens included.

    python manage.py backfill_meta_user_ids            # dry run
    python manage.py backfill_meta_user_ids --apply

Connections whose provider cannot inspect tokens (Threads, Instagram Login)
are skipped: their stored account id already is the id Meta sends.
"""

from django.core.management.base import BaseCommand

from apps.legal.meta_deletion import META_PLATFORMS
from apps.social_accounts.models import SocialAccount


class Command(BaseCommand):
    help = "Fill SocialAccount.meta_user_id for Meta connections made before it was recorded."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Save the ids (default is a dry run).")

    def handle(self, *args, **options):
        from apps.publisher.engine import _resolve_publish_credentials
        from providers import get_provider

        found = skipped = 0
        accounts = SocialAccount.objects.filter(platform__in=META_PLATFORMS, meta_user_id="").select_related(
            "workspace"
        )
        for account in accounts.iterator():
            if not account.oauth_access_token:
                skipped += 1
                continue
            try:
                provider = get_provider(account.platform, _resolve_publish_credentials(account))
                user_id = str(provider.debug_token(account.oauth_access_token).get("user_id") or "")
            except NotImplementedError:
                skipped += 1
                continue
            except Exception as exc:
                self.stderr.write(f"{account.pk} ({account.platform}): {type(exc).__name__}")
                skipped += 1
                continue
            if not user_id:
                skipped += 1
                continue
            found += 1
            if options["apply"]:
                SocialAccount.objects.filter(pk=account.pk, meta_user_id="").update(meta_user_id=user_id)
        verb = "Recorded" if options["apply"] else "Would record"
        self.stdout.write(f"{verb} {found} Meta user ids; skipped {skipped}.")
