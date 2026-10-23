"""Cursor pagination, the new-message poll, full-text search and the feed's query budget."""

import re
from datetime import timedelta

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from apps.inbox import feed
from apps.inbox.models import InboxMessage

HTMX = {"HTTP_HX_REQUEST": "true"}


def _url(name, workspace, **kwargs):
    return reverse(f"inbox:{name}", kwargs={"workspace_id": workspace.id, **kwargs})


def _row_ids(response):
    return re.findall(r'id="msg-([0-9a-f-]{36})"', response.content.decode())


def _load_more_url(response):
    match = re.search(r'hx-get="([^"]+cursor=[^"]+)"', response.content.decode())
    return match.group(1).replace("&amp;", "&") if match else None


@pytest.fixture(autouse=True)
def small_pages(monkeypatch):
    monkeypatch.setattr(feed, "MESSAGES_PER_PAGE", 10)


@pytest.mark.django_db
class TestPagination:
    def test_pages_cover_every_message_once(self, inbox_client, inbox_workspace, make_messages):
        make_messages(25)
        seen, url = [], _url("feed", inbox_workspace)
        while url:
            response = inbox_client.get(url, **HTMX)
            assert response.status_code == 200
            seen += _row_ids(response)
            url = _load_more_url(response)
        assert len(seen) == 25
        assert len(set(seen)) == 25
        assert seen == [str(m.id) for m in InboxMessage.objects.order_by("-received_at", "-id")]

    def test_new_arrivals_do_not_shift_the_next_page(self, inbox_client, inbox_workspace, inbox_account, make_messages):
        make_messages(15)
        first = inbox_client.get(_url("feed", inbox_workspace), **HTMX)
        next_url = _load_more_url(first)
        # Five messages land after page one was served.
        for i in range(5):
            InboxMessage.objects.create(
                workspace=inbox_workspace,
                social_account=inbox_account,
                platform_message_id=f"late-{i}",
                sender_name="New",
                received_at=timezone.now() + timedelta(minutes=i),
            )
        second = inbox_client.get(next_url, **HTMX)
        assert set(_row_ids(first)).isdisjoint(_row_ids(second))
        assert len(_row_ids(second)) == 5
        assert _load_more_url(second) is None

    def test_equal_timestamps_are_cut_by_id(self, inbox_client, inbox_workspace, inbox_account):
        stamp = timezone.now()
        for i in range(14):
            InboxMessage.objects.create(
                workspace=inbox_workspace,
                social_account=inbox_account,
                platform_message_id=f"same-{i}",
                sender_name="Ada",
                received_at=stamp,
            )
        first = inbox_client.get(_url("feed", inbox_workspace), **HTMX)
        second = inbox_client.get(_load_more_url(first), **HTMX)
        assert len(set(_row_ids(first)) | set(_row_ids(second))) == 14

    def test_filters_are_kept_across_pages(self, inbox_client, inbox_workspace, make_messages):
        make_messages(14, status="open", prefix="open")
        make_messages(14, status="resolved", prefix="done")
        url = _url("feed", inbox_workspace) + "?status=open&status="
        first = inbox_client.get(url, **HTMX)
        second = inbox_client.get(_load_more_url(first), **HTMX)
        ids = _row_ids(first) + _row_ids(second)
        assert len(ids) == 14
        assert set(InboxMessage.objects.filter(id__in=ids).values_list("status", flat=True)) == {"open"}

    def test_bad_cursor_falls_back_to_the_first_page(self, inbox_client, inbox_workspace, make_messages):
        make_messages(3)
        response = inbox_client.get(_url("feed", inbox_workspace) + "?cursor=garbage", **HTMX)
        assert len(_row_ids(response)) == 3

    def test_full_page_still_renders_for_a_plain_request(self, inbox_client, inbox_workspace, make_messages):
        make_messages(3)
        response = inbox_client.get(_url("feed", inbox_workspace))
        assert response.status_code == 200
        assert b'id="inbox-message-list"' in response.content
        assert b'class="inbox-load-more"' not in response.content


@pytest.mark.django_db
class TestPoll:
    def _cursor(self, message):
        return feed.encode_cursor(message)

    def test_counts_only_messages_after_the_cursor(self, inbox_client, inbox_workspace, inbox_account, make_messages):
        make_messages(3)
        newest = InboxMessage.objects.order_by("-received_at").first()
        assert (
            b"new message"
            not in inbox_client.get(_url("poll", inbox_workspace) + f"?since={self._cursor(newest)}").content
        )
        for i in range(2):
            InboxMessage.objects.create(
                workspace=inbox_workspace,
                social_account=inbox_account,
                platform_message_id=f"fresh-{i}",
                sender_name="New",
                received_at=timezone.now() + timedelta(seconds=i),
            )
        response = inbox_client.get(_url("poll", inbox_workspace) + f"?since={self._cursor(newest)}")
        assert b"2 new messages" in response.content

    def test_respects_the_feed_filters(self, inbox_client, inbox_workspace, inbox_account, make_messages):
        make_messages(1)
        newest = InboxMessage.objects.get()
        InboxMessage.objects.create(
            workspace=inbox_workspace,
            social_account=inbox_account,
            platform_message_id="neg",
            sender_name="Mad",
            sentiment="negative",
            received_at=timezone.now(),
        )
        since = self._cursor(newest)
        assert b"1 new message" in inbox_client.get(_url("poll", inbox_workspace) + f"?since={since}").content
        positive = inbox_client.get(_url("poll", inbox_workspace) + f"?since={since}&sentiment=positive")
        assert b"new message" not in positive.content

    def test_is_scoped_to_the_workspace(self, inbox_client, inbox_workspace, other_workspace_message):
        response = inbox_client.get(_url("poll", inbox_workspace))
        assert b"new message" not in response.content

    def test_refuses_non_members_and_roles_without_inbox(self, client, inbox_workspace, inbox_viewer_client, user):
        assert client.get(_url("poll", inbox_workspace)).status_code == 302
        client.force_login(user)
        assert client.get(_url("poll", inbox_workspace)).status_code == 403

    def test_updates_the_sidebar_unread_count(self, inbox_client, inbox_workspace, make_messages):
        make_messages(3, status="unread")
        make_messages(2, status="open", prefix="o")
        response = inbox_client.get(_url("poll", inbox_workspace))
        assert 'id="sidebar-inbox-badge"' in response.content.decode()
        assert 'hx-swap-oob="true"' in response.content.decode()
        assert re.search(r'class="sidebar-badge">3<', response.content.decode())

    def test_empty_list_learns_about_its_first_message(self, inbox_client, inbox_workspace, make_messages):
        make_messages(1)
        assert b"1 new message" in inbox_client.get(_url("poll", inbox_workspace) + "?since=").content

    def test_polling_is_paused_when_the_tab_is_hidden(self, inbox_client, inbox_workspace):
        body = inbox_client.get(_url("feed", inbox_workspace)).content.decode()
        assert "every 20s [!document.hidden]" in body
        assert "data-no-error-toast" in body

    def test_poll_is_cheap(self, inbox_client, inbox_workspace, make_messages):
        url = _url("poll", inbox_workspace)
        inbox_client.get(url)  # warm caches (session, content types)
        with CaptureQueriesContext(connection) as empty:
            inbox_client.get(url)
        make_messages(150, status="unread")
        with CaptureQueriesContext(connection) as full:
            inbox_client.get(url + "?since=")
        # The same two counts and the request's own session/membership lookups,
        # however many messages there are.
        assert len(full) == len(empty)
        assert len(full) <= 15


@pytest.mark.django_db
class TestSearch:
    @pytest.fixture
    def corpus(self, inbox_workspace, inbox_account):
        def create(i, sender, body):
            return InboxMessage.objects.create(
                workspace=inbox_workspace,
                social_account=inbox_account,
                platform_message_id=f"s-{i}",
                sender_name=sender,
                sender_handle=sender.lower().replace(" ", "_"),
                body=body,
                received_at=timezone.now() - timedelta(minutes=i),
            )

        return [
            create(1, "Ada Lovelace", "Where is my refund for the broken engine?"),
            create(2, "Grace Hopper", "Love the new compiler release"),
            create(3, "Alan Turing", "Can I change my delivery address?"),
        ]

    def _search(self, client, workspace, q):
        response = client.get(_url("feed", workspace) + f"?q={q}", **HTMX)
        return response, set(_row_ids(response))

    def test_matches_whole_words_in_the_body(self, inbox_client, inbox_workspace, corpus):
        _response, ids = self._search(inbox_client, inbox_workspace, "refund")
        assert ids == {str(corpus[0].id)}

    def test_matches_the_author_name(self, inbox_client, inbox_workspace, corpus):
        _response, ids = self._search(inbox_client, inbox_workspace, "Hopper")
        assert ids == {str(corpus[1].id)}

    def test_supports_quotes_and_exclusion(self, inbox_client, inbox_workspace, corpus):
        _response, ids = self._search(inbox_client, inbox_workspace, '"delivery address"')
        assert ids == {str(corpus[2].id)}
        _response, ids = self._search(inbox_client, inbox_workspace, "my -refund")
        assert ids == {str(corpus[2].id)}

    def test_partial_words_fall_back_to_a_substring_match(self, inbox_client, inbox_workspace, corpus):
        _response, ids = self._search(inbox_client, inbox_workspace, "compil")
        assert ids == {str(corpus[1].id)}

    def test_punctuation_only_queries_do_not_error(self, inbox_client, inbox_workspace, corpus):
        for q in ("%22", "%26%26", "-", "%27"):
            response, _ids = self._search(inbox_client, inbox_workspace, q)
            assert response.status_code == 200

    def test_highlights_matches_and_escapes_the_rest(self, inbox_client, inbox_workspace, inbox_account):
        InboxMessage.objects.create(
            workspace=inbox_workspace,
            social_account=inbox_account,
            platform_message_id="xss",
            sender_name="Mal",
            body="refund <script>alert(1)</script> please",
            received_at=timezone.now(),
        )
        response, _ids = self._search(inbox_client, inbox_workspace, "refund")
        html = response.content.decode()
        assert '<mark class="inbox-mark">refund</mark>' in html
        assert "<script>alert(1)" not in html
        assert "&lt;script&gt;" in html

    def test_search_is_workspace_scoped(self, inbox_client, inbox_workspace, other_workspace_message):
        _response, ids = self._search(inbox_client, inbox_workspace, "secret")
        assert ids == set()

    def test_the_search_uses_the_gin_index_expression(self, db):
        sql = str(InboxMessage.objects.alias(search=feed.search_vector()).filter(search="x").query)
        assert "to_tsvector(" in sql
        assert any(i.name == "inbox_msg_search_gin" for i in InboxMessage._meta.indexes)

    def test_highlight_filter_snippets_long_text(self):
        from apps.inbox.templatetags.inbox_extras import highlight

        text = "x" * 300 + " refund " + "y" * 300
        out = highlight(text, "refund")
        assert "<mark" in out
        assert len(out) < 400
        assert highlight("plain", "") == "plain"


@pytest.mark.django_db
class TestQueryBudget:
    def _count(self, client, url):
        with CaptureQueriesContext(connection) as ctx:
            response = client.get(url, **HTMX)
        assert response.status_code == 200
        return len(ctx)

    def test_feed_query_count_does_not_grow_with_the_page(
        self, inbox_client, inbox_workspace, make_messages, organization
    ):
        from apps.accounts.models import User

        assignees = [User.objects.create_user(email=f"u{i}@example.com", password="x", name=f"U{i}") for i in range(5)]
        make_messages(3, prefix="few")
        url = _url("feed", inbox_workspace)
        self._count(inbox_client, url)
        few = self._count(inbox_client, url)
        for i, user in enumerate(assignees):
            make_messages(6, prefix=f"many{i}", assigned_to=user)
        many = self._count(inbox_client, url)
        assert many == few

    def test_search_adds_one_query(self, inbox_client, inbox_workspace, make_messages):
        make_messages(30, body="refund please")
        plain, searched = _url("feed", inbox_workspace), _url("feed", inbox_workspace) + "?q=refund"
        self._count(inbox_client, plain)
        assert self._count(inbox_client, searched) == self._count(inbox_client, plain) + 1
