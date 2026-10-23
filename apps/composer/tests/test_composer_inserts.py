"""Composer inserts: hashtag sets, signatures and @mention suggestions.

Everything here is workspace scoped: a member sees their workspace's sets and
signatures and their own personal signatures, and nobody else's.
"""

import json
from unittest.mock import MagicMock, patch

from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.composer.models import HashtagSet, Post, Signature
from apps.composer.services import (
    default_signature,
    mention_suggestions,
    parse_hashtags,
    recent_hashtags,
    with_signature,
)
from apps.members.models import OrgMembership, WorkspaceMembership
from apps.organizations.models import Organization
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace
from providers.types import MentionSuggestion

from .test_unsplash import ComposerTestCase


def _other_workspace():
    org = Organization.objects.create(name="Other Org")
    return Workspace.objects.create(organization=org, name="Other Workspace")


class ParseHashtagsTests(TestCase):
    def test_normalises_separators_prefixes_and_case(self):
        self.assertEqual(
            parse_hashtags("coffee, #latteart  Coffee\n#baristalife"), ["#coffee", "#latteart", "#baristalife"]
        )

    def test_drops_things_that_are_not_hashtags(self):
        self.assertEqual(parse_hashtags("## 123 ! #ok_1 #"), ["#ok_1"])

    def test_empty(self):
        self.assertEqual(parse_hashtags(""), [])


class HashtagSetModelTests(ComposerTestCase):
    def test_text_joins_the_tags(self):
        hashtag_set = HashtagSet.objects.create(workspace=self.workspace, name="Coffee", hashtags=["#a", "#b"])
        self.assertEqual(hashtag_set.text, "#a #b")

    def test_name_is_unique_per_workspace_only(self):
        HashtagSet.objects.create(workspace=self.workspace, name="Coffee", hashtags=["#a"])
        HashtagSet.objects.create(workspace=_other_workspace(), name="Coffee", hashtags=["#a"])
        with self.assertRaises(IntegrityError), transaction.atomic():
            HashtagSet.objects.create(workspace=self.workspace, name="Coffee", hashtags=["#b"])

    def test_for_workspace_scopes(self):
        mine = HashtagSet.objects.create(workspace=self.workspace, name="Mine", hashtags=["#a"])
        HashtagSet.objects.create(workspace=_other_workspace(), name="Theirs", hashtags=["#b"])
        self.assertEqual(list(HashtagSet.objects.for_workspace(self.workspace.id)), [mine])


class SignatureModelTests(ComposerTestCase):
    def test_owned_by_exactly_one_of_workspace_and_user(self):
        for owners in ({}, {"workspace": self.workspace, "user": self.user}):
            with self.assertRaises(IntegrityError), transaction.atomic():
                Signature.objects.create(name="Bad", body="x", **owners)

    def test_one_default_per_owner(self):
        Signature.objects.create(workspace=self.workspace, name="A", body="a", is_default=True)
        Signature.objects.create(workspace=self.workspace, name="B", body="b")
        Signature.objects.create(user=self.user, name="C", body="c", is_default=True)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Signature.objects.create(workspace=self.workspace, name="D", body="d", is_default=True)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Signature.objects.create(user=self.user, name="E", body="e", is_default=True)

    def test_available_is_the_workspaces_plus_the_users_own(self):
        shared = Signature.objects.create(workspace=self.workspace, name="Shared", body="s")
        mine = Signature.objects.create(user=self.user, name="Mine", body="m")
        other_user = User.objects.create_user(email="other@example.com", password="x", tos_accepted_at=timezone.now())
        Signature.objects.create(user=other_user, name="Theirs", body="t")
        Signature.objects.create(workspace=_other_workspace(), name="Elsewhere", body="e")

        self.assertCountEqual(Signature.objects.available(self.workspace.id, self.user), [shared, mine])


class SignatureServiceTests(ComposerTestCase):
    def test_users_default_wins_over_the_workspaces(self):
        Signature.objects.create(workspace=self.workspace, name="House", body="house", is_default=True)
        self.assertEqual(default_signature(self.workspace, self.user).body, "house")
        Signature.objects.create(user=self.user, name="Mine", body="mine", is_default=True)
        self.assertEqual(default_signature(self.workspace, self.user).body, "mine")

    def test_none_without_a_default(self):
        Signature.objects.create(workspace=self.workspace, name="House", body="house")
        self.assertIsNone(default_signature(self.workspace, self.user))

    def test_with_signature_sets_it_apart(self):
        signature = Signature(name="S", body="-- Sam")
        self.assertEqual(with_signature("Hello  \n", signature), "Hello\n\n-- Sam")
        self.assertEqual(with_signature("", signature), "\n\n-- Sam")
        self.assertEqual(with_signature("Hello", None), "Hello")


class RecentHashtagsTests(ComposerTestCase):
    def _post(self, caption, workspace=None):
        return Post.objects.create(workspace=workspace or self.workspace, author=self.user, caption=caption)

    def test_most_recent_first_without_repeats_or_non_tags(self):
        self._post("old #Coffee #tea")
        self._post("new #latteart #coffee, item #1 and a@b.com #")
        self.assertEqual(recent_hashtags(self.workspace.id), ["#latteart", "#coffee", "#tea"])

    def test_limit(self):
        self._post(" ".join(f"#tag{n}" for n in range(30)))
        self.assertEqual(len(recent_hashtags(self.workspace.id, limit=5)), 5)

    def test_other_workspaces_are_not_read(self):
        self._post("#secret", workspace=_other_workspace())
        self.assertEqual(recent_hashtags(self.workspace.id), [])


class PickerEndpointTests(ComposerTestCase):
    def _get(self, name, **query):
        url = reverse(f"composer:{name}", kwargs={"workspace_id": self.workspace.id})
        return self.client.get(url, query)

    def test_hashtag_picker_lists_sets_and_recent_tags(self):
        HashtagSet.objects.create(workspace=self.workspace, name="Coffee", hashtags=["#coffee", "#latte"])
        Post.objects.create(workspace=self.workspace, author=self.user, caption="hi #fresh")

        data = self._get("hashtag_picker").json()

        self.assertEqual([s["name"] for s in data["sets"]], ["Coffee"])
        self.assertEqual(data["sets"][0]["text"], "#coffee #latte")
        self.assertEqual(data["sets"][0]["count"], 2)
        self.assertEqual(data["recent"], ["#fresh"])

    def test_hashtag_picker_never_leaks_another_workspace(self):
        other = _other_workspace()
        HashtagSet.objects.create(workspace=other, name="Theirs", hashtags=["#x"])
        Post.objects.create(workspace=other, author=self.user, caption="#theirs")

        data = self._get("hashtag_picker").json()

        self.assertEqual(data, {"sets": [], "recent": []})

    def test_signature_picker_lists_workspace_and_own_signatures_only(self):
        Signature.objects.create(workspace=self.workspace, name="House", body="house", is_default=True)
        Signature.objects.create(user=self.user, name="Mine", body="mine")
        stranger = User.objects.create_user(email="s@example.com", password="x", tos_accepted_at=timezone.now())
        Signature.objects.create(user=stranger, name="Stranger", body="stranger")
        Signature.objects.create(workspace=_other_workspace(), name="Elsewhere", body="elsewhere")

        signatures = self._get("signature_picker").json()["signatures"]

        self.assertEqual(
            sorted((s["name"], s["scope"], s["is_default"]) for s in signatures),
            [("House", "workspace", True), ("Mine", "personal", False)],
        )

    def test_pickers_require_membership_and_create_posts(self):
        outsider = User.objects.create_user(email="o@example.com", password="x", tos_accepted_at=timezone.now())
        viewer = User.objects.create_user(email="v@example.com", password="x", tos_accepted_at=timezone.now())
        OrgMembership.objects.create(user=viewer, organization=self.org, org_role=OrgMembership.OrgRole.MEMBER)
        WorkspaceMembership.objects.create(
            user=viewer, workspace=self.workspace, workspace_role=WorkspaceMembership.WorkspaceRole.VIEWER
        )
        for user in (outsider, viewer):
            self.client.force_login(user)
            for name in ("hashtag_picker", "signature_picker", "mention_search"):
                self.assertEqual(self._get(name).status_code, 403, (user.email, name))

    def test_pickers_require_login(self):
        self.client.logout()
        for name in ("hashtag_picker", "signature_picker", "mention_search"):
            self.assertEqual(self._get(name).status_code, 302, name)


class ComposeSignatureTests(ComposerTestCase):
    def _compose(self, **query):
        url = reverse("composer:compose", kwargs={"workspace_id": self.workspace.id})
        response = self.client.get(url, query)
        self.assertEqual(response.status_code, 200)
        return response

    def test_new_post_starts_with_the_default_signature(self):
        Signature.objects.create(workspace=self.workspace, name="House", body="Sent from the shop", is_default=True)
        self.assertEqual(self._compose().context["form"].initial["caption"], "\n\nSent from the shop")

    def test_no_default_means_an_empty_caption(self):
        Signature.objects.create(workspace=self.workspace, name="House", body="x")
        self.assertEqual(self._compose().context["form"].initial.get("caption", ""), "")

    def test_a_template_caption_is_left_alone(self):
        from apps.composer.models import PostTemplate

        Signature.objects.create(workspace=self.workspace, name="House", body="Sent from the shop", is_default=True)
        template = PostTemplate.objects.create(
            workspace=self.workspace, name="T", template_data={"caption": "From the template"}
        )
        self.assertEqual(self._compose(template=template.id).context["form"].initial["caption"], "From the template")

    def test_an_existing_post_is_not_signed_again(self):
        Signature.objects.create(workspace=self.workspace, name="House", body="Sent from the shop", is_default=True)
        post = Post.objects.create(workspace=self.workspace, author=self.user, caption="Already written")
        url = reverse("composer:compose_edit", kwargs={"workspace_id": self.workspace.id, "post_id": post.id})
        response = self.client.get(url)
        self.assertEqual(response.context["form"].initial["caption"], "Already written")

    def test_another_workspaces_default_is_not_used(self):
        Signature.objects.create(workspace=_other_workspace(), name="Theirs", body="theirs", is_default=True)
        self.assertEqual(self._compose().context["form"].initial.get("caption", ""), "")

    def test_toolbar_is_rendered_for_the_caption_and_char_limits_carry_mention_support(self):
        SocialAccount.objects.create(
            workspace=self.workspace,
            platform="bluesky",
            account_platform_id="did:plc:1",
            account_name="Sky",
            connection_status=SocialAccount.ConnectionStatus.CONNECTED,
        )
        SocialAccount.objects.create(
            workspace=self.workspace,
            platform="linkedin_personal",
            account_platform_id="li-1",
            account_name="Linked",
            connection_status=SocialAccount.ConnectionStatus.CONNECTED,
        )
        response = self._compose()
        limits = response.context["char_limits"]
        self.assertEqual(sorted(v["mention_lookup"] for v in limits.values()), [False, True])
        html = response.content.decode()
        self.assertIn('id="caption-editor"', html)
        self.assertIn("insertToolbar(", html)
        self.assertIn(reverse("composer:mention_search", kwargs={"workspace_id": self.workspace.id}), html)


class MentionSearchTests(ComposerTestCase):
    def setUp(self):
        super().setUp()
        self.bluesky = SocialAccount.objects.create(
            workspace=self.workspace,
            platform="bluesky",
            account_platform_id="did:plc:1",
            account_name="Sky",
            connection_status=SocialAccount.ConnectionStatus.CONNECTED,
        )
        self.url = reverse("composer:mention_search", kwargs={"workspace_id": self.workspace.id})

    def _search(self, q, *accounts):
        return self.client.get(self.url, {"q": q, "account": [str(a.id) for a in accounts]})

    @patch("apps.publisher.engine._provider_and_access_token")
    def test_returns_suggestions_from_the_accounts_provider(self, mock_provider):
        provider = MagicMock()
        provider.search_mentions.return_value = [MentionSuggestion(handle="alice.bsky.social", name="Alice")]
        mock_provider.return_value = (provider, "jwt")

        data = self._search("ali", self.bluesky).json()

        self.assertEqual(data, {"results": [{"handle": "alice.bsky.social", "name": "Alice", "avatar_url": ""}]})
        provider.search_mentions.assert_called_once_with("jwt", "ali")

    @patch("apps.publisher.engine._provider_and_access_token")
    def test_repeat_queries_are_served_from_the_cache(self, mock_provider):
        provider = MagicMock()
        provider.search_mentions.return_value = [MentionSuggestion(handle="alice.bsky.social")]
        mock_provider.return_value = (provider, "jwt")

        self._search("ali", self.bluesky)
        self._search("ALI", self.bluesky)

        self.assertEqual(provider.search_mentions.call_count, 1)

    @patch("apps.publisher.engine._provider_and_access_token")
    def test_a_failing_platform_yields_no_suggestions_and_no_error(self, mock_provider):
        mock_provider.side_effect = RuntimeError("down")

        response = self._search("ali", self.bluesky)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"results": []})

    @patch("apps.publisher.engine._provider_and_access_token")
    def test_other_workspaces_accounts_are_not_searched(self, mock_provider):
        foreign = SocialAccount.objects.create(
            workspace=_other_workspace(),
            platform="bluesky",
            account_platform_id="did:plc:2",
            account_name="Foreign",
            connection_status=SocialAccount.ConnectionStatus.CONNECTED,
        )

        self.assertEqual(self._search("ali", foreign).json(), {"results": []})
        mock_provider.assert_not_called()

    @patch("apps.publisher.engine._provider_and_access_token")
    def test_platforms_without_lookup_are_skipped(self, mock_provider):
        linkedin = SocialAccount.objects.create(
            workspace=self.workspace,
            platform="linkedin_personal",
            account_platform_id="li-1",
            account_name="Linked",
            connection_status=SocialAccount.ConnectionStatus.CONNECTED,
        )
        self.assertEqual(self._search("ali", linkedin).json(), {"results": []})
        mock_provider.assert_not_called()

    def test_bad_input_is_ignored(self):
        self.assertEqual(self.client.get(self.url, {"q": "", "account": str(self.bluesky.id)}).json(), {"results": []})
        self.assertEqual(self.client.get(self.url, {"q": "ali", "account": "not-a-uuid"}).json(), {"results": []})

    @patch("apps.publisher.engine._provider_and_access_token")
    def test_suggestions_are_merged_and_deduplicated_across_accounts(self, mock_provider):
        second = SocialAccount.objects.create(
            workspace=self.workspace,
            platform="bluesky",
            account_platform_id="did:plc:3",
            account_name="Sky 2",
            connection_status=SocialAccount.ConnectionStatus.CONNECTED,
        )
        provider = MagicMock()
        provider.search_mentions.side_effect = [
            [MentionSuggestion(handle="a.bsky.social"), MentionSuggestion(handle="b.bsky.social")],
            [MentionSuggestion(handle="B.bsky.social"), MentionSuggestion(handle="c.bsky.social")],
        ]
        mock_provider.return_value = (provider, "jwt")

        handles = [m["handle"] for m in mention_suggestions([self.bluesky, second], "x")]

        self.assertEqual(handles, ["a.bsky.social", "b.bsky.social", "c.bsky.social"])

    def test_json_is_valid_for_the_client(self):
        self.assertIsInstance(json.loads(self._search("x", self.bluesky).content), dict)
