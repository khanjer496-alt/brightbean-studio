"""Medium, WordPress, Hashnode and Listmonk providers, with every HTTP call mocked."""

from unittest.mock import patch

import httpx
import pytest

from providers import PROVIDER_REGISTRY, get_provider
from providers.exceptions import APIError, PublishError
from providers.hashnode import HashnodeProvider, tag_slug
from providers.listmonk import ListmonkProvider
from providers.medium import MediumProvider
from providers.types import AuthType, PostType, PublishContent
from providers.wordpress import WordPressProvider, markdown_to_html


class FakeHttp:
    """Stands in for ``httpx.Client``: answers each request from ``routes`` and records it."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def __call__(self, **kwargs):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def request(self, method, url, headers=None, params=None, json=None, files=None, data=None, content=None):
        self.calls.append({"method": method, "url": url, "headers": headers, "params": params, "json": json})
        for (route_method, suffix), answer in self.routes.items():
            if route_method == method and url.endswith(suffix):
                status, body = answer.pop(0) if isinstance(answer, list) else answer
                req = httpx.Request(method, url)
                if isinstance(body, str):
                    return httpx.Response(status, text=body, request=req)
                return httpx.Response(status, json=body, request=req)
        raise AssertionError(f"unexpected request {method} {url}")

    def sent(self, method, suffix):
        return [c for c in self.calls if c["method"] == method and c["url"].endswith(suffix)]


@pytest.fixture
def http():
    def install(routes):
        fake = FakeHttp(routes)
        patcher = patch("providers.base.httpx.Client", fake)
        patcher.start()
        installed.append(patcher)
        return fake

    installed = []
    yield install
    for patcher in installed:
        patcher.stop()


def article(**extra):
    return PublishContent(text="Hello **world**", title="A title", extra=extra)


class TestDeclarations:
    @pytest.mark.parametrize("platform", ["medium", "wordpress", "hashnode", "listmonk"])
    def test_pasted_credentials_platforms(self, platform):
        cls = PROVIDER_REGISTRY[platform]
        provider = cls()
        assert provider.auth_type is AuthType.SESSION
        assert cls.connect_url_name == f"connect_{platform}"
        assert cls.connect_fields
        assert not cls.thread.supports_threads
        assert PostType.ARTICLE in provider.supported_post_types
        assert cls.media_rules[PostType.IMAGE].media_types == frozenset({"image"})

    def test_only_the_self_hosted_ones_take_a_site_address(self):
        assert WordPressProvider.connect_url_field == WordPressProvider.account_url_credential == "site_url"
        assert ListmonkProvider.connect_url_field == ListmonkProvider.account_url_credential == "site_url"
        assert not MediumProvider.connect_url_field
        assert not HashnodeProvider.account_url_credential

    def test_secrets_are_password_inputs(self):
        secret_keys = {"medium": "token", "wordpress": "app_password", "hashnode": "token", "listmonk": "token"}
        for platform, key in secret_keys.items():
            field = next(f for f in PROVIDER_REGISTRY[platform].connect_fields if f.key == key)
            assert field.kind == "password"

    def test_registered(self):
        assert isinstance(get_provider("medium"), MediumProvider)
        assert isinstance(get_provider("wordpress"), WordPressProvider)
        assert isinstance(get_provider("hashnode"), HashnodeProvider)
        assert isinstance(get_provider("listmonk"), ListmonkProvider)

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("example.com", "https://example.com"),
            (" https://Example.com/blog/ ", "https://Example.com/blog"),
            ("https://example.com:8443", "https://example.com:8443"),
            ("http://example.com", ""),
            ("https://user:pw@example.com", ""),
            ("ftp://example.com", ""),
            ("", ""),
        ],
    )
    def test_site_urls_are_https_only(self, raw, expected):
        assert WordPressProvider.normalize_site_url(raw) == expected


class TestTitleAndTags:
    @pytest.mark.parametrize("cls", [MediumProvider, WordPressProvider, HashnodeProvider, ListmonkProvider])
    def test_a_title_is_required(self, cls):
        with pytest.raises(PublishError) as exc:
            cls({"site_url": "https://x.example"}).publish_post("t", PublishContent(text="body"))
        assert "requires a title" in str(exc.value)
        assert exc.value.retryable is False

    def test_medium_refuses_a_long_title(self):
        with pytest.raises(PublishError, match="limited to 100"):
            MediumProvider().publish_post("t", PublishContent(text="b", title="x" * 101))

    def test_tags_come_from_hashtags_capped_and_deduplicated(self):
        provider = MediumProvider()
        content = PublishContent(text="Intro #python #Python #django #flask #extra", title="t")
        assert provider._tags(content) == ["python", "django", "flask"]


class TestMedium:
    def test_profile(self, http):
        fake = http(
            {("GET", "/me"): (200, {"data": {"id": "u1", "username": "ann", "name": "Ann", "imageUrl": "i.png"}})}
        )
        profile = MediumProvider().get_profile("tok")
        assert (profile.platform_id, profile.name, profile.handle) == ("u1", "Ann", "ann")
        assert fake.calls[0]["headers"]["Authorization"] == "Bearer tok"

    def test_publish_sends_markdown_tags_and_canonical(self, http):
        fake = http({("POST", "/users/u1/posts"): (201, {"data": {"id": "p9", "url": "https://medium.com/@ann/x"}})})
        content = PublishContent(
            text="Body #tag",
            title="Title",
            link_url="https://example.com/orig",
            media_urls=["https://cdn.example/a.png?sig=1"],
            extra={"publish_status": "draft", "notify_followers": True},
        )
        result = MediumProvider({"author_id": "u1"}).publish_post("tok", content)
        assert (result.platform_post_id, result.url) == ("p9", "https://medium.com/@ann/x")
        body = fake.calls[0]["json"]
        assert body["contentFormat"] == "markdown"
        assert body["content"].startswith("# Title\n\n![](https://cdn.example/a.png?sig=1)\n\nBody #tag")
        assert body["publishStatus"] == "draft"
        assert body["tags"] == ["tag"]
        assert body["canonicalUrl"] == "https://example.com/orig"
        assert body["notifyFollowers"] is True

    def test_publication_posts_go_to_the_publication(self, http):
        fake = http({("POST", "/publications/pub1/posts"): (201, {"data": {"id": "p1", "url": "u"}})})
        MediumProvider({"author_id": "u1"}).publish_post("tok", article(publication="pub1"))
        assert fake.calls[0]["url"].endswith("/publications/pub1/posts")

    def test_publication_options(self, http):
        http({("GET", "/users/u1/publications"): (200, {"data": [{"id": "pub1", "name": "Eng"}]})})
        options = MediumProvider({"author_id": "u1"}).get_setting_options("tok", "publication")
        assert options == [{"id": "pub1", "name": "Eng"}]

    def test_a_bad_canonical_url_is_refused(self):
        assert MediumProvider.validate_post_settings({"canonical_url": "example.com"})
        assert not MediumProvider.validate_post_settings({"canonical_url": "https://example.com"})

    def test_a_revoked_token_is_a_401(self, http):
        http({("GET", "/me"): (401, {"errors": [{"message": "Token was invalid."}]})})
        with pytest.raises(APIError) as exc:
            MediumProvider().get_profile("tok")
        assert exc.value.status_code == 401


WP = {"site_url": "https://blog.example", "username": "ann"}


class TestWordPress:
    def test_markdown_becomes_html(self):
        assert "<strong>world</strong>" in markdown_to_html("Hello **world**")
        assert "<h2>" in markdown_to_html("## Sub")

    def test_profile_uses_basic_auth_and_names_the_site(self, http):
        fake = http(
            {
                ("GET", "/wp-json/wp/v2/users/me"): (
                    200,
                    {"id": 7, "slug": "ann", "avatar_urls": {"96": "a.png"}, "capabilities": {"publish_posts": True}},
                ),
                ("GET", "/wp-json"): (200, {"name": "Ann's Blog"}),
            }
        )
        profile = WordPressProvider(WP).get_profile("abcd efgh")
        assert profile.platform_id == "blog.example:7"
        assert profile.name == "Ann's Blog"
        assert profile.extra["account_data"] == {"username": "ann"}
        sent = fake.sent("GET", "/wp-json/wp/v2/users/me")[0]
        import base64

        assert sent["headers"]["Authorization"] == "Basic " + base64.b64encode(b"ann:abcd efgh").decode()
        assert sent["params"] == {"context": "edit"}

    def test_connect_explains_a_rejected_login(self, http):
        http({("GET", "/wp-json/wp/v2/users/me"): (401, {"code": "incorrect_password"})})
        with pytest.raises(PublishError, match="rejected the login"):
            WordPressProvider().connect_with_form(
                {"site_url": "https://blog.example", "username": "ann", "app_password": "x"}
            )

    def test_connect_refuses_a_user_who_cannot_publish(self, http):
        http(
            {
                ("GET", "/wp-json/wp/v2/users/me"): (200, {"id": 7, "capabilities": {"publish_posts": False}}),
                ("GET", "/wp-json"): (200, {"name": "B"}),
            }
        )
        with pytest.raises(PublishError, match="can't publish"):
            WordPressProvider().connect_with_form(
                {"site_url": "https://blog.example", "username": "ann", "app_password": "x"}
            )

    def test_html_answer_is_not_mistaken_for_the_api(self, http):
        http({("GET", "/wp-json/wp/v2/users/me"): (200, "<html>Welcome</html>")})
        with pytest.raises(PublishError, match="WordPress REST data"):
            WordPressProvider(WP).get_profile("x")

    def test_no_site_address_means_no_request(self):
        with pytest.raises(PublishError, match="no usable site address"):
            WordPressProvider({}).publish_post("x", article())

    def test_publish_creates_tags_category_and_featured_image(self, http, tmp_path):
        image = tmp_path / "cover.png"
        image.write_bytes(b"\x89PNG")
        fake = http(
            {
                ("GET", "/wp-json/wp/v2/tags"): [(200, [{"id": 4, "name": "Python"}]), (200, [])],
                ("POST", "/wp-json/wp/v2/tags"): (201, {"id": 5, "name": "django"}),
                ("POST", "/wp-json/wp/v2/media"): (201, {"id": 33}),
                ("POST", "/wp-json/wp/v2/posts"): (201, {"id": 90, "link": "https://blog.example/hello"}),
            }
        )
        content = PublishContent(
            text="Hello **world** #python #django",
            title="Hello",
            media_files=[str(image)],
            extra={"status": "draft", "category": "12"},
        )
        result = WordPressProvider(WP).publish_post("pw", content)
        assert (result.platform_post_id, result.url) == ("90", "https://blog.example/hello")
        post = fake.sent("POST", "/wp-json/wp/v2/posts")[0]["json"]
        assert post["status"] == "draft"
        assert post["categories"] == [12]
        assert post["tags"] == [4, 5]
        assert post["featured_media"] == 33
        assert "<strong>world</strong>" in post["content"]
        media = fake.sent("POST", "/wp-json/wp/v2/media")[0]["headers"]
        assert media["Content-Disposition"] == 'attachment; filename="cover.png"'
        assert media["Content-Type"] == "image/png"

    def test_a_tag_that_cannot_be_created_does_not_stop_the_post(self, http):
        fake = http(
            {
                ("GET", "/wp-json/wp/v2/tags"): (200, []),
                ("POST", "/wp-json/wp/v2/tags"): (403, {"code": "rest_cannot_create"}),
                ("POST", "/wp-json/wp/v2/posts"): (201, {"id": 1, "link": "l"}),
            }
        )
        WordPressProvider(WP).publish_post("pw", PublishContent(text="x #new", title="t"))
        assert "tags" not in fake.sent("POST", "/wp-json/wp/v2/posts")[0]["json"]

    def test_status_must_be_one_of_the_choices(self):
        assert WordPressProvider.validate_post_settings({"status": "trash"})
        assert not WordPressProvider.validate_post_settings({"status": "publish"})

    def test_category_options(self, http):
        http({("GET", "/wp-json/wp/v2/categories"): (200, [{"id": 3, "name": "News"}])})
        assert WordPressProvider(WP).get_setting_options("pw", "category") == [{"id": "3", "name": "News"}]

    def test_account_credentials_carry_the_username(self):
        class Account:
            platform_data = {"username": "ann"}
            instance_url = "https://blog.example"

        assert WordPressProvider.credentials_for_account(Account) == {"username": "ann"}
        assert WordPressProvider.profile_url(Account) == "https://blog.example"


class TestHashnode:
    def test_tag_slugs(self):
        assert tag_slug("Machine Learning!") == "machine-learning"

    def test_profile_sends_the_raw_token(self, http):
        fake = http(
            {("POST", "gql.hashnode.com"): (200, {"data": {"me": {"id": "h1", "name": "Ann", "username": "ann"}}})}
        )
        profile = HashnodeProvider().get_profile("tok")
        assert profile.platform_id == "h1"
        assert fake.calls[0]["headers"]["Authorization"] == "tok"

    def test_graphql_errors_in_a_200_are_failures(self, http):
        http(
            {
                ("POST", "gql.hashnode.com"): (
                    200,
                    {"errors": [{"message": "no", "extensions": {"code": "UNAUTHENTICATED"}}]},
                )
            }
        )
        with pytest.raises(PublishError, match="Pro plan") as exc:
            HashnodeProvider().get_profile("tok")
        assert exc.value.retryable is False

    def test_publish_maps_tags_canonical_and_cover(self, http):
        fake = http(
            {
                ("POST", "gql.hashnode.com"): (
                    200,
                    {"data": {"publishPost": {"post": {"id": "a1", "url": "https://h/x"}}}},
                )
            }
        )
        content = PublishContent(
            text="Body #Machine #django",
            title="T",
            link_url="https://example.com/o",
            media_urls=["https://cdn.example/c.jpg"],
            extra={"publication": "pub1", "subtitle": "Sub"},
        )
        result = HashnodeProvider().publish_post("tok", content)
        assert (result.platform_post_id, result.url) == ("a1", "https://h/x")
        variables = fake.calls[0]["json"]["variables"]["input"]
        assert variables["publicationId"] == "pub1"
        assert variables["originalArticleURL"] == "https://example.com/o"
        assert variables["subtitle"] == "Sub"
        assert variables["coverImageOptions"] == {"coverImageURL": "https://cdn.example/c.jpg"}
        assert variables["tags"] == [{"name": "Machine", "slug": "machine"}, {"name": "django", "slug": "django"}]

    def test_a_single_publication_is_used_without_being_chosen(self, http):
        fake = http(
            {
                ("POST", "gql.hashnode.com"): [
                    (200, {"data": {"me": {"publications": {"edges": [{"node": {"id": "only", "title": "Blog"}}]}}}}),
                    (200, {"data": {"publishPost": {"post": {"id": "a1", "url": "u"}}}}),
                ]
            }
        )
        HashnodeProvider().publish_post("tok", article())
        assert fake.calls[1]["json"]["variables"]["input"]["publicationId"] == "only"

    def test_several_publications_need_a_choice(self, http):
        edges = [{"node": {"id": "a", "title": "A"}}, {"node": {"id": "b", "title": "B"}}]
        http({("POST", "gql.hashnode.com"): (200, {"data": {"me": {"publications": {"edges": edges}}}})})
        with pytest.raises(PublishError, match="Choose which Hashnode publication"):
            HashnodeProvider().publish_post("tok", article())

    def test_subtitle_length_is_checked(self):
        assert HashnodeProvider.validate_post_settings({"subtitle": "x" * 151})


LM = {"site_url": "https://news.example", "username": "api"}


class TestListmonk:
    def test_profile_validates_by_reading_lists(self, http):
        fake = http({("GET", "/api/lists"): (200, {"data": {"results": [{"id": 1, "name": "All"}]}})})
        profile = ListmonkProvider(LM).get_profile("secret")
        assert profile.platform_id == "news.example:api"
        assert profile.extra["account_data"] == {"username": "api"}
        assert fake.calls[0]["headers"]["Authorization"] == "token api:secret"

    def test_connect_explains_a_rejected_login(self, http):
        http({("GET", "/api/lists"): (403, {"message": "forbidden"})})
        with pytest.raises(PublishError, match="rejected the login"):
            ListmonkProvider().connect_with_form({"site_url": "https://news.example", "username": "a", "token": "t"})

    def test_list_is_required(self):
        assert ListmonkProvider.validate_post_settings({}) == "Send to list is required."
        assert ListmonkProvider.validate_post_settings({"list": "abc"})
        assert not ListmonkProvider.validate_post_settings({"list": "3"})

    def test_options(self, http):
        http(
            {
                ("GET", "/api/lists"): (200, {"data": {"results": [{"id": 3, "name": "Weekly"}]}}),
                ("GET", "/api/templates"): (
                    200,
                    {"data": [{"id": 1, "name": "Default", "type": "campaign"}, {"id": 2, "name": "Tx", "type": "tx"}]},
                ),
            }
        )
        provider = ListmonkProvider(LM)
        assert provider.get_setting_options("t", "list") == [{"id": "3", "name": "Weekly"}]
        assert provider.get_setting_options("t", "template") == [{"id": "1", "name": "Default"}]

    def test_publish_creates_then_starts_the_campaign(self, http):
        fake = http(
            {
                ("POST", "/api/campaigns"): (200, {"data": {"id": 11}}),
                ("PUT", "/api/campaigns/11/status"): (200, {"data": {"id": 11}}),
            }
        )
        result = ListmonkProvider(LM).publish_post("t", article(list="3", template="2"))
        assert result.platform_post_id == "11"
        body = fake.sent("POST", "/api/campaigns")[0]["json"]
        assert body["subject"] == "A title"
        assert body["lists"] == [3]
        assert body["content_type"] == "markdown"
        assert body["template_id"] == 2
        assert fake.sent("PUT", "/api/campaigns/11/status")[0]["json"] == {"status": "running"}

    def test_a_draft_is_not_started(self, http):
        fake = http({("POST", "/api/campaigns"): (200, {"data": {"id": 11}})})
        ListmonkProvider(LM).publish_post("t", article(list="3", delivery="draft"))
        assert not fake.sent("PUT", "/api/campaigns/11/status")

    def test_a_campaign_that_cannot_start_is_not_retried(self, http):
        http(
            {
                ("POST", "/api/campaigns"): (200, {"data": {"id": 11}}),
                ("PUT", "/api/campaigns/11/status"): (500, {"message": "boom"}),
            }
        )
        with pytest.raises(PublishError, match="couldn't start it") as exc:
            ListmonkProvider(LM).publish_post("t", article(list="3"))
        assert exc.value.retryable is False

    def test_a_publish_without_a_list_is_refused(self):
        with pytest.raises(PublishError, match="Send to list is required"):
            ListmonkProvider(LM).publish_post("t", article())


def test_secrets_never_reach_a_logged_error(http, caplog):
    http({("GET", "/me"): (401, {"errors": [{"message": "bad"}]})})
    with pytest.raises(APIError) as exc:
        MediumProvider().get_profile("super-secret-token")
    assert "super-secret-token" not in str(exc.value)
    assert "super-secret-token" not in caplog.text
