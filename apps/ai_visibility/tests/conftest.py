"""A Team-plan workspace with a platform AI key and a fake Claude; no test reaches the real API."""

from __future__ import annotations

import re

import pytest

from apps.ai_visibility import services
from apps.ai_writer.tests.fakes import patch_anthropic
from apps.billing.tests.support import set_plan
from apps.organizations.models import Organization
from apps.workspaces.models import Workspace

BRAND = "Acme Scheduler"


@pytest.fixture(autouse=True)
def fake_claude():
    with patch_anthropic() as fake:
        yield fake


@pytest.fixture(autouse=True)
def platform_key(settings):
    settings.ANTHROPIC_API_KEY = "sk-ant-platform-key-for-tests"
    settings.BILLING_ENABLED = True
    return settings


@pytest.fixture
def workspace(db):
    org = Organization.objects.create(name="Bright Agency")
    set_plan(org, "team")
    return Workspace.objects.create(organization=org, name="Acme", timezone="UTC")


@pytest.fixture
def profile(workspace):
    return services.save_profile(
        workspace,
        brand_name=BRAND,
        website="https://www.acme-scheduler.com",
        aliases=["Acme"],
        prompts=["best social media scheduler for agencies in Dubai", "tools to schedule Instagram posts"],
    )


def answer(text, sources=()):
    return {"answer": text, "sources": list(sources)}


class Reader:
    """Answers the "read these answers" call, which names answers by random ids the test cannot know.

    ``readings`` maps a question (or ``"brand"`` for the what-do-you-know question) to the reading for it;
    a question without one is read as "names nothing, no sentiment". Set ``fail`` to an exception to raise instead.
    """

    def __init__(self):
        self.readings: dict[str, dict] = {}
        self.fail: BaseException | None = None


@pytest.fixture
def reader(fake_claude):
    from apps.ai_writer.tests.fakes import message

    reader = Reader()
    original = fake_claude._create

    def create(**kwargs):
        content = kwargs["messages"][0]["content"]
        if "<source id=" not in content:
            return original(**kwargs)
        fake_claude.calls.append(kwargs)
        if reader.fail:
            raise reader.fail
        found = re.findall(r'<source id="([^"]+)" question="([^"]*)"', content)
        blank = {"recommended": [], "sentiment": "none", "knows_brand": False}
        items = []
        for answer_id, question in found:
            key = "brand" if question.startswith("What do you know about") else question
            items.append({"id": answer_id, **reader.readings.get(key, blank)})
        return message({"answers": items})

    fake_claude._create = create
    return reader
