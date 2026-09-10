from allauth.account.adapter import DefaultAccountAdapter
from allauth.socialaccount.adapter import DefaultSocialAccountAdapter
from django.conf import settings

from apps.accounts.models import OAuthConnection


def _signup_allowed(request) -> bool:
    if settings.REGISTRATION_OPEN:
        return True
    token = getattr(request, "session", {}).get("pending_invite_token")
    if not token:
        return False
    from apps.members.models import Invitation

    invitation = Invitation.objects.filter(token=token, accepted_at__isnull=True).first()
    return bool(invitation and not invitation.is_expired)


class AccountAdapter(DefaultAccountAdapter):
    """Close public registration while keeping explicit invitation signup working."""

    def is_open_for_signup(self, request):
        return _signup_allowed(request)

    def send_mail(self, template_prefix, email, context):
        branded = {
            **context,
            "brand_name": settings.BRAND_NAME,
            "brand_short_name": settings.BRAND_SHORT_NAME,
            "brand_terms_url": settings.BRAND_TERMS_URL,
            "brand_privacy_url": settings.BRAND_PRIVACY_URL,
        }
        return super().send_mail(template_prefix, email, branded)


class SocialAccountAdapter(DefaultSocialAccountAdapter):
    """Custom adapter that syncs Google social logins to OAuthConnection."""

    def is_open_for_signup(self, request, sociallogin):
        return _signup_allowed(request)

    def populate_user(self, request, sociallogin, data):
        """Set user.name from Google profile (custom User model has 'name', not first/last)."""
        user = super().populate_user(request, sociallogin, data)
        first_name = data.get("first_name", "")
        last_name = data.get("last_name", "")
        full_name = f"{first_name} {last_name}".strip()
        if full_name and not user.name:
            user.name = full_name
        return user

    def save_user(self, request, sociallogin, form=None):
        """Create OAuthConnection after saving a new social signup."""
        user = super().save_user(request, sociallogin, form)
        self._sync_oauth_connection(user, sociallogin)
        return user

    def pre_social_login(self, request, sociallogin):
        """Sync OAuthConnection for returning users and auto-connected accounts."""
        super().pre_social_login(request, sociallogin)
        if sociallogin.is_existing:
            self._sync_oauth_connection(sociallogin.user, sociallogin)

    def _sync_oauth_connection(self, user, sociallogin):
        account = sociallogin.account
        if account.provider != "google":
            return
        provider_email = ""
        for ea in sociallogin.email_addresses:
            provider_email = ea.email
            break
        OAuthConnection.objects.update_or_create(
            provider=OAuthConnection.Provider.GOOGLE,
            provider_user_id=account.uid,
            defaults={"user": user, "provider_email": provider_email},
        )
