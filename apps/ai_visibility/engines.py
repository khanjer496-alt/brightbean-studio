"""The models the report asks, behind one interface.

An engine answers a question the way an assistant would, from whatever
``AIProvider`` it wraps. The one registered engine asks the organization's own
AI connection, whichever provider that is, and is named after the provider so
stored answers keep saying which AI wrote them. A provider that returns
citations would get its own ``AnswerEngine`` overriding :meth:`AnswerEngine.ask`
to pass them back in ``EngineAnswer.urls``.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from django.conf import settings

from apps.ai_writer import services as ai
from apps.ai_writer.client import resolve_ai

ANSWER_SYSTEM = """You are an assistant answering a person's question the way a general-purpose AI assistant would.

Rules:
- Answer from what you know. You cannot browse the web, so do not claim to have searched.
- When the question asks for recommendations, name the specific products, companies or people you would suggest, \
most relevant first, and say briefly why.
- If you do not know something, say so. Never invent products, companies, facts or web addresses.
- Include a web address in "sources" only when you are certain it exists; otherwise leave it empty.
- Answer in the JSON format requested."""

ANSWER_SCHEMA = {
    "type": "object",
    "properties": {"answer": {"type": "string"}, "sources": {"type": "array", "items": {"type": "string"}}},
    "required": ["answer", "sources"],
    "additionalProperties": False,
}

_URL = re.compile(r"https?://[^\s)<>\"']+", re.IGNORECASE)


@dataclass(frozen=True)
class EngineAnswer:
    text: str
    model: str
    urls: list[str] = field(default_factory=list)


def extract_urls(text: str, sources: list) -> list[str]:
    """Web addresses in the answer text and the model's own ``sources``, de-duplicated, in order."""
    found = [u.rstrip(".,;:") for u in _URL.findall(text)]
    found += [
        s.strip() for s in sources if isinstance(s, str) and s.strip().lower().startswith(("http://", "https://"))
    ]
    return list(dict.fromkeys(found))[:10]


class AnswerEngine(ABC):
    # Stored with each answer and as the key of ``VisibilityRun.engines``.
    name: str
    # Shown to the person reading the report: which AI answers.
    label: str

    @abstractmethod
    def available(self, organization) -> bool:
        """Whether this organization can use the engine right now."""

    @abstractmethod
    def ask(self, ctx: ai.AIContext, run: ai.AIRun, question: str) -> EngineAnswer:
        """Put ``question`` to the model. Raises an ``AIError`` subclass on failure."""


class ConnectedAIEngine(AnswerEngine):
    """Asks the organization's own AI connection, whatever provider it is."""

    def __init__(self, organization=None):
        config = resolve_ai(organization)
        self.config = config
        self.name = config.provider.name if config else ""
        self.label = f"{config.provider.label} · {config.provider.model}" if config else ""

    def available(self, organization) -> bool:
        return self.config is not None

    def ask(self, ctx, run, question):
        data = ai.run_prompt(
            ctx,
            run,
            action="visibility",
            system=ANSWER_SYSTEM,
            prompt=f"<request>\n{ai.escape_tags(question)}\n</request>",
            schema=ANSWER_SCHEMA,
        )
        text = data.get("answer")
        if not isinstance(text, str) or not text.strip():
            raise ai.AIResponseError()
        sources = data.get("sources") if isinstance(data.get("sources"), list) else []
        return EngineAnswer(
            text=text.strip(), model=run.last_model or run.config.provider.model, urls=extract_urls(text, sources)
        )


# "anthropic" is the name this setting had when Anthropic was the only engine; it
# still enables the organization's connection, whichever provider that is.
ENGINES: dict[str, type[AnswerEngine]] = {"connected": ConnectedAIEngine, "anthropic": ConnectedAIEngine}


def enabled_engines(organization) -> list[AnswerEngine]:
    """Engines named in settings that this organization can use right now."""
    classes = dict.fromkeys(ENGINES[n] for n in settings.AI_VISIBILITY_ENGINES if n in ENGINES)
    engines = [cls(organization) for cls in classes]
    return [e for e in engines if e.available(organization)]
