"""``GET /posts/``, ``DELETE /posts/{id}``, ``POST /posts/{id}/duplicate`` and tags/category on posts."""

from __future__ import annotations

from datetime import timedelta
from urllib.parse import urlencode

import pytest
from django.utils import timezone

from apps.api.tests.world import make_post
from apps.composer.models import ContentCategory, Post, Tag

URL = "/api/v1/posts/"


def ids(response):
    return [p["id"] for p in response.json()["posts"]]


@pytest.mark.django_db
class TestListPosts:
    def test_requires_a_key(self, world):
        assert world.anonymous.get(URL).status_code == 401

    def test_lists_newest_first_with_tags_and_category(self, world):
        older = make_post(world.workspace, world.owner, [world.account1], caption="old", tags=["a"])
        newer = make_post(world.workspace, world.owner, [world.account1], caption="new", tags=["b"])
        r = world.full.get(URL)
        assert r.status_code == 200, r.content
        body = r.json()
        assert ids(r) == [str(newer.id), str(older.id)]
        assert body["limit"] == 50 and body["next_cursor"] is None
        assert body["posts"][0]["tags"] == ["b"]
        assert body["posts"][0]["category_id"] is None

    def test_other_workspace_posts_are_invisible(self, world):
        make_post(world.other_workspace, world.other_owner, [world.other_account])
        assert world.full.get(URL).json()["posts"] == []

    def test_allowlist_hides_posts_with_a_target_outside_it(self, world):
        mine = make_post(world.workspace, world.owner, [world.account1])
        make_post(world.workspace, world.owner, [world.account2])
        make_post(world.workspace, world.owner, [world.account1, world.account2])
        assert ids(world.narrow.get(URL)) == [str(mine.id)]
        assert len(ids(world.full.get(URL))) == 3

    def test_filters_by_status_and_account_on_the_same_target(self, world):
        draft1 = make_post(world.workspace, world.owner, [world.account1], status="draft")
        make_post(world.workspace, world.owner, [world.account2], status="draft")
        make_post(world.workspace, world.owner, [world.account1], status="scheduled")
        r = world.full.get(URL + "?" + urlencode({"status": "draft", "social_account_id": str(world.account1.id)}))
        assert ids(r) == [str(draft1.id)]

    def test_account_filter_outside_allowlist_is_403(self, world):
        r = world.narrow.get(URL + "?" + urlencode({"social_account_id": str(world.account2.id)}))
        assert r.status_code == 403
        assert r.json()["error"] == "forbidden"

    def test_filters_by_date_range_using_schedule_or_creation(self, world):
        now = timezone.now()
        soon = make_post(
            world.workspace, world.owner, [world.account1], status="scheduled", scheduled_at=now + timedelta(days=2)
        )
        far = make_post(
            world.workspace, world.owner, [world.account1], status="scheduled", scheduled_at=now + timedelta(days=30)
        )
        draft = make_post(world.workspace, world.owner, [world.account1])
        window = {"date_from": (now + timedelta(days=1)).isoformat(), "date_to": (now + timedelta(days=5)).isoformat()}
        assert ids(world.full.get(URL + "?" + urlencode(window))) == [str(soon.id)]
        recent = {
            "date_from": (now - timedelta(minutes=5)).isoformat(),
            "date_to": (now + timedelta(minutes=5)).isoformat(),
        }
        assert ids(world.full.get(URL + "?" + urlencode(recent))) == [str(draft.id)]
        assert str(far.id) in ids(world.full.get(URL))

    def test_filters_by_tag_and_search(self, world):
        tagged = make_post(world.workspace, world.owner, [world.account1], caption="Spring sale", tags=["promo"])
        make_post(world.workspace, world.owner, [world.account1], caption="Other", title="Hello", tags=["misc"])
        assert ids(world.full.get(URL + "?tag=promo")) == [str(tagged.id)]
        assert ids(world.full.get(URL + "?search=spring")) == [str(tagged.id)]
        assert len(ids(world.full.get(URL + "?search=hello"))) == 1

    def test_validation_errors(self, world):
        assert world.full.get(URL + "?status=nope").status_code == 422
        assert world.full.get(URL + "?cursor=garbage").status_code == 422
        assert world.full.get(URL + "?limit=0").status_code == 422
        assert world.full.get(URL + "?limit=101").status_code == 422
        bad = {"date_from": "2026-02-02T00:00:00Z", "date_to": "2026-01-01T00:00:00Z"}
        assert world.full.get(URL + "?" + urlencode(bad)).status_code == 422

    def test_cursor_pagination_walks_every_post_once(self, world):
        made = [make_post(world.workspace, world.owner, [world.account1], caption=str(i)) for i in range(5)]
        seen, cursor = [], None
        for _ in range(5):
            q = {"limit": 2, **({"cursor": cursor} if cursor else {})}
            body = world.full.get(URL + "?" + urlencode(q)).json()
            seen += [p["id"] for p in body["posts"]]
            cursor = body["next_cursor"]
            if cursor is None:
                break
        assert sorted(seen) == sorted(str(p.id) for p in made)
        assert len(seen) == len(set(seen)) == 5

    def test_internal_notes_are_redacted_without_create_posts(self, world):
        post = make_post(world.workspace, world.owner, [world.account1])
        Post.objects.filter(id=post.id).update(internal_notes="secret")
        assert world.full.get(URL).json()["posts"][0]["internal_notes"] == "secret"
        assert world.bare.get(URL).json()["posts"][0]["internal_notes"] == ""


@pytest.mark.django_db
class TestDeletePost:
    def test_requires_a_key(self, world):
        post = make_post(world.workspace, world.owner, [world.account1])
        assert world.anonymous.delete(f"{URL}{post.id}").status_code == 401

    def test_needs_create_posts(self, world):
        post = make_post(world.workspace, world.owner, [world.account1])
        r = world.bare.delete(f"{URL}{post.id}")
        assert r.status_code == 403
        assert Post.objects.filter(id=post.id).exists()

    @pytest.mark.parametrize("status", ["draft", "scheduled", "approved", "failed"])
    def test_deletes_unpublished_posts(self, world, status):
        post = make_post(world.workspace, world.owner, [world.account1], status=status)
        r = world.full.delete(f"{URL}{post.id}")
        assert r.status_code == 200, r.content
        assert r.json() == {"deleted": True, "id": str(post.id)}
        assert not Post.objects.filter(id=post.id).exists()

    @pytest.mark.parametrize("status", ["published", "publishing"])
    def test_published_posts_are_refused(self, world, status):
        post = make_post(world.workspace, world.owner, [world.account1], status=status)
        r = world.full.delete(f"{URL}{post.id}")
        assert r.status_code == 409
        assert Post.objects.filter(id=post.id).exists()

    def test_other_workspace_and_out_of_allowlist_are_404(self, world):
        foreign = make_post(world.other_workspace, world.other_owner, [world.other_account])
        outside = make_post(world.workspace, world.owner, [world.account2])
        assert world.full.delete(f"{URL}{foreign.id}").status_code == 404
        assert world.narrow.delete(f"{URL}{outside.id}").status_code == 404
        assert Post.objects.filter(id__in=[foreign.id, outside.id]).count() == 2

    def test_someone_elses_post_needs_edit_others_posts(self, world):
        from apps.members.models import CustomRole, WorkspaceMembership

        post = make_post(world.workspace, world.other_owner, [world.account1])
        # Strip edit_others_posts from the issuer's role.
        role = CustomRole.objects.create(
            organization=world.workspace.organization,
            name="Writer",
            permissions={"create_posts": True, "publish_directly": True},
        )
        WorkspaceMembership.objects.filter(user=world.owner, workspace=world.workspace).update(custom_role=role)
        r = world.full.delete(f"{URL}{post.id}")
        assert r.status_code == 403
        assert "edit_others_posts" in r.json()["detail"]

    def test_deleting_a_series_start_removes_its_upcoming_copies(self, world):
        from apps.calendar.services import save_recurrence

        start = timezone.now() + timedelta(days=3)
        post = make_post(world.workspace, world.owner, [world.account1], status="scheduled", scheduled_at=start)
        save_recurrence(post, frequency="daily", count=3)
        assert Post.objects.filter(recurrence_source=post).exists()
        assert world.full.delete(f"{URL}{post.id}").status_code == 200
        assert not Post.objects.filter(workspace=world.workspace).exists()

    def test_malformed_id_is_422(self, world):
        assert world.full.delete(f"{URL}not-a-uuid").status_code == 422


@pytest.mark.django_db
class TestDuplicatePost:
    def test_requires_a_key(self, world):
        post = make_post(world.workspace, world.owner, [world.account1])
        assert world.anonymous.post(f"{URL}{post.id}/duplicate").status_code == 401

    def test_needs_create_posts(self, world):
        post = make_post(world.workspace, world.owner, [world.account1])
        assert world.bare.post(f"{URL}{post.id}/duplicate").status_code == 403

    def test_copies_a_published_post_into_a_draft(self, world):
        post = make_post(
            world.workspace, world.owner, [world.account1], status="published", caption="Hello", title="T", tags=["x"]
        )
        r = world.full.post(f"{URL}{post.id}/duplicate")
        assert r.status_code == 201, r.content
        body = r.json()
        assert body["id"] != str(post.id)
        assert body["caption"] == "Hello" and body["title"] == "Copy of T"
        assert body["status"] == "draft" and body["scheduled_at"] is None
        assert [p["social_account_id"] for p in body["platform_posts"]] == [str(world.account1.id)]
        assert Post.objects.get(id=post.id).platform_posts.get().status == "published"

    def test_allowlist_and_workspace_isolation(self, world):
        outside = make_post(world.workspace, world.owner, [world.account1, world.account2])
        foreign = make_post(world.other_workspace, world.other_owner, [world.other_account])
        assert world.narrow.post(f"{URL}{outside.id}/duplicate").status_code == 404
        assert world.full.post(f"{URL}{foreign.id}/duplicate").status_code == 404
        assert Post.objects.filter(workspace=world.workspace).count() == 1


@pytest.mark.django_db
class TestPostTagsAndCategory:
    def _create(self, world, **extra):
        payload = {"social_account_id": str(world.account1.id), "caption": "hi", **extra}
        return world.full.post(URL, data=payload, content_type="application/json")

    def test_create_with_tags_and_category(self, world):
        category = ContentCategory.objects.create(workspace=world.workspace, name="Promo")
        r = self._create(world, tags=["launch", "launch", " spring "], category_id=str(category.id))
        assert r.status_code == 201, r.content
        assert r.json()["tags"] == ["launch", "spring"]
        assert r.json()["category_id"] == str(category.id)
        assert set(Tag.objects.filter(workspace=world.workspace).values_list("name", flat=True)) == {"launch", "spring"}

    def test_create_rejects_foreign_category_and_long_tags_without_writing(self, world):
        foreign = ContentCategory.objects.create(workspace=world.other_workspace, name="Theirs")
        assert self._create(world, category_id=str(foreign.id)).status_code == 422
        assert self._create(world, tags=["x" * 101]).status_code == 422
        assert not Post.objects.filter(workspace=world.workspace).exists()

    def test_patch_replaces_tags(self, world):
        post = make_post(world.workspace, world.owner, [world.account1], tags=["old"])
        r = world.full.patch(f"{URL}{post.id}", data={"tags": ["new"]}, content_type="application/json")
        assert r.status_code == 200, r.content
        assert r.json()["tags"] == ["new"]
        assert (
            world.full.patch(
                f"{URL}{post.id}",
                data={"category_id": "00000000-0000-4000-8000-000000000000"},
                content_type="application/json",
            ).status_code
            == 422
        )
