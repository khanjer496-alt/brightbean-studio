"""``/api/v1/queues/*`` and ``/api/v1/slots/*``."""

from __future__ import annotations

import datetime as dt

import pytest
from django.utils import timezone

from apps.api.tests.world import make_post
from apps.calendar.models import PostingSlot, Queue, QueueEntry
from apps.composer.models import PlatformPost

QUEUES = "/api/v1/queues/"
SLOTS = "/api/v1/slots/"


def json_body(client, method, url, payload=None):
    return getattr(client, method)(url, data=payload or {}, content_type="application/json")


@pytest.fixture
def queue(world):
    """A queue on account 1 whose account posts every day at 10:00 and 15:00."""
    for day in range(7):
        for hour in (10, 15):
            PostingSlot.objects.create(social_account=world.account1, day_of_week=day, time=dt.time(hour, 0))
    return Queue.objects.create(workspace=world.workspace, name="Main", social_account=world.account1)


@pytest.fixture
def queue2(world):
    for day in range(7):
        PostingSlot.objects.create(social_account=world.account2, day_of_week=day, time=dt.time(9, 0))
    return Queue.objects.create(workspace=world.workspace, name="Second", social_account=world.account2)


@pytest.mark.django_db
class TestQueues:
    def test_requires_a_key(self, world, queue):
        assert world.anonymous.get(QUEUES).status_code == 401
        assert world.anonymous.get(f"{QUEUES}{queue.id}").status_code == 401
        assert json_body(world.anonymous, "post", f"{QUEUES}{queue.id}/entries", {}).status_code == 401

    def test_lists_with_next_free_slot_and_respects_allowlist(self, world, queue, queue2):
        body = world.full.get(QUEUES).json()
        assert [q["name"] for q in body["queues"]] == ["Main", "Second"]
        assert body["queues"][0]["next_free_slot"] is not None
        assert body["queues"][0]["entry_count"] == 0
        assert [q["name"] for q in world.narrow.get(QUEUES).json()["queues"]] == ["Main"]

    def test_workspace_isolation(self, world, queue):
        other = Queue.objects.create(workspace=world.other_workspace, name="Theirs", social_account=world.other_account)
        assert world.full.get(f"{QUEUES}{other.id}").status_code == 404
        assert "Theirs" not in [q["name"] for q in world.full.get(QUEUES).json()["queues"]]

    def test_out_of_allowlist_queue_is_404(self, world, queue2):
        assert world.narrow.get(f"{QUEUES}{queue2.id}").status_code == 404

    def test_add_post_takes_the_next_free_slot_then_the_one_after(self, world, queue):
        first = make_post(world.workspace, world.owner, [world.account1])
        second = make_post(world.workspace, world.owner, [world.account1])
        r1 = json_body(world.full, "post", f"{QUEUES}{queue.id}/entries", {"post_id": str(first.id)})
        assert r1.status_code == 201, r1.content
        r2 = json_body(world.full, "post", f"{QUEUES}{queue.id}/entries", {"post_id": str(second.id)})
        assert r2.status_code == 201, r2.content
        t1, t2 = r1.json()["assigned_slot_datetime"], r2.json()["assigned_slot_datetime"]
        assert t1 < t2
        assert PlatformPost.objects.get(post=first).status == "scheduled"
        assert PlatformPost.objects.get(post=first).scheduled_at.isoformat().replace("+00:00", "Z") == t1
        detail = world.full.get(f"{QUEUES}{queue.id}").json()
        assert detail["entry_count"] == 2 and len(detail["entries"]) == 2
        assert detail["next_free_slot"] > t2

    def test_priority_takes_the_earliest_slot(self, world, queue):
        first = make_post(world.workspace, world.owner, [world.account1])
        urgent = make_post(world.workspace, world.owner, [world.account1])
        t1 = json_body(world.full, "post", f"{QUEUES}{queue.id}/entries", {"post_id": str(first.id)}).json()
        t2 = json_body(
            world.full, "post", f"{QUEUES}{queue.id}/entries", {"post_id": str(urgent.id), "priority": True}
        ).json()
        assert t2["assigned_slot_datetime"] == t1["assigned_slot_datetime"]
        assert PlatformPost.objects.get(post=first).scheduled_at > PlatformPost.objects.get(post=urgent).scheduled_at

    def test_adding_needs_create_posts_and_publish_directly(self, world, queue):
        post = make_post(world.workspace, world.owner, [world.account1])
        assert (
            json_body(world.bare, "post", f"{QUEUES}{queue.id}/entries", {"post_id": str(post.id)}).status_code == 403
        )
        assert PlatformPost.objects.get(post=post).status == "draft"

    def test_post_must_target_the_queue_account_and_be_visible(self, world, queue):
        wrong = make_post(world.workspace, world.owner, [world.account2])
        foreign = make_post(world.other_workspace, world.other_owner, [world.other_account])
        multi = make_post(world.workspace, world.owner, [world.account1, world.account2])
        assert (
            json_body(world.full, "post", f"{QUEUES}{queue.id}/entries", {"post_id": str(wrong.id)}).status_code == 422
        )
        assert (
            json_body(world.full, "post", f"{QUEUES}{queue.id}/entries", {"post_id": str(foreign.id)}).status_code
            == 404
        )
        # A key limited to account 1 can't see a post that also targets account 2.
        assert (
            json_body(world.narrow, "post", f"{QUEUES}{queue.id}/entries", {"post_id": str(multi.id)}).status_code
            == 404
        )

    def test_published_posts_cannot_be_queued(self, world, queue):
        post = make_post(world.workspace, world.owner, [world.account1], status="published")
        assert (
            json_body(world.full, "post", f"{QUEUES}{queue.id}/entries", {"post_id": str(post.id)}).status_code == 409
        )

    def test_inactive_queue_and_validation(self, world, queue):
        post = make_post(world.workspace, world.owner, [world.account1])
        Queue.objects.filter(id=queue.id).update(is_active=False)
        assert (
            json_body(world.full, "post", f"{QUEUES}{queue.id}/entries", {"post_id": str(post.id)}).status_code == 422
        )
        assert json_body(world.full, "post", f"{QUEUES}{queue.id}/entries", {}).status_code == 422

    def test_full_queue_is_409(self, world):
        queue = Queue.objects.create(workspace=world.workspace, name="Empty", social_account=world.account1)
        post = make_post(world.workspace, world.owner, [world.account1])
        r = json_body(world.full, "post", f"{QUEUES}{queue.id}/entries", {"post_id": str(post.id)})
        assert r.status_code == 409
        assert PlatformPost.objects.get(post=post).status == "draft"

    def test_remove_entry_returns_the_post_to_draft(self, world, queue):
        post = make_post(world.workspace, world.owner, [world.account1])
        entry = json_body(world.full, "post", f"{QUEUES}{queue.id}/entries", {"post_id": str(post.id)}).json()
        assert world.bare.delete(f"{QUEUES}{queue.id}/entries/{entry['id']}").status_code == 403
        r = world.full.delete(f"{QUEUES}{queue.id}/entries/{entry['id']}")
        assert r.status_code == 200 and r.json()["deleted"] is True
        assert not QueueEntry.objects.filter(queue=queue).exists()
        assert PlatformPost.objects.get(post=post).status == "draft"
        assert world.full.delete(f"{QUEUES}{queue.id}/entries/{entry['id']}").status_code == 404


@pytest.mark.django_db
class TestSlots:
    def test_requires_a_key(self, world):
        assert world.anonymous.get(SLOTS).status_code == 401
        assert world.anonymous.get(f"{SLOTS}next-free?social_account_id={world.account1.id}").status_code == 401

    def test_list_filters_to_the_allowlist(self, world, queue, queue2):
        everything = world.full.get(SLOTS).json()["slots"]
        assert len(everything) == 14 + 7
        narrow = world.narrow.get(SLOTS).json()["slots"]
        assert {s["social_account_id"] for s in narrow} == {str(world.account1.id)}
        assert world.narrow.get(SLOTS + f"?social_account_id={world.account2.id}").status_code == 403
        only = world.full.get(SLOTS + f"?social_account_id={world.account2.id}").json()["slots"]
        assert len(only) == 7 and only[0]["time"] == "09:00"

    def test_create_update_delete(self, world):
        payload = {"social_account_id": str(world.account1.id), "day_of_week": 2, "time": "09:30"}
        r = json_body(world.full, "post", SLOTS, payload)
        assert r.status_code == 201, r.content
        slot = r.json()
        assert slot["day_name"] == "Wednesday" and slot["time"] == "09:30" and slot["is_active"] is True
        assert json_body(world.full, "post", SLOTS, payload).status_code == 409
        upd = json_body(world.full, "patch", f"{SLOTS}{slot['id']}", {"time": "11:15", "is_active": False})
        assert upd.status_code == 200 and upd.json()["time"] == "11:15" and upd.json()["is_active"] is False
        other = json_body(world.full, "post", SLOTS, {**payload, "time": "12:00"}).json()
        assert json_body(world.full, "patch", f"{SLOTS}{other['id']}", {"time": "11:15"}).status_code == 409
        assert world.full.delete(f"{SLOTS}{slot['id']}").json()["deleted"] is True
        assert not PostingSlot.objects.filter(id=slot["id"]).exists()

    def test_writes_need_manage_social_accounts(self, world, queue):
        slot = PostingSlot.objects.filter(social_account=world.account1).first()
        payload = {"social_account_id": str(world.account1.id), "day_of_week": 1, "time": "08:00"}
        assert json_body(world.bare, "post", SLOTS, payload).status_code == 403
        assert json_body(world.bare, "patch", f"{SLOTS}{slot.id}", {"is_active": False}).status_code == 403
        assert world.bare.delete(f"{SLOTS}{slot.id}").status_code == 403
        assert PostingSlot.objects.filter(id=slot.id, is_active=True).exists()

    def test_allowlist_and_isolation_on_writes(self, world, queue2):
        theirs = PostingSlot.objects.create(social_account=world.other_account, day_of_week=0, time=dt.time(7, 0))
        outside = PostingSlot.objects.filter(social_account=world.account2).first()
        denied = {"social_account_id": str(world.account2.id), "day_of_week": 1, "time": "08:00"}
        assert json_body(world.narrow, "post", SLOTS, denied).status_code == 403
        foreign = {**denied, "social_account_id": str(world.other_account.id)}
        assert json_body(world.full, "post", SLOTS, foreign).status_code == 403
        assert json_body(world.narrow, "patch", f"{SLOTS}{outside.id}", {"is_active": False}).status_code == 404
        assert world.full.delete(f"{SLOTS}{theirs.id}").status_code == 404
        assert PostingSlot.objects.filter(id__in=[outside.id, theirs.id]).count() == 2

    def test_validation(self, world):
        base = {"social_account_id": str(world.account1.id), "day_of_week": 1, "time": "08:00"}
        assert json_body(world.full, "post", SLOTS, {**base, "day_of_week": 7}).status_code == 422
        assert json_body(world.full, "post", SLOTS, {**base, "time": "late"}).status_code == 422
        assert json_body(world.full, "post", SLOTS, {"day_of_week": 1}).status_code == 422

    def test_next_free_skips_slots_that_scheduled_posts_hold(self, world, queue):
        url = f"{SLOTS}next-free?social_account_id={world.account1.id}&count=3"
        before = world.full.get(url)
        assert before.status_code == 200, before.content
        slots = before.json()["slots"]
        assert len(slots) == 3 and slots == sorted(slots)
        assert before.json()["timezone"]
        held = make_post(world.workspace, world.owner, [world.account1], status="scheduled")
        taken = dt.datetime.fromisoformat(slots[0].replace("Z", "+00:00"))
        PlatformPost.objects.filter(post=held).update(scheduled_at=taken)
        after = world.full.get(url).json()["slots"]
        assert slots[0] not in after and after[0] == slots[1]

    def test_next_free_scope_and_validation(self, world, queue):
        assert world.narrow.get(f"{SLOTS}next-free?social_account_id={world.account2.id}").status_code == 403
        assert world.full.get(f"{SLOTS}next-free?social_account_id={world.other_account.id}").status_code == 403
        assert world.full.get(f"{SLOTS}next-free?social_account_id={world.account1.id}&count=21").status_code == 422
        assert world.full.get(f"{SLOTS}next-free").status_code == 422
        empty = world.full.get(f"{SLOTS}next-free?social_account_id={world.account2.id}")
        assert empty.status_code == 200 and empty.json()["slots"] == []

    def test_next_free_is_in_the_future(self, world, queue):
        slots = world.full.get(f"{SLOTS}next-free?social_account_id={world.account1.id}").json()["slots"]
        assert dt.datetime.fromisoformat(slots[0].replace("Z", "+00:00")) > timezone.now()
