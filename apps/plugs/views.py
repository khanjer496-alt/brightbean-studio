"""The Automations page and the composer's per-post panel.

Seeing rules needs ``create_posts``; adding, switching and removing them needs
``publish_directly``, since a rule makes the workspace post (repost, reply) on
its own. Everything is looked up inside the workspace in the URL, so another
workspace's id is a 404. The autonomy policy still decides what a rule that
fires may do; the page says so.
"""

from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_GET, require_POST

from apps.autonomy.policy import get_policy
from apps.composer.models import PlatformPost
from apps.members.decorators import require_permission
from apps.social_accounts.models import SocialAccount
from providers import plug_capability_for

from . import services
from .models import PlugRule

RECENT_RUNS = 30


def _capability_json(account) -> dict:
    cap = plug_capability_for(account.platform)
    label = dict(PlugRule.TRIGGER_CHOICES)
    action_label = dict(PlugRule.ACTION_CHOICES)
    return {
        "id": str(account.id),
        "triggers": [{"value": t, "label": label[t]} for t in label if t in cap.triggers],
        "own": [{"value": a, "label": action_label[a]} for a in action_label if a in cap.own_actions],
        "cross": [{"value": a, "label": action_label[a]} for a in action_label if a in cap.cross_actions],
        "reposters": [{"id": str(r.id), "name": r.account_name} for r in services.reposter_choices(account)],
        "message_max": cap.message_max_length or account.char_limit,
        "metered": cap.metered_reads,
        "interval": cap.check_interval_minutes,
    }


def _watchable_accounts(workspace):
    return [
        a
        for a in SocialAccount.objects.for_workspace(workspace.id)
        .filter(connection_status=SocialAccount.ConnectionStatus.CONNECTED)
        .order_by("platform", "account_name")
        if services.can_have_rules(a)
    ]


def _home_context(request, **extra):
    workspace = request.workspace
    accounts = _watchable_accounts(workspace)
    rules = list(services.rules_for_workspace(workspace))
    defaults = [r for r in rules if r.is_default]
    on_posts = [r for r in rules if not r.is_default]
    policy = get_policy(workspace)
    return {
        "workspace": workspace,
        "accounts": accounts,
        "capabilities": [_capability_json(a) for a in accounts],
        "defaults": defaults,
        "on_posts": on_posts,
        "runs": services.runs_for_workspace(workspace)[:RECENT_RUNS],
        "policy": policy,
        "can_manage": request.workspace_membership.effective_permissions.get("publish_directly", False),
        "form": {},
        **extra,
    }


@login_required
@require_permission("create_posts")
@require_GET
def home(request, workspace_id):
    return render(request, "plugs/home.html", _home_context(request))


def _rule(request, rule_id) -> PlugRule:
    return get_object_or_404(PlugRule, id=rule_id, workspace=request.workspace)


def _fields_from(request, account) -> dict:
    reposter = None
    reposter_id = request.POST.get("actor_account") or ""
    if reposter_id:
        reposter = SocialAccount.objects.for_workspace(request.workspace.id).filter(id=_uuid(reposter_id)).first()
        if reposter is None:
            raise services.PlugError("That account is not in this workspace.")
    return {
        "trigger": request.POST.get("trigger", ""),
        "threshold": request.POST.get("threshold", ""),
        "window_hours": request.POST.get("window_hours", ""),
        "action": request.POST.get("action", ""),
        "actor_account": reposter,
        "message": request.POST.get("message", ""),
    }


def _uuid(value):
    import uuid

    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError):
        return None


@login_required
@require_permission("publish_directly")
@require_POST
def rule_add(request, workspace_id):
    workspace = request.workspace
    account = SocialAccount.objects.for_workspace(workspace.id).filter(id=_uuid(request.POST.get("account"))).first()
    post = None
    if request.POST.get("post"):
        post = (
            PlatformPost.objects.filter(post__workspace=workspace, post_id=_uuid(request.POST["post"]))
            .select_related("post", "social_account")
            .filter(social_account=account)
            .first()
        )
    error = ""
    if account is None or (request.POST.get("post") and post is None):
        error = "Choose one of this workspace's accounts."
    else:
        try:
            services.create_rule(
                workspace=workspace,
                account=account,
                platform_post=post,
                user=request.user,
                **_fields_from(request, account),
            )
        except services.PlugError as exc:
            error = str(exc)
    if request.POST.get("panel") == "composer" and request.htmx:
        return _panel(request, post.post if post else None, error=error, form=request.POST)
    if error:
        return render(request, "plugs/home.html", _home_context(request, error=error, form=request.POST), status=422)
    messages.success(request, "Automation added. It starts watching as soon as the post is live.")
    return redirect("plugs:home", workspace_id=workspace.id)


@login_required
@require_permission("publish_directly")
@require_POST
def rule_toggle(request, workspace_id, rule_id):
    rule = _rule(request, rule_id)
    services.update_rule(rule, is_active=not rule.is_active)
    return _back(request, rule)


@login_required
@require_permission("publish_directly")
@require_POST
def rule_delete(request, workspace_id, rule_id):
    rule = _rule(request, rule_id)
    post = rule.platform_post.post if rule.platform_post_id else None
    services.delete_rule(rule)
    if request.POST.get("panel") == "composer" and request.htmx:
        return _panel(request, post)
    messages.success(request, "Automation removed.")
    return redirect("plugs:home", workspace_id=request.workspace.id)


def _back(request, rule):
    if request.POST.get("panel") == "composer" and request.htmx and rule.platform_post_id:
        return _panel(request, rule.platform_post.post)
    return redirect("plugs:home", workspace_id=request.workspace.id)


def _panel(request, post, *, error="", form=None):
    workspace = request.workspace
    targets = []
    for pp in post.platform_posts.select_related("social_account").order_by("social_account__account_name"):
        if not services.can_have_rules(pp.social_account):
            continue
        runs = {r.rule_id: r for r in pp.plug_runs.all()}
        rules = list(pp.plug_rules.select_related("actor_account", "social_account"))
        for rule in rules:
            rule.run = runs.get(rule.id)
        targets.append(
            {
                "pp": pp,
                "caps": [_capability_json(pp.social_account)],
                "caps_id": f"plugs-caps-{pp.id}",
                "rules": rules,
            }
        )
    return render(
        request,
        "plugs/_composer_panel.html",
        {
            "workspace": workspace,
            "post": post,
            "targets": targets,
            "policy": get_policy(workspace),
            "can_manage": request.workspace_membership.effective_permissions.get("publish_directly", False),
            "error": error,
            "form": form or {},
        },
    )


@login_required
@require_permission("create_posts")
@require_GET
def composer_panel(request, workspace_id, post_id):
    from apps.composer.models import Post

    post = get_object_or_404(Post, id=post_id, workspace=request.workspace)
    return _panel(request, post)
