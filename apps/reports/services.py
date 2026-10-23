"""Running reports: queue, build, store, email, announce — once per slot, with retries.

A run moves ``pending -> running -> succeeded | failed``. Two things keep the
scheduler honest:

* **One run per slot.** A scheduled run carries its slot ("W2026-43"); a unique
  constraint on (definition, slot) means two overlapping sweeps, or a restart,
  cannot both create it.
* **Claim before work.** ``claim`` flips ``pending -> running`` with a single
  conditional UPDATE, so only one worker builds a given run, and a run whose
  worker died is put back by ``reap_stale_runs``.

A failed attempt goes back to ``pending`` with a later ``next_attempt_at`` until
``MAX_ATTEMPTS``. The snapshot and PDF are kept across attempts, so a retry
after a failed email only sends the email.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta
from email.utils import formataddr, parseaddr

from django.conf import settings
from django.core import signing
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.core.mail import EmailMultiAlternatives
from django.db import IntegrityError, transaction
from django.db.models import F, Q
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.utils.text import slugify

from apps.billing.entitlements import PlanLimitError, get_entitlements, require_feature
from apps.billing.plans import Feature
from apps.common.mail import send_or_raise
from apps.members.models import WorkspaceMembership
from apps.notifications.engine import notify
from apps.notifications.models import EventType

from . import periods
from .builder import build_report_data
from .models import ReportDefinition, ReportRun
from .pdf import render_pdf

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
RETRY_DELAYS = (timedelta(minutes=5), timedelta(minutes=30))
STALE_AFTER = timedelta(minutes=30)
SHARE_SALT = "reports.share"
# "Run now" limits per workspace: builds are heavy and may call the AI provider,
# and a delivered run emails every recipient, so neither can be fired in a loop.
MANUAL_RUNS_PER_HOUR = 20
DELIVERED_RUNS_PER_DAY = 10


class RunLimitError(Exception):
    """Too many "Run now" requests; the message says what to wait for."""


# ---------------------------------------------------------------------------
# Who may do what
# ---------------------------------------------------------------------------


def can_view_reports(membership) -> bool:
    return bool(membership and membership.effective_permissions.get("view_analytics", False))


def can_manage_reports(membership) -> bool:
    """Creating, editing and running reports is for roles that manage the channels (owner, manager)."""
    if membership is None:
        return False
    perms = membership.effective_permissions
    return bool(perms.get("view_analytics", False) and perms.get("manage_social_accounts", False))


def reports_enabled(organization) -> bool:
    return get_entitlements(organization).has(Feature.BRANDED_REPORTS)


# ---------------------------------------------------------------------------
# Definitions
# ---------------------------------------------------------------------------

SCHEDULE_FIELDS = ("frequency", "weekday", "day_of_month", "send_time", "is_active")


def touch_schedule(definition: ReportDefinition, previous: dict | None, now: datetime | None = None) -> None:
    """Start the schedule's clock when it is switched on or changed (see ``schedule_anchor``)."""
    changed = previous is None or any(previous.get(f) != getattr(definition, f) for f in SCHEDULE_FIELDS)
    if changed:
        definition.schedule_anchor = now or timezone.now()


def snapshot_schedule(definition: ReportDefinition) -> dict:
    return {f: getattr(definition, f) for f in SCHEDULE_FIELDS}


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------


def create_run(
    definition: ReportDefinition,
    *,
    trigger: str,
    user=None,
    deliver: bool = False,
    schedule_key: str = "",
    now: datetime | None = None,
) -> ReportRun | None:
    """A pending run for the definition's current range; ``None`` if that slot already has one."""
    rng = periods.resolve_range(definition, now=now)
    try:
        with transaction.atomic():
            return ReportRun.objects.create(
                definition=definition,
                trigger=trigger,
                period_start=rng.start,
                period_end=rng.end,
                schedule_key=schedule_key,
                deliver=deliver,
                requested_by=user,
            )
    except IntegrityError:
        return None


def check_manual_run_limits(definition: ReportDefinition, *, deliver: bool, now: datetime | None = None) -> None:
    """Refuse a "Run now" past the workspace's hourly build or daily delivery allowance."""
    now = now or timezone.now()
    manual = ReportRun.objects.filter(
        definition__workspace_id=definition.workspace_id, trigger=ReportRun.Trigger.MANUAL
    )
    if manual.filter(created_at__gte=now - timedelta(hours=1)).count() >= MANUAL_RUNS_PER_HOUR:
        raise RunLimitError(f"This workspace has run {MANUAL_RUNS_PER_HOUR} reports in the last hour. Try again later.")
    if deliver and (
        manual.filter(deliver=True, created_at__gte=now - timedelta(days=1)).count() >= DELIVERED_RUNS_PER_DAY
    ):
        raise RunLimitError(
            f"This workspace has emailed {DELIVERED_RUNS_PER_DAY} reports on demand today. "
            "Run it without emailing, or try again tomorrow."
        )
    if deliver and not definition.recipients:
        raise RunLimitError("This report has no recipients to email. Add some, or run it without emailing.")


def queue_manual_run(definition: ReportDefinition, user, *, deliver: bool = False) -> ReportRun:
    """ "Run now": enqueue a run for a worker to build, and hand back the pending row.

    Raises :class:`RunLimitError` past the workspace's "Run now" allowance.
    """
    require_feature(definition.workspace.organization, Feature.BRANDED_REPORTS)
    check_manual_run_limits(definition, deliver=deliver)
    run = create_run(definition, trigger=ReportRun.Trigger.MANUAL, user=user, deliver=deliver)
    from .tasks import execute_report_run

    execute_report_run(str(run.id))
    return run


def schedule_due_runs(now: datetime | None = None) -> int:
    """Create a run for every scheduled definition whose slot has come. Returns how many were created."""
    now = now or timezone.now()
    created = 0
    entitled: dict[object, bool] = {}
    definitions = ReportDefinition.objects.filter(is_active=True).exclude(frequency=ReportDefinition.Frequency.NONE)
    for definition in definitions.select_related("workspace__organization"):
        slot = periods.due_slot(definition, now)
        if slot is None:
            continue
        org = definition.workspace.organization
        if org.pk not in entitled:
            entitled[org.pk] = reports_enabled(org)
        if not entitled[org.pk] or definition.workspace.is_archived:
            continue
        key, fire_at = slot
        # The range is resolved at the slot's own time, so a late sweep still reports the period it was meant for.
        run = create_run(definition, trigger=ReportRun.Trigger.SCHEDULED, deliver=True, schedule_key=key, now=fire_at)
        created += bool(run)
    return created


def due_run_ids(limit: int, now: datetime | None = None) -> list:
    now = now or timezone.now()
    runs = ReportRun.objects.filter(
        Q(next_attempt_at__isnull=True) | Q(next_attempt_at__lte=now), status=ReportRun.Status.PENDING
    )
    return list(runs.order_by("created_at").values_list("id", flat=True)[:limit])


def reap_stale_runs(now: datetime | None = None) -> int:
    """Put runs back in the queue whose worker died mid-build (a deploy, an OOM kill).

    A run that has already used every attempt is failed instead, so a report
    that kills its worker each time (out of memory) can't loop forever.
    """
    now = now or timezone.now()
    stale = ReportRun.objects.filter(status=ReportRun.Status.RUNNING, started_at__lt=now - STALE_AFTER)
    failed = stale.filter(attempts__gte=MAX_ATTEMPTS).update(
        status=ReportRun.Status.FAILED,
        error="The build stopped before it finished, on every attempt.",
        next_attempt_at=None,
        finished_at=now,
    )
    return failed + stale.update(status=ReportRun.Status.PENDING, next_attempt_at=None)


def claim(run_id, now: datetime | None = None) -> ReportRun | None:
    """Take a pending run for this worker, or ``None`` if someone else did (or it isn't due)."""
    now = now or timezone.now()
    due = ReportRun.objects.filter(
        Q(next_attempt_at__isnull=True) | Q(next_attempt_at__lte=now), pk=run_id, status=ReportRun.Status.PENDING
    )
    claimed = due.update(status=ReportRun.Status.RUNNING, started_at=now, attempts=F("attempts") + 1)
    if not claimed:
        return None
    return ReportRun.objects.select_related("definition__workspace__organization").get(pk=run_id)


class DeliveryError(Exception):
    """The report is built but not every recipient got it."""


def execute_run(run_id, now: datetime | None = None) -> ReportRun | None:
    """Build, store, email and announce one run. Never raises: failures are recorded on the run."""
    run = claim(run_id, now)
    if run is None:
        return None
    definition = run.definition
    try:
        require_feature(definition.workspace.organization, Feature.BRANDED_REPORTS)
        if not run.data:
            rng = periods.range_for_dates(definition, run.period_start, run.period_end)
            run.data = build_report_data(definition, rng, user=run.requested_by, now=now)
            run.save(update_fields=["data"])
        if not run.pdf_file:
            name = f"{slugify(definition.name) or 'report'}-{run.period_start}-{run.period_end}.pdf"
            run.pdf_file.save(name, ContentFile(render_pdf(run.data)), save=False)
            run.save(update_fields=["pdf_file"])
        if run.deliver:
            try:
                _email_recipients(run)
            except DeliveryError:
                if run.attempts < MAX_ATTEMPTS:
                    raise
                # Out of retries: the report itself is fine, so it is ready to read and
                # share; ``email_error`` says who didn't get it.
                logger.warning("Report run %s is ready but not every recipient got it", run.pk)
        run.status = ReportRun.Status.SUCCEEDED
        run.error = ""
        run.finished_at = timezone.now()
        run.next_attempt_at = None
        run.save(update_fields=["status", "error", "finished_at", "next_attempt_at"])
    except PlanLimitError as exc:
        _fail(run, str(exc), permanent=True)
        return run
    except Exception as exc:
        logger.exception("Report run %s failed (attempt %s)", run.pk, run.attempts)
        _fail(run, f"{type(exc).__name__}: {exc}"[:500], permanent=False)
        return run
    _announce(run)
    return run


def _fail(run: ReportRun, message: str, *, permanent: bool) -> None:
    run.error = message
    if permanent or run.attempts >= MAX_ATTEMPTS:
        run.status = ReportRun.Status.FAILED
        run.next_attempt_at = None
        run.finished_at = timezone.now()
    else:
        run.status = ReportRun.Status.PENDING
        run.next_attempt_at = timezone.now() + RETRY_DELAYS[min(run.attempts - 1, len(RETRY_DELAYS) - 1)]
    run.save(update_fields=["status", "error", "next_attempt_at", "finished_at"])


# ---------------------------------------------------------------------------
# Shareable links
# ---------------------------------------------------------------------------


def make_token(run: ReportRun) -> str:
    return signing.dumps({"run": str(run.pk), "key": str(run.definition.share_key)}, salt=SHARE_SALT)


def resolve_token(token: str) -> ReportRun | None:
    """The run a share link points at, or ``None``.

    ``None`` when the link is forged or older than ``REPORT_LINK_MAX_AGE_DAYS``,
    when the report (or the run) was deleted, when its links were revoked
    (``share_key`` replaced), or when the workspace is archived.
    """
    try:
        payload = signing.loads(token, salt=SHARE_SALT, max_age=settings.REPORT_LINK_MAX_AGE_DAYS * 86400)
    except signing.BadSignature:  # includes SignatureExpired
        return None
    if not isinstance(payload, dict):
        return None
    try:
        run = (
            ReportRun.objects.select_related("definition__workspace__organization")
            .filter(status=ReportRun.Status.SUCCEEDED, definition__workspace__is_archived=False)
            .get(pk=payload.get("run"))
        )
    except (ReportRun.DoesNotExist, ValueError, TypeError, AttributeError, ValidationError):
        return None
    if str(run.definition.share_key) != payload.get("key"):
        return None
    return run


def revoke_links(definition: ReportDefinition) -> None:
    """Stop every shareable link to this report's runs working (emailed ones included)."""
    definition.share_key = uuid.uuid4()
    definition.save(update_fields=["share_key", "updated_at"])


def share_url(run: ReportRun, view: str = "reports_public:view") -> str:
    return settings.APP_URL.rstrip("/") + reverse(view, kwargs={"token": make_token(run)})


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------


def _email_recipients(run: ReportRun) -> None:
    """Email the PDF and a link to each recipient not yet sent to; raise if any failed."""
    definition = run.definition
    pending = [r for r in definition.recipients if r not in run.emailed_to]
    if not pending:
        return
    data = run.data
    link = share_url(run)
    context = {
        "data": data,
        "link": link,
        "valid_days": settings.REPORT_LINK_MAX_AGE_DAYS,
        "summary": (data.get("ai_summary") or {}).get("text", "").split("\n")[0],
        "brand_name": data["branding"]["product"],
        # Mail clients block ``data:`` images, so the email loads the snapshot's logo through the link.
        "logo_src": share_url(run, "reports_public:logo") if data["branding"].get("logo") else "",
    }
    subject = f"{data['title']} - {data['period']['label']}"
    text = render_to_string("reports/email/report.txt", context)
    html = render_to_string("reports/email/report.html", context)
    with run.pdf_file.open("rb") as handle:
        pdf_bytes = handle.read()
    failures = []
    for address in pending:
        msg = EmailMultiAlternatives(
            subject=subject,
            body=text,
            from_email=_sender(data),
            to=[address],
        )
        msg.attach_alternative(html, "text/html")
        msg.attach(f"{slugify(data['title']) or 'report'}.pdf", pdf_bytes, "application/pdf")
        try:
            send_or_raise(msg)
        except Exception as exc:
            logger.warning("Could not email report %s to a recipient: %s", run.pk, exc)
            failures.append(address)
            continue
        run.emailed_to = [*run.emailed_to, address]
        run.save(update_fields=["emailed_to"])
    if failures:
        run.email_error = f"Could not send to {len(failures)} of {len(pending)} recipient(s)."
        run.save(update_fields=["email_error"])
        raise DeliveryError(run.email_error)
    if run.email_error:
        run.email_error = ""
        run.save(update_fields=["email_error"])


def _sender(data: dict) -> str:
    """The From header: the agency's name on the platform's address.

    "Agency via PostDelegate" normally; just the agency when the report is
    white-labelled, so the product's name appears nowhere in the email.
    """
    _, address = parseaddr(getattr(settings, "DEFAULT_FROM_EMAIL", "") or "noreply@localhost")
    agency = data["branding"]["agency"]
    product = data["branding"]["product"]
    name = f"{agency} via {product}" if product else agency
    return formataddr((name, address or "noreply@localhost"))


def _announce(run: ReportRun) -> None:
    """Tell the people who manage reports that this one is ready (once)."""
    if run.notified_at:
        return
    definition = run.definition
    emailed = f" and emailed to {len(run.emailed_to)} recipient(s)" if run.emailed_to else ""
    title = f"Report ready: {definition.name}"
    body = f"{run.data['period']['label']}{emailed}."
    memberships = WorkspaceMembership.objects.filter(workspace=definition.workspace).select_related(
        "user", "custom_role"
    )
    for membership in memberships:
        if not can_manage_reports(membership):
            continue
        try:
            notify(
                membership.user,
                EventType.REPORT_GENERATED,
                title=title,
                body=body,
                data={
                    "workspace_id": str(definition.workspace_id),
                    "report_id": str(definition.pk),
                    "run_id": str(run.pk),
                    "url": reverse(
                        "reports:run_detail", kwargs={"workspace_id": definition.workspace_id, "run_id": run.pk}
                    ),
                },
            )
        except Exception:
            logger.exception("Could not announce report run %s", run.pk)
    run.notified_at = timezone.now()
    run.save(update_fields=["notified_at"])
