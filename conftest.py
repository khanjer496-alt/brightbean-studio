import pytest
from django.core.cache import cache
from django.utils import timezone

from apps.accounts.models import User
from apps.media_library.storage import reset_cached_client
from apps.members.models import OrgMembership
from apps.organizations.models import Organization


@pytest.fixture
def user(db):
    return User.objects.create_user(
        email="test@example.com", password="testpass123", name="Test User", tos_accepted_at=timezone.now()
    )


@pytest.fixture
def organization(db):
    return Organization.objects.create(name="Test Organization")


@pytest.fixture
def org_owner(db, user, organization):
    OrgMembership.objects.create(user=user, organization=organization, org_role=OrgMembership.OrgRole.OWNER)
    return user


@pytest.fixture(autouse=True)
def _fresh_storage_client():
    """Keep the memoized boto3 client from leaking across tests.

    ``apps.media_library.storage`` caches one client for the life of the
    process — the point is to stop django-storages rebuilding a boto3 Session
    per thread. Without this, the first test to touch S3 would pin its client
    and every later ``override_settings`` on a bucket or endpoint would be
    silently ignored.
    """
    reset_cached_client()
    yield
    reset_cached_client()


@pytest.fixture(autouse=True)
def _fresh_cache():
    """Start every test with an empty default cache.

    The test settings use one in-process LocMemCache for the whole run, and
    ``AuthRateLimitMiddleware`` counts every login and signup POST from the
    test client's single IP in it. Without this, the eleventh such POST of the
    run — in whichever test happens to make it — gets a 429.
    """
    cache.clear()


@pytest.fixture
def autonomous_agents(settings):
    """Agents publish and reply on their own, with no undo window.

    The production defaults are conservative (agents draft, a person approves
    what goes out) and every test runs on them unless it asks otherwise. Suites
    that exercise what an agent does once trusted (scheduling over REST/MCP,
    sending replies) opt in with ``pytestmark = pytest.mark.usefixtures("autonomous_agents")``.
    """
    settings.AUTONOMY_DEFAULT_LEVELS = {
        "draft_level": "full_auto",
        "publish_level": "full_auto",
        "reply_level": "full_auto",
        "recycle_level": "per_item",
    }
    settings.AUTONOMY_DEFAULT_UNDO_MINUTES = 0
