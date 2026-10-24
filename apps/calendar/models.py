"""Content Calendar models (F-2.3) - scheduling, time slots, queues, and events."""

import uuid
from datetime import timedelta

from django.conf import settings
from django.db import models

from apps.common.managers import WorkspaceScopedManager
from apps.common.validators import validate_hex_color


class PostingSlot(models.Model):
    """Recurring time slot for a social account.

    Defines default publishing times (e.g., Mon/Wed/Fri at 9 AM)
    used for queue-based scheduling.
    """

    class DayOfWeek(models.IntegerChoices):
        MONDAY = 0, "Monday"
        TUESDAY = 1, "Tuesday"
        WEDNESDAY = 2, "Wednesday"
        THURSDAY = 3, "Thursday"
        FRIDAY = 4, "Friday"
        SATURDAY = 5, "Saturday"
        SUNDAY = 6, "Sunday"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    social_account = models.ForeignKey(
        "social_accounts.SocialAccount",
        on_delete=models.CASCADE,
        related_name="posting_slots",
    )
    day_of_week = models.IntegerField(choices=DayOfWeek.choices)
    time = models.TimeField(help_text="Posting time (in workspace timezone).")
    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "calendar_posting_slot"
        ordering = ["day_of_week", "time"]
        unique_together = [("social_account", "day_of_week", "time")]

    def __str__(self):
        return f"{self.get_day_of_week_display()} @ {self.time.strftime('%H:%M')} ({self.social_account})"

    @property
    def day_name(self):
        return self.get_day_of_week_display()


class Queue(models.Model):
    """A named publishing queue that maps posts to time slots.

    Each queue is tied to a workspace and optionally to a content category
    and social account. Posts added to a queue are auto-assigned to the
    next available PostingSlot datetime.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(
        "workspaces.Workspace",
        on_delete=models.CASCADE,
        related_name="queues",
    )
    name = models.CharField(max_length=100)
    category = models.ForeignKey(
        "composer.ContentCategory",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="queues",
    )
    social_account = models.ForeignKey(
        "social_accounts.SocialAccount",
        on_delete=models.CASCADE,
        related_name="queues",
    )
    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = WorkspaceScopedManager()

    class Meta:
        db_table = "calendar_queue"
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} ({self.social_account})"


class QueueEntry(models.Model):
    """A post's position within a queue, with its assigned publish slot."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    queue = models.ForeignKey(
        Queue,
        on_delete=models.CASCADE,
        related_name="entries",
    )
    post = models.ForeignKey(
        "composer.Post",
        on_delete=models.CASCADE,
        related_name="queue_entries",
    )
    position = models.PositiveIntegerField(default=0)
    assigned_slot_datetime = models.DateTimeField(
        null=True,
        blank=True,
        help_text="The computed publish datetime from the queue's posting slots.",
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "calendar_queue_entry"
        ordering = ["position"]
        unique_together = [("queue", "post")]

    def __str__(self):
        return f"QueueEntry(pos={self.position}): {self.post_id} in {self.queue.name}"


class RecurrenceRule(models.Model):
    """Defines how a post recurs (daily, weekly, monthly).

    The background task generates individual Post records for each
    recurrence up to 90 days ahead. The series stops at ``end_date`` or after
    ``count`` posts, whichever the rule sets; with neither it runs on.
    """

    class Frequency(models.TextChoices):
        DAILY = "daily", "Daily"
        WEEKLY = "weekly", "Weekly"
        MONTHLY = "monthly", "Monthly"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    post = models.OneToOneField(
        "composer.Post",
        on_delete=models.CASCADE,
        related_name="recurrence_rule",
    )
    frequency = models.CharField(max_length=10, choices=Frequency.choices)
    interval = models.PositiveIntegerField(
        default=1,
        help_text="Repeat every N frequency units (e.g., every 2 weeks).",
    )
    weekdays = models.JSONField(
        default=list,
        blank=True,
        help_text=(
            "Weekly rules only: the days to post on, 0=Monday … 6=Sunday, in the "
            "workspace timezone. Empty = the weekday of the first post."
        ),
    )
    end_date = models.DateField(
        null=True,
        blank=True,
        help_text="Stop generating recurrences after this date. Null = indefinite.",
    )
    count = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Total posts in the series, counting the first. Null = no limit.",
    )
    last_generated_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Timestamp of last recurrence generation run.",
    )
    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "calendar_recurrence_rule"

    def __str__(self):
        return f"RecurrenceRule({self.frequency} every {self.interval}) for {self.post_id}"

    @property
    def summary(self):
        """The rule in words, e.g. "Every Tuesday and Thursday until 31 Dec"."""
        from .services import recurrence_summary

        return recurrence_summary(
            self.frequency,
            interval=self.interval,
            weekdays=self.weekdays,
            end_date=self.end_date,
            count=self.count,
        )


class CustomCalendarEvent(models.Model):
    """Custom event on the workspace calendar (e.g., product launch, campaign start).

    Displayed as full-width colored bars spanning start_date to end_date.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(
        "workspaces.Workspace",
        on_delete=models.CASCADE,
        related_name="calendar_events",
    )
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True, default="")
    start_date = models.DateField()
    end_date = models.DateField()
    color = models.CharField(
        max_length=7,
        default="#3B82F6",
        help_text="Hex color for the event bar.",
        validators=[validate_hex_color],
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = WorkspaceScopedManager()

    class Meta:
        db_table = "calendar_custom_event"
        ordering = ["start_date"]

    def __str__(self):
        return f"{self.title} ({self.start_date} – {self.end_date})"


class EvergreenItem(models.Model):
    """A piece of content that is re-posted into one account's queue now and then.

    The content is copied when the item is added (``caption``, ``media_asset_ids``,
    …), so editing or deleting the post it came from changes nothing here. The
    periodic ``fill_queues_from_evergreen`` task turns an item into a fresh post
    whenever its account's queue has an empty slot and the item is due, which
    needs ``interval_days`` to have passed since it last went out and fewer than
    ``max_repeats`` repeats.
    """

    class SourceKind(models.TextChoices):
        POST = "post", "Published post"
        TEMPLATE = "template", "Template"
        IDEA = "idea", "Idea"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(
        "workspaces.Workspace",
        on_delete=models.CASCADE,
        related_name="evergreen_items",
    )
    social_account = models.ForeignKey(
        "social_accounts.SocialAccount",
        on_delete=models.CASCADE,
        related_name="evergreen_items",
    )
    category = models.ForeignKey(
        "composer.ContentCategory",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="evergreen_items",
        help_text="Evergreen categories take turns when a queue has several open slots.",
    )

    source_kind = models.CharField(max_length=10, choices=SourceKind.choices, default=SourceKind.POST)
    source_post = models.ForeignKey(
        "composer.Post",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="evergreen_items",
    )

    # Content, copied when the item was added.
    title = models.CharField(max_length=255, blank=True, default="")
    caption = models.TextField(blank=True, default="")
    first_comment = models.TextField(blank=True, default="")
    tags = models.JSONField(default=list, blank=True)
    media_asset_ids = models.JSONField(default=list, blank=True)
    platform_caption = models.TextField(null=True, blank=True)
    platform_extra = models.JSONField(default=dict, blank=True)

    # Recycling rules.
    interval_days = models.PositiveIntegerField(
        default=30, help_text="Never repost more often than every this many days."
    )
    max_repeats = models.PositiveIntegerField(
        null=True, blank=True, help_text="Stop after this many repeats. Empty means no limit."
    )
    vary_with_ai = models.BooleanField(
        default=False, help_text="Reword it with the AI writer each time; the original wording is the fallback."
    )
    pre_approved = models.BooleanField(
        default=False,
        help_text="Repeats skip the approval workflow because the original was already approved for recycling.",
    )
    pre_approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    approved_fingerprint = models.CharField(
        max_length=64,
        blank=True,
        default="",
        help_text="The content's fingerprint when it was pre-approved; any change since sends repeats to review.",
    )
    is_paused = models.BooleanField(default=False)

    # Progress.
    repeat_count = models.PositiveIntegerField(default=0)
    last_posted_at = models.DateTimeField(
        null=True, blank=True, help_text="When it last went out (or is due to), originals included."
    )
    last_recycled_at = models.DateTimeField(
        null=True, blank=True, help_text="When the task last picked it; orders the category rotation."
    )
    last_skip_reason = models.CharField(max_length=255, blank=True, default="")

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = WorkspaceScopedManager()

    class Meta:
        db_table = "calendar_evergreen_item"
        ordering = ["category__name", "last_posted_at", "created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["social_account", "source_post"],
                condition=models.Q(source_post__isnull=False),
                name="evergreen_one_item_per_post_and_account",
            ),
        ]
        indexes = [models.Index(fields=["social_account", "is_paused"])]

    def __str__(self):
        return f"Evergreen({self.social_account_id}): {(self.title or self.caption)[:40]}"

    @property
    def snippet(self):
        text = self.title or self.caption
        return text if len(text) <= 100 else text[:100] + "…"

    @property
    def category_name(self):
        return self.category.name if self.category_id else "Uncategorised"

    @property
    def repeats_left(self):
        return None if self.max_repeats is None else max(self.max_repeats - self.repeat_count, 0)

    @property
    def next_eligible_at(self):
        """The earliest the minimum interval allows another repeat (``None``: whenever)."""
        if self.last_posted_at is None:
            return None
        return self.last_posted_at + timedelta(days=self.interval_days)


class EvergreenRepeat(models.Model):
    """One time an evergreen item was re-posted: the post made, and how it was worded."""

    class Outcome(models.TextChoices):
        SCHEDULED = "scheduled", "Scheduled in the queue"
        AWAITING_APPROVAL = "awaiting_approval", "Waiting for approval"

    class Wording(models.TextChoices):
        ORIGINAL = "original", "Original wording"
        AI = "ai", "Reworded with AI"

    class Approval(models.TextChoices):
        NOT_REQUIRED = "not_required", "Workspace needs no approval"
        PRE_APPROVED = "pre_approved", "Pre-approved for recycling"
        REVIEW = "review", "Sent for review"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    item = models.ForeignKey(EvergreenItem, on_delete=models.CASCADE, related_name="repeats")
    post = models.ForeignKey(
        "composer.Post",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="evergreen_repeats",
    )
    platform_post = models.ForeignKey(
        "composer.PlatformPost",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="evergreen_repeats",
    )
    scheduled_for = models.DateTimeField()
    outcome = models.CharField(max_length=20, choices=Outcome.choices)
    wording = models.CharField(max_length=10, choices=Wording.choices, default=Wording.ORIGINAL)
    # The audit of what went out unattended: the exact text the task wrote and
    # why it did or didn't need a person's approval.
    text = models.TextField(blank=True, default="")
    approval = models.CharField(max_length=12, choices=Approval.choices, default=Approval.NOT_REQUIRED)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "calendar_evergreen_repeat"
        ordering = ["-scheduled_for"]
        constraints = [
            # Two overlapping runs can never book the same item into the same slot twice.
            models.UniqueConstraint(fields=["item", "scheduled_for"], name="evergreen_one_repeat_per_item_and_slot"),
        ]

    def __str__(self):
        return f"EvergreenRepeat({self.item_id} @ {self.scheduled_for:%Y-%m-%d %H:%M})"


class SavedView(models.Model):
    """A named set of calendar / Publish-page filters, personal or shared with the workspace.

    ``filters`` holds only the whitelisted query parameters (see
    ``apps.calendar.saved_views``), as ``{param: [values]}``; opening the view
    is a link to the page with those parameters, so the page treats it exactly
    like filters the user picked by hand.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(
        "workspaces.Workspace",
        on_delete=models.CASCADE,
        related_name="saved_views",
    )
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="saved_views",
    )
    name = models.CharField(max_length=80)
    filters = models.JSONField(default=dict)
    is_shared = models.BooleanField(default=False, help_text="Visible to everyone in the workspace.")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = WorkspaceScopedManager()

    class Meta:
        db_table = "calendar_saved_view"
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(fields=["workspace", "owner", "name"], name="uniq_saved_view_owner_name"),
        ]

    def __str__(self):
        return f"{self.name} ({'shared' if self.is_shared else 'personal'})"


class SavedViewDefault(models.Model):
    """The view a person lands on when they open the calendar with no filters (one per person per workspace)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(
        "workspaces.Workspace",
        on_delete=models.CASCADE,
        related_name="saved_view_defaults",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="saved_view_defaults",
    )
    view = models.ForeignKey(SavedView, on_delete=models.CASCADE, related_name="defaults")

    class Meta:
        db_table = "calendar_saved_view_default"
        constraints = [
            models.UniqueConstraint(fields=["workspace", "user"], name="uniq_saved_view_default_per_user"),
        ]

    def __str__(self):
        return f"Default view of {self.user_id} in {self.workspace_id}"
