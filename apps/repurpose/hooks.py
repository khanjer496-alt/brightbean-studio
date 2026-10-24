"""The autonomy policy for repurposing: who may turn a source into drafts, and the trail it leaves.

People are the review step and are never held back here. An agent (an API key
or an MCP session) goes through the workspace's autonomy policy for the
*draft* action, the same gate ``POST /posts`` uses for a draft:

* ``before_create``: before a run spends AI requests, drafting must not be Off
  for any target account (nor crisis pause on). A refusal is logged.
* ``before_accept``: each chosen output is checked, its text included so a
  blocked phrase is caught, before it becomes a draft, idea or hashtag set.
* ``record_decisions``: each decision goes in the audit trail (linked to the
  draft, when there is one) and the agent gets back the most restrictive
  notice, e.g. "Held for review because ...".

Publishing is never decided here: accepted outputs are only ever drafts.
``before_*`` raise :class:`apps.autonomy.gate.AutonomyRefusalError`, already logged.
"""

from __future__ import annotations

from apps.autonomy.gate import PostGate, content_text, gate_post

_ORDER = {"blocked": 0, "held": 1, "executed": 2}


def _gate(workspace, actor, account, text: str, *, operation: str, origin: str) -> PostGate:
    return gate_post(
        workspace=workspace,
        actor=actor,
        account=account,
        text=text,
        schedule=False,
        operation=operation,
        source="mcp" if origin == "mcp" else "api",
    )


def before_create(workspace, actor, accounts, *, origin: str) -> list[tuple[None, PostGate]]:
    """Gate an agent's new run once per target account (once overall for account-less outputs)."""
    if actor is None or actor.is_human:
        return []
    pairs = [
        (None, _gate(workspace, actor, account, "", operation="repurpose.create", origin=origin))
        for account in (accounts or [None])
    ]
    for _, gate in pairs:
        if gate.blocked:
            raise gate.refuse()
    return pairs


def output_text(out) -> str:
    return content_text(out.title, out.text, *(str(p) for p in out.parts or ()))


def before_accept(job, outputs, actor) -> list[tuple[object, PostGate]]:
    """Gate each output an agent accepts, on the text it would become."""
    if actor is None or actor.is_human:
        return []
    pairs = [
        (
            out,
            _gate(
                job.workspace,
                actor,
                out.social_account,
                output_text(out),
                operation="repurpose.accept",
                origin=job.origin,
            ),
        )
        for out in outputs
    ]
    for _, gate in pairs:
        if gate.blocked:
            raise gate.refuse()
    return pairs


def record_decisions(pairs, *, source_text: str = "") -> dict | None:
    """Write each decision to the trail and return the most restrictive notice for the agent."""
    notes = []
    for out, gate in pairs:
        _, note = gate.record_with_notice(post=getattr(out, "post", None), source_text=source_text)
        notes.append(note)
    return min(notes, key=lambda n: _ORDER[n["decision"]]) if notes else None
