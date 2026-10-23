"""Turn a definition and a date range into the report's figures.

The result is a plain JSON-serialisable dict (the run's ``data``): numbers are
already formatted, dates are strings, thumbnails and the logo are data URIs.
Rendering — web page, PDF, email — only lays it out. All the maths is the
analytics rollup's, so a report can't disagree with the Analytics page.
"""

from __future__ import annotations

import base64
import io
import logging
from datetime import datetime
from statistics import median

from django.conf import settings
from django.db.models import Min, Q
from django.utils import timezone
from django.utils.text import Truncator

from apps.ai_writer import services as ai
from apps.ai_writer.client import AIError
from apps.analytics import rollup
from apps.analytics.ranges import AnalyticsRange
from apps.analytics.templatetags.analytics_extras import delta_color, fmt_metric, fmt_signed
from apps.billing.entitlements import PlanLimitError, get_entitlements
from apps.billing.plans import Feature
from apps.inbox.models import InboxMessage, InboxReply
from apps.social_accounts.models import SocialAccount

from .branding import branding_for
from .models import (
    SECTION_ACTIVITY,
    SECTION_AI_SUMMARY,
    SECTION_INBOX,
    SECTION_NETWORKS,
    SECTION_SUMMARY,
    SECTION_TOP_POSTS,
    SECTION_TRENDS,
    ReportDefinition,
)

logger = logging.getLogger(__name__)

DATA_VERSION = 1
TOP_POSTS = 5
THUMB_SIZE = (160, 160)
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def report_accounts(definition: ReportDefinition) -> list[SocialAccount]:
    """The connected accounts the report covers: the picked ones, or all of them."""
    accounts = SocialAccount.objects.filter(
        workspace=definition.workspace, connection_status=SocialAccount.ConnectionStatus.CONNECTED
    )
    picked = list(definition.social_accounts.values_list("id", flat=True))
    if picked:
        accounts = accounts.filter(id__in=picked)
    return list(accounts.order_by("platform", "account_name"))


def _display(value, kind: str) -> str:
    return "Not reported" if value is None else fmt_metric(value, kind)


def _kpis(rows, compare: bool) -> list[dict]:
    out = []
    for k in rollup.kpis(rows, compare):
        delta = k["delta"] if compare else None
        out.append(
            {
                "key": k["key"],
                "label": k["label"],
                "value": k["value"],
                "display": _display(k["value"], k["kind"]),
                "previous_display": _display(k["previous"], k["kind"]) if compare and k["previous"] is not None else "",
                "delta": delta,
                "delta_display": fmt_signed(delta) if delta is not None else "",
                "tone": delta_color(delta) if delta is not None else "neutral",
                "reporting": k["reporting"],
                "total": k["total"],
            }
        )
    return out


def _trends(rows, rng: AnalyticsRange) -> list[dict]:
    out = []
    for key, label in rollup.SERIES_LABELS.items():
        t = rollup.trend(rows, rng, key)
        current = t["datasets"][0]["values"]
        previous = t["datasets"][1]["values"] if len(t["datasets"]) > 1 else []
        known = [v for v in current if v is not None]
        out.append(
            {
                "key": key,
                "label": label,
                "labels": t["labels"],
                "current": current,
                "previous": previous,
                "total": sum(known) if known else None,
                "total_display": fmt_metric(sum(known), "count") if known else "Not reported",
            }
        )
    return out


def _networks(rows) -> list[dict]:
    out = []
    for n in rollup.network_breakdown(rows):
        out.append(
            {
                "name": n["name"],
                "platform": n["platform"],
                "accounts": n["accounts"],
                "followers": fmt_metric(n["followers"], "count") if n["followers"] is not None else "–",
                "exposure": fmt_metric(n["exposure"], "count") if n["exposure"] is not None else "–",
                "engagements": fmt_metric(n["engagements"], "count") if n["engagements"] is not None else "–",
                "rate": fmt_metric(n["engagement_rate"], "percent") if n["engagement_rate"] is not None else "–",
                "new_followers": fmt_metric(n["new_followers"], "count") if n["new_followers"] is not None else "–",
                "posts": n["posts"],
                "share": n["share"],
                "unavailable": n["unavailable"],
            }
        )
    return out


def _thumbnail(post) -> str:
    """A small JPEG data URI of the post's first image, or ``""``."""
    attachment = next(iter(post.post.media_attachments.all()), None)
    if attachment is None:
        return ""
    asset = attachment.media_asset
    source = asset.thumbnail or (asset.file if asset.is_image else None)
    if not source:
        return ""
    try:
        from PIL import Image

        with source.open("rb") as handle:
            image = Image.open(handle)
            image.thumbnail(THUMB_SIZE)
            out = io.BytesIO()
            image.convert("RGB").save(out, format="JPEG", quality=80)
        return "data:image/jpeg;base64," + base64.b64encode(out.getvalue()).decode()
    except Exception:
        logger.info("Could not make a report thumbnail for asset %s", asset.pk, exc_info=True)
        return ""


def _top_posts(post_data: list[dict]) -> list[dict]:
    out = []
    for r in rollup.top_posts(post_data, "exposure", TOP_POSTS):
        out.append(
            {
                "caption": Truncator(r["caption"] or "(no caption)").chars(160),
                "network": r["account"].get_platform_display(),
                "account": r["account"].account_name,
                "published": r["published_at"].strftime("%b %-d, %Y"),
                "format": r["format"],
                "exposure": fmt_metric(r["exposure"], "count"),
                "exposure_label": r["exposure_label"],
                "engagements": fmt_metric(r["engagements"], "count") if r["engagements"] is not None else "–",
                "rate": fmt_metric(r["rate"], "percent") if r["rate"] is not None else "–",
                "thumb": _thumbnail(r["post"]),
            }
        )
    return out


def _activity(accounts, rng: AnalyticsRange, post_data: list[dict]) -> dict:
    per_day = rollup.daily_posts(accounts, rng)
    days = rng.day_list()
    counts = [per_day.get(d, 0) for d in days]
    by_weekday = [0] * 7
    for d, n in zip(days, counts, strict=True):
        by_weekday[d.weekday()] += n
    total = sum(counts)
    busiest = WEEKDAYS[by_weekday.index(max(by_weekday))] if total else ""
    return {
        "labels": [d.isoformat() for d in days],
        "counts": counts,
        "total": total,
        "busiest_weekday": busiest,
        "formats": [{"format": f["format"], "posts": f["posts"]} for f in rollup.format_performance(post_data)],
    }


def _inbox(definition: ReportDefinition, accounts, rng: AnalyticsRange) -> dict | None:
    """Messages received, how many got a sent reply, and the median time to the first one."""
    if not get_entitlements(definition.workspace.organization).has(Feature.INBOX):
        return None
    lo, hi = rng.bounds()
    messages = InboxMessage.objects.for_workspace(definition.workspace_id).filter(
        social_account__in=accounts, received_at__gte=lo, received_at__lt=hi, parent_message__isnull=True
    )
    received = messages.count()
    if not received:
        return None
    first_reply = dict(
        messages.annotate(
            first=Min(
                "replies__sent_at", filter=Q(replies__status=InboxReply.Status.SENT, replies__sent_at__isnull=False)
            )
        )
        .exclude(first__isnull=True)
        .values_list("id", "first")
    )
    received_at = dict(messages.filter(id__in=first_reply).values_list("id", "received_at"))
    hours = [max((first_reply[i] - received_at[i]).total_seconds() / 3600, 0.0) for i in first_reply]
    replied = len(hours)
    med = median(hours) if hours else None
    return {
        "received": received,
        "replied": replied,
        "reply_rate": f"{replied / received * 100:.0f}%",
        "median_response": _hours(med),
    }


def _hours(hours: float | None) -> str:
    if hours is None:
        return "No replies yet"
    if hours < 1:
        return f"{max(round(hours * 60), 1)} min"
    if hours < 48:
        return f"{hours:.1f} h"
    return f"{hours / 24:.1f} days"


# ---------------------------------------------------------------------------
# The summary paragraph
# ---------------------------------------------------------------------------


def _figures(data: dict) -> str:
    """The report's numbers as plain lines for the AI prompt: metrics only, no account names."""
    lines = [f"Period: {data['period']['label']} ({data['period']['days']} days)."]
    for k in data["kpis"]:
        if k["value"] is None:
            lines.append(f"{k['label']}: not reported.")
            continue
        line = f"{k['label']}: {k['display']}"
        if k["delta_display"]:
            line += f" ({k['delta_display']} vs previous period, {k['previous_display']} before)"
        lines.append(line + ".")
    for n in data["networks"]:
        lines.append(
            f"{n['name']}: {n['exposure']} views and impressions, {n['engagements']} engagements, "
            f"{n['posts']} posts, {n['new_followers']} new followers."
        )
    if data.get("activity"):
        lines.append(f"Busiest posting day: {data['activity']['busiest_weekday'] or 'none'}.")
    if data.get("inbox"):
        i = data["inbox"]
        lines.append(
            f"Inbox: {i['received']} messages, {i['reply_rate']} replied, median reply {i['median_response']}."
        )
    return "\n".join(lines)


def fallback_summary(data: dict) -> str:
    """A plain, honest paragraph from the numbers, for when AI isn't available."""
    by_key = {k["key"]: k for k in data["kpis"]}
    sentences = []
    for key, noun in (
        ("exposure", "Views and impressions"),
        ("engagements", "Engagements"),
        ("posts", "Posts published"),
    ):
        k = by_key.get(key)
        if not k or k["value"] is None:
            continue
        if not k["delta_display"]:
            sentences.append(f"{noun} were {k['display']}.")
        elif not k["delta"]:
            sentences.append(f"{noun} were {k['display']}, level with the previous period.")
        else:
            direction = "up" if k["delta"] > 0 else "down"
            sentences.append(
                f"{noun} were {k['display']}, {direction} {k['delta_display'].lstrip('+−')} on the previous period."
            )
    if data["top_posts"]:
        top = data["top_posts"][0]
        sentences.append(f"The most seen post was on {top['network']} with {top['exposure']} views.")
    if not sentences:
        return "No figures were reported for this period."
    return " ".join(sentences) + " Keep posting at a steady pace and repeat what your best posts did."


def _ai_summary(definition: ReportDefinition, data: dict, user) -> dict:
    ctx = ai.AIContext(workspace=definition.workspace, user=user)
    try:
        text = ai.summarize_report(
            ctx,
            _figures(data),
            period=data["period"]["label"],
            top_posts=[p["caption"] for p in data["top_posts"]],
        )
        return {"text": text, "source": "ai"}
    except (AIError, PlanLimitError, ValueError) as exc:
        logger.info("Report AI summary unavailable for %s: %s", definition.pk, exc)
        return {"text": fallback_summary(data), "source": "fallback"}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def build_report_data(
    definition: ReportDefinition,
    rng: AnalyticsRange,
    *,
    user=None,
    with_ai: bool = True,
    now: datetime | None = None,
    accounts: list[SocialAccount] | None = None,
) -> dict:
    """Everything the report shows. ``with_ai=False`` (live previews) never calls the provider.

    ``accounts`` overrides the saved selection, for previewing a definition that isn't saved yet.
    """
    accounts = report_accounts(definition) if accounts is None else accounts
    sections = list(definition.sections)
    rows = rollup.collect(accounts, rng)
    posts = list(rollup.posts_queryset(accounts, rng).prefetch_related("post__media_attachments__media_asset"))
    post_data = rollup.post_rows(posts, accounts, rng.tz)

    data: dict = {
        "version": DATA_VERSION,
        "title": definition.name,
        "workspace": definition.workspace.name,
        "sections": sections,
        "generated_at": (now or timezone.now()).astimezone(rng.tz).strftime("%b %-d, %Y %H:%M %Z"),
        "period": {
            "start": rng.start.isoformat(),
            "end": rng.end.isoformat(),
            "label": f"{rng.start:%b %-d} – {rng.end:%b %-d, %Y}",
            "days": rng.days,
            "timezone": str(rng.tz),
            "compare": rng.compare,
            "previous_label": f"{rng.previous_start:%b %-d} – {rng.previous_end:%b %-d, %Y}" if rng.compare else "",
        },
        "branding": branding_for(definition),
        "accounts": len(accounts),
        "kpis": _kpis(rows, rng.compare) if SECTION_SUMMARY in sections or SECTION_AI_SUMMARY in sections else [],
        "trends": _trends(rows, rng) if SECTION_TRENDS in sections else [],
        "networks": _networks(rows) if SECTION_NETWORKS in sections or SECTION_AI_SUMMARY in sections else [],
        "top_posts": _top_posts(post_data) if SECTION_TOP_POSTS in sections or SECTION_AI_SUMMARY in sections else [],
        "activity": _activity(accounts, rng, post_data) if SECTION_ACTIVITY in sections else None,
        "inbox": _inbox(definition, accounts, rng) if SECTION_INBOX in sections else None,
        "excluded": sorted({r.unavailable for r in rows if r.unavailable}),
        "ai_summary": None,
    }
    if SECTION_AI_SUMMARY in sections:
        data["ai_summary"] = (
            _ai_summary(definition, data, user)
            if with_ai
            else {"text": "The AI summary is written when the report is generated.", "source": "preview"}
        )
    data["product_name"] = settings.BRAND_NAME
    return data
