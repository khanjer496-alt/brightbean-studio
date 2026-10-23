"""Post Composer models (F-2.1) - core content creation entities.

Models:
    ContentCategory - Content categories for posts (e.g., Educational, Promotional).
    Idea - A content idea on the Kanban board, scoped to a workspace.
    Post - The base content entity, scoped to a workspace.
    PlatformPost - Per-platform variant of a post (caption/media overrides).
    PostMedia - Media attachments with ordering and alt text.
    PostPart - A later part of a thread (the post itself is the first part).
    PostPartMedia - Media attachments on a thread part.
    PlatformPostPart - Per-account override and publish outcome of one thread part.
    PostVersion - Immutable snapshots for version history.
    PostTemplate - Reusable post templates.
    HashtagSet - A named group of hashtags a workspace inserts as one.
    Signature - Sign-off text a user or a workspace adds to posts.
    CSVImportJob - Tracks bulk CSV import jobs.
"""

import uuid
from urllib.parse import urlsplit

from django.conf import settings
from django.db import models
from django.utils import timezone

from apps.common.managers import WorkspaceScopedManager
from apps.common.validators import validate_hex_color


class ContentCategory(models.Model):
    """Content category for posts (e.g., Educational, Promotional, Behind the scenes).

    Categories are defined per workspace and used for calendar filtering,
    analytics filtering, and queue-based scheduling.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(
        "workspaces.Workspace",
        on_delete=models.CASCADE,
        related_name="content_categories",
    )
    name = models.CharField(max_length=100)
    color = models.CharField(
        max_length=7,
        default="#3B82F6",
        help_text="Hex color for calendar display, e.g. #FF5733",
        validators=[validate_hex_color],
    )
    position = models.PositiveIntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = WorkspaceScopedManager()

    class Meta:
        db_table = "composer_content_category"
        ordering = ["position", "name"]
        unique_together = [("workspace", "name")]
        verbose_name_plural = "content categories"

    def __str__(self):
        return self.name


class Tag(models.Model):
    """A reusable tag scoped to a workspace.

    Tags can be applied to Posts, Ideas, and other content types.
    They are workspace-scoped so each workspace has its own tag namespace.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(
        "workspaces.Workspace",
        on_delete=models.CASCADE,
        related_name="tags",
    )
    name = models.CharField(max_length=100)

    created_at = models.DateTimeField(auto_now_add=True)

    objects = WorkspaceScopedManager()

    class Meta:
        db_table = "composer_tag"
        unique_together = ("workspace", "name")
        ordering = ["name"]

    def __str__(self):
        return self.name


class IdeaGroup(models.Model):
    """A Kanban column/group for organising ideas, scoped to a workspace."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(
        "workspaces.Workspace",
        on_delete=models.CASCADE,
        related_name="idea_groups",
    )
    name = models.CharField(max_length=100)
    position = models.PositiveIntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = WorkspaceScopedManager()

    class Meta:
        db_table = "composer_idea_group"
        ordering = ["position", "created_at"]

    def __str__(self):
        return self.name


class Idea(models.Model):
    """A content idea on the Kanban board, scoped to a workspace."""

    class Status(models.TextChoices):
        UNASSIGNED = "unassigned", "Unassigned"
        TODO = "todo", "To Do"
        IN_PROGRESS = "in_progress", "In Progress"
        DONE = "done", "Done"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(
        "workspaces.Workspace",
        on_delete=models.CASCADE,
        related_name="ideas",
    )
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="authored_ideas",
    )

    # Content
    title = models.CharField(max_length=255)
    description = models.TextField(blank=True, default="")
    tags = models.JSONField(default=list, blank=True)
    media_asset = models.ForeignKey(
        "media_library.MediaAsset",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="idea_usages",
    )

    # Kanban
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.UNASSIGNED,
        db_index=True,
    )
    group = models.ForeignKey(
        IdeaGroup,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="ideas",
    )
    position = models.PositiveIntegerField(default=0)

    # Optional link to a Post (when idea is converted)
    post = models.OneToOneField(
        "Post",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="source_idea",
    )

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = WorkspaceScopedManager()

    class Meta:
        db_table = "composer_idea"
        ordering = ["position", "-created_at"]

    def __str__(self):
        return f"Idea({self.group or self.status}): {self.title[:50]}"


class IdeaMedia(models.Model):
    """Media attachment on an idea, with stable ordering."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    idea = models.ForeignKey(
        Idea,
        on_delete=models.CASCADE,
        related_name="media_attachments",
    )
    media_asset = models.ForeignKey(
        "media_library.MediaAsset",
        on_delete=models.CASCADE,
        related_name="idea_attachments",
    )
    position = models.PositiveIntegerField(default=0, help_text="Ordering position on the idea.")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "composer_idea_media"
        ordering = ["position", "created_at"]
        unique_together = [("idea", "media_asset")]

    def __str__(self):
        return f"IdeaMedia(pos={self.position}): {self.media_asset.filename}"


class Post(models.Model):
    """A piece of content created in the composer.

    A Post holds the shared/base content. PlatformPost children hold
    per-platform overrides **and their own editorial status** — each social
    account flows through the workflow independently so one platform can be
    a draft while another is scheduled or already published.

    ``Post.status`` is a derived aggregate over ``platform_posts`` (see
    ``apps.composer.status.derive_post_status``), kept for list/dashboard
    rendering and backwards compatibility with existing templates.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(
        "workspaces.Workspace",
        on_delete=models.CASCADE,
        related_name="posts",
    )
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="authored_posts",
    )

    # Content
    title = models.CharField(max_length=255, blank=True, default="")
    caption = models.TextField(blank=True, default="")
    first_comment = models.TextField(blank=True, default="")
    internal_notes = models.TextField(blank=True, default="")
    tags = models.JSONField(default=list, blank=True)
    category = models.ForeignKey(
        ContentCategory,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="posts",
    )

    # Scheduling (default when a PlatformPost doesn't set its own scheduled_at)
    scheduled_at = models.DateTimeField(blank=True, null=True, db_index=True)
    published_at = models.DateTimeField(blank=True, null=True)

    # Optional draft-stage suggestion of when to publish — distinct from the
    # committed ``scheduled_at``. Lets a planner/agent record "this should go
    # out Tue 9am" without queuing it for the publisher. Cleared once the post
    # is actually scheduled.
    proposed_publish_at = models.DateTimeField(blank=True, null=True)

    # The post whose ``RecurrenceRule`` generated this one. Lets the calendar
    # mark every occurrence of a series, and lets editing or stopping the
    # series find the upcoming copies it made. Null for ordinary posts and for
    # copies generated before the link existed.
    recurrence_source = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="recurrence_occurrences",
    )

    # Metadata
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = WorkspaceScopedManager()

    class Meta:
        db_table = "composer_post"
        ordering = ["-created_at"]

    def __str__(self):
        snippet = (self.caption[:50] + "...") if len(self.caption) > 50 else self.caption
        return f"Post({self.status}): {snippet or '(no caption)'}"

    # ------------------------------------------------------------------
    # Derived status (aggregated across PlatformPost children)
    # ------------------------------------------------------------------

    @property
    def status(self):
        from .status import derive_post_status

        # Use prefetch-friendly iteration so list/grid views don't trigger an
        # extra query per post when ``platform_posts`` is already prefetched.
        statuses = [pp.status for pp in self.platform_posts.all()]
        return derive_post_status(statuses)

    def get_status_display(self):
        """Human label mirroring the old Django-generated method."""
        return dict(PlatformPost.Status.choices).get(self.status, self.status)

    @property
    def display_status(self):
        """``status`` as a status pill shows it: a published post whose thread stopped reads partial.

        Display only — ``status`` keeps its meaning for everything that
        filters or branches on it. Iterates ``platform_posts.all()`` like
        ``status``, so a prefetch covers it.
        """
        status = self.status
        if status == PlatformPost.Status.PUBLISHED and any(pp.is_thread_partial for pp in self.platform_posts.all()):
            return PlatformPost.PARTIALLY_PUBLISHED
        return status

    @property
    def display_status_label(self):
        if self.display_status == PlatformPost.PARTIALLY_PUBLISHED:
            return PlatformPost.PARTIALLY_PUBLISHED_LABEL
        return self.get_status_display()

    @property
    def status_color(self):
        return PlatformPost.STATUS_COLORS.get(self.status, "gray")

    @property
    def is_editable(self):
        """Whether the post can be edited in the composer."""
        return self.status in (
            "draft",
            "changes_requested",
            "rejected",
            "approved",
            "scheduled",
        )

    @property
    def is_schedulable(self):
        """Whether the post can be scheduled."""
        return self.status in ("draft", "approved")

    @property
    def caption_snippet(self):
        """First 100 characters of caption for preview."""
        if len(self.caption) <= 100:
            return self.caption
        return self.caption[:100] + "…"

    @property
    def series_rule(self):
        """The ``RecurrenceRule`` this post belongs to, or None.

        A series' first post holds the rule; every copy it generated points
        back at that post. Cheap when the caller ``select_related``
        ``recurrence_rule`` and ``recurrence_source__recurrence_rule``.
        """
        from django.core.exceptions import ObjectDoesNotExist

        for post in (self, self.recurrence_source):
            if post is None:
                continue
            try:
                return post.recurrence_rule
            except ObjectDoesNotExist:
                continue
        return None

    @property
    def thread_part_count(self):
        """How many parts this post publishes as: 1, plus one per ``PostPart``.

        Iterates ``thread_parts.all()`` so a list that prefetched
        ``thread_parts`` pays no query per row for its thread badge.
        """
        return 1 + len(self.thread_parts.all())

    @property
    def is_thread(self):
        return self.thread_part_count > 1

    @property
    def platform_posts_summary(self):
        """Summary of target platforms."""
        return list(self.platform_posts.values_list("social_account__platform", flat=True))


class PlatformPost(models.Model):
    youtube_video_checked_at = models.DateTimeField(null=True, blank=True, db_index=True)

    """A per-platform variant of a Post.

    Created for each selected social account when composing. Holds optional
    caption/media overrides and — crucially — owns its own editorial status,
    so each social account flows through the workflow independently.
    """

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        PENDING_REVIEW = "pending_review", "Pending Review"
        PENDING_CLIENT = "pending_client", "Pending Client"
        APPROVED = "approved", "Approved"
        CHANGES_REQUESTED = "changes_requested", "Changes Requested"
        REJECTED = "rejected", "Rejected"
        SCHEDULED = "scheduled", "Scheduled"
        PUBLISHING = "publishing", "Publishing"
        PUBLISHED = "published", "Published"
        FAILED = "failed", "Failed"
        ON_HOLD = "on_hold", "On Hold"

    # Every field :meth:`transition_to` may write. A caller that passes
    # ``update_fields`` MUST include these or the transition half-applies
    # silently — which is exactly how the retry-budget and publish-handle
    # resets went missing on three of the five scheduling paths. Spell the
    # set once here rather than re-listing it at each call site.
    TRANSITION_FIELDS = (
        "status",
        "published_at",
        "platform_post_id",
        "retry_count",
        "next_retry_at",
        "publish_error",
    )

    # Statuses that must never be removed by *accidental* deletion paths
    # (composer account deselection, autosave sync): a published or
    # mid-publish row is history, and deleting it cascades away its
    # PublishLog records. Explicit deletion (the post delete action) is the
    # user's call and intentionally bypasses this.
    PROTECTED_STATUSES = (Status.PUBLISHED, Status.PUBLISHING)

    # Statuses whose scheduled time may be changed by a calendar drag-and-drop.
    # A dropped draft/failed chip becomes ``scheduled`` (an implicit
    # (re)schedule / retry); the others just move in time, keeping their status.
    # ``published`` and ``publishing`` are excluded — the post is done or in
    # flight and must not be dragged. Shared by the calendar chip templates and
    # the reschedule endpoint so both agree on what's draggable.
    RESCHEDULABLE_STATUSES = (
        Status.DRAFT,
        Status.APPROVED,
        Status.SCHEDULED,
        Status.FAILED,
        Status.PENDING_REVIEW,
        Status.PENDING_CLIENT,
    )

    # Valid state transitions (from → set of allowed targets). Mirrors the old
    # Post-level state machine minus ``partially_published`` — that concept
    # only applies at the aggregate/Post level and is produced by
    # ``derive_post_status``.
    VALID_TRANSITIONS = {
        "draft": {"pending_review", "scheduled", "publishing"},
        "pending_review": {"approved", "changes_requested", "rejected"},
        "approved": {"pending_client", "scheduled", "publishing", "draft", "on_hold", "pending_review"},
        "pending_client": {"approved", "changes_requested", "rejected"},
        "changes_requested": {"pending_review", "draft"},
        "rejected": {"draft", "pending_review"},
        "scheduled": {"publishing", "draft"},
        "publishing": {"published", "failed", "scheduled"},  # scheduled = retry
        "failed": {"publishing", "draft", "scheduled"},
        # Client-requested hold: parked out of the publish path. The team resolves
        # it back to approved (resume), draft (rework), or changes_requested. There
        # is deliberately no on_hold → scheduled edge — un-hold to ``approved`` first
        # so nothing publishes straight out of a hold.
        "on_hold": {"approved", "draft", "changes_requested"},
        "published": set(),  # terminal
    }

    class FirstCommentStatus(models.TextChoices):
        NONE = "none", "Not requested"
        PENDING = "pending", "Pending"
        POSTED = "posted", "Posted"
        FAILED = "failed", "Failed"

    class ThreadStatus(models.TextChoices):
        NONE = "none", "No thread"
        PENDING = "pending", "Posting thread"
        COMPLETE = "complete", "Thread complete"
        # The first post and some parts are live and the rest are not. The row
        # stays ``published`` — the first post is history — and this is what
        # says the thread behind it is unfinished and can be resumed.
        PARTIAL = "partial", "Partially published"

    # The display-only status of a published row whose thread stopped part-way
    # (``display_status``); it reuses the post-level aggregate's styling.
    PARTIALLY_PUBLISHED = "partially_published"
    PARTIALLY_PUBLISHED_LABEL = "Partially published"

    STATUS_COLORS = {
        "draft": "gray",
        "pending_review": "orange",
        "pending_client": "amber",
        "approved": "teal",
        "changes_requested": "orange",
        "rejected": "red",
        "scheduled": "blue",
        "publishing": "indigo",
        "published": "green",
        "partially_published": "yellow",  # only used by Post-level aggregate
        "failed": "red",
        "on_hold": "violet",
    }

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    post = models.ForeignKey(
        Post,
        on_delete=models.CASCADE,
        related_name="platform_posts",
    )
    social_account = models.ForeignKey(
        "social_accounts.SocialAccount",
        on_delete=models.CASCADE,
        related_name="platform_posts",
    )

    # Per-platform overrides (null = use base post values)
    platform_specific_title = models.TextField(blank=True, null=True)
    platform_specific_caption = models.TextField(blank=True, null=True)
    platform_specific_media = models.JSONField(
        blank=True,
        null=True,
        help_text="JSON list of media asset IDs with platform-specific ordering/cropping.",
    )
    platform_specific_first_comment = models.TextField(blank=True, null=True)
    platform_extra = models.JSONField(
        default=dict,
        blank=True,
        help_text="Per-platform metadata (privacy, tags, thumbnail_asset_id, etc.)",
    )

    # Editorial + publishing state
    status = models.CharField(
        max_length=30,
        choices=Status.choices,
        default=Status.DRAFT,
        db_index=True,
    )
    platform_post_id = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="The post ID on the platform after publishing.",
    )
    # The published post's public address, as the platform reported it on publish. Rows published
    # before this column existed (or by a provider that reports none) stay null until
    # :meth:`public_url` derives one from ``platform_post_id``.
    permalink = models.CharField(max_length=500, null=True, blank=True)
    publish_error = models.TextField(blank=True, default="")
    published_at = models.DateTimeField(blank=True, null=True)
    # Durable proof that the *current publishable payload* completed the
    # workspace's approval workflow. The publisher validates the fingerprint
    # immediately before dispatch, so a stray status update cannot bypass review.
    approval_completed_at = models.DateTimeField(blank=True, null=True)
    approval_fingerprint = models.CharField(max_length=64, blank=True, default="")

    # First-comment outcome. Kept in dedicated columns rather than in
    # ``platform_extra``: that field is an *input* channel — the publish engine
    # merges it into ``PublishContent.extra``, which providers forward into
    # their API payloads — so status written there would ride along on the next
    # publish. A post whose comment failed must also be distinguishable from a
    # fully successful one, which needs a queryable column.
    first_comment_status = models.CharField(
        max_length=10,
        choices=FirstCommentStatus.choices,
        default=FirstCommentStatus.NONE,
        db_index=True,
    )
    first_comment_id = models.CharField(max_length=255, blank=True, default="")
    first_comment_error = models.TextField(blank=True, default="")
    first_comment_retry_count = models.PositiveIntegerField(default=0)
    first_comment_posted_at = models.DateTimeField(blank=True, null=True)
    # Thread outcome, kept apart from ``status`` for the same reason the first
    # comment is: the first post is live and must read ``published``, while
    # the parts behind it are still posting, finished, or stuck part-way.
    # ``db_default`` so a worker still on the previous release can keep
    # inserting rows while this column is rolled out.
    thread_status = models.CharField(
        max_length=10,
        choices=ThreadStatus.choices,
        default=ThreadStatus.NONE,
        db_default=ThreadStatus.NONE,
        db_index=True,
    )
    thread_error = models.TextField(blank=True, default="", db_default="")
    scheduled_at = models.DateTimeField(
        blank=True,
        null=True,
        db_index=True,
        help_text="Per-platform scheduled publish time. NULL falls back to Post.scheduled_at.",
    )
    retry_count = models.PositiveIntegerField(default=0)
    next_retry_at = models.DateTimeField(blank=True, null=True)

    # Analytics sync state — the read-side counterpart to the retry budget
    # above, owned solely by ``apps.analytics.tasks``.
    #
    # ``analytics_attempted_at`` is the last time the sync *tried* this post's
    # per-post metrics fetch, successful or not. The cadence ladder used to be
    # derived from the newest ``PostInsightsSnapshot.captured_at`` instead,
    # which meant a post whose fetch kept failing wrote no row, looked
    # never-synced, and was retried on every hourly tick forever. That is the
    # amplification that emptied a whole day of YouTube API quota in an hour.
    #
    # NULL means "never attempted under this scheme"; the sync falls back to the
    # old snapshot signal for those, so no backfill is needed.
    analytics_attempted_at = models.DateTimeField(blank=True, null=True)

    # Consecutive failed attempts, reset to 0 by any success. Drives the
    # exponential backoff in ``apps.analytics.tasks._analytics_failure_backoff``
    # so a permanently unfetchable post (deleted video, revoked visibility)
    # costs one call a week rather than one an hour.
    analytics_failure_count = models.PositiveSmallIntegerField(default=0, db_default=0)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "composer_platform_post"
        unique_together = [("post", "social_account")]
        indexes = [
            models.Index(
                fields=["status", "scheduled_at"],
                name="idx_pp_status_sched",
            ),
        ]

    def __str__(self):
        return f"PlatformPost({self.social_account.platform}): {self.status}"

    def public_url(self) -> str:
        """Where the published post can be seen (https), or "" when it isn't known.

        Uses the stored permalink, else asks the provider to build one from the remote id and
        keeps it, so the derivation happens once per row. Only https addresses are returned,
        because they end up as links on public pages.
        """
        if self.status != self.Status.PUBLISHED:
            return ""
        url = self.permalink or ""
        if not url and self.platform_post_id:
            from providers import PROVIDER_REGISTRY

            provider = PROVIDER_REGISTRY.get(self.social_account.platform)
            url = (provider.permalink_for(self.platform_post_id, self.social_account) if provider else "")[:500]
            if url:
                self.permalink = url
                type(self).objects.filter(pk=self.pk, permalink__isnull=True).update(permalink=url)
        return url if url.startswith("https://") else ""

    @property
    def is_reschedulable(self):
        """Whether this chip may be dragged to a new date on the calendar."""
        return self.status in self.RESCHEDULABLE_STATUSES

    @property
    def is_bulk_selectable(self):
        """Whether this row can be the target of a bulk draft/publish/delete.

        Every branch of ``bulk_platform_action`` skips ``PROTECTED_STATUSES``, so
        offering a checkbox on those rows would only ever be a no-op.
        """
        return self.status not in self.PROTECTED_STATUSES

    # ------------------------------------------------------------------
    # State machine
    # ------------------------------------------------------------------

    def can_transition_to(self, new_status):
        """Check if a status transition is valid."""
        allowed = self.VALID_TRANSITIONS.get(self.status, set())
        return new_status in allowed

    def transition_to(self, new_status):
        """Transition to a new status, raising ValueError if invalid."""
        if not self.can_transition_to(new_status):
            raise ValueError(
                f"Invalid status transition: {self.status} → {new_status}. "
                f"Allowed: {self.VALID_TRANSITIONS.get(self.status, set())}"
            )
        self.status = new_status
        if new_status == "published":
            self.published_at = timezone.now()
        if new_status == "scheduled":
            # A deliberate re-schedule is a fresh start, so the old attempt's
            # backoff must not carry over: a spent retry_count would otherwise
            # fail the post on its first attempt instead of giving it the full
            # budget, and a stale next_retry_at would hold it back from the due
            # query. Safe to do here because the publish engine's own
            # retry path sets ``status`` directly and never comes through this
            # method — so this cannot clear a backoff it just scheduled.
            self.retry_count = 0
            self.next_retry_at = None
            self.publish_error = ""
            # The handle belongs to the *previous* attempt. Left in place, the
            # confirmation sweep would treat it as this attempt's and settle the
            # post against an outcome that has nothing to do with it — reporting
            # a publish that never reached the platform as done.
            self.platform_post_id = ""

    @property
    def status_color(self):
        return self.STATUS_COLORS.get(self.status, "gray")

    @property
    def is_editable(self):
        return self.status in (
            "draft",
            "changes_requested",
            "rejected",
            "approved",
            "scheduled",
        )

    @property
    def is_schedulable(self):
        return self.status in ("draft", "approved")

    @property
    def effective_title(self):
        """Return platform-specific title or fall back to base post title."""
        if self.platform_specific_title is not None:
            return self.platform_specific_title
        return self.post.title

    @property
    def effective_caption(self):
        """Return platform-specific caption or fall back to base post caption."""
        if self.platform_specific_caption is not None:
            return self.platform_specific_caption
        return self.post.caption

    @property
    def effective_first_comment(self):
        """Return platform-specific first comment or fall back to base."""
        if self.platform_specific_first_comment is not None:
            return self.platform_specific_first_comment
        return self.post.first_comment

    @property
    def is_thread_partial(self):
        """The first post is live but the thread behind it stopped part-way."""
        return self.thread_status == self.ThreadStatus.PARTIAL

    @property
    def display_status(self):
        """``status`` as a chip or pill shows it: ``partially_published`` for a stopped thread.

        Display only. ``status`` stays ``published`` — the first post is live,
        and the publisher, filters and sweeps all depend on that.
        """
        if self.status == self.Status.PUBLISHED and self.is_thread_partial:
            return self.PARTIALLY_PUBLISHED
        return self.status

    @property
    def display_status_label(self):
        if self.display_status == self.PARTIALLY_PUBLISHED:
            return self.PARTIALLY_PUBLISHED_LABEL
        return self.get_status_display()

    @property
    def platform(self):
        return self.social_account.platform

    @property
    def char_limit(self):
        return self.social_account.char_limit

    @property
    def caption_length(self):
        # Not len(): platforms that escape reserved characters send more than
        # was typed, and the limit applies to what is sent.
        return self.social_account.caption_wire_length(self.effective_caption)

    @property
    def is_over_limit(self):
        return self.caption_length > self.char_limit


# Backwards-compat alias: lots of existing code imports ``Post.Status.DRAFT`` /
# iterates ``Post.Status.choices`` to build filter UIs. The enum lives on
# PlatformPost now; re-expose it on Post so callers don't need updating.
Post.Status = PlatformPost.Status  # type: ignore[attr-defined]


class PostMedia(models.Model):
    """Media attachment on a post, with ordering and alt text."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    post = models.ForeignKey(
        Post,
        on_delete=models.CASCADE,
        related_name="media_attachments",
    )
    media_asset = models.ForeignKey(
        "media_library.MediaAsset",
        on_delete=models.CASCADE,
        related_name="post_usages",
    )
    position = models.PositiveIntegerField(default=0, help_text="Ordering position in carousel.")
    alt_text = models.TextField(blank=True, default="")
    platform_overrides = models.JSONField(
        default=dict,
        blank=True,
        help_text='Per-platform crop/format overrides, e.g. {"instagram": {"crop": "4:5"}}',
    )

    class Meta:
        db_table = "composer_post_media"
        ordering = ["position"]
        unique_together = [("post", "media_asset")]

    def __str__(self):
        return f"PostMedia(pos={self.position}): {self.media_asset.filename}"


class PostPart(models.Model):
    """A later part of a thread.

    The post's own caption and media are the thread's first part, so a post
    with no ``PostPart`` rows is a single post exactly as before. Each part is
    published after the one before it — as a reply or a comment, whichever the
    platform's ``ThreadCapability`` says — optionally after a delay.
    """

    # A delay longer than a day is a second post, not a thread; it belongs on
    # the calendar where it can be seen and moved.
    MAX_DELAY_SECONDS = 24 * 60 * 60

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    post = models.ForeignKey(
        Post,
        on_delete=models.CASCADE,
        related_name="thread_parts",
    )
    position = models.PositiveIntegerField(default=0, help_text="Order in the thread; the post itself comes first.")
    caption = models.TextField(blank=True, default="")
    delay_seconds = models.PositiveIntegerField(
        default=0,
        help_text="Seconds to wait after the previous part is live before publishing this one.",
    )
    # A closing part ("Follow for more") kept last whatever order the other
    # parts are put in.
    is_finisher = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "composer_post_part"
        ordering = ["position", "created_at"]

    def __str__(self):
        return f"PostPart(pos={self.position}) for {self.post_id}"


class PostPartMedia(models.Model):
    """Media attachment on a thread part, with ordering and alt text."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    part = models.ForeignKey(
        PostPart,
        on_delete=models.CASCADE,
        related_name="media_attachments",
    )
    media_asset = models.ForeignKey(
        "media_library.MediaAsset",
        on_delete=models.CASCADE,
        related_name="post_part_usages",
    )
    position = models.PositiveIntegerField(default=0)
    alt_text = models.TextField(blank=True, default="")

    class Meta:
        db_table = "composer_post_part_media"
        ordering = ["position"]
        unique_together = [("part", "media_asset")]

    def __str__(self):
        return f"PostPartMedia(pos={self.position}): {self.media_asset.filename}"


class PlatformPostPart(models.Model):
    """One thread part as it goes to one account.

    Holds the per-account caption override — the part-level counterpart of
    ``PlatformPost.platform_specific_caption`` — and the part's publish
    outcome on that account, so a thread that stops at part 3 knows exactly
    which parts are live and resumes from the first one that isn't.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        # Claimed by a worker that is calling the platform right now.
        POSTING = "posting", "Posting"
        PUBLISHED = "published", "Published"
        FAILED = "failed", "Failed"
        # The platform may have published it before the response was lost. It
        # is never re-sent on its own; resuming past it is the user's call.
        UNCONFIRMED = "unconfirmed", "Unconfirmed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    platform_post = models.ForeignKey(
        PlatformPost,
        on_delete=models.CASCADE,
        related_name="part_states",
    )
    part = models.ForeignKey(
        PostPart,
        on_delete=models.CASCADE,
        related_name="platform_states",
    )
    # Null = use the part's caption, exactly as ``platform_specific_caption``.
    platform_specific_caption = models.TextField(blank=True, null=True)

    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING, db_index=True)
    platform_part_id = models.CharField(max_length=255, blank=True, default="")
    url = models.CharField(max_length=500, blank=True, default="")
    # What the platform returned for the part. A reply chain needs some of it
    # to reply to this part in turn (Bluesky's cid).
    response = models.JSONField(default=dict, blank=True)
    publish_error = models.TextField(blank=True, default="")
    retry_count = models.PositiveIntegerField(default=0)
    published_at = models.DateTimeField(blank=True, null=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "composer_platform_post_part"
        unique_together = [("platform_post", "part")]

    def __str__(self):
        return f"PlatformPostPart({self.status}) for {self.platform_post_id}"

    @property
    def effective_caption(self):
        """Return the account's caption override or fall back to the part's caption."""
        if self.platform_specific_caption is not None:
            return self.platform_specific_caption
        return self.part.caption


class PostVersion(models.Model):
    """Immutable snapshot of a post state for version history."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    post = models.ForeignKey(
        Post,
        on_delete=models.CASCADE,
        related_name="versions",
    )
    version_number = models.PositiveIntegerField()
    snapshot = models.JSONField(
        help_text="Full post state at time of save (caption, media, platforms, etc.).",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "composer_post_version"
        ordering = ["-version_number"]
        unique_together = [("post", "version_number")]

    def __str__(self):
        return f"PostVersion(v{self.version_number}) for {self.post_id}"


class PostTemplate(models.Model):
    """Reusable post template scoped to a workspace.

    Stores a snapshot of post configuration (caption, media references,
    category, platform selections, first comment, hashtags) that can be
    loaded into the composer as a starting point.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(
        "workspaces.Workspace",
        on_delete=models.CASCADE,
        related_name="post_templates",
    )
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True, default="")
    template_data = models.JSONField(
        default=dict,
        help_text=("JSON snapshot: caption, first_comment, category_id, platform_ids, hashtags, media_asset_ids, tags"),
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
        db_table = "composer_post_template"
        ordering = ["name"]

    def __str__(self):
        return self.name


class HashtagSet(models.Model):
    """A named group of hashtags, scoped to a workspace.

    Inserted into a caption as one piece ("#coffee #latteart"). ``hashtags`` is
    the normalised list, each entry with its leading ``#``; see
    ``services.parse_hashtags``.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(
        "workspaces.Workspace",
        on_delete=models.CASCADE,
        related_name="hashtag_sets",
    )
    name = models.CharField(max_length=100)
    hashtags = models.JSONField(default=list)
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
        db_table = "composer_hashtag_set"
        unique_together = ("workspace", "name")
        ordering = ["name"]

    def __str__(self):
        return self.name

    @property
    def text(self):
        return " ".join(self.hashtags)


class SignatureManager(models.Manager):
    def for_workspace(self, workspace_id):
        return self.get_queryset().filter(workspace_id=workspace_id)

    def for_user(self, user):
        return self.get_queryset().filter(user=user)

    def available(self, workspace_id, user):
        """The signatures ``user`` may insert in ``workspace_id``: their own and the workspace's."""
        return self.get_queryset().filter(
            models.Q(workspace_id=workspace_id) | models.Q(user=user),
        )


class Signature(models.Model):
    """Sign-off text appended to a caption, owned by a user or by a workspace.

    A personal signature (``user`` set) follows the user into every workspace
    they belong to; a workspace signature (``workspace`` set) is shared by all
    its members. Each owner has at most one default, which the composer appends
    to the caption of a new post.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(
        "workspaces.Workspace",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="signatures",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="signatures",
    )
    name = models.CharField(max_length=100)
    body = models.TextField()
    is_default = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = SignatureManager()

    class Meta:
        db_table = "composer_signature"
        ordering = ["name"]
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(workspace__isnull=False, user__isnull=True)
                    | models.Q(workspace__isnull=True, user__isnull=False)
                ),
                name="signature_owned_by_workspace_or_user",
            ),
            models.UniqueConstraint(
                fields=["workspace"],
                condition=models.Q(is_default=True, workspace__isnull=False),
                name="signature_one_default_per_workspace",
            ),
            models.UniqueConstraint(
                fields=["user"],
                condition=models.Q(is_default=True, user__isnull=False),
                name="signature_one_default_per_user",
            ),
        ]

    def __str__(self):
        return self.name


class CSVImportJob(models.Model):
    """Tracks a bulk CSV import job for posts."""

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        PROCESSING = "processing", "Processing"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(
        "workspaces.Workspace",
        on_delete=models.CASCADE,
        related_name="csv_import_jobs",
    )
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    file = models.FileField(upload_to="csv_imports/")
    column_mapping = models.JSONField(
        default=dict,
        help_text="Maps CSV column indices to post fields.",
    )
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
    )
    total_rows = models.PositiveIntegerField(default=0)
    processed_rows = models.PositiveIntegerField(default=0)
    result_summary = models.JSONField(
        default=dict,
        help_text='{"created": N, "errors": N, "warnings": [...]}',
    )

    created_at = models.DateTimeField(auto_now_add=True)

    objects = WorkspaceScopedManager()

    class Meta:
        db_table = "composer_csv_import_job"
        ordering = ["-created_at"]

    def __str__(self):
        return f"CSVImportJob({self.status}): {self.total_rows} rows"


class Feed(models.Model):
    """An RSS feed subscription scoped to a workspace."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(
        "workspaces.Workspace",
        on_delete=models.CASCADE,
        related_name="feeds",
    )
    name = models.CharField(max_length=255)
    url = models.URLField(max_length=500, help_text="RSS feed URL")
    website_url = models.URLField(max_length=500, blank=True, default="")
    added_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    objects = WorkspaceScopedManager()

    class Meta:
        db_table = "composer_feed"
        ordering = ["name"]
        unique_together = [("workspace", "url")]

    @property
    def favicon_url(self):
        """Return a best-effort favicon URL derived from website_url."""
        if not self.website_url:
            return ""

        parsed = urlsplit(self.website_url)
        if not parsed.scheme or not parsed.netloc:
            return ""

        return f"{parsed.scheme}://{parsed.netloc}/favicon.ico"

    def __str__(self):
        return self.name
