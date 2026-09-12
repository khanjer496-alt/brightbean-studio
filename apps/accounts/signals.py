from allauth.account.signals import user_signed_up
from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.utils import timezone


def provision_organization_and_workspace(user):
    """Create a default Organization, Workspace, and memberships for a new user.

    Skips if the user already belongs to an organization (e.g. invited users).
    Safe to call multiple times - the guard is idempotent.
    """
    from apps.members.models import OrgMembership, WorkspaceMembership
    from apps.organizations.models import Organization
    from apps.workspaces.models import Workspace

    # Skip if user was invited to an existing org or already provisioned
    if OrgMembership.objects.filter(user=user).exists():
        return

    org = Organization.objects.create(
        name="My Organization",
        default_timezone="UTC",
    )

    OrgMembership.objects.create(
        user=user,
        organization=org,
        org_role=OrgMembership.OrgRole.OWNER,
    )

    # Create a default workspace so the user can start immediately
    workspace = Workspace.objects.create(
        organization=org,
        name="My Workspace",
        description="Your default workspace. Rename it anytime.",
    )

    WorkspaceMembership.objects.create(
        user=user,
        workspace=workspace,
        workspace_role=WorkspaceMembership.WorkspaceRole.OWNER,
    )

    # Set as last workspace so dashboard redirects here
    user.last_workspace_id = workspace.id
    user.save(update_fields=["last_workspace_id"])


def _set_org_timezone_from_browser(request, user):
    """Update the user's organization timezone from the browser_timezone cookie."""
    from urllib.parse import unquote
    from zoneinfo import available_timezones

    from apps.members.models import OrgMembership

    browser_tz = request.COOKIES.get("browser_timezone", "")
    # Defensive length cap: IANA timezone names are all under 40 chars.
    # Reject anything suspiciously long before decoding/validating.
    if not browser_tz or len(browser_tz) > 64:
        return
    browser_tz = unquote(browser_tz)
    if len(browser_tz) > 64 or browser_tz not in available_timezones():
        return
    membership = OrgMembership.objects.filter(user=user).select_related("organization").first()
    if membership and membership.organization.default_timezone == "UTC":
        membership.organization.default_timezone = browser_tz
        membership.organization.save(update_fields=["default_timezone"])


@receiver(user_signed_up)
def create_organization_on_signup(sender, request, user, **kwargs):
    """Finalize signup metadata after the adapter atomically provisions membership."""
    if getattr(request, "_signup_membership_user_id", None) != user.pk:
        # Unknown signup paths must never fall back to creating an owner
        # organization when public registration is closed or an invite failed.
        if not settings.REGISTRATION_OPEN or request.session.get("pending_invite_token"):
            raise PermissionDenied("Signup membership was not established by a valid invitation.")
        provision_organization_and_workspace(user)

    # Try to set the organization timezone from the browser cookie set during signup.
    if not getattr(request, "_signup_invited", False):
        _set_org_timezone_from_browser(request, user)

    # Email signups see ToS text on the signup form, so auto-accept.
    # Social signups (Google OAuth) will be redirected to a dedicated ToS page.
    if not kwargs.get("sociallogin"):
        user.tos_accepted_at = timezone.now()
        user.save(update_fields=["tos_accepted_at"])


@receiver(post_save, sender="accounts.User")
def create_organization_on_user_create(sender, instance, created, **kwargs):
    """Provision admin/shell users; signup adapters own their atomic provisioning."""
    if created and not getattr(instance, "_signup_managed", False):
        provision_organization_and_workspace(instance)
