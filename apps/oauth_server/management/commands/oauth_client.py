"""Register and rotate confidential OAuth clients: a fixed client id and secret, for hosts that cannot use dynamic
registration (Microsoft Copilot Studio and Power Platform connectors need one for certification).

    manage.py oauth_client create --name "Microsoft Copilot Studio" --redirect-uri https://global.consent.azure-apim.net/redirect/...
    manage.py oauth_client rotate --client-id <id>
    manage.py oauth_client list

The secret is printed once and stored hashed (django-oauth-toolkit hashes it on save); ``rotate`` issues a new one and
the old one stops working at once. These clients go through the same consent screen as any other, where they are
shown as verified. PKCE is optional for them (apps.oauth_server.validator) and S256-only when sent.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from oauth2_provider.generators import generate_client_secret
from oauth2_provider.models import get_application_model

from apps.oauth_server.views import is_allowed_redirect_uri


class Command(BaseCommand):
    help = "Create, rotate or list confidential OAuth clients (static client id and secret)."

    def add_arguments(self, parser):
        sub = parser.add_subparsers(dest="action", required=True)
        create = sub.add_parser("create", help="Register a confidential client and print its secret once.")
        create.add_argument("--name", required=True)
        create.add_argument("--redirect-uri", action="append", required=True, dest="redirect_uris")
        rotate = sub.add_parser("rotate", help="Replace a confidential client's secret and print the new one once.")
        rotate.add_argument("--client-id", required=True)
        sub.add_parser("list", help="List confidential clients (never their secrets).")

    def handle(self, *args, **options):
        return getattr(self, f"_{options['action']}")(options)

    def _create(self, options):
        uris = options["redirect_uris"]
        bad = [u for u in uris if not is_allowed_redirect_uri(u)]
        if bad:
            raise CommandError(f"Not an https (or loopback http) redirect URI: {', '.join(bad)}")
        model = get_application_model()
        secret = generate_client_secret()
        app = model.objects.create(
            name=options["name"][:255],
            client_type=model.CLIENT_CONFIDENTIAL,
            authorization_grant_type=model.GRANT_AUTHORIZATION_CODE,
            redirect_uris=" ".join(uris),
            client_secret=secret,
            hash_client_secret=True,
            skip_authorization=False,
        )
        self._print_secret(app.client_id, secret)

    def _rotate(self, options):
        model = get_application_model()
        app = model.objects.filter(client_id=options["client_id"], client_type=model.CLIENT_CONFIDENTIAL).first()
        if app is None:
            raise CommandError("No confidential client has that client id.")
        secret = generate_client_secret()
        app.client_secret = secret
        app.hash_client_secret = True
        app.save(update_fields=["client_secret", "hash_client_secret", "updated"])
        self._print_secret(app.client_id, secret)

    def _list(self, options):
        model = get_application_model()
        for app in model.objects.filter(client_type=model.CLIENT_CONFIDENTIAL).order_by("created"):
            self.stdout.write(f"{app.client_id}\t{app.name}\t{app.redirect_uris}")

    def _print_secret(self, client_id: str, secret: str) -> None:
        self.stdout.write(f"client_id:     {client_id}")
        self.stdout.write(f"client_secret: {secret}")
        self.stdout.write(self.style.WARNING("Store the secret now: it is stored hashed and is not shown again."))
