"""Ideas, idea groups, templates, tags, categories, calendar events, brand voice and usage."""

from __future__ import annotations

import pytest

from apps.api.tests.world import make_post
from apps.calendar.models import CustomCalendarEvent
from apps.composer.models import ContentCategory, Idea, IdeaGroup, PostTemplate, Tag
from apps.media_library.models import MediaAsset


def send(client, method, url, payload=None):
    return getattr(client, method)(url, data=payload or {}, content_type="application/json")


NO_UUID = "00000000-0000-4000-8000-000000000000"


@pytest.mark.django_db
class TestIdeas:
    URL = "/api/v1/ideas/"
    GROUPS = "/api/v1/idea-groups/"

    def test_requires_a_key_and_create_posts(self, world):
        assert world.anonymous.get(self.URL).status_code == 401
        assert world.anonymous.get(self.GROUPS).status_code == 401
        assert world.bare.get(self.URL).status_code == 403
        assert send(world.bare, "post", self.URL, {"title": "x"}).status_code == 403
        assert world.bare.get(self.GROUPS).status_code == 403
        assert not Idea.objects.exists()

    def test_crud_and_move_between_columns(self, world):
        todo = send(world.full, "post", self.GROUPS, {"name": "To do"}).json()
        done = send(world.full, "post", self.GROUPS, {"name": "Done"}).json()
        assert (todo["position"], done["position"]) == (1, 2)
        r = send(
            world.full,
            "post",
            self.URL,
            {"title": " Reels about X ", "tags": ["video", "video"], "group_id": todo["id"]},
        )
        assert r.status_code == 201, r.content
        idea = r.json()
        assert idea["title"] == "Reels about X" and idea["tags"] == ["video"] and idea["group_id"] == todo["id"]
        assert Tag.objects.filter(workspace=world.workspace, name="video").exists()
        moved = send(
            world.full, "patch", f"{self.URL}{idea['id']}", {"group_id": done["id"], "position": 3, "title": "Renamed"}
        )
        assert moved.status_code == 200 and moved.json()["group_id"] == done["id"] and moved.json()["position"] == 3
        assert world.full.get(f"{self.URL}{idea['id']}").json()["title"] == "Renamed"
        groups = {g["name"]: g["idea_count"] for g in world.full.get(self.GROUPS).json()["groups"]}
        assert groups == {"To do": 0, "Done": 1}
        assert world.full.delete(f"{self.URL}{idea['id']}").json() == {"deleted": True, "id": idea["id"]}
        assert not Idea.objects.exists()

    def test_defaults_to_the_first_column(self, world):
        first = IdeaGroup.objects.create(workspace=world.workspace, name="Inbox", position=0)
        IdeaGroup.objects.create(workspace=world.workspace, name="Later", position=1)
        assert send(world.full, "post", self.URL, {"title": "x"}).json()["group_id"] == str(first.id)

    def test_group_delete_needs_an_empty_column(self, world):
        group = IdeaGroup.objects.create(workspace=world.workspace, name="Busy")
        Idea.objects.create(workspace=world.workspace, title="x", group=group)
        assert world.full.delete(f"{self.GROUPS}{group.id}").status_code == 409
        Idea.objects.all().delete()
        assert world.full.delete(f"{self.GROUPS}{group.id}").status_code == 200

    def test_validation(self, world):
        assert send(world.full, "post", self.URL, {"title": ""}).status_code == 422
        assert send(world.full, "post", self.URL, {"title": "  "}).status_code == 422
        assert send(world.full, "post", self.URL, {"title": "x", "group_id": NO_UUID}).status_code == 404
        assert send(world.full, "post", self.URL, {"title": "x", "tags": ["y" * 101]}).status_code == 422
        assert send(world.full, "post", self.GROUPS, {"name": ""}).status_code == 422
        assert world.full.get(self.URL + "?cursor=bad").status_code == 422

    def test_workspace_isolation(self, world):
        theirs = Idea.objects.create(workspace=world.other_workspace, title="theirs")
        group = IdeaGroup.objects.create(workspace=world.other_workspace, name="theirs")
        assert world.full.get(self.URL).json()["ideas"] == []
        assert world.full.get(f"{self.URL}{theirs.id}").status_code == 404
        assert send(world.full, "patch", f"{self.URL}{theirs.id}", {"title": "mine"}).status_code == 404
        assert world.full.delete(f"{self.URL}{theirs.id}").status_code == 404
        assert world.full.delete(f"{self.GROUPS}{group.id}").status_code == 404
        assert send(world.full, "post", self.URL, {"title": "x", "group_id": str(group.id)}).status_code == 404
        assert Idea.objects.filter(id=theirs.id, title="theirs").exists()

    def test_filter_and_cursor_pagination(self, world):
        group = IdeaGroup.objects.create(workspace=world.workspace, name="G")
        for i in range(5):
            Idea.objects.create(
                workspace=world.workspace, title=f"idea {i}", tags=["t"] if i % 2 == 0 else [], group=group, position=i
            )
        assert len(world.full.get(self.URL + "?tag=t").json()["ideas"]) == 3
        assert len(world.full.get(self.URL + "?search=idea 4").json()["ideas"]) == 1
        assert len(world.full.get(self.URL + f"?group_id={group.id}").json()["ideas"]) == 5
        seen, cursor = [], None
        while True:
            body = world.full.get(self.URL + "?limit=2" + (f"&cursor={cursor}" if cursor else "")).json()
            seen += [i["title"] for i in body["ideas"]]
            cursor = body["next_cursor"]
            if not cursor:
                break
        assert seen == [f"idea {i}" for i in range(5)]


@pytest.mark.django_db
class TestTemplates:
    URL = "/api/v1/templates/"

    def test_requires_a_key_and_create_posts_for_writes(self, world):
        assert world.anonymous.get(self.URL).status_code == 401
        assert send(world.bare, "post", self.URL, {"name": "x"}).status_code == 403
        assert world.bare.get(self.URL).status_code == 200  # reads are open, like the composer's picker
        assert not PostTemplate.objects.exists()

    def test_crud(self, world):
        category = ContentCategory.objects.create(workspace=world.workspace, name="Promo")
        asset = MediaAsset.objects.create(
            organization=world.workspace.organization, workspace=world.workspace, filename="a.png", media_type="image"
        )
        r = send(
            world.full,
            "post",
            self.URL,
            {
                "name": "Weekly",
                "caption": "Hello {{name}}",
                "tags": ["weekly"],
                "category_id": str(category.id),
                "platform_account_ids": [str(world.account1.id)],
                "media_asset_ids": [str(asset.id)],
            },
        )
        assert r.status_code == 201, r.content
        tpl = r.json()
        assert tpl["caption"] == "Hello {{name}}" and tpl["platform_account_ids"] == [str(world.account1.id)]
        assert tpl["media_asset_ids"] == [str(asset.id)] and tpl["category_id"] == str(category.id)
        upd = send(world.full, "patch", f"{self.URL}{tpl['id']}", {"caption": "Changed", "name": "Weekly 2"})
        assert upd.status_code == 200 and upd.json()["caption"] == "Changed" and upd.json()["tags"] == ["weekly"]
        assert world.full.get(f"{self.URL}{tpl['id']}").json()["name"] == "Weekly 2"
        assert world.full.delete(f"{self.URL}{tpl['id']}").json()["deleted"] is True
        assert world.full.get(f"{self.URL}{tpl['id']}").status_code == 404

    def test_allowlist_filters_reads_and_refuses_writes(self, world):
        tpl = PostTemplate.objects.create(
            workspace=world.workspace,
            name="Both",
            template_data={"platform_account_ids": [str(world.account1.id), str(world.account2.id)]},
        )
        assert world.full.get(f"{self.URL}{tpl.id}").json()["platform_account_ids"] == [
            str(world.account1.id),
            str(world.account2.id),
        ]
        assert world.narrow.get(f"{self.URL}{tpl.id}").json()["platform_account_ids"] == [str(world.account1.id)]
        denied = send(world.narrow, "post", self.URL, {"name": "x", "platform_account_ids": [str(world.account2.id)]})
        assert denied.status_code == 403
        foreign = send(
            world.full, "post", self.URL, {"name": "x", "platform_account_ids": [str(world.other_account.id)]}
        )
        assert foreign.status_code == 403
        # A narrower key replacing its accounts must not erase the ones it can't see.
        send(world.narrow, "patch", f"{self.URL}{tpl.id}", {"platform_account_ids": []})
        tpl.refresh_from_db()
        assert tpl.template_data["platform_account_ids"] == [str(world.account2.id)]

    def test_validation_and_isolation(self, world):
        theirs = PostTemplate.objects.create(workspace=world.other_workspace, name="theirs")
        other_category = ContentCategory.objects.create(workspace=world.other_workspace, name="x")
        other_asset = MediaAsset.objects.create(
            organization=world.other_workspace.organization,
            workspace=world.other_workspace,
            filename="b.png",
            media_type="image",
        )
        assert send(world.full, "post", self.URL, {"name": ""}).status_code == 422
        assert (
            send(world.full, "post", self.URL, {"name": "x", "category_id": str(other_category.id)}).status_code == 422
        )
        assert (
            send(world.full, "post", self.URL, {"name": "x", "media_asset_ids": [str(other_asset.id)]}).status_code
            == 422
        )
        assert world.full.get(f"{self.URL}{theirs.id}").status_code == 404
        assert send(world.full, "patch", f"{self.URL}{theirs.id}", {"name": "mine"}).status_code == 404
        assert world.full.delete(f"{self.URL}{theirs.id}").status_code == 404
        assert world.full.get(self.URL + "?cursor=bad").status_code == 422
        assert world.full.get(self.URL).json()["templates"] == []

    def test_search_and_pagination(self, world):
        for name in ["Alpha", "Bravo", "Charlie"]:
            PostTemplate.objects.create(workspace=world.workspace, name=name)
        assert [t["name"] for t in world.full.get(self.URL + "?search=brav").json()["templates"]] == ["Bravo"]
        page = world.full.get(self.URL + "?limit=2").json()
        assert [t["name"] for t in page["templates"]] == ["Alpha", "Bravo"] and page["next_cursor"]
        rest = world.full.get(self.URL + f"?limit=2&cursor={page['next_cursor']}").json()
        assert [t["name"] for t in rest["templates"]] == ["Charlie"] and rest["next_cursor"] is None


@pytest.mark.django_db
class TestTags:
    URL = "/api/v1/tags/"

    def test_requires_a_key_and_create_posts_for_writes(self, world):
        assert world.anonymous.get(self.URL).status_code == 401
        assert send(world.bare, "post", self.URL, {"name": "x"}).status_code == 403
        tag = Tag.objects.create(workspace=world.workspace, name="keep")
        assert send(world.bare, "patch", f"{self.URL}{tag.id}", {"name": "y"}).status_code == 403
        assert world.bare.delete(f"{self.URL}{tag.id}").status_code == 403
        assert Tag.objects.filter(id=tag.id, name="keep").exists()

    def test_create_is_idempotent_and_validated(self, world):
        first = send(world.full, "post", self.URL, {"name": " launch "})
        again = send(world.full, "post", self.URL, {"name": "launch"})
        assert first.status_code == again.status_code == 201
        assert first.json()["id"] == again.json()["id"] and first.json()["name"] == "launch"
        assert send(world.full, "post", self.URL, {"name": ""}).status_code == 422
        assert send(world.full, "post", self.URL, {"name": "   "}).status_code == 422
        assert send(world.full, "post", self.URL, {"name": "x" * 101}).status_code == 422

    def test_rename_and_delete_cascade_to_posts_and_ideas(self, world):
        tag = Tag.objects.create(workspace=world.workspace, name="old")
        post = make_post(world.workspace, world.owner, [world.account1], tags=["old", "keep"])
        idea = Idea.objects.create(workspace=world.workspace, title="i", tags=["old"])
        r = send(world.full, "patch", f"{self.URL}{tag.id}", {"name": "new"})
        assert r.status_code == 200 and r.json()["name"] == "new"
        post.refresh_from_db()
        idea.refresh_from_db()
        assert post.tags == ["new", "keep"] and idea.tags == ["new"]
        assert world.full.delete(f"{self.URL}{tag.id}").status_code == 200
        post.refresh_from_db()
        idea.refresh_from_db()
        assert post.tags == ["keep"] and idea.tags == []
        assert not Tag.objects.filter(id=tag.id).exists()

    def test_rename_conflict(self, world):
        a = Tag.objects.create(workspace=world.workspace, name="a")
        Tag.objects.create(workspace=world.workspace, name="b")
        assert send(world.full, "patch", f"{self.URL}{a.id}", {"name": "b"}).status_code == 409

    def test_isolation_search_and_pagination(self, world):
        theirs = Tag.objects.create(workspace=world.other_workspace, name="theirs")
        for name in ["aa", "ab", "ba"]:
            Tag.objects.create(workspace=world.workspace, name=name)
        assert send(world.full, "patch", f"{self.URL}{theirs.id}", {"name": "mine"}).status_code == 404
        assert world.full.delete(f"{self.URL}{theirs.id}").status_code == 404
        assert [t["name"] for t in world.full.get(self.URL + "?search=a").json()["tags"]] == ["aa", "ab", "ba"]
        assert [t["name"] for t in world.full.get(self.URL + "?search=b").json()["tags"]] == ["ab", "ba"]
        page = world.full.get(self.URL + "?limit=2").json()
        assert len(page["tags"]) == 2 and page["next_cursor"]
        assert len(world.full.get(self.URL + f"?limit=2&cursor={page['next_cursor']}").json()["tags"]) == 1


@pytest.mark.django_db
class TestCategories:
    URL = "/api/v1/categories/"

    def test_requires_a_key_and_create_posts_for_writes(self, world):
        assert world.anonymous.get(self.URL).status_code == 401
        assert send(world.bare, "post", self.URL, {"name": "x"}).status_code == 403
        category = ContentCategory.objects.create(workspace=world.workspace, name="keep")
        assert send(world.bare, "patch", f"{self.URL}{category.id}", {"name": "y"}).status_code == 403
        assert world.bare.delete(f"{self.URL}{category.id}").status_code == 403
        assert world.bare.get(self.URL).status_code == 200

    def test_crud_and_post_uses_it(self, world):
        r = send(world.full, "post", self.URL, {"name": "Educational", "color": "#112233"})
        assert r.status_code == 201, r.content
        category = r.json()
        assert category["color"] == "#112233" and category["position"] == 1
        upd = send(world.full, "patch", f"{self.URL}{category['id']}", {"name": "Edu", "position": 5})
        assert upd.status_code == 200 and upd.json()["name"] == "Edu" and upd.json()["position"] == 5
        post = send(
            world.full,
            "post",
            "/api/v1/posts/",
            {"social_account_id": str(world.account1.id), "caption": "c", "category_id": category["id"]},
        )
        assert post.json()["category_id"] == category["id"]
        assert world.full.delete(f"{self.URL}{category['id']}").status_code == 200
        from apps.composer.models import Post

        assert Post.objects.get(id=post.json()["id"]).category_id is None

    def test_validation_conflicts_and_isolation(self, world):
        send(world.full, "post", self.URL, {"name": "Dup"})
        assert send(world.full, "post", self.URL, {"name": "Dup"}).status_code == 409
        assert send(world.full, "post", self.URL, {"name": "x", "color": "red"}).status_code == 422
        assert send(world.full, "post", self.URL, {"name": ""}).status_code == 422
        other = ContentCategory.objects.create(workspace=world.workspace, name="Other")
        assert send(world.full, "patch", f"{self.URL}{other.id}", {"name": "Dup"}).status_code == 409
        assert send(world.full, "patch", f"{self.URL}{other.id}", {"color": "nope"}).status_code == 422
        theirs = ContentCategory.objects.create(workspace=world.other_workspace, name="theirs")
        assert send(world.full, "patch", f"{self.URL}{theirs.id}", {"name": "mine"}).status_code == 404
        assert world.full.delete(f"{self.URL}{theirs.id}").status_code == 404
        assert "theirs" not in [c["name"] for c in world.full.get(self.URL).json()["categories"]]


@pytest.mark.django_db
class TestCalendarEvents:
    URL = "/api/v1/events/"

    def test_requires_a_key_and_create_posts_for_writes(self, world):
        assert world.anonymous.get(self.URL).status_code == 401
        payload = {"title": "Launch", "start_date": "2026-03-01"}
        assert send(world.bare, "post", self.URL, payload).status_code == 403
        event = CustomCalendarEvent.objects.create(
            workspace=world.workspace, title="keep", start_date="2026-03-01", end_date="2026-03-01"
        )
        assert world.bare.delete(f"{self.URL}{event.id}").status_code == 403
        assert CustomCalendarEvent.objects.filter(id=event.id).exists()

    def test_create_list_and_delete(self, world):
        r = send(
            world.full, "post", self.URL, {"title": "Launch", "start_date": "2026-03-01", "end_date": "2026-03-03"}
        )
        assert r.status_code == 201, r.content
        event = r.json()
        assert event["end_date"] == "2026-03-03" and event["color"] == "#3B82F6"
        single = send(world.full, "post", self.URL, {"title": "Day", "start_date": "2026-05-05"}).json()
        assert single["end_date"] == "2026-05-05"
        assert [e["title"] for e in world.full.get(self.URL).json()["events"]] == ["Launch", "Day"]
        assert world.full.delete(f"{self.URL}{event['id']}").json()["deleted"] is True
        assert [e["title"] for e in world.full.get(self.URL).json()["events"]] == ["Day"]

    def test_date_range_selects_overlapping_events(self, world):
        for title, start, end in [
            ("a", "2026-03-01", "2026-03-05"),
            ("b", "2026-03-10", "2026-03-12"),
            ("c", "2026-04-01", "2026-04-01"),
        ]:
            CustomCalendarEvent.objects.create(workspace=world.workspace, title=title, start_date=start, end_date=end)
        names = lambda q: [e["title"] for e in world.full.get(self.URL + q).json()["events"]]  # noqa: E731
        assert names("?date_from=2026-03-04&date_to=2026-03-11") == ["a", "b"]
        assert names("?date_from=2026-03-13") == ["c"]
        assert names("?date_to=2026-03-02") == ["a"]

    def test_validation_and_isolation(self, world):
        assert send(world.full, "post", self.URL, {"title": "", "start_date": "2026-03-01"}).status_code == 422
        assert send(world.full, "post", self.URL, {"title": "x", "start_date": "not-a-date"}).status_code == 422
        backwards = {"title": "x", "start_date": "2026-03-05", "end_date": "2026-03-01"}
        assert send(world.full, "post", self.URL, backwards).status_code == 422
        assert (
            send(world.full, "post", self.URL, {"title": "x", "start_date": "2026-03-01", "color": "x"}).status_code
            == 422
        )
        assert world.full.get(self.URL + "?date_from=2026-03-05&date_to=2026-03-01").status_code == 422
        theirs = CustomCalendarEvent.objects.create(
            workspace=world.other_workspace, title="theirs", start_date="2026-03-01", end_date="2026-03-01"
        )
        assert world.full.get(self.URL).json()["events"] == []
        assert world.full.delete(f"{self.URL}{theirs.id}").status_code == 404

    def test_pagination(self, world):
        for i in range(3):
            CustomCalendarEvent.objects.create(
                workspace=world.workspace, title=f"e{i}", start_date=f"2026-03-0{i + 1}", end_date=f"2026-03-0{i + 1}"
            )
        page = world.full.get(self.URL + "?limit=2").json()
        assert [e["title"] for e in page["events"]] == ["e0", "e1"] and page["next_cursor"]
        rest = world.full.get(self.URL + f"?limit=2&cursor={page['next_cursor']}").json()
        assert [e["title"] for e in rest["events"]] == ["e2"] and rest["next_cursor"] is None
        assert world.full.get(self.URL + "?cursor=!!").status_code == 422


@pytest.mark.django_db
class TestBrandVoice:
    URL = "/api/v1/brand-voice/"

    def test_requires_a_key(self, world):
        assert world.anonymous.get(self.URL).status_code == 401
        assert send(world.anonymous, "patch", self.URL, {"tone": "x"}).status_code == 401

    def test_read_needs_a_content_or_settings_permission(self, world):
        assert world.bare.get(self.URL).status_code == 403
        assert world.full.get(self.URL).json() == {"tone": "", "words_to_use": "", "words_to_avoid": "", "examples": []}

    def test_update_needs_manage_workspace_settings(self, world):
        from apps.members.models import CustomRole, WorkspaceMembership

        assert send(world.bare, "patch", self.URL, {"tone": "x"}).status_code == 403
        role = CustomRole.objects.create(
            organization=world.workspace.organization, name="Writer", permissions={"create_posts": True}
        )
        WorkspaceMembership.objects.filter(user=world.owner, workspace=world.workspace).update(custom_role=role)
        # The key lists manage_workspace_settings but its issuer lost it.
        assert send(world.full, "patch", self.URL, {"tone": "x"}).status_code == 403
        assert world.full.get(self.URL).status_code == 200
        world.workspace.refresh_from_db()
        assert world.workspace.brand_voice_tone == ""

    def test_partial_update(self, world):
        r = send(
            world.full,
            "patch",
            self.URL,
            {"tone": " Warm and plain ", "words_to_avoid": "synergy", "examples": ["One", "", "Two"]},
        )
        assert r.status_code == 200, r.content
        assert r.json() == {
            "tone": "Warm and plain",
            "words_to_use": "",
            "words_to_avoid": "synergy",
            "examples": ["One", "Two"],
        }
        again = send(world.full, "patch", self.URL, {"words_to_use": "friendly"}).json()
        assert (
            again["tone"] == "Warm and plain"
            and again["words_to_use"] == "friendly"
            and again["examples"] == ["One", "Two"]
        )
        assert send(world.full, "patch", self.URL, {"examples": []}).json()["examples"] == []

    def test_validation(self, world):
        assert send(world.full, "patch", self.URL, {"tone": "x" * 1001}).status_code == 422
        assert send(world.full, "patch", self.URL, {"examples": ["a", "b", "c", "d"]}).status_code == 422
        assert send(world.full, "patch", self.URL, {"examples": ["x" * 2001]}).status_code == 422
        world.workspace.refresh_from_db()
        assert world.workspace.brand_voice_tone == ""

    def test_is_scoped_to_the_keys_workspace(self, world):
        send(world.full, "patch", self.URL, {"tone": "Alpha tone"})
        assert world.other.get(self.URL).json()["tone"] == ""
        world.other_workspace.refresh_from_db()
        assert world.other_workspace.brand_voice_tone == ""


@pytest.mark.django_db
class TestUsage:
    URL = "/api/v1/usage/"

    def test_requires_a_key(self, world):
        assert world.anonymous.get(self.URL).status_code == 401

    def test_self_hosted_reports_no_limits(self, world, settings):
        settings.BILLING_ENABLED = False
        body = world.bare.get(self.URL).json()
        assert body["billing_enabled"] is False and body["plan_slug"] is None
        keys = {m["key"] for m in body["usage"]}
        assert keys == {"channels", "workspaces", "posts", "api_calls", "ai_requests"}
        assert all(m["limit"] is None and m["at_limit"] is False for m in body["usage"])
        assert {m["key"]: m["used"] for m in body["usage"]}["channels"] == 2

    def test_billing_enabled_reports_the_plan_and_limits(self, world, settings):
        settings.BILLING_ENABLED = True
        body = world.full.get(self.URL).json()
        assert body["billing_enabled"] is True and body["plan_slug"] and body["plan_name"]
        channels = next(m for m in body["usage"] if m["key"] == "channels")
        assert channels["used"] == 2 and channels["limit"] is not None

    def test_is_read_only(self, world):
        assert send(world.full, "post", self.URL, {}).status_code == 405
        assert send(world.full, "patch", self.URL, {}).status_code == 405
