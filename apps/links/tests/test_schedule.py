"""Links are tagged and shortened per account when a post is scheduled."""

from __future__ import annotations

import datetime as dt
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.composer.models import PlatformPost, PlatformPostPart, PostPart
from apps.composer.services import create_post, set_post_parts, thread_blocker, transition_platform_post
from apps.links import services
from apps.links.models import PreparedCopy, ShortLink

from .conftest import BROWSER, configure, schedule_post

pytestmark = pytest.mark.django_db

LONG = "https://shop.example.com/products/very-long-product-name?color=red&size=large&ref=newsletter-2026"


def query(url):
    return {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}


def schedule(pp):
    transition_platform_post(pp, "scheduled", scheduled_at=timezone.now() + dt.timedelta(days=1))
    return PlatformPost.objects.get(pk=pp.pk)


class TestUtm:
    def test_each_network_gets_its_own_source(self, workspace, linkedin, x_account):
        configure(workspace, shorten_mode="never", utm_enabled=True, utm_campaign_from="post")
        post, (a, b) = schedule_post(workspace, [linkedin, x_account], f"Sale {LONG}", title="Spring Sale")
        a, b = schedule(a), schedule(b)
        url_a = services.extract_urls(a.effective_caption)[0]
        url_b = services.extract_urls(b.effective_caption)[0]
        assert query(url_a)["utm_source"] == "linkedin" and query(url_b)["utm_source"] == "x"
        for url in (url_a, url_b):
            q = query(url)
            assert q["utm_medium"] == "social" and q["utm_campaign"] == "spring-sale"
            assert q["color"] == "red" and q["ref"] == "newsletter-2026"
        # the post itself still holds what was typed
        assert LONG in post.caption and "utm_" not in post.caption

    def test_campaign_from_category_tag_or_fallback(self, workspace, linkedin):
        from apps.composer.models import ContentCategory

        category = ContentCategory.objects.create(workspace=workspace, name="Product Launch")
        configure(
            workspace, shorten_mode="never", utm_enabled=True, utm_campaign_from="category", utm_campaign="always-on"
        )
        _, (a,) = schedule_post(workspace, [linkedin], LONG, category=category)
        assert query(services.extract_urls(schedule(a).effective_caption)[0])["utm_campaign"] == "product-launch"
        configure(workspace, shorten_mode="never", utm_enabled=True, utm_campaign_from="tag")
        _, (b,) = schedule_post(workspace, [linkedin], LONG, tags=["Black Friday", "x"])
        assert query(services.extract_urls(schedule(b).effective_caption)[0])["utm_campaign"] == "black-friday"
        _, (c,) = schedule_post(workspace, [linkedin], LONG)
        assert query(services.extract_urls(schedule(c).effective_caption)[0])["utm_campaign"] == "always-on"

    def test_custom_parameters_and_existing_utm_are_respected(self, workspace, linkedin):
        configure(
            workspace,
            shorten_mode="never",
            utm_enabled=True,
            utm_custom={"utm_content": "spring", "ref": "social"},
        )
        _, (a,) = schedule_post(workspace, [linkedin], "Go https://example.com/p?utm_source=newsletter#top, now")
        text = schedule(a).effective_caption
        url = services.extract_urls(text)[0]
        q = query(url)
        assert q["utm_source"] == "newsletter" and q["utm_content"] == "spring" and q["ref"] == "social"
        assert url.endswith("#top") and text.endswith(", now")

    def test_off_by_default_and_never_touches_drafts(self, workspace, linkedin):
        _, (a,) = schedule_post(workspace, [linkedin], LONG)
        assert schedule(a).effective_caption == LONG
        configure(workspace, shorten_mode="always", utm_enabled=True)
        _, (b,) = schedule_post(workspace, [linkedin], LONG)
        assert b.effective_caption == LONG and b.platform_specific_caption is None

    def test_rescheduling_does_not_stack_parameters(self, workspace, linkedin):
        configure(workspace, shorten_mode="never", utm_enabled=True)
        _, (a,) = schedule_post(workspace, [linkedin], LONG)
        a = schedule(a)
        first = a.effective_caption
        transition_platform_post(a, "draft")
        assert schedule(PlatformPost.objects.get(pk=a.pk)).effective_caption == first

    def test_preview_shows_the_final_address_per_network(self, workspace):
        configure(workspace, shorten_mode="never", utm_enabled=True, utm_custom={"utm_content": "hero"})
        out = services.utm_preview(workspace, url="https://example.com/a", platform="bluesky", title="Hello World")
        assert query(out) == {
            "utm_source": "bluesky",
            "utm_medium": "social",
            "utm_campaign": "hello-world",
            "utm_content": "hero",
        }


class TestShorten:
    def test_one_short_link_per_account_each_with_its_own_utm(self, workspace, linkedin, x_account, settings):
        configure(workspace, utm_enabled=True)
        _, (a, b) = schedule_post(workspace, [linkedin, x_account], f"Read {LONG}.")
        a, b = schedule(a), schedule(b)
        short_a = services.extract_urls(a.effective_caption)[0]
        short_b = services.extract_urls(b.effective_caption)[0]
        prefix = f"{settings.APP_URL.rstrip('/')}/s/"
        assert short_a.startswith(prefix) and short_b.startswith(prefix) and short_a != short_b
        assert a.effective_caption.endswith(".")  # the full stop stays outside the link
        links = {link.short_url: link for link in ShortLink.objects.filter(workspace=workspace)}
        assert query(links[short_a].destination_url)["utm_source"] == "linkedin"
        assert query(links[short_b].destination_url)["utm_source"] == "x"
        assert links[short_a].platform_post_id == a.pk and links[short_a].social_account_id == linkedin.pk
        assert (
            Client().get(short_a.replace(settings.APP_URL.rstrip("/"), ""), **BROWSER)["Location"]
            == links[short_a].destination_url
        )

    def test_rescheduling_reuses_the_links(self, workspace, linkedin):
        configure(workspace)
        _, (a,) = schedule_post(workspace, [linkedin], f"Read {LONG}")
        a = schedule(a)
        first = a.effective_caption
        assert ShortLink.objects.count() == 1
        transition_platform_post(a, "draft")
        again = schedule(PlatformPost.objects.get(pk=a.pk))
        assert again.effective_caption == first and ShortLink.objects.count() == 1

    def test_repeated_url_in_one_caption_shares_a_link(self, workspace, linkedin):
        configure(workspace)
        _, (a,) = schedule_post(workspace, [linkedin], f"{LONG} and again {LONG}")
        urls = services.extract_urls(schedule(a).effective_caption)
        assert len(urls) == 1 and ShortLink.objects.count() == 1

    @pytest.mark.parametrize(
        ("mode", "shorten", "expected"),
        [
            ("always", None, True),
            ("always", False, False),
            ("always", True, True),
            ("ask", None, False),
            ("ask", True, True),
            ("ask", False, False),
            ("never", True, False),
            ("never", None, False),
        ],
    )
    def test_preference_and_per_post_choice(self, workspace, linkedin, mode, shorten, expected):
        configure(workspace, shorten_mode=mode)
        _, (a,) = schedule_post(workspace, [linkedin], f"Read {LONG}", shorten=shorten)
        assert (schedule(a).effective_caption != f"Read {LONG}") is expected

    def test_first_comment_is_shortened(self, workspace, linkedin):
        configure(workspace, utm_enabled=True)
        post = create_post(
            workspace=workspace, social_account=linkedin, caption="Main", first_comment=f"More: {LONG}", status="draft"
        )
        (child,) = post.platform_posts.all()
        child = schedule(child)
        assert "/s/" in child.effective_first_comment and post.first_comment == f"More: {LONG}"

    def test_thread_parts_are_shortened_per_account(self, workspace, x_account):
        from apps.composer.services import ThreadPartSpec

        configure(workspace, utm_enabled=True)
        post = create_post(workspace=workspace, social_account=x_account, caption="Main", status="draft")
        (child,) = post.platform_posts.all()
        set_post_parts(post, [ThreadPartSpec(caption=f"Part two {LONG}")])
        child = schedule(child)
        state = PlatformPostPart.objects.get(platform_post=child)
        assert "/s/" in state.effective_caption
        assert PostPart.objects.get(post=post).caption == f"Part two {LONG}"

    def test_create_post_scheduled_over_the_api_service_shortens_parts_too(self, workspace, x_account):
        from apps.composer.services import ThreadPartSpec

        configure(workspace)
        post = create_post(
            workspace=workspace,
            social_account=x_account,
            caption=f"Hello {LONG}",
            status="scheduled",
            scheduled_at=timezone.now() + dt.timedelta(days=1),
            parts=[ThreadPartSpec(caption=f"Part {LONG}")],
        )
        (child,) = post.platform_posts.all()
        assert "/s/" in child.effective_caption
        assert "/s/" in PlatformPostPart.objects.get(platform_post=child).effective_caption

    def test_a_published_part_is_never_rewritten(self, workspace, x_account):
        from apps.composer.services import ThreadPartSpec

        configure(workspace)
        post = create_post(workspace=workspace, social_account=x_account, caption="Hi", status="draft")
        set_post_parts(post, [ThreadPartSpec(caption=f"Part {LONG}")])
        (child,) = post.platform_posts.all()
        part = post.thread_parts.get()
        PlatformPostPart.objects.create(platform_post=child, part=part, status="published")
        schedule(child)
        assert ShortLink.objects.count() == 0

    def test_already_short_links_are_left_alone(self, workspace, linkedin):
        configure(workspace)
        mine = services.create_short_link(workspace, LONG)
        _, (a,) = schedule_post(workspace, [linkedin], f"Read {mine.short_url}")
        assert schedule(a).effective_caption == f"Read {mine.short_url}"
        assert ShortLink.objects.count() == 1

    def test_non_web_addresses_and_mailto_are_ignored(self, workspace, linkedin):
        configure(workspace)
        _, (a,) = schedule_post(workspace, [linkedin], "Mail mailto:a@example.com or ftp://example.com/x")
        assert schedule(a).effective_caption == "Mail mailto:a@example.com or ftp://example.com/x"


class TestCounting:
    def test_counts_use_the_short_url_length(self, workspace, bluesky):
        configure(workspace)
        link_settings = services.get_settings(workspace)
        caption = "A" * 100 + " " + "https://example.com/" + "p" * 300
        counted = services.counting_text(caption, link_settings, shorten=True)
        assert len(counted) == 101 + services.expected_short_length(link_settings)
        assert bluesky.caption_wire_length(caption) > bluesky.char_limit
        assert bluesky.caption_wire_length(counted) <= bluesky.char_limit
        assert services.counting_text(caption, link_settings, shorten=False) == caption

    def test_x_keeps_its_weighted_count_where_a_url_is_23(self, workspace, x_account):
        configure(workspace)
        link_settings = services.get_settings(workspace)
        caption = "Big news " + "https://example.com/" + "p" * 400 + " \U0001f389"
        raw = x_account.caption_wire_length(caption)
        counted = x_account.caption_wire_length(services.counting_text(caption, link_settings, shorten=True))
        # a URL weighs 23 on X whatever its length, so shortening never changes the weighted count
        assert raw == counted == len("Big news ") + 23 + 1 + 2

    def test_thread_part_limits_use_the_short_length(self, workspace, bluesky):
        configure(workspace)
        part = "B" * 100 + " https://example.com/" + "q" * 400
        assert thread_blocker(bluesky, [(part, [])]) != ""
        counting = services.link_counting(workspace, "1")
        assert thread_blocker(bluesky, [(part, [])], counting=counting) == ""
        assert services.link_counting(workspace, "0") is None

    def test_composer_preview_counts_short_links_only_when_shortening(self, workspace, owner, bluesky):
        configure(workspace, shorten_mode="ask")
        client = Client()
        client.force_login(owner)
        url = reverse("composer:preview", kwargs={"workspace_id": workspace.id})
        caption = "C" * 100 + " https://example.com/" + "z" * 400
        data = {"caption": caption, "selected_accounts": str(bluesky.id)}
        plain = client.post(url, data).context["previews"][0]
        short = client.post(url, {**data, "shorten_links": "1"}).context["previews"][0]
        assert plain["char_count"] == len(caption) and plain["is_over_limit"]
        assert short["char_count"] < 200 and not short["is_over_limit"]


class TestExternalFailure:
    def test_a_failing_service_falls_back_to_a_built_in_link_and_says_so(self, workspace, owner, linkedin, mock_http):
        configure(workspace, provider="dub", api_key="key-1", utm_enabled=True)
        mock_http.reply(lambda request: httpx.Response(500))
        _, (a,) = schedule_post(workspace, [linkedin], f"Read {LONG}")
        text = schedule(a).effective_caption
        link = ShortLink.objects.get()
        assert link.provider == "builtin" and text == f"Read {link.short_url}"
        assert query(link.destination_url)["utm_source"] == "linkedin"
        problem = PreparedCopy.objects.get(platform_post=a).problem
        assert "error (500)" in problem and "built-in" in problem
        client = Client()
        client.force_login(owner)
        page = client.get(reverse("links:list", kwargs={"workspace_id": workspace.id}))
        assert "built-in short link was used instead" in page.content.decode()

    def test_a_plan_limit_leaves_the_tagged_address_and_says_so(self, workspace, linkedin, monkeypatch):
        from apps.billing.entitlements import PlanLimitError

        def refuse(org):
            raise PlanLimitError("Your plan includes 0 short links a month.", code="short_link_limit")

        monkeypatch.setattr(services, "check_can_create_short_link", refuse)
        configure(workspace, utm_enabled=True)
        _, (a,) = schedule_post(workspace, [linkedin], f"Read {LONG}")
        text = schedule(a).effective_caption
        assert text.startswith("Read https://shop.example.com/") and "utm_source=linkedin" in text
        assert "left as typed" in PreparedCopy.objects.get(platform_post=a).problem

    def test_an_external_service_makes_the_short_links(self, workspace, linkedin, mock_http):
        configure(workspace, provider="dub", api_key="key-1")
        mock_http.reply(
            lambda r: httpx.Response(200, json={"id": "link_1", "key": "abc", "shortLink": "https://dub.sh/abc"})
        )
        _, (a,) = schedule_post(workspace, [linkedin], f"Read {LONG}")
        a = schedule(a)
        assert a.effective_caption == "Read https://dub.sh/abc"
        link = ShortLink.objects.get()
        assert link.provider == "dub" and link.external_id == "link_1"
        # the external address is already short next time
        transition_platform_post(a, "draft")
        again = schedule(PlatformPost.objects.get(pk=a.pk))
        assert again.effective_caption == "Read https://dub.sh/abc" and len(mock_http.calls) == 1


class TestComposerSave:
    def test_scheduling_from_the_composer_shortens_per_account(self, workspace, owner, linkedin, x_account):
        configure(workspace, shorten_mode="ask", utm_enabled=True)
        client = Client()
        client.force_login(owner)
        when = timezone.now() + dt.timedelta(days=3)
        data = {
            "action": "schedule",
            "title": "Launch",
            "caption": f"New: {LONG}",
            "selected_accounts": f"{linkedin.id},{x_account.id}",
            "scheduled_date": when.strftime("%Y-%m-%d"),
            "scheduled_time": "10:00",
            "shorten_links": "1",
        }
        response = client.post(reverse("composer:save_post", kwargs={"workspace_id": workspace.id}), data)
        assert response.status_code in (200, 204, 302), response.content
        pps = {pp.social_account.platform: pp for pp in PlatformPost.objects.select_related("social_account")}
        assert set(pps) == {"linkedin", "x"} and {pp.status for pp in pps.values()} == {"scheduled"}
        shorts = {p: services.extract_urls(pp.effective_caption)[0] for p, pp in pps.items()}
        assert "/s/" in shorts["linkedin"] and shorts["linkedin"] != shorts["x"]
        assert pps["linkedin"].post.shorten_links is True and pps["linkedin"].post.caption == f"New: {LONG}"

    def test_unchecked_keeps_links_as_typed(self, workspace, owner, linkedin):
        configure(workspace, shorten_mode="always")
        client = Client()
        client.force_login(owner)
        when = timezone.now() + dt.timedelta(days=3)
        data = {
            "action": "schedule",
            "caption": f"New: {LONG}",
            "selected_accounts": str(linkedin.id),
            "scheduled_date": when.strftime("%Y-%m-%d"),
            "scheduled_time": "10:00",
            "shorten_links": "0",
        }
        client.post(reverse("composer:save_post", kwargs={"workspace_id": workspace.id}), data)
        pp = PlatformPost.objects.get()
        assert pp.effective_caption == f"New: {LONG}" and ShortLink.objects.count() == 0

    def test_should_shorten_prompt_check(self, workspace, owner):
        configure(workspace, shorten_mode="ask")
        client = Client()
        client.force_login(owner)
        url = reverse("links:should_shorten", kwargs={"workspace_id": workspace.id})
        assert client.post(url, {"messages": [f"see {LONG}"]}).json()["ask"] is True
        assert client.post(url, {"messages": ["no link"]}).json()["ask"] is False
        configure(workspace, shorten_mode="always")
        assert client.post(url, {"messages": [f"see {LONG}"]}).json()["ask"] is False


class TestPlanLimit:
    def test_short_links_per_month_follow_the_plan(self, workspace, linkedin, settings):
        from apps.billing.tests.support import set_plan

        settings.BILLING_ENABLED = True
        set_plan(workspace.organization, "free")
        configure(workspace)
        for i in range(100):
            ShortLink.objects.create(
                workspace=workspace, code=f"c{i:05d}", destination_url="https://example.com/", short_url=f"x{i}"
            )
        with pytest.raises(services.LinkError, match="short links a month"):
            services.create_short_link(workspace, "https://example.com/more")
        set_plan(workspace.organization, "ultimate")
        assert services.create_short_link(workspace, "https://example.com/more")


def test_campaign_from_the_caption_leaves_its_addresses_out(workspace):
    from types import SimpleNamespace

    link_settings = services.get_settings(workspace)
    post = SimpleNamespace(title="", caption="Spring sale https://shop.example.com/spring?x=1 ends Friday")
    assert services.campaign_for(link_settings, post) == "spring-sale-ends-friday"


class TestEditsAfterScheduling:
    def test_editing_the_post_caption_is_not_hidden_behind_the_rewritten_copy(self, workspace, linkedin):
        configure(workspace)
        post, (a,) = schedule_post(workspace, [linkedin], f"Read {LONG}")
        first = schedule(a)
        old_link = ShortLink.objects.get()
        assert first.effective_caption == f"Read {old_link.short_url}"
        # an edit through the API or an autosave changes only the post's own caption
        post.caption = f"Now read {LONG} and https://other.example.org/page"
        post.save(update_fields=["caption", "updated_at"])
        a = PlatformPost.objects.get(pk=a.pk)
        links = {link.destination_url: link.short_url for link in ShortLink.objects.all()}
        assert a.effective_caption == f"Now read {links[LONG]} and {links['https://other.example.org/page']}"
        assert links[LONG] == old_link.short_url  # the same address keeps its link

    def test_removing_the_link_restores_the_post_text_and_drops_the_unused_link(self, workspace, linkedin):
        configure(workspace)
        post, (a,) = schedule_post(workspace, [linkedin], f"Read {LONG}")
        schedule(a)
        post.caption = "No link any more"
        post.save()
        a = PlatformPost.objects.get(pk=a.pk)
        assert a.platform_specific_caption is None and a.effective_caption == "No link any more"
        assert not ShortLink.objects.exists()

    def test_turning_shortening_off_for_the_post_restores_the_typed_links(self, workspace, linkedin):
        configure(workspace)
        post, (a,) = schedule_post(workspace, [linkedin], f"Read {LONG}")
        schedule(a)
        post.shorten_links = False
        post.save(update_fields=["shorten_links"])
        assert PlatformPost.objects.get(pk=a.pk).effective_caption == f"Read {LONG}"

    def test_an_authors_own_override_is_kept_and_only_its_links_change(self, workspace, linkedin):
        configure(workspace)
        post, (a,) = schedule_post(workspace, [linkedin], "Base text")
        a.platform_specific_caption = f"Custom {LONG}"
        a.save()
        a = schedule(a)
        link = ShortLink.objects.get()
        assert a.effective_caption == f"Custom {link.short_url}"
        post.caption = "Edited base"
        post.save()
        assert PlatformPost.objects.get(pk=a.pk).effective_caption == f"Custom {link.short_url}"

    def test_saves_that_do_not_touch_text_do_nothing(self, workspace, linkedin, django_assert_max_num_queries):
        configure(workspace)
        post, (a,) = schedule_post(workspace, [linkedin], f"Read {LONG}")
        a = schedule(a)
        with django_assert_max_num_queries(2):  # the two UPDATEs and nothing else
            a.save(update_fields=["scheduled_at"])
            post.save(update_fields=["internal_notes"])


class TestApprovals:
    @pytest.fixture
    def approval_workspace(self, workspace, owner):
        workspace.approval_workflow_mode = "required_internal"
        workspace.save(update_fields=["approval_workflow_mode"])
        return workspace

    def test_reviewers_approve_the_final_links_and_scheduling_keeps_the_approval(
        self, approval_workspace, owner, linkedin
    ):
        from apps.approvals import services as approvals
        from apps.approvals.policy import approval_allows_publish

        configure(approval_workspace, utm_enabled=True)
        post, (a,) = schedule_post(approval_workspace, [linkedin], f"Read {LONG}")
        post.author = owner
        post.save(update_fields=["author"])
        approvals.submit_for_review(post, owner, approval_workspace)
        a = PlatformPost.objects.get(pk=a.pk)
        link = ShortLink.objects.get()
        # what the reviewer sees is already the final text
        assert a.status == "pending_review" and a.effective_caption == f"Read {link.short_url}"
        approvals.approve_post(post, owner, approval_workspace)
        a = PlatformPost.objects.get(pk=a.pk)
        a = schedule(a)
        assert a.effective_caption == f"Read {link.short_url}"
        assert approval_allows_publish(a)
        assert ShortLink.objects.count() == 1

    def test_settings_changed_after_approval_never_rewrite_approved_content(self, approval_workspace, owner, linkedin):
        from apps.approvals import services as approvals
        from apps.approvals.policy import approval_allows_publish

        post, (a,) = schedule_post(approval_workspace, [linkedin], f"Read {LONG}")
        approvals.submit_for_review(post, owner, approval_workspace)
        approvals.approve_post(post, owner, approval_workspace)
        configure(approval_workspace, utm_enabled=True)  # turned on after the reviewer said yes
        a = schedule(PlatformPost.objects.get(pk=a.pk))
        assert a.effective_caption == f"Read {LONG}"
        assert approval_allows_publish(a) and not ShortLink.objects.exists()
