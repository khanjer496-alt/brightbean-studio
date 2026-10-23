"""Self-service account deletion: who may delete, what goes, what stays."""

import hashlib
import logging

from allauth.mfa.models import Authenticator
from allauth.mfa.recovery_codes.internal.auth import RecoveryCodes
from allauth.mfa.totp.internal.auth import TOTP
from allauth.mfa.utils import is_mfa_enabled
from django.core import signing
from django.db import transaction
from django.utils.crypto import constant_time_compare, salted_hmac

from apps.members.models import OrgMembership
from apps.social_accounts.models import SocialAccount
from apps.social_accounts.removal import remove_account, remove_accounts_in_organization

logger = logging.getLogger(__name__)

CONFIRMATION_PHRASE = "DELETE MY ACCOUNT"
# A person who signs in only with Google has no password to re-enter, and
# allauth cannot re-authenticate through the provider. Typing an email address
# proves nothing, so they confirm through a link sent to that address instead.
EMAIL_LINK_MAX_AGE_SECONDS = 60 * 60
_EMAIL_LINK_SALT = "apps.accounts.deletion.email-link"


def needs_password(user) -> bool:
    return user.has_usable_password()


def needs_authenticator_code(user) -> bool:
    return is_mfa_enabled(user)


def _valid_second_factor(user, code: str) -> bool:
    code = (code or "").strip().replace(" ", "")
    if not code:
        return False
    totp = Authenticator.objects.filter(user=user, type=Authenticator.Type.TOTP).first()
    if totp is not None and TOTP(totp).validate_code(code):
        return True
    recovery = Authenticator.objects.filter(user=user, type=Authenticator.Type.RECOVERY_CODES).first()
    return recovery is not None and RecoveryCodes(recovery).validate_code(code)


def reauthentication_error(user, *, password="", code="", phrase="") -> str:
    """Return why the request cannot proceed, or an empty string when it can.

    Users without a password pass this step with the phrase and second factor
    alone; the emailed link is then their proof of identity (``email_link_token``).
    """
    if (phrase or "").strip() != CONFIRMATION_PHRASE:
        return f"Type {CONFIRMATION_PHRASE} exactly to confirm."
    if needs_password(user) and not user.check_password(password or ""):
        return "Your password is incorrect."
    if needs_authenticator_code(user) and not _valid_second_factor(user, code):
        return "Enter a valid authenticator or recovery code."
    return ""


def _account_fingerprint(user) -> str:
    # Changing the email or password invalidates links already sent.
    return salted_hmac(_EMAIL_LINK_SALT, f"{user.pk}:{user.email}:{user.password}").hexdigest()


def email_link_token(user) -> str:
    return signing.dumps({"u": str(user.pk), "f": _account_fingerprint(user)}, salt=_EMAIL_LINK_SALT)


def email_link_is_valid(user, token: str) -> bool:
    """True for an unexpired link issued to ``user``, the signed-in user."""
    try:
        data = signing.loads(token, salt=_EMAIL_LINK_SALT, max_age=EMAIL_LINK_MAX_AGE_SECONDS)
    except signing.BadSignature:
        return False
    return (
        isinstance(data, dict)
        and constant_time_compare(str(data.get("u", "")), str(user.pk))
        and constant_time_compare(str(data.get("f", "")), _account_fingerprint(user))
    )


def _organizations(user):
    return [m.organization for m in OrgMembership.objects.filter(user=user).select_related("organization")]


def _only_member(organization) -> bool:
    return OrgMembership.objects.filter(organization=organization).count() == 1


def organizations_deleted_with_account(user):
    """Organizations the user is the only member of: they go with the account."""
    return [org for org in _organizations(user) if _only_member(org)]


def subscriptions_cancelled_with_account(user):
    """Paid subscriptions of the organizations deleted with the account; they are cancelled first."""
    subscriptions = []
    for org in organizations_deleted_with_account(user):
        subscription = getattr(org, "billing_subscription", None)
        if subscription is not None and subscription.has_paid_subscription:
            subscriptions.append(subscription)
    return subscriptions


def blockers(user) -> list[str]:
    """Reasons the account cannot be deleted yet, in words the person can act on."""
    reasons = []
    solo = {org.pk for org in organizations_deleted_with_account(user)}
    for membership in OrgMembership.objects.filter(user=user, org_role=OrgMembership.OrgRole.OWNER).select_related(
        "organization"
    ):
        org = membership.organization
        if org.pk in solo:
            continue
        other_owner = (
            OrgMembership.objects.filter(organization=org, org_role=OrgMembership.OrgRole.OWNER)
            .exclude(user=user)
            .exists()
        )
        if not other_owner:
            reasons.append(
                f"You are the only owner of {org.name}, which has other members. "
                "Make someone else an owner, or remove them, first."
            )
    return reasons


class SubscriptionCancelError(Exception):
    """Stripe would not cancel a subscription, so nothing was deleted."""


def _cancel_subscriptions(user) -> None:
    from apps.billing import services as billing

    for subscription in subscriptions_cancelled_with_account(user):
        try:
            billing.cancel_subscription_now(subscription.organization)
        except Exception as exc:
            logger.exception("Could not cancel subscription %s before account deletion", subscription.pk)
            raise SubscriptionCancelError(subscription.organization.name) from exc


def _cancel_scheduled_org_deletion(org) -> None:
    from background_task.models import Task

    Task.objects.filter(
        task_name="apps.organizations.tasks.execute_scheduled_org_deletion",
        task_params__contains=str(org.id),
    ).delete()


def delete_account(user) -> None:
    """Cancel billing, revoke platform access, then delete the user and what only they held.

    Organizations where the user is the only member are deleted with all their
    content and uploaded files, after their paid subscription is cancelled. In
    organizations with other members, channels and posts stay with the
    workspace and lose their author. Either way the user's memberships,
    notifications, sessions on every device, OAuth applications and grants to
    third-party apps, and sign-in data go. Channels the user authorised for
    YouTube are removed, since that grant is theirs.

    Raises ``SubscriptionCancelError`` before deleting anything if Stripe
    refuses a cancellation, so nobody is billed for a deleted organization.
    """
    if blockers(user):
        raise ValueError("Account deletion is blocked.")

    _cancel_subscriptions(user)

    solo_orgs = organizations_deleted_with_account(user)
    for org in solo_orgs:
        remove_accounts_in_organization(org)
    for account in SocialAccount.objects.filter(youtube_authorized_by=user):
        remove_account(account)

    # Audit trail without personal data: the email is hashed so a later
    # "was X deleted?" question can be answered without keeping X.
    email_digest = hashlib.sha256(user.email.strip().lower().encode()).hexdigest()
    avatar = (user.avatar.storage, user.avatar.name) if user.avatar else None
    user_id = user.pk
    with transaction.atomic():
        for org in solo_orgs:
            _cancel_scheduled_org_deletion(org)
            org.hard_delete()
        # CASCADE removes accounts.Session rows, allauth sign-in data, MFA,
        # memberships and oauth2_provider applications and tokens. Django
        # sessions on other devices stop resolving to a user.
        user.delete()
        if avatar:
            transaction.on_commit(lambda: _delete_file(*avatar))
    logger.info(
        "Account %s deleted by its owner (email sha256 %s, %d organizations)", user_id, email_digest, len(solo_orgs)
    )


def _delete_file(storage, name) -> None:
    try:
        storage.delete(name)
    except Exception:
        logger.warning("Could not delete the avatar of a deleted account")
