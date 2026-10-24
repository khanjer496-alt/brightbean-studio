"""Account-set rules shared by the settings pages, the composer, CSV import, repurposing, the REST API and MCP."""

from __future__ import annotations

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from apps.billing.entitlements import PlanLimitError, check_can_add_account_set
from apps.social_accounts.models import SocialAccount
from providers import PROVIDER_REGISTRY, account_default_choices_for, account_default_keys_for

from .models import AccountSet

MAX_SETS = 100
NAME_MAX = 80
# A value a platform default may hold: short strings, booleans.
VALUE_MAX = 200


class SetError(Exception):
    """A request that can't be honoured; ``message`` is safe to show."""

    def __init__(self, message: str, field: str = ""):
        super().__init__(message)
        self.message = message
        self.field = field


def can_manage(membership) -> bool:
    perms = membership.effective_permissions if membership else {}
    return bool(perms.get("manage_social_accounts") or perms.get("manage_workspace_settings"))


def can_use(membership) -> bool:
    perms = membership.effective_permissions if membership else {}
    return bool(perms.get("create_posts"))


def sets_for(workspace):
    return AccountSet.objects.filter(workspace=workspace).prefetch_related("accounts")


def get_set(workspace, set_id) -> AccountSet | None:
    if not set_id:
        return None
    try:
        return sets_for(workspace).filter(pk=set_id).first()
    except (ValueError, TypeError, ValidationError):
        return None


def set_by_name(workspace, name) -> AccountSet | None:
    """A set by its name, ignoring case (CSV import columns name sets that way)."""
    name = " ".join((name or "").split())
    if not name:
        return None
    return sets_for(workspace).filter(name__iexact=name).first()


def _accounts(workspace, account_ids) -> list[SocialAccount]:
    ids = list(dict.fromkeys(str(i) for i in account_ids or []))
    found = {str(a.id): a for a in SocialAccount.objects.filter(workspace=workspace, id__in=ids)}
    missing = [i for i in ids if i not in found]
    if missing:
        raise SetError("One of those accounts isn't in this workspace.", "account_ids")
    return [found[i] for i in ids]


def clean_defaults(accounts, defaults) -> dict[str, dict]:
    """Keep only keys each account's platform allows, for accounts that are in the set."""
    if not defaults:
        return {}
    if not isinstance(defaults, dict):
        raise SetError("Defaults must be settings per account.", "defaults")
    by_id = {str(a.id): a for a in accounts}
    cleaned: dict[str, dict] = {}
    for account_id, values in defaults.items():
        account = by_id.get(str(account_id))
        if account is None:
            raise SetError("Defaults can only be set for accounts in the set.", "defaults")
        if not isinstance(values, dict):
            raise SetError("Defaults must be settings per account.", "defaults")
        allowed = set(account_default_keys_for(account.platform))
        unknown = sorted(set(values) - allowed)
        if unknown:
            offered = ", ".join(sorted(allowed)) or "none"
            raise SetError(
                f"{account.get_platform_display()} can't default {', '.join(unknown)}; it offers: {offered}.",
                "defaults",
            )
        choices = account_default_choices_for(account.platform)
        kept = {}
        for key, value in values.items():
            if value is None or value == "":
                continue
            if isinstance(value, str) and len(value) > VALUE_MAX:
                raise SetError(f"The value for {key} is too long.", "defaults")
            if not isinstance(value, (str, bool, int)):
                raise SetError(f"The value for {key} must be text or true/false.", "defaults")
            allowed_values = choices.get(key)
            if allowed_values is not None:
                # ``True in (1, 0)`` is true in Python, so a switch must be a real boolean.
                is_switch = set(allowed_values) == {True, False}
                if (is_switch and not isinstance(value, bool)) or (not is_switch and value not in allowed_values):
                    shown = "true or false" if is_switch else ", ".join(str(v) for v in allowed_values)
                    raise SetError(f"{account.get_platform_display()} {key} must be one of: {shown}.", "defaults")
            kept[key] = value
        if kept:
            cleaned[str(account.id)] = kept
    return cleaned


def _name(workspace, name, exclude=None) -> str:
    name = " ".join((name or "").split())
    if not name:
        raise SetError("Give the set a name.", "name")
    if len(name) > NAME_MAX:
        raise SetError(f"Use a name of at most {NAME_MAX} characters.", "name")
    clash = AccountSet.objects.filter(workspace=workspace, name__iexact=name)
    if exclude is not None:
        clash = clash.exclude(pk=exclude.pk)
    if clash.exists():
        raise SetError("A set with that name already exists.", "name")
    return name


def create_set(workspace, *, name, account_ids, defaults=None, user=None) -> AccountSet:
    name = _name(workspace, name)
    accounts = _accounts(workspace, account_ids)
    if not accounts:
        raise SetError("Pick at least one account.", "account_ids")
    if AccountSet.objects.filter(workspace=workspace).count() >= MAX_SETS:
        raise SetError(f"A workspace can keep at most {MAX_SETS} sets.")
    try:
        check_can_add_account_set(workspace.organization)
    except PlanLimitError as exc:
        raise SetError(str(exc)) from exc
    cleaned = clean_defaults(accounts, defaults)
    try:
        with transaction.atomic():
            obj = AccountSet.objects.create(workspace=workspace, name=name, defaults=cleaned, created_by=user)
            obj.accounts.set(accounts)
    except IntegrityError as exc:
        raise SetError("A set with that name already exists.", "name") from exc
    return obj


def update_set(obj: AccountSet, *, name=None, account_ids=None, defaults=None, hidden_account_ids=()) -> AccountSet:
    """Change a set. ``hidden_account_ids`` are members the caller can't see (an API key's allowlist); they stay."""
    workspace = obj.workspace
    if name is not None:
        obj.name = _name(workspace, name, exclude=obj)
    accounts = None
    if account_ids is not None:
        accounts = _accounts(workspace, [*account_ids, *hidden_account_ids])
        if not accounts:
            raise SetError("Pick at least one account.", "account_ids")
    current = accounts if accounts is not None else list(obj.accounts.all())
    if defaults is not None:
        hidden = {str(i) for i in hidden_account_ids}
        kept = {k: v for k, v in obj.defaults.items() if k in hidden}
        obj.defaults = {**kept, **clean_defaults(current, defaults)}
    elif accounts is not None:
        keep = {str(a.id) for a in accounts}
        obj.defaults = {k: v for k, v in obj.defaults.items() if k in keep}
    try:
        with transaction.atomic():
            obj.save()
            if accounts is not None:
                obj.accounts.set(accounts)
    except IntegrityError as exc:
        raise SetError("A set with that name already exists.", "name") from exc
    return obj


def delete_set(obj: AccountSet) -> None:
    obj.delete()


def usable_accounts(obj: AccountSet, *, connected_only=True, allowed_ids=None) -> list[SocialAccount]:
    """The set's accounts a post can go to now, in a stable order."""
    accounts = list(obj.accounts.all())
    if connected_only:
        accounts = [a for a in accounts if a.connection_status == SocialAccount.ConnectionStatus.CONNECTED]
    if allowed_ids is not None:
        accounts = [a for a in accounts if a.id in allowed_ids]
    return sorted(accounts, key=lambda a: (a.platform, (a.account_name or a.account_handle or "").lower()))


def defaults_for(obj: AccountSet, account) -> dict:
    return dict(obj.defaults.get(str(account.id), {}))


def composer_payload(obj: AccountSet) -> dict:
    """What the composer needs to apply a set in one click: the account ids and each one's defaults."""
    accounts = usable_accounts(obj)
    return {
        "id": str(obj.id),
        "name": obj.name,
        "account_ids": [str(a.id) for a in accounts],
        "defaults": {str(a.id): defaults_for(obj, a) for a in accounts if defaults_for(obj, a)},
        "skipped": [
            a.account_name or a.account_handle
            for a in obj.accounts.all()
            if a.connection_status != SocialAccount.ConnectionStatus.CONNECTED
        ],
    }


def default_fields(platform: str) -> list[dict]:
    """The settings an account on ``platform`` can default, as form fields (declared settings first)."""
    provider_cls = PROVIDER_REGISTRY.get(platform)
    if provider_cls is None:
        return []
    declared = [field.as_dict() for field in provider_cls.post_settings]
    declared_keys = {field["key"] for field in declared}
    extra = []
    for key in provider_cls.account_default_keys:
        if key in declared_keys:
            continue
        allowed = provider_cls.account_default_choices.get(key)
        is_switch = allowed is not None and set(allowed) == {True, False}
        extra.append(
            {
                "key": key,
                "label": key.replace("_", " ").capitalize(),
                "kind": "checkbox" if is_switch else ("select" if allowed else "text"),
                "choices": [] if is_switch or not allowed else [{"value": v, "label": str(v)} for v in allowed],
                "help": "",
                "placeholder": "",
                "options_from_provider": False,
            }
        )
    return declared + extra


def parse_defaults(get, accounts) -> dict[str, dict]:
    """The defaults a set form posts, as ``d_<key>_<account id>`` fields. ``true``/``false`` become booleans."""
    values: dict[str, dict] = {}
    for account in accounts:
        row = {}
        for field in default_fields(account.platform):
            raw = str(get(f"d_{field['key']}_{account.id}", "") or "").strip()
            if not raw:
                continue
            row[field["key"]] = {"true": True, "false": False}.get(raw.lower(), raw)
        if row:
            values[str(account.id)] = row
    return values
