from allauth.account.adapter import DefaultAccountAdapter
from allauth.socialaccount.adapter import DefaultSocialAccountAdapter
from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.db import transaction

from apps.accounts.models import OAuthConnection


def _signup_invitation(request, *, email=None, lock=False):
    """Resolve invitation policy again at save time, optionally locking the row."""
    from apps.members.models import Invitation

    token = getattr(request, "session", {}).get("pending_invite_token")
    if not token:
        if settings.REGISTRATION_OPEN:
            return None
        raise PermissionDenied("Registration requires a valid invitation.")
    invitations = Invitation.objects.all()
    if lock:
        invitations = invitations.select_for_update()
    invitation = invitations.filter(token=token, accepted_at__isnull=True).first()
    if not invitation or invitation.is_expired:
        raise PermissionDenied("This invitation is no longer valid.")
    if email is not None and email.strip().lower() != invitation.email.strip().lower():
        raise PermissionDenied("Use the email address this invitation was sent to.")
    return invitation


def _signup_allowed(request, *, email=None) -> bool:
    try:
        _signup_invitation(request, email=email)
    except PermissionDenied:
        return False
    return True


def _finish_signup_membership(request, user, invitation):
    from apps.accounts.signals import provision_organization_and_workspace
    from apps.members.services import accept_invitation

    if invitation is None:
        provision_organization_and_workspace(user)
    else:
        try:
            accept_invitation(invitation, user)
        except ValueError as exc:
            raise PermissionDenied("This invitation can no longer be accepted.") from exc
    # The later allauth signal must not consume the same invitation twice.
    request._signup_membership_user_id = user.pk
    request._signup_invited = invitation is not None
    request.session.pop("pending_invite_token", None)


class AccountAdapter(DefaultAccountAdapter):
    """Close public registration while keeping explicit invitation signup working."""

    def is_open_for_signup(self, request):
        return _signup_allowed(request)

    @transaction.atomic
    def save_user(self, request, user, form, commit=True):
        # A social form delegates user persistence here; the outer social
        # adapter owns its invitation transaction and membership claim.
        if not commit or getattr(user, "_social_signup_managed", False):
            return super().save_user(request, user, form, commit=commit)
        invitation = _signup_invitation(request, email=form.cleaned_data.get("email", ""), lock=True)
        user._signup_managed = True
        user = super().save_user(request, user, form, commit=True)
        _finish_signup_membership(request, user, invitation)
        return user

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
        return _signup_allowed(request, email=sociallogin.user.email or "")

    def populate_user(self, request, sociallogin, data):
        """Set user.name from Google profile (custom User model has 'name', not first/last)."""
        user = super().populate_user(request, sociallogin, data)
        first_name = data.get("first_name", "")
        last_name = data.get("last_name", "")
        full_name = f"{first_name} {last_name}".strip()
        if full_name and not user.name:
            user.name = full_name
        return user

    @transaction.atomic
    def save_user(self, request, sociallogin, form=None):
        """Commit the user, social identity and invitation claim together."""
        # The authenticated provider identity must match too: a form may not
        # relabel a different social identity as the invited recipient.
        invitation = _signup_invitation(request, email=sociallogin.user.email or "", lock=True)
        sociallogin.user._signup_managed = True
        sociallogin.user._social_signup_managed = True
        user = super().save_user(request, sociallogin, form)
        _finish_signup_membership(request, user, invitation)
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
