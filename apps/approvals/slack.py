"""Approval requests in Slack: connect a channel, post Approve / Request changes buttons, act on them.

This is its own Slack install (``SlackApprovalConnection``), separate from
publishing to Slack, because it needs scopes publishing does not (reading
members' verified emails, opening the request-changes dialog). It reuses the
Slack app's client id and secret, and needs the app's signing secret
(``signing_secret`` in the Slack credentials) to verify what Slack sends back;
without all three the feature is hidden.

Every request Slack sends to the interactions endpoint is checked with Slack's
v0 signature and a five-minute replay window before anything is read from it.
A Slack user acts as the PostDelegate member whose *verified* email matches
their confirmed Slack email; anyone else is sent a link instead.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from urllib.parse import parse_qs, urlparse

import httpx
from allauth.account.models import EmailAddress
from background_task import background
from django.conf import settings
from django.core import signing
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.urls import reverse

from apps.accounts.models import User
from apps.credentials.models import resolve_platform_credentials
from apps.members.models import WorkspaceMembership

from . import decisions
from .models import Channel, SlackApprovalConnection

logger = logging.getLogger(__name__)

API_BASE = "https://slack.com/api"
AUTH_URL = "https://slack.com/oauth/v2/authorize"
SCOPES = ["chat:write", "channels:read", "groups:read", "channels:join", "users:read", "users:read.email"]
REPLAY_WINDOW_SECONDS = 60 * 5
STATE_SALT = "approvals.slack_oauth"
STATE_MAX_AGE = 600
TIMEOUT = 8.0

ACTION_APPROVE = "approval_approve"
ACTION_CHANGES = "approval_changes"
ACTION_HELD_APPROVE = "held_approve"
ACTION_HELD_DISMISS = "held_dismiss"
MODAL_CALLBACK = "approval_changes_modal"
COMMENT_BLOCK = "comment_block"
COMMENT_ACTION = "comment"


# --------------------------------------------------------------------------- credentials


def app_credentials(workspace) -> dict:
    return resolve_platform_credentials("slack", workspace.organization_id, workspace.id)


def is_available(workspace) -> bool:
    """Whether the Slack app is fully configured; the feature is hidden otherwise."""
    creds = app_credentials(workspace)
    return all(str(creds.get(key) or "").strip() for key in ("client_id", "client_secret", "signing_secret"))


def signing_secret_for(connection) -> str:
    return str(app_credentials(connection.workspace).get("signing_secret") or "")


# --------------------------------------------------------------------------- signatures


def compute_signature(secret: str, timestamp: str, body: bytes) -> str:
    base = b"v0:" + timestamp.encode() + b":" + body
    return "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()


def verify_signature(secret: str, timestamp: str, signature: str, body: bytes, *, now: float | None = None) -> bool:
    """Slack's v0 check: a fresh timestamp (within five minutes) and a matching HMAC."""
    if not secret or not timestamp or not signature:
        return False
    try:
        age = abs((time.time() if now is None else now) - int(timestamp))
    except ValueError:
        return False
    if age > REPLAY_WINDOW_SECONDS:
        return False
    return hmac.compare_digest(compute_signature(secret, timestamp, body), signature)


# --------------------------------------------------------------------------- Web API


def api_call(method: str, token: str, payload: dict) -> dict:
    """Call a Slack Web API method with a JSON body; ``ok: false`` answers come back as-is."""
    response = httpx.post(
        f"{API_BASE}/{method}",
        json=payload,
        headers={"Authorization": f"Bearer {token}"},
        timeout=TIMEOUT,
    )
    return response.json()


def _get(method: str, token: str, params: dict) -> dict:
    response = httpx.get(
        f"{API_BASE}/{method}", params=params, headers={"Authorization": f"Bearer {token}"}, timeout=TIMEOUT
    )
    return response.json()


# --------------------------------------------------------------------------- connecting


def redirect_uri() -> str:
    return f"{settings.APP_URL.rstrip('/')}{reverse('approval_slack:callback')}"


def authorize_url(workspace, user) -> str:
    from urllib.parse import urlencode

    state = signing.dumps({"w": str(workspace.id), "u": str(user.pk)}, salt=STATE_SALT)
    params = {
        "client_id": app_credentials(workspace)["client_id"],
        "scope": ",".join(SCOPES),
        "redirect_uri": redirect_uri(),
        "state": state,
    }
    return f"{AUTH_URL}?{urlencode(params)}"


def read_state(state: str) -> dict | None:
    try:
        return signing.loads(state, salt=STATE_SALT, max_age=STATE_MAX_AGE)
    except signing.BadSignature:
        return None


def connect(workspace, user, code: str) -> SlackApprovalConnection:
    """Finish the install: exchange ``code`` and keep the bot token. Raises ValueError with a user-safe reason."""
    creds = app_credentials(workspace)
    response = httpx.post(
        f"{API_BASE}/oauth.v2.access",
        data={
            "client_id": creds["client_id"],
            "client_secret": creds["client_secret"],
            "code": code,
            "redirect_uri": redirect_uri(),
        },
        timeout=TIMEOUT,
    ).json()
    if not response.get("ok") or not response.get("access_token"):
        raise ValueError("Slack didn't accept the connection. Please try again.")
    granted = set((response.get("scope") or "").split(","))
    missing = [scope for scope in SCOPES if scope not in granted]
    if missing:
        raise ValueError(f"Slack didn't grant: {', '.join(missing)}. Reconnect and approve every permission.")
    team = response.get("team") or {}
    connection, _ = SlackApprovalConnection.objects.update_or_create(
        workspace=workspace,
        defaults={
            "team_id": team.get("id", ""),
            "team_name": team.get("name", ""),
            "bot_token": response["access_token"],
            "channel_id": "",
            "channel_name": "",
            "connected_by": user,
        },
    )
    return connection


def list_channels(connection) -> list[dict]:
    data = _get(
        "conversations.list",
        connection.bot_token,
        {"types": "public_channel,private_channel", "exclude_archived": "true", "limit": "200"},
    )
    return [{"id": c["id"], "name": c["name"]} for c in data.get("channels", [])] if data.get("ok") else []


def choose_channel(connection, channel_id: str) -> None:
    """Pick the channel for requests: joins a public channel; a private one needs the app invited."""
    channels = {c["id"]: c["name"] for c in list_channels(connection)}
    if channel_id not in channels:
        raise ValueError("That channel isn't available to the Slack app.")
    api_call("conversations.join", connection.bot_token, {"channel": channel_id})
    connection.channel_id = channel_id
    connection.channel_name = f"#{channels[channel_id]}"
    connection.save(update_fields=["channel_id", "channel_name", "updated_at"])


def settings_context(workspace) -> dict:
    """What the approvals settings page needs; ``available`` False hides the Slack card entirely."""
    if not is_available(workspace):
        return {"available": False}
    connection = SlackApprovalConnection.objects.filter(workspace=workspace).first()
    return {
        "available": True,
        "connection": connection,
        "channels": list_channels(connection) if connection and not connection.channel_id else [],
    }


# --------------------------------------------------------------------------- requests


def _app_link(workspace) -> str:
    path = reverse("calendar:calendar", kwargs={"workspace_id": workspace.id})
    return f"{settings.APP_URL.rstrip('/')}{path}?mode=list&tab=approvals"


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def request_blocks(post, targets, fingerprint: str, *, resolved: str = "") -> list[dict]:
    """The message: what each network will publish, then the two buttons (or the outcome once decided)."""
    from apps.composer.previews import post_previews

    previews = post_previews(post, targets)["previews"]
    blocks: list[dict] = [
        {"type": "header", "text": {"type": "plain_text", "text": "Post waiting for your approval"[:150]}}
    ]
    for p in previews:
        lines = [f"*{_escape(p['account'].get_platform_display())}* · {_escape(p['account'].account_name or '')}"]
        if p["title"] and p["needs_title"]:
            lines.append(f"*Title:* {_escape(p['title'])}")
        lines.append(_escape(p["truncated_caption"] or "_No caption_")[:1500])
        for part in (p["thread"] or {}).get("parts", []):
            lines.append(f"_Part {part['number']}:_ {_escape(part['caption'])[:300]}")
        if p["first_comment"]:
            lines.append(f"*First comment:* {_escape(p['first_comment'])[:300]}")
        lines.extend(f"*{_escape(row['label'])}:* {_escape(row['value'])}" for row in p["settings"])
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)[:2900]}})
    if resolved:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": _escape(resolved)}]})
        return blocks
    value = f"{post.id}|{fingerprint}"
    blocks.append(
        {
            "type": "actions",
            "block_id": "approval_actions",
            "elements": [
                {
                    "type": "button",
                    "style": "primary",
                    "action_id": ACTION_APPROVE,
                    "text": {"type": "plain_text", "text": "Approve"},
                    "value": value,
                },
                {
                    "type": "button",
                    "action_id": ACTION_CHANGES,
                    "text": {"type": "plain_text", "text": "Request changes"},
                    "value": value,
                },
            ],
        }
    )
    return blocks


def post_request(workspace, post) -> bool:
    """Post the approval request for ``post`` to the workspace's channel; False when nothing was sent."""
    connection = SlackApprovalConnection.objects.filter(workspace=workspace).first()
    if connection is None or not connection.is_ready or not is_available(workspace):
        return False
    states = {"pending_review", "pending_client"}
    targets = list(
        post.platform_posts.filter(status__in=states)
        .select_related("social_account")
        .order_by("social_account__platform")
    )
    if not targets:
        return False
    from .policy import targets_fingerprint

    fingerprint = targets_fingerprint(targets)
    data = api_call(
        "chat.postMessage",
        connection.bot_token,
        {
            "channel": connection.channel_id,
            "text": "A post is waiting for your approval.",
            "blocks": request_blocks(post, targets, fingerprint),
        },
    )
    if not data.get("ok"):
        logger.warning("Slack approval request failed for workspace %s: %s", workspace.id, data.get("error"))
    return bool(data.get("ok"))


@background(schedule=0)
def post_request_task(post_id):
    from apps.composer.models import Post

    post = Post.objects.select_related("workspace").filter(pk=post_id).first()
    if post is not None:
        post_request(post.workspace, post)


def queue_request(post, workspace) -> None:
    """Ask for the Slack message in the background; a no-op unless Slack approvals are connected."""
    if SlackApprovalConnection.objects.filter(workspace=workspace).exclude(channel_id="").exists():
        post_request_task(str(post.id))


def held_blocks(action, *, resolved: str = "") -> list[dict]:
    """A held agent item: what was held and why, then Approve / Dismiss (or the outcome once decided)."""
    account = action.social_account.account_name if action.social_account else action.account_label
    lines = [f"*{_escape(action.actor_name)}* · {_escape(action.get_action_type_display())}"]
    if account:
        lines[0] += f" · {_escape(account)}"
    lines.append(_escape(action.summary)[:1200])
    if action.final_text:
        lines.append(">" + _escape(action.final_text)[:1500].replace("\n", "\n>"))
    blocks: list[dict] = [
        {"type": "header", "text": {"type": "plain_text", "text": "Held for review by your autonomy settings"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)[:2900]}},
    ]
    if resolved:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": _escape(resolved)}]})
        return blocks
    elements = []
    if decisions.can_approve_from_outside(action):
        elements.append(
            {
                "type": "button",
                "style": "primary",
                "action_id": ACTION_HELD_APPROVE,
                "text": {"type": "plain_text", "text": "Approve"},
                "value": str(action.id),
            }
        )
    elements.append(
        {
            "type": "button",
            "action_id": ACTION_HELD_DISMISS,
            "text": {"type": "plain_text", "text": "Dismiss"},
            "value": str(action.id),
        }
    )
    blocks.append({"type": "actions", "block_id": "held_actions", "elements": elements})
    return blocks


def post_held_request(action) -> bool:
    """Post a held agent item to the workspace's approval channel; False when nothing was sent."""
    connection = SlackApprovalConnection.objects.filter(workspace_id=action.workspace_id).first()
    if connection is None or not connection.is_ready or not is_available(connection.workspace):
        return False
    data = api_call(
        "chat.postMessage",
        connection.bot_token,
        {"channel": connection.channel_id, "text": "Agent work held for review.", "blocks": held_blocks(action)},
    )
    if not data.get("ok"):
        logger.warning("Slack held-item request failed for workspace %s: %s", action.workspace_id, data.get("error"))
    return bool(data.get("ok"))


@background(schedule=0)
def post_held_request_task(action_id):
    from apps.autonomy.models import AgentAction

    action = AgentAction.objects.select_related("social_account").filter(pk=action_id).first()
    if action is not None and action.decision == "held" and not action.review_outcome:
        post_held_request(action)


def queue_held_request(action) -> None:
    """Ask for the Slack message about a held agent item; a no-op unless Slack approvals are connected."""
    if SlackApprovalConnection.objects.filter(workspace_id=action.workspace_id).exclude(channel_id="").exists():
        post_held_request_task(str(action.id))


# --------------------------------------------------------------------------- interactions


def member_for_slack_user(connection, slack_user_id: str):
    """The PostDelegate user behind a Slack user, matched on verified email on both sides, or None."""
    data = _get("users.info", connection.bot_token, {"user": slack_user_id})
    user = data.get("user") or {}
    profile = user.get("profile") or {}
    email = (profile.get("email") or "").strip().lower()
    if not data.get("ok") or not email or not user.get("is_email_confirmed", False):
        return None
    # A bot, a deactivated account, or someone from another Slack team never maps.
    if user.get("is_bot") or user.get("deleted") or user.get("team_id") not in (None, connection.team_id):
        return None
    candidate = User.objects.filter(email__iexact=email, is_active=True).first()
    if (
        candidate is None
        or not EmailAddress.objects.filter(user=candidate, email__iexact=email, verified=True).exists()
    ):
        return None
    if not WorkspaceMembership.objects.filter(user=candidate, workspace=connection.workspace).exists():
        return None
    return candidate


def _verified_connections(payload: dict, timestamp: str, signature: str, body: bytes):
    team_id = (payload.get("team") or {}).get("id") or (payload.get("user") or {}).get("team_id") or ""
    if not team_id:
        return []
    connections = SlackApprovalConnection.objects.filter(team_id=team_id).select_related("workspace")
    return [c for c in connections if verify_signature(signing_secret_for(c), timestamp, signature, body)]


def _post_for(connection, post_id):
    from apps.composer.models import Post

    try:
        return Post.objects.filter(pk=post_id, workspace=connection.workspace).first()
    except (ValueError, ValidationError):
        return None


def _held_for(connection, action_id):
    from apps.autonomy.models import AgentAction

    try:
        return (
            AgentAction.objects.select_related("social_account", "workspace")
            .filter(pk=action_id, workspace=connection.workspace)
            .first()
        )
    except (ValueError, ValidationError):
        return None


def _ephemeral(text: str) -> dict:
    return {"response_type": "ephemeral", "replace_original": False, "text": text}


def _respond(response_url: str, body: dict) -> None:
    # response_url comes from a signed payload, but only ever post to Slack's own host.
    if urlparse(response_url).hostname not in ("hooks.slack.com",):
        return
    try:
        httpx.post(response_url, json=body, timeout=TIMEOUT)
    except httpx.HTTPError:
        logger.warning("Could not reach Slack's response_url", exc_info=True)


@background(schedule=0)
def respond_task(response_url, text):
    _respond(response_url, _ephemeral(text))


def _split_value(value: str) -> tuple[str, str]:
    post_id, _, fingerprint = (value or "").partition("|")
    return post_id, fingerprint


def _link_text(connection) -> str:
    return f"I couldn't match your Slack account to a verified PostDelegate member. Review it here instead: {_app_link(connection.workspace)}"


def _outcome_text(decision: str, user) -> str:
    verb = "Approved" if decision == decisions.APPROVE else "Changes requested"
    return f"{verb} by {user.display_name} via Slack."


def _decide(connection, slack_user_id, post_id, fingerprint, decision, comment=""):
    """Run the decision as the mapped member; returns ``(user, error)``."""
    user = member_for_slack_user(connection, slack_user_id)
    if user is None:
        return None, _link_text(connection)
    post = _post_for(connection, post_id)
    if post is None:
        return user, "That post no longer exists."
    try:
        decisions.decide(
            post, user, connection.workspace, decision, comment, channel=Channel.SLACK, expected_fingerprint=fingerprint
        )
    except decisions.DecisionError as exc:
        return user, str(exc)
    return user, ""


def _decide_held(connection, slack_user_id, action_id, verb):
    """Approve or dismiss a held agent item as the mapped member, through the autonomy service."""
    user = member_for_slack_user(connection, slack_user_id)
    if user is None:
        return None, None, _link_text(connection)
    action = _held_for(connection, action_id)
    if action is None:
        return user, None, "That item no longer exists."
    try:
        decisions.decide_held(action, user, verb, channel=Channel.SLACK)
    except decisions.DecisionError as exc:
        return user, action, str(exc)
    return user, action, ""


def _update_message(connection, channel: str, ts: str, post_id: str, text: str) -> None:
    post = _post_for(connection, post_id)
    if post is None or not channel or not ts:
        return
    targets = list(post.platform_posts.select_related("social_account").order_by("social_account__platform"))
    api_call(
        "chat.update",
        connection.bot_token,
        {"channel": channel, "ts": ts, "text": text, "blocks": request_blocks(post, targets, "", resolved=text)},
    )


def _tell(connection, payload: dict, text: str) -> None:
    """A private note to whoever clicked: through ``response_url`` when Slack gave one, else an ephemeral message."""
    if payload.get("response_url"):
        _respond(payload["response_url"], _ephemeral(text))
        return
    channel = payload.get("_channel", "")
    user = (payload.get("user") or {}).get("id", "")
    if channel and user:
        api_call("chat.postEphemeral", connection.bot_token, {"channel": channel, "user": user, "text": text})


def _submission_fields(payload: dict) -> tuple[str, str, str, str, str]:
    view = payload["view"]
    post_id, fingerprint, channel, ts = (json.loads(view.get("private_metadata") or "[]") + ["", "", "", ""])[:4]
    comment = (
        ((view.get("state") or {}).get("values") or {}).get(COMMENT_BLOCK, {}).get(COMMENT_ACTION, {}).get("value")
        or ""
    ).strip()
    return post_id, fingerprint, channel, ts, comment


def acknowledge_interaction(payload: dict, connection) -> dict | None:
    """The part of an interaction that must happen inside Slack's three-second window.

    Slack wants every interaction answered with a 200 within three seconds and
    a ``trigger_id`` is only good for three seconds, so opening the dialog is
    the one Slack call made here. Matching the Slack user to a member, making
    the decision and updating the message (which each cost a Slack round trip
    or more) are queued as :func:`process_interaction_task`. The return value
    is the JSON body for Slack (or None for an empty 200).
    https://docs.slack.dev/interactivity/handling-user-interaction
    """
    kind = payload.get("type")
    if kind == "block_actions":
        action = (payload.get("actions") or [{}])[0]
        action_id = action.get("action_id")
        if action_id == ACTION_CHANGES:
            post_id, fingerprint = _split_value(action.get("value", ""))
            api_call(
                "views.open",
                connection.bot_token,
                {
                    "trigger_id": payload.get("trigger_id", ""),
                    "view": _changes_modal(
                        post_id,
                        fingerprint,
                        (payload.get("channel") or {}).get("id", ""),
                        (payload.get("message") or {}).get("ts", ""),
                    ),
                },
            )
        elif action_id in (ACTION_APPROVE, ACTION_HELD_APPROVE, ACTION_HELD_DISMISS):
            process_interaction_task(json.dumps(payload), str(connection.pk))
        return None

    if kind == "view_submission" and (payload.get("view") or {}).get("callback_id") == MODAL_CALLBACK:
        # An empty comment is answered in the modal itself, which only the
        # response to this request can do.
        if not _submission_fields(payload)[4]:
            return {"response_action": "errors", "errors": {COMMENT_BLOCK: "Say what should change."}}
        process_interaction_task(json.dumps(payload), str(connection.pk))
        return {"response_action": "clear"}
    return None


@background(schedule=0)
def process_interaction_task(payload_json, connection_id):
    """Run a queued interaction once; whatever stops it is told to the person who clicked.

    Everything is looked up again here (the connection, the Slack user's
    member, the post or held item and its state), because minutes may have
    passed since the click. A failure is not retried by the queue: the person
    is told and pointed at the app, where the item shows where it stands.
    """
    payload = json.loads(payload_json)
    connection = SlackApprovalConnection.objects.select_related("workspace").filter(pk=connection_id).first()
    if connection is None:
        if payload.get("response_url"):
            _respond(payload["response_url"], _ephemeral("Slack approvals were disconnected from that workspace."))
        return
    try:
        handle_interaction(payload, connection)
    except Exception:
        logger.exception("Slack interaction failed")
        channel = (payload.get("channel") or {}).get("id", "")
        if not channel and payload.get("view"):
            try:
                channel = _submission_fields(payload)[2]
            except (ValueError, KeyError, TypeError):
                channel = ""
        text = f"Something went wrong handling that. Check where it stands here: {_app_link(connection.workspace)}"
        try:
            _tell(connection, {**payload, "_channel": channel}, text)
        except Exception:
            logger.warning("Could not tell the Slack user about the failure", exc_info=True)


def handle_interaction(payload: dict, connection) -> None:
    """Do the work behind an acknowledged interaction: map the Slack user, decide, update the message."""
    kind = payload.get("type")
    slack_user = (payload.get("user") or {}).get("id", "")

    if kind == "block_actions":
        action = (payload.get("actions") or [{}])[0]
        post_id, fingerprint = _split_value(action.get("value", ""))
        channel = (payload.get("channel") or {}).get("id", "")
        ts = (payload.get("message") or {}).get("ts", "")
        payload = {**payload, "_channel": channel}
        if action.get("action_id") in (ACTION_HELD_APPROVE, ACTION_HELD_DISMISS):
            verb = "approve" if action["action_id"] == ACTION_HELD_APPROVE else "dismiss"
            user, held, error = _decide_held(connection, slack_user, action.get("value", ""), verb)
            if error:
                _tell(connection, payload, error)
            elif channel and ts:
                done = f"{'Approved' if verb == 'approve' else 'Dismissed'} by {user.display_name} via Slack."
                api_call(
                    "chat.update",
                    connection.bot_token,
                    {"channel": channel, "ts": ts, "text": done, "blocks": held_blocks(held, resolved=done)},
                )
        elif action.get("action_id") == ACTION_APPROVE:
            user, error = _decide(connection, slack_user, post_id, fingerprint, decisions.APPROVE)
            if error:
                _tell(connection, payload, error)
            else:
                _update_message(connection, channel, ts, post_id, _outcome_text(decisions.APPROVE, user))
        return

    if kind == "view_submission" and (payload.get("view") or {}).get("callback_id") == MODAL_CALLBACK:
        post_id, fingerprint, channel, ts, comment = _submission_fields(payload)
        user, error = _decide(connection, slack_user, post_id, fingerprint, decisions.REQUEST_CHANGES, comment)
        if error:
            # The dialog is already closed, so the reason goes to the person privately.
            _tell(connection, {**payload, "_channel": channel}, error)
            return
        _update_message(connection, channel, ts, post_id, _outcome_text(decisions.REQUEST_CHANGES, user))


def _changes_modal(post_id, fingerprint, channel, ts) -> dict:
    return {
        "type": "modal",
        "callback_id": MODAL_CALLBACK,
        "private_metadata": json.dumps([post_id, fingerprint, channel, ts]),
        "title": {"type": "plain_text", "text": "Request changes"},
        "submit": {"type": "plain_text", "text": "Send feedback"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {
                "type": "input",
                "block_id": COMMENT_BLOCK,
                "label": {"type": "plain_text", "text": "What should change?"},
                "element": {"type": "plain_text_input", "action_id": COMMENT_ACTION, "multiline": True},
            }
        ],
    }


def process_request(body: bytes, timestamp: str, signature: str, retry_num: str = ""):
    """Verify one request from Slack and acknowledge it: ``(http_status, json_body_or_None)``.

    A request that fails the signature or replay check is refused before its
    contents are used for anything. Only the DB lookups and, for the dialog, one
    Slack call happen here (see :func:`acknowledge_interaction`). Slack resends
    an interaction it believes was not acknowledged (``X-Slack-Retry-Num``), and
    a signed request can be replayed: an identical payload is acknowledged with
    an empty 200 and not acted on a second time.
    """
    try:
        raw = parse_qs(body.decode("utf-8"), strict_parsing=True).get("payload", [""])[0]
        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return 400, None
    connections = _verified_connections(payload, timestamp, signature, body)
    if not connections:
        return 401, None
    claim = f"slack-approval-int:{hashlib.sha256(raw.encode()).hexdigest()}"
    if not cache.add(claim, 1, REPLAY_WINDOW_SECONDS + 60):
        logger.info("Slack interaction already handled (retry %s)", retry_num or "-")
        return 200, None
    # Several PostDelegate workspaces may share one Slack team: act only through
    # the one that owns the post or held item. Nothing is looked up outside it.
    action = (payload.get("actions") or [{}])[0]
    subject_id = _split_value(action.get("value", ""))[0]
    if not subject_id and payload.get("view"):
        try:
            subject_id = (json.loads(payload["view"].get("private_metadata") or "[]") + [""])[0]
        except ValueError:
            return 400, None
    held = action.get("action_id") in (ACTION_HELD_APPROVE, ACTION_HELD_DISMISS)
    lookup = _held_for if held else _post_for
    owner = next((c for c in connections if subject_id and lookup(c, subject_id) is not None), None)
    if owner is None:
        if payload.get("response_url"):
            respond_task(payload["response_url"], "That item isn't in a workspace connected to this Slack.")
        return 200, None
    try:
        body = acknowledge_interaction(payload, owner)
    except Exception:
        # Not handled after all: let Slack's resend through.
        cache.delete(claim)
        raise
    if body and body.get("response_action") == "errors":
        # Nothing was done, so the same submission may be sent again.
        cache.delete(claim)
    return 200, body
