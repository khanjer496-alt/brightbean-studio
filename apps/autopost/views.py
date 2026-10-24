"""The Feeds page: follow a feed, set how its new items are posted, and see what became of each item.

Reading needs ``create_posts``; changing anything needs ``publish_directly``, because a feed makes the
workspace post on its own. Every feed is looked up inside the workspace in the URL (another workspace's id
is a 404). The autonomy policy still decides what each post may do; the page says so.
"""

from __future__ import annotations

import datetime as dt
import uuid

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from apps.autonomy.policy import get_policy
from apps.composer.models import Feed
from apps.members.decorators import require_permission
from apps.social_accounts.models import SocialAccount

from . import services
from .models import FeedAutopost

CHECK_NOW_COOLDOWN = dt.timedelta(minutes=1)


def _uuids(values):
    out = []
    for value in values:
        try:
            out.append(uuid.UUID(str(value)))
        except (ValueError, TypeError):
            continue
    return out


def _accounts(workspace):
    return list(
        SocialAccount.objects.for_workspace(workspace.id)
        .filter(connection_status=SocialAccount.ConnectionStatus.CONNECTED)
        .order_by("platform", "account_name")
    )


def _can_manage(request) -> bool:
    return request.workspace_membership.effective_permissions.get("publish_directly", False)


def _feed(request, feed_id) -> Feed:
    return get_object_or_404(Feed.objects.select_related("autopost"), id=feed_id, workspace=request.workspace)


def _autopost(feed):
    return getattr(feed, "autopost", None)


@login_required
@require_permission("create_posts")
@require_GET
def home(request, workspace_id):
    workspace = request.workspace
    feeds = list(Feed.objects.for_workspace(workspace.id).select_related("autopost").order_by("name"))
    return render(
        request,
        "autopost/home.html",
        {
            "workspace": workspace,
            "feeds": feeds,
            "can_manage": _can_manage(request),
            "policy": get_policy(workspace),
            "error": request.GET.get("error", ""),
            "form": {},
        },
    )


@login_required
@require_permission("publish_directly")
@require_POST
def feed_add(request, workspace_id):
    workspace = request.workspace
    try:
        feed = services.create_feed(
            workspace, request.user, name=request.POST.get("name", ""), url=request.POST.get("url", "")
        )
    except services.AutopostError as exc:
        feeds = list(Feed.objects.for_workspace(workspace.id).select_related("autopost").order_by("name"))
        return render(
            request,
            "autopost/home.html",
            {
                "workspace": workspace,
                "feeds": feeds,
                "can_manage": True,
                "policy": get_policy(workspace),
                "error": str(exc),
                "form": request.POST,
            },
            status=422,
        )
    return redirect("autopost:detail", workspace_id=workspace.id, feed_id=feed.id)


def _values(autopost, posted) -> dict:
    """What the settings form shows: what was just posted, else what is saved, else the defaults."""
    if posted:
        return {
            key: posted.get(key, "")
            for key in ("mode", "destination", "delay_minutes", "max_posts_per_day", "include", "exclude")
        }
    if autopost is None:
        return {
            "mode": FeedAutopost.Mode.LINK,
            "destination": FeedAutopost.Destination.DRAFT,
            "delay_minutes": 60,
            "max_posts_per_day": 3,
            "include": "",
            "exclude": "",
        }
    return {
        "mode": autopost.mode,
        "destination": autopost.destination,
        "delay_minutes": autopost.delay_minutes,
        "max_posts_per_day": autopost.max_posts_per_day,
        "include": ", ".join(autopost.include_keywords),
        "exclude": ", ".join(autopost.exclude_keywords),
    }


def _detail_context(request, feed, **extra):
    autopost = _autopost(feed)
    posted = extra.get("form")
    chosen = (
        posted.getlist("accounts")
        if posted
        else (list(autopost.accounts.values_list("id", flat=True)) if autopost else [])
    )
    return {
        "workspace": request.workspace,
        "feed": feed,
        "autopost": autopost,
        "values": _values(autopost, posted),
        "accounts": _accounts(request.workspace),
        "chosen_accounts": {str(a) for a in chosen},
        "items": services.recent_items(autopost) if autopost else [],
        "can_manage": _can_manage(request),
        "policy": get_policy(request.workspace),
        "modes": FeedAutopost.Mode.choices,
        "destinations": FeedAutopost.Destination.choices,
        "form": {},
        **extra,
    }


@login_required
@require_permission("create_posts")
@require_GET
def detail(request, workspace_id, feed_id):
    return render(request, "autopost/detail.html", _detail_context(request, _feed(request, feed_id)))


@login_required
@require_permission("publish_directly")
@require_POST
def save(request, workspace_id, feed_id):
    workspace = request.workspace
    feed = _feed(request, feed_id)
    accounts = SocialAccount.objects.for_workspace(workspace.id).filter(id__in=_uuids(request.POST.getlist("accounts")))
    fields = {
        "accounts": list(accounts),
        "mode": request.POST.get("mode", ""),
        "destination": request.POST.get("destination", ""),
        "delay_minutes": request.POST.get("delay_minutes", ""),
        "max_posts_per_day": request.POST.get("max_posts_per_day", ""),
        "include": request.POST.get("include", ""),
        "exclude": request.POST.get("exclude", ""),
    }
    try:
        autopost = _autopost(feed)
        if autopost is None:
            services.enable(workspace, feed, request.user, **fields)
            messages.success(request, "Auto-posting is on. Only items published from now on are posted.")
        else:
            services.update(autopost, **fields)
            messages.success(request, "Saved.")
    except services.AutopostError as exc:
        return render(
            request,
            "autopost/detail.html",
            _detail_context(request, feed, error=str(exc), form=request.POST),
            status=422,
        )
    return redirect("autopost:detail", workspace_id=workspace.id, feed_id=feed.id)


def _existing(request, feed_id):
    feed = _feed(request, feed_id)
    autopost = _autopost(feed)
    if autopost is None:
        raise Http404("Auto-posting is not set up for this feed.")
    return feed, autopost


@login_required
@require_permission("publish_directly")
@require_POST
def toggle(request, workspace_id, feed_id):
    feed, autopost = _existing(request, feed_id)
    services.update(autopost, is_active=not autopost.is_active)
    return redirect("autopost:detail", workspace_id=request.workspace.id, feed_id=feed.id)


@login_required
@require_permission("publish_directly")
@require_POST
def check_now(request, workspace_id, feed_id):
    feed, autopost = _existing(request, feed_id)
    if autopost.last_checked_at and timezone.now() - autopost.last_checked_at < CHECK_NOW_COOLDOWN:
        messages.info(request, "It was checked a moment ago. Try again in a minute.")
    else:
        result = services.poll(autopost, force=True)
        if result.status == "error":
            messages.error(request, result.error)
        else:
            messages.success(request, f"Checked: {result.new_items} new item(s), {result.posted} handled.")
    return redirect("autopost:detail", workspace_id=request.workspace.id, feed_id=feed.id)


@login_required
@require_permission("publish_directly")
@require_POST
def stop(request, workspace_id, feed_id):
    feed, autopost = _existing(request, feed_id)
    services.disable(autopost)
    messages.success(request, "Auto-posting is off. Posts already made stay where they are.")
    return redirect("autopost:detail", workspace_id=request.workspace.id, feed_id=feed.id)
