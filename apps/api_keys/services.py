"""Token generation, hashing, issuance, lookup, and revocation for ApiKey.

Token shape::

    bb_studio_<random32>_<lookup8>
    \\_______/ \\_______/ \\_____/
     prefix     secret     lookup

The secret part is what gets HMAC-hashed at rest. The lookup part is an
indexed plaintext column that makes verification O(1) without revealing
the secret. Verification then constant-time-compares the recomputed HMAC
against the stored hash.

Every request reads the key row fresh. A row cache used to sit here, but the
default cache is per process (LocMem) unless Redis is configured, so a
revocation or edit could not reach the other workers and a revoked token kept
working for up to 30 seconds. Revoking, rotating and editing now take effect on
the very next request on every worker.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from typing import Any

from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

from apps.api_keys import capabilities as caps
from apps.api_keys.models import ApiKey, ConnectedApp

# Deployment-configurable customer-facing prefix.  Upstream keeps its original
# default; PostDelegate sets ``postdelegate_`` without changing the token
# entropy, lookup or HMAC-at-rest design.
TOKEN_PREFIX = settings.API_TOKEN_PREFIX  # compatibility export for existing imports
LOOKUP_LEN = 8
MAX_IP_RANGES = 20

#: Passed for an argument the caller is not changing (``None`` is a real value for most of them).
UNSET: Any = object()


# ---------------------------------------------------------------------------
# HMAC pepper derivation
# ---------------------------------------------------------------------------


def _derive_hmac_pepper() -> bytes:
    """Derive a 256-bit HMAC pepper from SECRET_KEY via HKDF.

    Distinct ``info`` bytes from the field-encryption key derivation in
    ``apps.common.encryption`` so a compromise of one secret never reveals
    the other.
    """
    secret = settings.SECRET_KEY.encode("utf-8")
    salt = getattr(settings, "ENCRYPTION_KEY_SALT", None)
    if not salt:
        raise ValueError(
            "ENCRYPTION_KEY_SALT must be set for ApiKey HMAC peppering. "
            "Generate a random value and add it to your environment."
        )
    if isinstance(salt, str):
        salt = salt.encode("utf-8")
    return HKDF(
        algorithm=SHA256(),
        length=32,
        salt=salt,
        info=b"brightbean-api-key-hmac",
    ).derive(secret)


# ---------------------------------------------------------------------------
# Token format helpers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ParsedToken:
    """Result of splitting a raw bearer string into its three parts."""

    random_part: str
    lookup_prefix: str


def _hmac_hex(random_part: str) -> str:
    return hmac.new(
        _derive_hmac_pepper(),
        random_part.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _make_lookup(random_part: str) -> str:
    return hashlib.sha256(random_part.encode("utf-8")).hexdigest()[:LOOKUP_LEN]


def parse_token(raw: str) -> ParsedToken | None:
    """Split a raw bearer string into its parts, or None on malformed input.

    Strict: exact prefix match, exact part count, lookup length, and a
    plausible secret length. Anything else is treated as an unparseable
    token (caller turns this into 401).
    """
    token_prefix = settings.API_TOKEN_PREFIX
    if not isinstance(raw, str) or not raw.startswith(token_prefix):
        return None
    body = raw[len(token_prefix) :]
    # secrets.token_urlsafe() emits A-Z, a-z, 0-9, '-' and '_' — so the
    # secret itself may contain underscores. Split only on the LAST '_'
    # so the lookup suffix is always isolated correctly.
    parts = body.rsplit("_", 1)
    if len(parts) != 2:
        return None
    random_part, lookup = parts
    if len(lookup) != LOOKUP_LEN:
        return None
    # token_urlsafe(32) yields ~43 chars; allow a sensible range so a
    # future tweak to the entropy size doesn't break parsing.
    if not (32 <= len(random_part) <= 64):
        return None
    if _make_lookup(random_part) != lookup:
        # The lookup is content-addressed to the random part, so a
        # mismatch is a sure sign of tampering or a typo.
        return None
    return ParsedToken(random_part=random_part, lookup_prefix=lookup)


# ---------------------------------------------------------------------------
# Issuance & revocation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IssuedKey:
    """Return value of ``issue_api_key`` — the only place plaintext exists."""

    api_key: ApiKey
    plaintext_token: str


def normalize_ip_ranges(ranges) -> list[str]:
    """CIDR ranges (a bare address counts as a /32 or /128) as canonical strings. ValueError on anything else."""
    import ipaddress

    out: list[str] = []
    for raw in ranges or ():
        text = str(raw).strip()
        if not text:
            continue
        try:
            network = str(ipaddress.ip_network(text, strict=False))
        except ValueError as exc:
            raise ValueError(f"'{text}' is not an IP address or CIDR range.") from exc
        if network not in out:
            out.append(network)
    if len(out) > MAX_IP_RANGES:
        raise ValueError(f"Add at most {MAX_IP_RANGES} IP ranges.")
    return out


def _check_scope(workspace, *, capabilities, daily_request_budget) -> list[str] | None:
    """Validate the tool-level scope; returns the cleaned capability list (None = not limited)."""
    cleaned = None
    if capabilities is not None:
        cleaned = caps.clean(capabilities)
        if not cleaned:
            raise ValueError("Choose at least one capability for the token.")
    if daily_request_budget is not None:
        from apps.billing.entitlements import get_entitlements

        plan_cap = get_entitlements(workspace.organization).max_api_calls_per_day
        if plan_cap is not None and daily_request_budget > plan_cap:
            raise ValueError(
                f"The daily request budget must stay within your plan's allowance of {plan_cap} calls a day."
            )
    return cleaned


def fit_request_budget(workspace, daily_request_budget: int | None) -> int | None:
    """A request budget no larger than the plan's daily allowance, so a preset made for a bigger plan still applies."""
    if daily_request_budget is None:
        return None
    from apps.billing.entitlements import get_entitlements

    plan_cap = get_entitlements(workspace.organization).max_api_calls_per_day
    return daily_request_budget if plan_cap is None else min(daily_request_budget, plan_cap)


def issue_api_key(
    *,
    workspace,
    social_accounts,
    issued_by,
    name: str,
    permissions: list[str],
    expires_at=None,
    capabilities: list[str] | None = None,
    preset: str = "",
    daily_request_budget: int | None = None,
    daily_ai_budget: int | None = None,
    daily_post_budget: int | None = None,
    allowed_ip_ranges=(),
    requires_approval: bool = False,
) -> IssuedKey:
    """Create a new ``ApiKey`` and return the plaintext token alongside.

    The plaintext lives only in the returned ``IssuedKey``. Callers must
    surface it to the user once and never store it.

    Defense-in-depth on the inputs:
      * Issuer must hold the org-level ``manage_api_keys`` permission
        (default-granted to org ``owner`` and ``admin``). The HTTP form view
        also gates on this, but issuance is callable from shells, management
        commands, and future internal paths — re-check here so the contract
        described in the model docstring ("Issue and revoke Agent API keys")
        is enforced regardless of caller.
      * Every ``SocialAccount`` in ``social_accounts`` must belong to
        ``workspace`` (caller's UI form should also enforce this; we re-check
        here so a tampered POST can't slip a foreign account in).
      * ``permissions`` must be a subset of the issuer's current
        effective workspace permissions (so an admin can't grant a
        permission they don't transitively hold).
      * ``capabilities`` limits the token to groups of routes and tools (None = not
        limited, only for callers that predate scoped tokens); the budgets, IP ranges
        and ``requires_approval`` can only narrow what the token does.
    """
    from apps.members.models import OrgMembership, WorkspaceMembership, has_org_permission

    sa_list = list(social_accounts)
    if not sa_list:
        raise ValueError("An API key must allowlist at least one connected account.")
    for sa in sa_list:
        if sa.workspace_id != workspace.id:
            raise ValueError(f"SocialAccount {sa.id} does not belong to workspace {workspace.id}.")

    # Org-level manage_api_keys gate — must be checked before any
    # workspace-permission logic so a non-admin who happens to have rich
    # workspace perms still can't mint a key.
    org_membership = OrgMembership.objects.filter(user=issued_by, organization_id=workspace.organization_id).first()
    if not has_org_permission(org_membership, "manage_api_keys"):
        raise ValueError(
            f"User {issued_by} lacks the org-level 'manage_api_keys' permission "
            f"for organization {workspace.organization_id}; cannot issue API keys."
        )

    # Permission intersection check — the issuer can only grant what they hold.
    try:
        membership = WorkspaceMembership.objects.get(user=issued_by, workspace=workspace)
        granter_perms = {k for k, v in membership.effective_permissions.items() if v}
    except WorkspaceMembership.DoesNotExist as exc:
        raise ValueError(
            f"User {issued_by} has no membership in workspace {workspace}; cannot issue an API key on their behalf."
        ) from exc

    requested = set(permissions or [])
    ungrantable = requested - granter_perms
    if ungrantable:
        raise ValueError(f"Issuer cannot grant permissions they don't hold: {sorted(ungrantable)}")

    cleaned_caps = _check_scope(workspace, capabilities=capabilities, daily_request_budget=daily_request_budget)
    ip_ranges = normalize_ip_ranges(allowed_ip_ranges)

    random_part = secrets.token_urlsafe(32)
    lookup = _make_lookup(random_part)
    token_hash = _hmac_hex(random_part)
    plaintext = f"{settings.API_TOKEN_PREFIX}{random_part}_{lookup}"

    api_key = ApiKey.objects.create(
        workspace=workspace,
        issued_by=issued_by,
        name=name,
        lookup_prefix=lookup,
        token_hash=token_hash,
        permissions=sorted(requested),
        expires_at=expires_at,
        capabilities=cleaned_caps,
        preset=preset if caps.preset(preset) else "",
        daily_request_budget=daily_request_budget,
        daily_ai_budget=daily_ai_budget,
        daily_post_budget=daily_post_budget,
        allowed_ip_ranges=ip_ranges,
        requires_approval=requires_approval,
    )
    api_key.social_accounts.set(sa_list)
    return IssuedKey(api_key=api_key, plaintext_token=plaintext)


def update_api_key(
    api_key: ApiKey,
    *,
    editor,
    permissions: list[str],
    social_accounts,
    expires_at=None,
    capabilities: Any = UNSET,
    preset: Any = UNSET,
    daily_request_budget: Any = UNSET,
    daily_ai_budget: Any = UNSET,
    daily_post_budget: Any = UNSET,
    allowed_ip_ranges: Any = UNSET,
    requires_approval: Any = UNSET,
) -> ApiKey:
    """Edit an existing key's permissions, account allowlist, and expiry in place.

    Used by the org UI's "Edit" action so a key's scope can change after
    creation without re-issuing (which would mint a new token and break every
    agent already using it). The token, ``workspace``, and ``issued_by`` are
    immutable here.

    Authorization mirrors ``issue_api_key`` — re-checked server-side so a
    tampered form post can't escape it:
      * ``editor`` must hold the org-level ``manage_api_keys`` permission.
      * ``editor`` must have a ``WorkspaceMembership`` in the key's workspace.
      * Every account must belong to that workspace; the allowlist stays >= 1.

    Permissions get the one twist issuance doesn't need: the editor may only
    flip permissions **within their own grantable set** (their effective
    workspace permissions ∩ ``PERMISSION_KEYS``). Any permission the key
    already holds that the editor cannot grant — e.g. granted by a
    higher-privileged admin — is **preserved**, never silently stripped. The
    ``& editor_grantable`` clamp also means a tampered post naming a permission
    the editor lacks is dropped rather than raising.

    The tool-level scope (capabilities, preset, budgets, IP ranges, approval mode)
    is left alone unless passed. Rows are read fresh on every request, so the new
    scope applies on the very next API request.
    """
    from django.db import transaction

    from apps.members.models import (
        PERMISSION_KEYS,
        OrgMembership,
        WorkspaceMembership,
        has_org_permission,
    )

    workspace = api_key.workspace

    # Org-level gate first — same ordering as issue_api_key, so a non-admin
    # with rich workspace perms still can't edit a key.
    org_membership = OrgMembership.objects.filter(user=editor, organization_id=workspace.organization_id).first()
    if not has_org_permission(org_membership, "manage_api_keys"):
        raise ValueError(
            f"User {editor} lacks the org-level 'manage_api_keys' permission "
            f"for organization {workspace.organization_id}; cannot edit API keys."
        )

    try:
        membership = WorkspaceMembership.objects.get(user=editor, workspace=workspace)
    except WorkspaceMembership.DoesNotExist as exc:
        raise ValueError(
            f"User {editor} has no membership in workspace {workspace}; cannot edit an API key there."
        ) from exc

    # Accounts: non-empty, all in the key's workspace (mirrors issue_api_key).
    sa_list = list(social_accounts)
    if not sa_list:
        raise ValueError("An API key must allowlist at least one connected account.")
    for sa in sa_list:
        if sa.workspace_id != workspace.id:
            raise ValueError(f"SocialAccount {sa.id} does not belong to workspace {workspace.id}.")

    # Permissions: flip only within the editor's grantable set; preserve the rest.
    editor_grantable = {k for k, v in membership.effective_permissions.items() if v and k in PERMISSION_KEYS}
    current = set(api_key.permissions or [])
    new_permissions = (set(permissions or []) & editor_grantable) | (current - editor_grantable)

    scope_fields = {
        "capabilities": capabilities,
        "preset": preset,
        "daily_request_budget": daily_request_budget,
        "daily_ai_budget": daily_ai_budget,
        "daily_post_budget": daily_post_budget,
        "allowed_ip_ranges": allowed_ip_ranges,
        "requires_approval": requires_approval,
    }
    changes = {k: v for k, v in scope_fields.items() if v is not UNSET}
    if "capabilities" in changes or "daily_request_budget" in changes:
        cleaned = _check_scope(
            workspace,
            capabilities=changes.get("capabilities", api_key.capabilities),
            daily_request_budget=changes.get("daily_request_budget", api_key.daily_request_budget),
        )
        if "capabilities" in changes:
            changes["capabilities"] = cleaned
    if "preset" in changes and not caps.preset(changes["preset"]):
        changes["preset"] = ""
    if "allowed_ip_ranges" in changes:
        changes["allowed_ip_ranges"] = normalize_ip_ranges(changes["allowed_ip_ranges"])

    with transaction.atomic():
        api_key.permissions = sorted(new_permissions)
        api_key.expires_at = expires_at
        for field, value in changes.items():
            setattr(api_key, field, value)
        api_key.save(update_fields=["permissions", "expires_at", *changes])
        api_key.social_accounts.set(sa_list)
    return api_key


def rotate_api_key(api_key: ApiKey, *, editor) -> IssuedKey:
    """Give a key a new secret in place and kill the old one on the spot.

    The row, its scope, budgets, history and audit trail stay; only the lookup prefix and hash change,
    so a request with the old token no longer finds the key. Returns the new plaintext, shown once.
    """
    from apps.members.models import OrgMembership, has_org_permission

    workspace = api_key.workspace
    org_membership = OrgMembership.objects.filter(user=editor, organization_id=workspace.organization_id).first()
    if not has_org_permission(org_membership, "manage_api_keys"):
        raise ValueError(
            f"User {editor} lacks the org-level 'manage_api_keys' permission "
            f"for organization {workspace.organization_id}; cannot rotate API keys."
        )
    if not api_key.is_active:
        raise ValueError("Only an active key can be rotated.")
    random_part = secrets.token_urlsafe(32)
    api_key.lookup_prefix = _make_lookup(random_part)
    api_key.token_hash = _hmac_hex(random_part)
    api_key.save(update_fields=["lookup_prefix", "token_hash"])
    return IssuedKey(
        api_key=api_key, plaintext_token=f"{settings.API_TOKEN_PREFIX}{random_part}_{api_key.lookup_prefix}"
    )


def revoke_api_key(api_key: ApiKey) -> None:
    """Mark a key revoked. Every worker reads the row fresh, so the next request, anywhere, gets a 401."""
    if api_key.revoked_at is None:
        api_key.revoked_at = timezone.now()
        api_key.save(update_fields=["revoked_at"])


# ---------------------------------------------------------------------------
# OAuth connections: the same limits for a client a person connected with OAuth
# ---------------------------------------------------------------------------


def record_oauth_consent(*, user, application, capabilities: list[str], requires_approval: bool) -> ConnectedApp:
    """Store what the person allowed on the consent screen for this client.

    A fresh consent replaces the capabilities and approval mode, and lifts an earlier revocation (the person just
    approved the client again). Budgets and expiry set on the API keys page stay.
    """
    cleaned = caps.clean(capabilities)
    if not cleaned:
        raise ValueError("Choose at least one capability.")
    connection, _ = ConnectedApp.objects.update_or_create(
        user=user,
        application=application,
        defaults={"capabilities": cleaned, "requires_approval": requires_approval, "revoked_at": None},
    )
    if connection.expires_at is not None and connection.expires_at <= timezone.now():
        # An expiry that has passed would leave the client the person just approved unable to connect.
        connection.expires_at = None
        connection.save(update_fields=["expires_at", "updated_at"])
    return connection


def update_connected_app(
    connection: ConnectedApp,
    *,
    capabilities: list[str],
    requires_approval: bool,
    expires_at=None,
    daily_request_budget: int | None = None,
    daily_ai_budget: int | None = None,
    daily_post_budget: int | None = None,
) -> ConnectedApp:
    """Change a connected client's limits. Its tokens keep at most what the person consented to (their scopes)."""
    cleaned = caps.clean(capabilities)
    if not cleaned:
        raise ValueError("Choose at least one capability.")
    connection.capabilities = cleaned
    connection.requires_approval = requires_approval
    connection.expires_at = expires_at
    connection.daily_request_budget = daily_request_budget
    connection.daily_ai_budget = daily_ai_budget
    connection.daily_post_budget = daily_post_budget
    connection.save(
        update_fields=[
            "capabilities",
            "requires_approval",
            "expires_at",
            "daily_request_budget",
            "daily_ai_budget",
            "daily_post_budget",
            "updated_at",
        ]
    )
    return connection


def revoke_connected_app(connection: ConnectedApp) -> None:
    """Cut a connected client off now: its access tokens stop working and its refresh tokens can't mint new ones.

    The tokens are deleted (refresh tokens are revoked the way django-oauth-toolkit does it), and the connection
    is marked revoked, which ``apps.api.auth`` and the refresh validator both check.
    """
    from django.db import transaction
    from oauth2_provider.models import get_access_token_model, get_grant_model, get_refresh_token_model

    with transaction.atomic():
        if connection.revoked_at is None:
            connection.revoked_at = timezone.now()
            connection.save(update_fields=["revoked_at", "updated_at"])
        mine = {"user_id": connection.user_id, "application_id": connection.application_id}
        for refresh in get_refresh_token_model().objects.filter(revoked__isnull=True, **mine):
            refresh.revoke()
        get_access_token_model().objects.filter(**mine).delete()
        get_grant_model().objects.filter(**mine).delete()


# ---------------------------------------------------------------------------
# Verification (read path) — hot path; gets called on every API request.
# ---------------------------------------------------------------------------


def verify_token(raw: str) -> ApiKey | None:
    """Resolve a raw bearer string to an active ``ApiKey``, or None.

    Returns None for: malformed token, unknown prefix, hash mismatch,
    revoked key, expired key, **issuer no longer a member of the key's
    workspace** (or issuer deleted or deactivated, or the workspace archived).
    The caller turns None into 401.

    The row is read from the database on every call, never from a cache: a process-local cache cannot learn about
    a revocation made on another worker. That costs three indexed queries: the key joined to its issuer and
    workspace (by the unique ``lookup_prefix``), the issuer's membership (by the unique user + workspace pair,
    kept on the key as ``issuer_membership`` for the permission intersection), and the account allowlist. The
    capabilities, budgets and IP ranges are columns of the key row, so they cost nothing more.

    Constant-time compare on the hash defends against timing oracles on
    the secret part.
    """
    from django.db.models import prefetch_related_objects

    parsed = parse_token(raw)
    if parsed is None:
        return None

    api_key = ApiKey.objects.select_related("issued_by", "workspace").filter(lookup_prefix=parsed.lookup_prefix).first()
    if api_key is None or not api_key.is_active:
        return None

    expected_hash = _hmac_hex(parsed.random_part)
    if not hmac.compare_digest(expected_hash, api_key.token_hash):
        return None

    # Even an unrevoked, unexpired key must die if the issuer was offboarded, deleted, or moved out of the
    # workspace. The model docstring promises this; the model property alone can't express it without a DB hit.
    membership = _issuer_membership(api_key)
    if membership is None:
        return None
    api_key.issuer_membership = membership  # type: ignore[attr-defined]
    prefetch_related_objects([api_key], "social_accounts")
    return api_key


def _issuer_membership(api_key: ApiKey):
    """The issuer's ``WorkspaceMembership`` in the key's workspace, or None when the key must not work.

    None when the issuer was deleted (FK set to NULL by ``on_delete=SET_NULL``) or deactivated, when the workspace
    is archived, or when their workspace membership has been removed. Demotion within the workspace is handled by
    the per-request permission intersection in the Ninja auth class, which reuses this row.
    """
    from apps.members.models import WorkspaceMembership

    if api_key.issued_by_id is None or not api_key.issued_by.is_active or api_key.workspace.is_archived:
        return None
    return (
        WorkspaceMembership.objects.select_related("custom_role")
        .filter(user_id=api_key.issued_by_id, workspace_id=api_key.workspace_id)
        .first()
    )


# ---------------------------------------------------------------------------
# Debounced last_used update — fired from the auth layer post-verification.
# ---------------------------------------------------------------------------


_LAST_USED_DEBOUNCE_SECONDS = 60


def _last_touched_cache_key(api_key_pk) -> str:
    return f"apikey:last_touched:{api_key_pk}"


def touch_last_used(api_key: ApiKey, *, ip: str | None) -> None:
    """Update ``last_used_at`` / ``last_used_ip`` at most once per 60s.

    Uses a raw ``UPDATE`` (no signals, no full save). The debounce is keyed in the cache
    (`apikey:last_touched:<pk>`) rather than on ``api_key.last_used_at``, so it does not
    depend on how fresh the row in hand is.
    """
    debounce_key = _last_touched_cache_key(api_key.pk)
    # `add` is an atomic set-if-not-exists — only the first caller in the
    # window wins the right to do the UPDATE; everyone else short-circuits.
    if not cache.add(debounce_key, "1", _LAST_USED_DEBOUNCE_SECONDS):
        return
    now = timezone.now()
    ApiKey.objects.filter(pk=api_key.pk).update(last_used_at=now, last_used_ip=ip)


def touch_connection_used(connection) -> None:
    """``ConnectedApp.last_used_at``, at most once a minute (same debounce as keys)."""
    if not cache.add(f"oauthconn:last_touched:{connection.pk}", "1", _LAST_USED_DEBOUNCE_SECONDS):
        return
    type(connection).objects.filter(pk=connection.pk).update(last_used_at=timezone.now())
