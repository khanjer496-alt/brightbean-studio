"""What the public "Connect your AI" page lists: one entry per AI app or developer tool.

``status`` is honest about where each listing stands. ``available`` means a person can connect today by pasting the
server URL (or using the link) and signing in. ``directory`` entries also have, or will have, a listing in the
vendor's own directory; until that is approved the page says to add the server by URL.
Sources for each procedure are in ``integrations/README.md``.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from urllib.parse import quote

from django.conf import settings

SERVER_NAME = "postdelegate"


@dataclass(frozen=True)
class Link:
    label: str
    href: str
    #: Opens an app (a custom URL scheme) rather than a web page.
    app: bool = False


@dataclass(frozen=True)
class Platform:
    slug: str
    name: str
    group: str
    status: str
    summary: str
    steps: tuple[str, ...]
    command: str = ""
    links: tuple[Link, ...] = field(default_factory=tuple)
    note: str = ""

    @property
    def status_label(self) -> str:
        return STATUS_LABELS[self.status]


def mcp_url() -> str:
    return f"{settings.MCP_PUBLIC_BASE_URL}/api/v1/mcp"


def cursor_install_url(url: str) -> str:
    config = base64.b64encode(json.dumps({"url": url}, separators=(",", ":")).encode()).decode()
    return f"cursor://anysphere.cursor-deeplink/mcp/install?name={SERVER_NAME}&config={quote(config, safe='')}"


def vscode_install_url(url: str) -> str:
    config = json.dumps({"name": SERVER_NAME, "type": "http", "url": url}, separators=(",", ":"))
    return f"vscode:mcp/install?{quote(config, safe='')}"


def _plugin_command() -> str:
    """The Claude Code plugin install command, once the marketplace repository is public and named in settings."""
    repo = getattr(settings, "AI_PLUGIN_MARKETPLACE_REPO", "")
    if not repo:
        return ""
    return f"claude plugin marketplace add {repo} && claude plugin install postdelegate@postdelegate"


def platforms() -> list[Platform]:
    url = mcp_url()
    return [
        Platform(
            slug="claude",
            name="Claude",
            group="AI assistants",
            status="available",
            summary="Claude on the web, desktop and mobile, and Claude Code.",
            steps=(
                "Open Settings, then Connectors, and choose Add custom connector.",
                f"Paste {url} and sign in to approve access. You choose what Claude may do on the approval screen.",
                "Optional: install the PostDelegate plugin, which adds the skill that teaches Claude the scheduling workflows.",
            ),
            command=_plugin_command(),
        ),
        Platform(
            slug="chatgpt",
            name="ChatGPT",
            group="AI assistants",
            status="directory",
            summary="ChatGPT apps, with a post preview, a week calendar and an approval list shown inline.",
            steps=(
                "Open Settings, then Apps, and turn on developer mode if your plan requires it for custom apps.",
                f"Create an app with the server URL {url}, choose OAuth, and sign in to approve access.",
                "Once PostDelegate is listed in the app directory you will be able to add it from there instead.",
            ),
        ),
        Platform(
            slug="copilot-studio",
            name="Microsoft Copilot Studio",
            group="AI assistants",
            status="available",
            summary="Add PostDelegate as a tool for a Copilot Studio agent.",
            steps=(
                "In your agent, open Tools, choose Add a tool, then New tool, then Model Context Protocol.",
                f"Enter {url} as the server URL and choose OAuth 2.0 with dynamic discovery.",
                "Create the connection, sign in to PostDelegate, and add the tool to your agent.",
            ),
        ),
        Platform(
            slug="gemini",
            name="Gemini",
            group="AI assistants",
            status="limited",
            summary="Custom MCP servers in Gemini Enterprise, and in the Gemini app where Google offers custom apps.",
            steps=(
                "Gemini Enterprise: an administrator allows custom MCP servers, then adds a connection with the server URL.",
                "Gemini app: where your account offers it, add a custom app under Connected apps and paste the server URL.",
            ),
            note="Google decides which accounts and plans can add custom servers.",
        ),
        Platform(
            slug="mistral",
            name="Mistral Le Chat",
            group="AI assistants",
            status="available",
            summary="Add PostDelegate as a custom MCP connector in Le Chat.",
            steps=(
                "Open the Connectors page and choose Add Connector, then the Custom MCP Connector tab.",
                f"Enter a name and {url} as the server URL, then choose Connect and sign in.",
            ),
        ),
        Platform(
            slug="perplexity",
            name="Perplexity",
            group="AI assistants",
            status="available",
            summary="Add PostDelegate as a remote connector in Perplexity.",
            steps=(
                "Open Plugins in the sidebar and choose Add custom.",
                f"Choose a remote connector, enter {url}, and sign in to approve access.",
            ),
            note="Availability depends on your Perplexity plan and platform.",
        ),
        Platform(
            slug="claude-code",
            name="Claude Code",
            group="Developer tools",
            status="available",
            summary="Add the server to the Claude Code command line.",
            steps=("Run the command, then run /mcp inside Claude Code and sign in.",),
            command=f"claude mcp add --transport http {SERVER_NAME} {url}",
        ),
        Platform(
            slug="cursor",
            name="Cursor",
            group="Developer tools",
            status="available",
            summary="One click in Cursor, then sign in.",
            steps=("Choose Add to Cursor, confirm the server, and sign in when Cursor asks.",),
            links=(Link("Add to Cursor", cursor_install_url(url), app=True),),
        ),
        Platform(
            slug="vscode",
            name="Visual Studio Code",
            group="Developer tools",
            status="available",
            summary="One click in VS Code with GitHub Copilot agent mode, or from the command line.",
            steps=(
                "Choose Install in VS Code and confirm, or run the command.",
                "Start the server from the MCP view and sign in.",
            ),
            command=f'code --add-mcp \'{{"name":"{SERVER_NAME}","type":"http","url":"{url}"}}\'',
            links=(Link("Install in VS Code", vscode_install_url(url), app=True),),
        ),
        Platform(
            slug="raycast",
            name="Raycast",
            group="Developer tools",
            status="available",
            summary="Use PostDelegate from Raycast AI.",
            steps=(
                "Run the Install MCP Server command in Raycast.",
                f"Name it PostDelegate, enter {url}, choose the HTTP transport, and leave OAuth on dynamic.",
            ),
        ),
    ]


STATUS_LABELS = {
    "available": "Connect by URL",
    "directory": "Directory listing in review",
    "limited": "Depends on your plan",
}
