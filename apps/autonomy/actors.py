"""Who is acting: one place that turns a request into an actor for the audit trail.

A *human* is a signed-in person using the web app. An *agent* is anything that
arrives through the Agent API or MCP: an API key, or an OAuth client acting for
a person. *System* is work the platform starts on its own: evergreen recycling,
repeating posts, AI rewording.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .models import ActorType


@dataclass(frozen=True)
class ActorInfo:
    type: str
    name: str
    user: Any = None
    api_key: Any = None
    # The credential behind an agent, API key or OAuth stand-in: its own limits can only tighten the dial.
    token: Any = None

    @property
    def is_human(self) -> bool:
        return self.type == ActorType.HUMAN


def human(user) -> ActorInfo:
    name = (getattr(user, "display_name", "") or getattr(user, "email", "") or "A team member") if user else "Someone"
    return ActorInfo(type=ActorType.HUMAN, name=name, user=user)


def system(name: str) -> ActorInfo:
    return ActorInfo(type=ActorType.SYSTEM, name=name)


def agent(api_key, user=None) -> ActorInfo:
    """The actor behind an API key or an OAuth MCP session (an ``ApiKey``-shaped stand-in)."""
    from apps.api_keys.models import ApiKey

    person = user if user is not None else getattr(api_key, "issued_by", None)
    if getattr(api_key, "is_oauth", False):
        who = (getattr(person, "display_name", "") or getattr(person, "email", "")) if person else ""
        name = f"MCP session ({who})" if who else "MCP session"
    else:
        name = getattr(api_key, "name", "") or "API key"
    return ActorInfo(
        type=ActorType.AGENT,
        name=name[:255],
        user=person,
        api_key=api_key if isinstance(api_key, ApiKey) else None,
        token=api_key,
    )
