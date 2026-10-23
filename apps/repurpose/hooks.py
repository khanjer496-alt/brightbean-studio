"""Extension points for policy that lives outside the pipeline."""

from __future__ import annotations


def before_accept(job, outputs, user) -> None:
    """Called before reviewed outputs become drafts or ideas; raise ``ValueError`` to refuse.

    This is where the autonomy policy plugs in once ``apps.autonomy`` is part of
    the build: evaluate its gate for ``user`` (or the API key behind ``job.origin``)
    and record the decision in its audit trail. Without it, a person accepting
    outputs is the review step, and everything accepted is only ever a draft or
    an idea. Publishing is never decided here.
    """
