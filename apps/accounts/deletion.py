"""Self-service account deletion: who may delete, what goes, what stays."""

import logging

from allauth.mfa.models import Authenticator
from allauth.mfa.recovery_codes.internal.auth import RecoveryCodes
from allauth.mfa.totp.internal.auth import TOTP
from allauth.mfa.utils import is_mfa_enabled
from django.db import transaction

from apps.members.models import OrgMembership
from apps.social_accounts.models import SocialAccount
from apps.social_accounts.removal import remove_account, remove_accounts_in_organization

logger = logging.getLogger(__name__)

CONFIRMATION_PHRASE = "DELETE MY ACCOUNT"


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


def reauthentication_error(user, *, password="", code="", email="", phrase="") -> str:
    """Return why the request cannot proceed, or an empty string when it can."""
    if (phrase or "").strip() != CONFIRMATION_PHRASE:
        return f"Type {CONFIRMATION_PHRASE} exactly to confirm."
    if needs_password(user):
        if not user.check_password(password or ""):
            return "Your password is incorrect."
    elif (email or "").strip().lower() != user.email.lower():
        return "Type your account email address to confirm."
    if needs_authenticator_code(user) and not _valid_second_factor(user, code):
        return "Enter a valid authenticator or recovery code."
    return ""


def _organizations(user):
    return [m.organization for m in OrgMembership.objects.filter(user=user).select_related("organization")]


def _only_member(organization) -> bool:
    return OrgMembership.objects.filter(organization=organization).count() == 1


def organizations_deleted_with_account(user):
    """Organizations the user is the only member of: they go with the account."""
    return [org for org in _organizations(user) if _only_member(org)]


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
    for org in organizations_deleted_with_account(user):
        subscription = getattr(org, "billing_subscription", None)
        if subscription is not None and subscription.has_paid_subscription and not subscription.cancel_at_period_end:
            reasons.append(f"{org.name} has an active paid subscription. Cancel it under Plan & billing first.")
    return reasons


def delete_account(user) -> None:
    """Revoke platform access, then delete the user and what only they held.

    The rule: organizations where the user is the only member are deleted with
    all their content. In organizations with other members, channels and posts
    stay with the workspace and lose their author, and the user's membership,
    notifications, sessions and sign-in data are deleted. Channels the user
    authorised for YouTube are removed, since that grant is theirs.
    """
    if blockers(user):
        raise ValueError("Account deletion is blocked.")

    solo_orgs = organizations_deleted_with_account(user)
    for org in solo_orgs:
        remove_accounts_in_organization(org)
    for account in SocialAccount.objects.filter(youtube_authorized_by=user):
        remove_account(account)

    from apps.accounts.models import User

    with transaction.atomic():
        workspace_ids = []
        for org in solo_orgs:
            workspace_ids += list(org.workspaces.values_list("id", flat=True))
            org.delete()
        User.objects.filter(last_workspace_id__in=workspace_ids).update(last_workspace_id=None)
        user.delete()
