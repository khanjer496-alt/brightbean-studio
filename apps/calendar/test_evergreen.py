"""Evergreen recycling: adding items, the selection rules and the periodic fill."""

from __future__ import annotations

import datetime as dt
from datetime import timedelta

import pytest
from django.utils import timezone

from apps.accounts.models import User
from apps.ai_writer.models import AIRequestLog
from apps.ai_writer.tests.fakes import patch_anthropic
from apps.billing.entitlements import PlanLimitError
from apps.calendar import evergreen
from apps.calendar import services as queue_services
from apps.calendar.models import EvergreenItem, EvergreenRepeat, PostingSlot, Queue
from apps.composer.models import ContentCategory, PlatformPost, Post, PostMedia
from apps.media_library.models import MediaAsset
from apps.members.models import OrgMembership, WorkspaceMembership
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace

NOW = timezone.now()


@pytest.fixture
def workspace(db, organization):
    return Workspace.objects.create(organization=organization, name="Main", timezone="UTC")


@pytest.fixture
def author(db, organization, workspace):
    user = User.objects.create_user(
        email="author@example.com", password="x" * 12, name="Author", tos_accepted_at=timezone.now()
    )
    OrgMembership.objects.create(user=user, organization=organization, org_role=OrgMembership.OrgRole.OWNER)
    WorkspaceMembership.objects.create(
        user=user, workspace=workspace, workspace_role=WorkspaceMembership.WorkspaceRole.OWNER
    )
    return user


@pytest.fixture
def account(workspace):
    return SocialAccount.objects.create(
        workspace=workspace,
        platform="bluesky",
        account_platform_id="sky",
        account_name="sky",
        connection_status="connected",
        oauth_access_token="tok",
    )


@pytest.fixture
def queue(workspace, account):
    for day in range(7):
        for hour in (10, 15, 19):
            PostingSlot.objects.create(social_account=account, day_of_week=day, time=dt.time(hour, 0))
    return Queue.objects.create(workspace=workspace, name="Main", social_account=account)


def published_post(workspace, account, caption="Ten tips for better sleep", author=None, days_ago=60):
    post = Post.objects.create(workspace=workspace, caption=caption, author=author, tags=["tips"])
    when = timezone.now() - timedelta(days=days_ago)
    PlatformPost.objects.create(
        post=post, social_account=account, status="published", published_at=when, platform_post_id=f"p-{post.id}"
    )
    return post


def add(post, account, user=None, **kwargs):
    pp = post.platform_posts.get(social_account=account)
    return evergreen.add_from_platform_post(pp, user=user, **kwargs)


def make_asset(workspace, media_type="image", file="media/x.jpg"):
    return MediaAsset.objects.create(
        organization=workspace.organization,
        workspace=workspace,
        file=file,
        filename="x",
        media_type=media_type,
        mime_type="image/jpeg",
    )


@pytest.mark.django_db
class TestAdding:
    def test_copies_the_content_and_counts_the_original_as_the_first_posting(self, workspace, account, author):
        post = published_post(workspace, account, author=author)
        asset = make_asset(workspace)
        PostMedia.objects.create(post=post, media_asset=asset)
        item = add(post, account, author, interval_days=45, max_repeats=3)
        assert item.caption == post.caption and item.tags == ["tips"]
        assert item.media_asset_ids == [str(asset.id)]
        assert (item.interval_days, item.max_repeats, item.repeat_count) == (45, 3, 0)
        assert item.last_posted_at == post.platform_posts.get().published_at
        assert not item.pre_approved

    def test_only_published_posts_and_only_once(self, workspace, account, author):
        draft = Post.objects.create(workspace=workspace, caption="x")
        PlatformPost.objects.create(post=draft, social_account=account, status="draft")
        with pytest.raises(evergreen.EvergreenError, match="published"):
            evergreen.add_from_platform_post(draft.platform_posts.get(), user=author)
        post = published_post(workspace, account)
        add(post, account, author)
        with pytest.raises(evergreen.EvergreenError, match="already"):
            add(post, account, author)

    def test_rules_are_validated(self):
        assert evergreen.clean_rules("30", "") == (30, None)
        assert evergreen.clean_rules(7, 0) == (7, None)
        for bad in ((0, None), (4000, None), ("x", None), (30, "x"), (30, 5000)):
            with pytest.raises(evergreen.EvergreenError):
                evergreen.clean_rules(*bad)

    def test_template_and_idea_sources(self, workspace, account, author):
        from apps.composer.models import Idea, PostTemplate

        template = PostTemplate.objects.create(
            workspace=workspace, name="Tip", template_data={"caption": "A template tip", "tags": ["a"]}
        )
        idea = Idea.objects.create(workspace=workspace, title="Idea", description="An idea worth repeating")
        from_template = evergreen.add_from_template(template, account, user=author)
        from_idea = evergreen.add_from_idea(idea, account, user=author, interval_days=10)
        assert from_template.source_kind == "template" and from_template.caption == "A template tip"
        assert from_idea.source_kind == "idea" and from_idea.interval_days == 10
        assert from_template.last_posted_at is None  # never posted: due straight away
        empty = PostTemplate.objects.create(workspace=workspace, name="Empty", template_data={})
        with pytest.raises(evergreen.EvergreenError, match="nothing to recycle"):
            evergreen.add_from_template(empty, account)


@pytest.mark.django_db
class TestSelectionRules:
    def item(self, workspace, account, **kw):
        defaults = {"workspace": workspace, "social_account": account, "caption": "c", "interval_days": 30}
        return EvergreenItem.objects.create(**{**defaults, **kw})

    def test_minimum_interval(self, workspace, account):
        item = self.item(workspace, account, last_posted_at=NOW - timedelta(days=29))
        assert not evergreen.is_due(item, NOW)
        assert evergreen.is_due(item, NOW + timedelta(days=1, minutes=1))
        assert evergreen.is_due(self.item(workspace, account, last_posted_at=None), NOW)

    def test_max_repeats_and_pause(self, workspace, account):
        done = self.item(workspace, account, max_repeats=2, repeat_count=2)
        assert not evergreen.is_due(done, NOW)
        done.max_repeats = 3
        assert evergreen.is_due(done, NOW)
        done.is_paused = True
        assert not evergreen.is_due(done, NOW)

    def test_categories_take_turns_and_the_oldest_item_goes_first(self, workspace, account):
        tips = ContentCategory.objects.create(workspace=workspace, name="Evergreen: Tips")
        promo = ContentCategory.objects.create(workspace=workspace, name="Evergreen: Promo")
        t_old = self.item(workspace, account, category=tips, last_posted_at=NOW - timedelta(days=90))
        t_new = self.item(workspace, account, category=tips, last_posted_at=NOW - timedelta(days=40))
        p_old = self.item(workspace, account, category=promo, last_posted_at=NOW - timedelta(days=50))
        items = [t_new, p_old, t_old]
        # Promo was used last, so tips go first, and its oldest item within it.
        rotation = {promo.id: NOW - timedelta(days=1), tips.id: NOW - timedelta(days=2)}
        assert evergreen.choose_next(items, rotation) == t_old
        # A category that was never used beats one that was.
        assert evergreen.choose_next(items, {tips.id: NOW}) == p_old
        assert evergreen.choose_next([t_new, t_old], {}) == t_old


@pytest.mark.django_db
class TestFill:
    def setup_item(self, workspace, account, author, **kw):
        post = published_post(workspace, account, author=author)
        return add(post, account, author, interval_days=30, **kw)

    def test_fills_an_open_slot_with_a_scheduled_post_and_logs_the_repeat(self, workspace, account, queue, author):
        item = self.setup_item(workspace, account, author)
        result = evergreen.fill_account(account, now=NOW)
        assert result.scheduled >= 1
        repeat = EvergreenRepeat.objects.get(item=item)
        pp = repeat.platform_post
        assert pp.status == "scheduled" and pp.scheduled_at == repeat.scheduled_for
        assert pp.post.caption == item.caption and pp.post.tags == ["tips"]
        assert pp.post.queue_entries.filter(queue=queue, assigned_slot_datetime=pp.scheduled_at).exists()
        item.refresh_from_db()
        assert item.repeat_count == 1 and item.last_posted_at == pp.scheduled_at
        assert repeat.wording == "original"

    def test_one_item_is_not_repeated_within_its_interval_even_with_many_open_slots(
        self, workspace, account, queue, author
    ):
        item = self.setup_item(workspace, account, author)
        evergreen.fill_account(account, now=NOW)
        assert item.repeats.count() == 1  # the slots after the first stay open

    def test_running_twice_is_idempotent(self, workspace, account, queue, author):
        item = self.setup_item(workspace, account, author)
        evergreen.fill_account(account, now=NOW)
        again = evergreen.fill_account(account, now=NOW)
        assert again.scheduled == 0
        assert item.repeats.count() == 1
        assert Post.objects.filter(evergreen_repeats__item=item).count() == 1

    def test_taken_slots_are_left_alone(self, workspace, account, queue, author):
        self.setup_item(workspace, account, author)
        first_free = queue_services.next_available_slots(account, 1, after=NOW + evergreen.FILL_LEAD)[0]
        manual = Post.objects.create(workspace=workspace, caption="mine")
        PlatformPost.objects.create(post=manual, social_account=account, status="scheduled", scheduled_at=first_free)
        evergreen.fill_account(account, now=NOW)
        repeat = EvergreenRepeat.objects.get()
        assert repeat.scheduled_for != first_free

    def test_only_the_next_few_days_are_filled(self, workspace, account, queue, author):
        self.setup_item(workspace, account, author)
        evergreen.fill_account(account, now=NOW)
        assert EvergreenRepeat.objects.get().scheduled_for <= NOW + evergreen.FILL_HORIZON

    def test_rotates_categories_across_slots(self, workspace, account, queue, author):
        tips = ContentCategory.objects.create(workspace=workspace, name="Evergreen: Tips")
        promo = ContentCategory.objects.create(workspace=workspace, name="Evergreen: Promo")
        a = self.setup_item(workspace, account, author, category=tips)
        b_post = published_post(workspace, account, caption="Buy our thing", author=author)
        b = add(b_post, account, author, category=promo, interval_days=30)
        evergreen.fill_account(account, now=NOW)
        assert a.repeats.count() == 1 and b.repeats.count() == 1

    def test_paused_and_maxed_items_are_skipped(self, workspace, account, queue, author):
        paused = self.setup_item(workspace, account, author)
        evergreen.set_paused(paused, True)
        assert evergreen.fill_account(account, now=NOW).scheduled == 0
        evergreen.set_paused(paused, False)
        EvergreenItem.objects.filter(pk=paused.pk).update(max_repeats=1, repeat_count=1)
        assert evergreen.fill_account(account, now=NOW).scheduled == 0

    def test_item_whose_media_was_deleted_is_skipped_with_a_reason(self, workspace, account, queue, author):
        post = published_post(workspace, account, author=author)
        asset = make_asset(workspace)
        PostMedia.objects.create(post=post, media_asset=asset)
        item = add(post, account, author)
        asset.delete()
        result = evergreen.fill_account(account, now=NOW)
        assert result.scheduled == 0 and result.skipped == 1
        item.refresh_from_db()
        assert "deleted" in item.last_skip_reason
        assert not EvergreenRepeat.objects.exists()

    def test_media_that_no_longer_passes_platform_rules_is_skipped(self, workspace, account, queue, author):
        post = published_post(workspace, account, author=author)
        asset = make_asset(workspace)
        PostMedia.objects.create(post=post, media_asset=asset)
        item = add(post, account, author)
        EvergreenItem.objects.filter(pk=item.pk).update(
            platform_extra={}, social_account=SocialAccount.objects.get(pk=account.pk)
        )
        # Reddit-style networks take no media at all: switching the account's platform makes it invalid.
        SocialAccount.objects.filter(pk=account.pk).update(platform="reddit")
        account.refresh_from_db()
        item.refresh_from_db()
        assert evergreen.media_problem(item)
        assert evergreen.fill_account(account, now=NOW).scheduled == 0

    def test_plan_limit_stops_the_account_without_creating_posts(self, workspace, account, queue, author, monkeypatch):
        item = self.setup_item(workspace, account, author)

        def refuse(*args, **kwargs):
            raise PlanLimitError("limit", code="post_limit")

        monkeypatch.setattr(evergreen, "check_can_schedule_posts", refuse)
        result = evergreen.fill_account(account, now=NOW)
        assert result.scheduled == 0
        assert not item.repeats.exists()
        assert Post.objects.filter(evergreen_repeats__isnull=False).count() == 0

    def test_no_active_queue_means_nothing_happens(self, workspace, account, author):
        self.setup_item(workspace, account, author)
        assert evergreen.fill_account(account, now=NOW).scheduled == 0

    def test_fill_queues_covers_connected_accounts_and_survives_a_failure(
        self, workspace, account, queue, author, monkeypatch
    ):
        item = self.setup_item(workspace, account, author)
        other = SocialAccount.objects.create(
            workspace=workspace, platform="mastodon", account_platform_id="m", connection_status="connected"
        )
        EvergreenItem.objects.create(workspace=workspace, social_account=other, caption="c")
        real = evergreen.fill_account

        def flaky(acc, now=None):
            if acc.pk == other.pk:
                raise RuntimeError("boom")
            return real(acc, now=now)

        monkeypatch.setattr(evergreen, "fill_account", flaky)
        total = evergreen.fill_queues(now=NOW)
        assert total.scheduled >= 1 and item.repeats.count() == 1

    def test_the_periodic_task_is_registered_once(self, db):
        from background_task.models import Task

        from apps.calendar.apps import CalendarConfig

        CalendarConfig._register_tasks(None)
        CalendarConfig._register_tasks(None)
        assert Task.objects.filter(verbose_name="fill_queues_from_evergreen").count() == 1


@pytest.mark.django_db
class TestApproval:
    def test_requires_approval_so_the_repeat_goes_to_review_as_a_draft(self, workspace, account, queue, author):
        Workspace.objects.filter(pk=workspace.pk).update(approval_workflow_mode="required_internal")
        workspace.refresh_from_db()
        item = add(published_post(workspace, account, author=author), account, author)
        item.refresh_from_db()
        result = evergreen.fill_account(account, now=NOW)
        assert result.awaiting_approval == 1 and result.scheduled == 0
        repeat = EvergreenRepeat.objects.get(item=item)
        assert repeat.outcome == "awaiting_approval"
        assert repeat.platform_post.status == "pending_review"
        assert repeat.platform_post.scheduled_at is None
        assert repeat.post.proposed_publish_at == repeat.scheduled_for
        assert not repeat.post.queue_entries.exists()

    def test_only_one_repeat_waits_for_review_at_a_time(self, workspace, account, queue, author):
        Workspace.objects.filter(pk=workspace.pk).update(approval_workflow_mode="required_internal")
        first = add(published_post(workspace, account, author=author), account, author)
        add(published_post(workspace, account, caption="Another", author=author), account, author)
        evergreen.fill_account(account, now=NOW)
        evergreen.fill_account(account, now=NOW)
        assert EvergreenRepeat.objects.count() == 1
        assert first.repeats.count() + EvergreenRepeat.objects.exclude(item=first).count() == 1

    def test_pre_approved_items_are_scheduled_despite_the_workflow(self, workspace, account, queue, author):
        Workspace.objects.filter(pk=workspace.pk).update(approval_workflow_mode="required_internal")
        item = add(published_post(workspace, account, author=author), account, author, pre_approved=True)
        assert item.pre_approved_by == author
        result = evergreen.fill_account(account, now=NOW)
        assert result.scheduled == 1
        pp = EvergreenRepeat.objects.get(item=item).platform_post
        assert pp.status == "scheduled" and pp.approval_completed_at is not None

    def test_approval_not_required_means_normal_scheduling(self, workspace, account, queue, author):
        add(published_post(workspace, account, author=author), account, author)
        assert evergreen.fill_account(account, now=NOW).scheduled == 1
        repeat = EvergreenRepeat.objects.get()
        assert repeat.approval == "not_required" and repeat.text == "Ten tips for better sleep"

    def test_media_changed_after_pre_approval_goes_to_review(self, workspace, account, queue, author):
        Workspace.objects.filter(pk=workspace.pk).update(approval_workflow_mode="required_internal")
        post = published_post(workspace, account, author=author)
        asset = make_asset(workspace)
        PostMedia.objects.create(post=post, media_asset=asset)
        item = add(post, account, author, pre_approved=True)
        assert item.approved_fingerprint == evergreen.content_fingerprint(item)
        MediaAsset.objects.filter(pk=asset.pk).update(file="media/replaced.jpg")
        assert evergreen.fill_account(account, now=NOW).awaiting_approval == 1
        assert EvergreenRepeat.objects.get().approval == "review"

    def test_ai_wording_is_not_covered_by_pre_approval(self, workspace, account, queue, author):
        Workspace.objects.filter(pk=workspace.pk).update(approval_workflow_mode="required_internal")
        add(published_post(workspace, account, author=author), account, author, pre_approved=True, vary_with_ai=True)
        assert evergreen.fill_account(account, now=NOW).awaiting_approval == 1


@pytest.mark.django_db
class TestAiVariation:
    def test_reworded_text_is_used_and_logged_as_a_system_request(
        self, workspace, account, queue, author, settings, fake_claude
    ):
        settings.ANTHROPIC_API_KEY = "sk-ant-platform-key-for-tests"
        Workspace.objects.filter(pk=workspace.pk).update(brand_voice_tone="Warm and dry")
        item = add(published_post(workspace, account, author=author), account, author, vary_with_ai=True)
        fake_claude.queue({"text": "Ten sleep tips, freshly worded"})
        evergreen.fill_account(account, now=NOW)
        repeat = EvergreenRepeat.objects.get(item=item)
        assert repeat.wording == "ai"
        assert repeat.post.caption == "Ten sleep tips, freshly worded"
        assert item.caption == "Ten tips for better sleep"  # the stored original is untouched
        assert "Warm and dry" in fake_claude.systems[0]
        log = AIRequestLog.objects.get()
        assert log.source == AIRequestLog.Source.SYSTEM and log.action == "vary"

    def test_falls_back_to_the_original_when_ai_is_not_configured(self, workspace, account, queue, author, settings):
        settings.ANTHROPIC_API_KEY = ""
        item = add(published_post(workspace, account, author=author), account, author, vary_with_ai=True)
        evergreen.fill_account(account, now=NOW)
        repeat = EvergreenRepeat.objects.get(item=item)
        assert repeat.wording == "original" and repeat.post.caption == item.caption

    def test_falls_back_when_the_provider_fails(self, workspace, account, queue, author, settings, fake_claude):
        settings.ANTHROPIC_API_KEY = "sk-ant-platform-key-for-tests"
        item = add(published_post(workspace, account, author=author), account, author, vary_with_ai=True)
        fake_claude.queue(RuntimeError("provider down"))
        try:
            evergreen.fill_account(account, now=NOW)
        except RuntimeError:
            pytest.fail("a failing provider must not stop recycling")
        assert EvergreenRepeat.objects.get(item=item).wording == "original"

    def test_falls_back_without_the_organizations_own_ai(self, workspace, account, queue, author, settings):
        # Hosted mode: the platform key never powers customer AI, so the wording stays.
        settings.ANTHROPIC_API_KEY = "sk-ant-platform-key-for-tests"
        settings.AI_PLATFORM_KEY_FOR_CUSTOMERS = False
        item = add(published_post(workspace, account, author=author), account, author, vary_with_ai=True)
        evergreen.fill_account(account, now=NOW)
        assert EvergreenRepeat.objects.get(item=item).wording == "original"

    def test_post_as_is_never_calls_ai(self, workspace, account, queue, author, settings, fake_claude):
        settings.ANTHROPIC_API_KEY = "sk-ant-platform-key-for-tests"
        add(published_post(workspace, account, author=author), account, author, vary_with_ai=False)
        evergreen.fill_account(account, now=NOW)
        assert fake_claude.calls == []

    def test_a_rewording_that_drops_a_link_falls_back_to_the_original(
        self, workspace, account, queue, author, settings, fake_claude
    ):
        settings.ANTHROPIC_API_KEY = "sk-ant-platform-key-for-tests"
        caption = "Ten tips https://example.com/sleep #rest"
        item = add(published_post(workspace, account, caption, author=author), account, author, vary_with_ai=True)
        fake_claude.queue({"text": "Ten sleep tips, freshly worded #rest"})
        evergreen.fill_account(account, now=NOW)
        repeat = EvergreenRepeat.objects.get(item=item)
        assert repeat.wording == "original" and repeat.post.caption == caption


@pytest.mark.django_db
class TestOriginalAndAccountState:
    def test_a_deleted_original_pauses_the_item(self, workspace, account, queue, author):
        post = published_post(workspace, account, author=author)
        item = add(post, account, author)
        post.delete()
        assert evergreen.fill_account(account, now=NOW).skipped == 1
        item.refresh_from_db()
        assert item.is_paused and "deleted" in item.last_skip_reason
        assert not EvergreenRepeat.objects.exists()

    def test_an_original_the_network_stopped_returning_is_skipped_not_paused(self, workspace, account, queue, author):
        post = published_post(workspace, account, author=author)
        item = add(post, account, author)
        post.platform_posts.update(analytics_failure_count=evergreen.ORIGINAL_GONE_FAILURES)
        assert evergreen.fill_account(account, now=NOW).skipped == 1
        item.refresh_from_db()
        assert not item.is_paused and "deleted there" in item.last_skip_reason

    def test_disconnected_accounts_and_deleting_organizations_are_left_alone(self, workspace, account, queue, author):
        add(published_post(workspace, account, author=author), account, author)
        workspace.organization.deletion_requested_at = timezone.now()
        workspace.organization.save()
        assert evergreen.fill_queues(now=NOW).scheduled == 0
        workspace.organization.deletion_requested_at = None
        workspace.organization.save()
        SocialAccount.objects.filter(pk=account.pk).update(connection_status="disconnected")
        account.refresh_from_db()
        with pytest.raises(evergreen.SkipError, match="disconnected"):
            evergreen.recycle(EvergreenItem.objects.get(), queue, NOW + timedelta(days=1))

    def test_text_over_the_platform_limit_is_skipped(self, workspace, account, queue, author):
        item = add(published_post(workspace, account, "x" * (account.char_limit + 1), author=author), account, author)
        assert evergreen.fill_account(account, now=NOW).skipped == 1
        item.refresh_from_db()
        assert "longer than" in item.last_skip_reason

    def test_series_posts_and_repeats_are_refused(self, workspace, account, queue, author):
        item = add(published_post(workspace, account, author=author), account, author)
        evergreen.fill_account(account, now=NOW)
        repeat_post = EvergreenRepeat.objects.get(item=item).post
        PlatformPost.objects.filter(post=repeat_post).update(status="published", published_at=timezone.now())
        with pytest.raises(evergreen.EvergreenError, match="itself an evergreen repeat"):
            add(repeat_post, account, author)


@pytest.fixture
def fake_claude():
    with patch_anthropic() as fake:
        yield fake
