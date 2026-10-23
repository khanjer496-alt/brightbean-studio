"""Views for the Unified Social Inbox (F-3.1)."""

import json
import logging
from urllib.parse import urlencode

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import HttpResponse, JsonResponse, QueryDict
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.views.decorators.http import require_GET, require_POST

from apps.ai_writer import services as ai_services
from apps.ai_writer.client import ai_available
from apps.ai_writer.views import ai_action
from apps.autonomy import service as autonomy_service
from apps.autonomy.actors import human as human_actor
from apps.autonomy.models import ActionType, Decision
from apps.members.decorators import require_permission
from apps.members.models import WorkspaceMembership
from apps.notifications.engine import notify
from apps.notifications.models import EventType
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace

from . import feed as inbox_feed_queries
from . import services as inbox_services
from .forms import (
    AssignForm,
    BulkActionForm,
    InternalNoteForm,
    ReplyForm,
    SavedReplyForm,
    SentimentForm,
    SLAConfigForm,
    StatusForm,
    TriageForm,
)
from .models import (
    InboxMessage,
    InboxReply,
    InboxSLAConfig,
    InternalNote,
    SavedReply,
    rating_band,
)

logger = logging.getLogger(__name__)


def _detail_context(workspace, message, *, actor=None):
    """Build the full context needed for the message detail panel."""
    sla_config = InboxSLAConfig.objects.filter(workspace=workspace, is_active=True).first()
    saved_replies = _saved_replies_for(workspace, message)
    team_members = WorkspaceMembership.objects.filter(
        workspace=workspace,
    ).select_related("user")
    replies = list(message.replies.select_related("author"))
    notes = list(message.internal_notes.select_related("author"))
    # Sent replies sit in the chronological thread; drafts (and failed
    # sends awaiting a retry) are pending work, surfaced by the composer.
    sent_replies = [r for r in replies if r.status == InboxReply.Status.SENT]
    draft_replies = [r for r in replies if r.status != InboxReply.Status.SENT]
    thread = sorted(
        [("reply", r, r.sent_at or r.created_at) for r in sent_replies] + [("note", n, n.created_at) for n in notes],
        key=lambda x: x[2],
    )
    child_messages = InboxMessage.objects.filter(parent_message=message).select_related("social_account")
    return {
        "workspace": workspace,
        "message": message,
        "thread": thread,
        "draft_replies": draft_replies,
        "child_messages": child_messages,
        "sla_config": sla_config,
        "saved_replies": saved_replies,
        "team_members": team_members,
        "reply_form": ReplyForm(),
        "note_form": InternalNoteForm(),
        "status_choices": InboxMessage.Status.choices,
        "attachments": message.attachments,
        "reply_eligibility": inbox_services.reply_eligibility(message, origin="web_inbox", actor=actor),
        "saved_reply_options": _saved_reply_options(saved_replies, message),
        "ai_enabled": ai_available(workspace.organization),
    }


def _saved_replies_for(workspace, message):
    """The saved replies that fit ``message``: for a review its star band's and the unbanded ones, else only unbanded."""
    band = rating_band(message.rating) if message.message_type == InboxMessage.MessageType.REVIEW else ""
    replies = SavedReply.objects.for_workspace(workspace.id)
    if band:
        # The band's own replies come first.
        return sorted(replies.filter(rating_band__in=[band, ""]), key=lambda sr: sr.rating_band != band)
    return list(replies.filter(rating_band=""))


def _saved_reply_options(saved_replies, message):
    """Saved replies with their variables filled in for this message, for the ``/`` picker."""
    variables = {
        "sender_name": message.sender_name,
        "account_name": message.social_account.account_name,
        "post_url": getattr(message.related_post, "published_url", "") or "",
    }
    return [{"id": str(sr.id), "title": sr.title, "body": sr.render(variables)} for sr in saved_replies]


def _get_workspace(request, workspace_id):
    """Resolve workspace and enforce membership check."""
    workspace = get_object_or_404(Workspace, id=workspace_id)
    if not request.user.is_authenticated:
        raise PermissionDenied("Authentication required.")
    has_membership = WorkspaceMembership.objects.filter(
        user=request.user,
        workspace=workspace,
    ).exists()
    if not has_membership:
        raise PermissionDenied("You are not a member of this workspace.")
    return workspace


# --- Main Feed ---


@login_required
@require_permission("use_inbox")
def inbox_feed(request, workspace_id):
    """Main inbox feed with filtering, cursor pagination, and split-panel layout."""
    workspace = _get_workspace(request, workspace_id)

    cursor = request.GET.get("cursor", "")
    # SLA config for countdown display
    sla_config = InboxSLAConfig.objects.filter(workspace=workspace, is_active=True).first()
    context = {
        "workspace": workspace,
        "sla_config": sla_config,
        **inbox_feed_queries.feed_page(workspace, request.GET, request.user, cursor),
    }

    # A cursor request is "Load more": just the next rows and their sentinel.
    if cursor and request.htmx:
        return render(request, "inbox/partials/_message_page.html", context)

    # Team members for assignment dropdown
    team_members = WorkspaceMembership.objects.filter(
        workspace=workspace,
    ).select_related("user")

    # Connected accounts for filter dropdown
    social_accounts = SocialAccount.objects.filter(
        workspace=workspace,
        connection_status=SocialAccount.ConnectionStatus.CONNECTED,
    )

    context.update(
        {
            "team_members": team_members,
            "social_accounts": social_accounts,
            "status_choices": InboxMessage.Status.choices,
            "sentiment_choices": InboxMessage.Sentiment.choices,
        }
    )

    if request.htmx:
        return render(request, "inbox/partials/_message_list.html", context)
    return render(request, "inbox/feed.html", context)


def poll_new_messages(request, workspace_id):
    """The "N new messages" pill and the sidebar's unread count, polled by the feed.

    Counts messages matching the feed's filters that arrived after the list
    was rendered, so the list itself never moves under someone reading it.
    """
    if not request.user.is_authenticated:
        # 286 tells htmx to stop polling. A login redirect would be followed
        # and the login page swapped into the message list.
        return HttpResponse(status=286)
    return _poll_new_messages(request, workspace_id)


@login_required
@require_permission("use_inbox")
@require_GET
def _poll_new_messages(request, workspace_id):
    workspace = _get_workspace(request, workspace_id)
    since = request.GET.get("since", "")
    count = inbox_feed_queries.new_message_count(workspace, request.GET, request.user, since)
    idle, seconds = inbox_feed_queries.poll_backoff(request.GET, count)
    pairs = inbox_feed_queries.filter_pairs(request.GET)
    context = {
        "workspace": workspace,
        "new_count": count,
        "new_count_capped": count >= inbox_feed_queries.NEW_MESSAGES_CAP,
        # Screen readers hear about new messages once, not on every poll.
        "announce": count > 0 and str(count) != request.GET.get("seen"),
        "filter_query": urlencode(pairs),
        "poll_query": urlencode([*pairs, ("since", since), ("seen", count), ("idle", idle)]),
        "poll_seconds": seconds,
        "oob_badge": True,
        "sidebar_unread_inbox_count": inbox_feed_queries.unread_count(workspace),
    }
    # Rendered without ``request`` on purpose: this runs every 20 seconds per open
    # tab, and the page-wide context processors (sidebar, channels...) are not
    # needed by a fragment that only carries a pill and one badge.
    return HttpResponse(render_to_string("inbox/partials/_poll.html", context))


@login_required
@require_permission("reply_from_inbox")
@require_GET
def bulk_count(request, workspace_id):
    """How many messages match the feed's filters, for "select all matching"."""
    workspace = _get_workspace(request, workspace_id)
    qs, _active, _view = inbox_feed_queries.filtered_messages(workspace, request.GET, request.user)
    return JsonResponse({"count": qs.order_by().count(), "limit": inbox_services.BULK_SELECT_ALL_MAX})


@login_required
@require_permission("use_inbox")
def message_detail(request, workspace_id, message_id):
    """Message detail with thread, replies, notes, and reply composer."""
    workspace = _get_workspace(request, workspace_id)
    message = get_object_or_404(
        InboxMessage.objects.select_related("social_account", "assigned_to"),
        id=message_id,
        workspace=workspace,
    )

    # Mark as read → open
    if message.status == InboxMessage.Status.UNREAD:
        message.status = InboxMessage.Status.OPEN
        message.save(update_fields=["status"])

    context = _detail_context(workspace, message, actor=request.user)

    if request.htmx:
        return render(request, "inbox/partials/_message_panel.html", context)
    return render(request, "inbox/message_detail.html", context)


# --- Reply ---


def _get_workspace_reply(workspace, reply_id):
    return get_object_or_404(
        InboxReply.objects.select_related("inbox_message", "inbox_message__social_account", "author"),
        id=reply_id,
        inbox_message__workspace=workspace,
    )


@login_required
@require_permission("reply_from_inbox")
@require_POST
def send_reply(request, workspace_id, message_id):
    """Send a reply via the platform API and record it."""
    workspace = _get_workspace(request, workspace_id)
    message = get_object_or_404(InboxMessage, id=message_id, workspace=workspace)

    form = ReplyForm(request.POST)
    if not form.is_valid():
        return HttpResponse("Invalid reply.", status=400)

    body = form.cleaned_data["body"]
    account = message.social_account

    # A reply is only recorded if the platform accepted it. Recording it
    # regardless would show the team a sent reply the customer never got.
    try:
        reply = inbox_services.send_reply(message=message, body=body, author=request.user, origin="web_inbox")
    except Exception as exc:
        logger.warning(
            "Reply failed: message_id=%s platform=%s error_type=%s", message.id, account.platform, type(exc).__name__
        )
        response = render(
            request,
            "inbox/partials/_reply_error.html",
            {
                "platform_label": account.get_platform_display(),
                "reason": inbox_services.reply_failure_reason(exc),
            },
        )
        # htmx does not swap on a 4xx/5xx, so the failure is reported as a
        # normal swap plus a header the composer reads to keep the draft text.
        response["HX-Reply-Failed"] = "1"
        return response

    autonomy_service.record(
        workspace=workspace,
        actor=human_actor(request.user),
        action_type=ActionType.REPLY,
        operation="reply.send",
        decision=Decision.EXECUTED,
        account=account,
        reply=reply,
        final_text=body,
        summary="Sent from the inbox.",
    )
    context = {"reply": reply, "workspace": workspace, "message": message}
    return render(request, "inbox/partials/_reply_item.html", context)


@login_required
@require_permission("reply_from_inbox")
@require_POST
def delete_review_reply(request, workspace_id, message_id):
    """Take the public reply off a review, on the platform and here."""
    workspace = _get_workspace(request, workspace_id)
    message = get_object_or_404(
        InboxMessage.objects.select_related("social_account"), id=message_id, workspace=workspace
    )
    try:
        inbox_services.delete_review_reply(message)
    except ValueError as exc:
        return HttpResponse(str(exc), status=409)
    except Exception as exc:
        logger.warning("Review reply delete failed: message_id=%s error_type=%s", message.id, type(exc).__name__)
        return HttpResponse(inbox_services.reply_failure_reason(exc), status=502)
    return render(
        request, "inbox/partials/_message_panel.html", _detail_context(workspace, message, actor=request.user)
    )


@login_required
@require_permission("use_inbox")
@require_POST
def save_reply_draft(request, workspace_id, message_id):
    """Save a reply as a draft without sending it."""
    workspace = _get_workspace(request, workspace_id)
    message = get_object_or_404(InboxMessage, id=message_id, workspace=workspace)

    form = ReplyForm(request.POST)
    if not form.is_valid():
        return HttpResponse("Invalid reply.", status=400)

    try:
        inbox_services.create_reply_draft(
            message=message,
            body=form.cleaned_data["body"],
            author=request.user,
        )
    except ValueError as exc:
        return HttpResponse(str(exc), status=400)

    message.refresh_from_db()
    return render(
        request, "inbox/partials/_message_panel.html", _detail_context(workspace, message, actor=request.user)
    )


@login_required
@require_permission("reply_from_inbox")
@require_POST
def send_reply_draft(request, workspace_id, reply_id):
    """Deliver an existing draft reply to the platform."""
    workspace = _get_workspace(request, workspace_id)
    reply = _get_workspace_reply(workspace, reply_id)
    message = reply.inbox_message

    failed = False
    try:
        inbox_services.send_reply_now(reply, actor=request.user, origin="web_inbox")
    except inbox_services.ReplyStateError as exc:
        return HttpResponse(str(exc), status=409)
    except Exception:
        # The draft is kept (now in ``failed`` state) so the team can retry
        # or discard it; the re-rendered panel shows it with failed styling.
        logger.exception("Failed to send draft reply %s", reply.id)
        failed = True

    message.refresh_from_db()
    panel = render(
        request, "inbox/partials/_message_panel.html", _detail_context(workspace, message, actor=request.user)
    )
    if failed:
        panel["HX-Reply-Failed"] = "1"
    return panel


@login_required
@require_permission("use_inbox")
@require_POST
def discard_reply_draft(request, workspace_id, reply_id):
    """Delete a draft (or failed) reply."""
    workspace = _get_workspace(request, workspace_id)
    reply = _get_workspace_reply(workspace, reply_id)
    message = reply.inbox_message

    try:
        inbox_services.discard_reply_draft(reply)
    except inbox_services.ReplyStateError as exc:
        return HttpResponse(str(exc), status=409)

    message.refresh_from_db()
    return render(
        request, "inbox/partials/_message_panel.html", _detail_context(workspace, message, actor=request.user)
    )


# --- Internal Note ---


@login_required
@require_permission("reply_from_inbox")
@require_POST
def add_note(request, workspace_id, message_id):
    """Add an internal note to a message."""
    workspace = _get_workspace(request, workspace_id)
    message = get_object_or_404(InboxMessage, id=message_id, workspace=workspace)

    form = InternalNoteForm(request.POST)
    if not form.is_valid():
        return HttpResponse("Invalid note.", status=400)

    note = InternalNote.objects.create(
        inbox_message=message,
        author=request.user,
        body=form.cleaned_data["body"],
    )

    context = {"note": note, "workspace": workspace, "message": message}
    return render(request, "inbox/partials/_note_item.html", context)


# --- Assignment ---


@login_required
@require_permission("reply_from_inbox")
@require_POST
def assign_message(request, workspace_id, message_id):
    """Assign a message to a team member."""
    workspace = _get_workspace(request, workspace_id)
    message = get_object_or_404(InboxMessage, id=message_id, workspace=workspace)

    form = AssignForm(request.POST)
    if not form.is_valid():
        return HttpResponse("Invalid assignment.", status=400)

    assigned_to_id = form.cleaned_data.get("assigned_to")
    if assigned_to_id:
        # Verify the user is a workspace member
        membership = (
            WorkspaceMembership.objects.filter(workspace=workspace, user_id=assigned_to_id)
            .select_related("user")
            .first()
        )
        if not membership:
            return HttpResponse("User is not a workspace member.", status=400)
        message.assigned_to = membership.user
    else:
        message.assigned_to = None

    message.save(update_fields=["assigned_to"])

    # Notify the assignee
    if message.assigned_to and message.assigned_to != request.user:
        notify(
            user=message.assigned_to,
            event_type=EventType.NEW_INBOX_MESSAGE,
            title=f"You were assigned a {message.get_message_type_display()}",
            body=f"From {message.sender_name}: {message.body[:100]}",
            data={
                "message_id": str(message.id),
                "workspace_id": str(workspace.id),
            },
        )

    context = _detail_context(workspace, message, actor=request.user)
    return render(request, "inbox/partials/_message_panel.html", context)


# --- Status ---


@login_required
@require_permission("reply_from_inbox")
@require_POST
def change_status(request, workspace_id, message_id):
    """Change message status."""
    workspace = _get_workspace(request, workspace_id)
    message = get_object_or_404(InboxMessage, id=message_id, workspace=workspace)

    form = StatusForm(request.POST)
    if not form.is_valid():
        return HttpResponse("Invalid status.", status=400)

    message.status = form.cleaned_data["status"]
    message.save(update_fields=["status"])

    context = _detail_context(workspace, message, actor=request.user)
    return render(request, "inbox/partials/_message_panel.html", context)


# --- Sentiment Override ---


@login_required
@require_permission("reply_from_inbox")
@require_POST
def change_sentiment(request, workspace_id, message_id):
    """Override sentiment manually."""
    workspace = _get_workspace(request, workspace_id)
    message = get_object_or_404(InboxMessage, id=message_id, workspace=workspace)

    form = SentimentForm(request.POST)
    if not form.is_valid():
        return HttpResponse("Invalid sentiment.", status=400)

    message.sentiment = form.cleaned_data["sentiment"]
    message.sentiment_source = InboxMessage.SentimentSource.MANUAL
    message.save(update_fields=["sentiment", "sentiment_source"])

    context = {"message": message, "workspace": workspace}
    return render(request, "inbox/partials/_sentiment_badge.html", context)


# --- Bulk Actions ---


@login_required
@require_permission("reply_from_inbox")
@require_POST
def bulk_action(request, workspace_id):
    """Perform bulk actions on selected messages, or on every message matching the filters."""
    workspace = _get_workspace(request, workspace_id)

    form = BulkActionForm(request.POST)
    if not form.is_valid():
        return HttpResponse(" ".join(e for errors in form.errors.values() for e in errors), status=400)

    data = form.cleaned_data
    filters = QueryDict(data["filters"])
    if data["select_all"]:
        matching, _active, _view = inbox_feed_queries.filtered_messages(workspace, filters, request.user)
        message_ids = list(matching.order_by().values_list("id", flat=True)[: inbox_services.BULK_SELECT_ALL_MAX + 1])
        if len(message_ids) > inbox_services.BULK_SELECT_ALL_MAX:
            return HttpResponse(
                f"Too many messages match to act on at once (limit {inbox_services.BULK_SELECT_ALL_MAX}). Narrow the filters.",
                status=400,
            )
        if len(message_ids) != data["expected_count"]:
            return HttpResponse(
                f"The inbox changed: {len(message_ids)} messages match now, not {data['expected_count']}. "
                "Nothing was changed; review and try again.",
                status=409,
            )
    else:
        message_ids = data["message_ids"]

    try:
        changed = inbox_services.bulk_update(
            workspace, message_ids, action=data["action"], value=data["value"], actor=request.user
        )
    except inbox_services.BulkActionError as exc:
        return HttpResponse(str(exc), status=400)

    # Re-render the list under the same filters the person was looking at.
    context = {
        "workspace": workspace,
        "sla_config": InboxSLAConfig.objects.filter(workspace=workspace, is_active=True).first(),
        **inbox_feed_queries.feed_page(workspace, filters, request.user),
    }
    response = render(request, "inbox/partials/_message_list.html", context)
    response["HX-Trigger"] = json.dumps({"inboxBulkDone": {"count": changed}})
    return response


# --- Keyboard triage ---


@login_required
@require_permission("reply_from_inbox")
@require_POST
def triage_message(request, workspace_id, message_id):
    """Apply one keyboard-triage action and return the updated list row."""
    workspace = _get_workspace(request, workspace_id)
    message = get_object_or_404(
        InboxMessage.objects.select_related("social_account", "assigned_to"), id=message_id, workspace=workspace
    )

    form = TriageForm(request.POST)
    if not form.is_valid():
        return HttpResponse("Invalid action.", status=400)

    inbox_services.apply_triage(message, form.cleaned_data["action"], actor=request.user)
    sla_config = InboxSLAConfig.objects.filter(workspace=workspace, is_active=True).first()
    return render(
        request,
        "inbox/partials/_message_row.html",
        {"workspace": workspace, "message": message, "sla_config": sla_config, "active_filters": {}},
    )


# --- AI reply draft ---


@ai_action("reply_from_inbox")
def ai_draft_reply(request, ctx, message_id):
    """Propose a reply to a message, in the workspace's brand voice (never sent; the person edits it)."""
    message = get_object_or_404(
        InboxMessage.objects.select_related("social_account", "parent_message"),
        id=message_id,
        workspace=ctx.workspace,
    )
    thread = []
    if message.parent_message_id:
        parent = message.parent_message
        thread.append(("customer", parent.sender_name, parent.body))
    sent_replies = message.replies.filter(status=InboxReply.Status.SENT).select_related("author")
    for reply in sent_replies:
        thread.append(("brand", reply.author.display_name if reply.author else "Team", reply.body))
    if message.message_type == InboxMessage.MessageType.REVIEW:
        text = ai_services.draft_review_reply(
            ctx, message.body, rating=message.rating, reviewer=message.sender_name, account=message.social_account
        )
        return render(request, "inbox/partials/_ai_draft_result.html", {"text": text})
    text = ai_services.draft_reply(
        ctx, message.body, sender=message.sender_name, thread=thread, account=message.social_account
    )
    return render(request, "inbox/partials/_ai_draft_result.html", {"text": text})


# --- Saved Replies ---


@login_required
@require_permission("manage_workspace_settings")
def saved_replies_list(request, workspace_id):
    """List saved replies for a workspace."""
    workspace = _get_workspace(request, workspace_id)
    replies = SavedReply.objects.for_workspace(workspace.id)

    context = {"workspace": workspace, "saved_replies": replies}
    return render(request, "inbox/saved_replies.html", context)


@login_required
@require_permission("manage_workspace_settings")
def saved_reply_create(request, workspace_id):
    """Create a new saved reply."""
    workspace = _get_workspace(request, workspace_id)

    if request.method == "POST":
        form = SavedReplyForm(request.POST)
        if form.is_valid():
            reply = form.save(commit=False)
            reply.workspace = workspace
            reply.created_by = request.user
            reply.save()
            return redirect("inbox:saved_replies", workspace_id=workspace.id)
    else:
        form = SavedReplyForm()

    context = {"workspace": workspace, "form": form}

    if request.htmx:
        return render(request, "inbox/partials/_saved_reply_form.html", context)
    return render(request, "inbox/saved_replies.html", context)


@login_required
@require_permission("manage_workspace_settings")
def saved_reply_edit(request, workspace_id, reply_id):
    """Edit an existing saved reply."""
    workspace = _get_workspace(request, workspace_id)
    reply = get_object_or_404(SavedReply, id=reply_id, workspace=workspace)

    if request.method == "POST":
        form = SavedReplyForm(request.POST, instance=reply)
        if form.is_valid():
            form.save()
            return redirect("inbox:saved_replies", workspace_id=workspace.id)
    else:
        form = SavedReplyForm(instance=reply)

    context = {"workspace": workspace, "form": form, "saved_reply": reply}

    if request.htmx:
        return render(request, "inbox/partials/_saved_reply_form.html", context)
    return render(request, "inbox/saved_replies.html", context)


@login_required
@require_permission("manage_workspace_settings")
@require_POST
def saved_reply_delete(request, workspace_id, reply_id):
    """Delete a saved reply."""
    workspace = _get_workspace(request, workspace_id)
    reply = get_object_or_404(SavedReply, id=reply_id, workspace=workspace)
    reply.delete()
    return redirect("inbox:saved_replies", workspace_id=workspace.id)


# --- SLA Config ---


@login_required
@require_permission("manage_workspace_settings")
def sla_config(request, workspace_id):
    """Configure SLA settings for inbox."""
    workspace = _get_workspace(request, workspace_id)
    config, _created = InboxSLAConfig.objects.get_or_create(
        workspace=workspace,
        defaults={"target_response_minutes": 120, "is_active": False},
    )

    if request.method == "POST":
        form = SLAConfigForm(request.POST, instance=config)
        if form.is_valid():
            form.save()
            return redirect("inbox:feed", workspace_id=workspace.id)
    else:
        form = SLAConfigForm(instance=config)

    context = {"workspace": workspace, "form": form, "config": config}
    return render(request, "inbox/sla_config.html", context)
