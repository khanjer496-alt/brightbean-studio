"""A stand-in for the Anthropic SDK client, so no test ever reaches the API.

``FakeAnthropic`` replaces ``anthropic.Anthropic``; queue the JSON objects
the next calls should answer with, then read back what each call sent.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch


def message(data=None, *, stop_reason="end_turn", text=None, input_tokens=120, output_tokens=40):
    """A Messages API response carrying ``data`` as its JSON text block."""
    return SimpleNamespace(
        stop_reason=stop_reason,
        model="claude-opus-5-5",
        content=[SimpleNamespace(type="text", text=text if text is not None else json.dumps(data or {}))],
        usage=SimpleNamespace(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
        ),
    )


class FakeAnthropic:
    """Records every client the code builds and every request it sends."""

    def __init__(self):
        self.responses: list = []
        self.calls: list[dict] = []
        self.clients: list[dict] = []

    def queue(self, *items):
        """Each item is a dict (answered as JSON), a response, or an exception to raise."""
        self.responses.extend(items)
        return self

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        if not self.responses:
            raise AssertionError("FakeAnthropic: unexpected API call (nothing queued)")
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return message(item) if isinstance(item, dict) else item

    def __call__(self, **client_kwargs):
        self.clients.append(client_kwargs)
        return SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(create=self._create)))

    @property
    def prompts(self) -> list[str]:
        return [call["messages"][0]["content"] for call in self.calls]

    @property
    def systems(self) -> list[str]:
        return [call["system"][0]["text"] for call in self.calls]


def patch_anthropic():
    """Context manager: ``with patch_anthropic() as fake: ...``."""
    fake = FakeAnthropic()
    return _Patched(fake)


class _Patched:
    def __init__(self, fake):
        self.fake = fake
        self._patch = patch("anthropic.Anthropic", fake)

    def __enter__(self):
        self._patch.start()
        return self.fake

    def __exit__(self, *exc):
        self._patch.stop()
        return False
