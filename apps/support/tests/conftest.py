import time

import pytest
from django.utils import timezone

from apps.accounts.models import User

PASSWORD = "S3cure-pass-for-tests!"


def make_user(email, **kwargs):
    return User.objects.create_user(email=email, password=PASSWORD, tos_accepted_at=timezone.now(), **kwargs)


def record_authentication(client, *, seconds_ago=0, reauthenticated=True):
    """Pretend the session signed in, or re-entered a password, that long ago."""
    session = client.session
    session["account_authentication_methods"] = [
        {"method": "password", "at": time.time() - seconds_ago, "reauthenticated": reauthenticated}
    ]
    session.save()


@pytest.fixture
def superuser(db):
    return make_user("support@example.com", is_staff=True, is_superuser=True)


@pytest.fixture
def customer(db):
    return make_user("customer@example.com", name="Customer")


@pytest.fixture
def staff_client(client, superuser):
    client.force_login(superuser)
    record_authentication(client)
    return client
