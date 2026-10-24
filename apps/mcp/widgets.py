"""UI widgets (MCP Apps / ChatGPT Apps SDK) served as MCP resources.

Three small self-contained HTML bundles render the structured result of a few tools inline in hosts that support
MCP Apps: a post preview card, a week calendar and an approval list. The bundles are built from
``integrations/chatgpt/widgets`` by ``integrations/chatgpt/build.mjs`` into ``apps/mcp/widgets/`` and committed, so the
server needs no Node at runtime.

The widget URI doubles as a host-side cache key: change a bundle in a way a cached copy must not survive and bump
its ``version`` here, which changes the URI every tool points at.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from django.conf import settings

#: MIME type of an MCP Apps view (https://developers.openai.com/plugins/build/chatgpt-ui).
WIDGET_MIME_TYPE = "text/html;profile=mcp-app"

_DIR = Path(__file__).resolve().parent / "widgets"


@dataclass(frozen=True)
class Widget:
    slug: str
    version: int
    title: str
    description: str
    #: Tools whose result the widget renders.
    tools: tuple[str, ...]
    invoking: str
    invoked: str

    @property
    def uri(self) -> str:
        return f"ui://widget/{self.slug}-v{self.version}.html"

    @property
    def path(self) -> Path:
        return _DIR / f"{self.slug}.html"

    def read_html(self) -> str:
        return self.path.read_text(encoding="utf-8")

    def resource_descriptor(self) -> dict:
        return {
            "uri": self.uri,
            "name": self.title,
            "title": self.title,
            "description": self.description,
            "mimeType": WIDGET_MIME_TYPE,
        }

    def content(self) -> dict:
        ui: dict = {
            "prefersBorder": True,
            # The widgets load nothing and call no API; an empty allowlist keeps it that way.
            "csp": {"connectDomains": [], "resourceDomains": []},
        }
        domain = getattr(settings, "MCP_WIDGET_DOMAIN", "")
        if domain:
            ui["domain"] = domain
        return {
            "uri": self.uri,
            "mimeType": WIDGET_MIME_TYPE,
            "text": self.read_html(),
            "_meta": {"ui": ui, "openai/widgetDescription": self.description},
        }

    def tool_meta(self) -> dict:
        return {
            "ui": {"resourceUri": self.uri},
            "openai/outputTemplate": self.uri,
            "openai/toolInvocation/invoking": self.invoking,
            "openai/toolInvocation/invoked": self.invoked,
        }


WIDGETS: tuple[Widget, ...] = (
    Widget(
        slug="post-preview",
        version=1,
        title="Post preview",
        description="Shows a post as a card: caption, targets, schedule and review status.",
        tools=("get_post", "create_draft", "schedule_post"),
        invoking="Opening the post",
        invoked="Post ready",
    ),
    Widget(
        slug="week-calendar",
        version=1,
        title="Week calendar",
        description="Shows scheduled and published posts laid out by day across a week.",
        tools=("list_posts",),
        invoking="Loading the calendar",
        invoked="Calendar ready",
    ),
    Widget(
        slug="approval-list",
        version=1,
        title="Approval list",
        description="Lists posts waiting for a review decision, with who they target and when they go out.",
        tools=("list_approvals",),
        invoking="Loading the approval queue",
        invoked="Approval queue ready",
    ),
)

_BY_URI = {w.uri: w for w in WIDGETS}
_BY_TOOL = {tool: w for w in WIDGETS for tool in w.tools}


def widget_for_tool(name: str) -> Widget | None:
    return _BY_TOOL.get(name)


def widget_for_uri(uri: str) -> Widget | None:
    return _BY_URI.get(uri)
