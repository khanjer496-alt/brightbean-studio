"""Template helpers for the approval-workflow UI."""

from django import template

register = template.Library()


# Presentation metadata per editorial status (dot / background / foreground /
# border + label). Kept here so every approval surface (publish tab, composer
# panel, client portal) renders an identical badge from one source. Colours are
# the theme tokens, matching the calendar's status legend, so the pill sits on
# the dark surfaces instead of carrying light-theme tints.
def _tone(dot, soft, text, label):
    return {"label": label, "dot": dot, "bg": soft, "fg": text, "bd": f"color-mix(in srgb, {dot} 35%, transparent)"}


_WAITING = ("var(--warning-500)", "var(--warning-50)", "var(--warning-700)")
_BAD = ("var(--error-500)", "var(--error-50)", "var(--error-700)")

STATUS_META = {
    "draft": _tone("var(--text-tertiary)", "var(--surface-2)", "var(--text-secondary)", "Draft"),
    "pending_review": _tone(*_WAITING, "Pending Review"),
    "pending_client": _tone(*_WAITING, "Pending Client"),
    "approved": _tone("var(--accent-teal)", "var(--accent-teal-soft)", "var(--accent-teal)", "Approved"),
    "changes_requested": _tone(*_WAITING, "Changes Requested"),
    "rejected": _tone(*_BAD, "Rejected"),
    "scheduled": _tone("var(--info-500)", "var(--info-50)", "var(--info-700)", "Scheduled"),
    "publishing": _tone("var(--accent-indigo)", "var(--accent-indigo-soft)", "var(--accent-indigo)", "Publishing"),
    "published": _tone("var(--success-500)", "var(--success-50)", "var(--success-700)", "Published"),
    "partially_published": _tone(
        "var(--accent-amber)", "var(--accent-amber-soft)", "var(--accent-amber)", "Partially Published"
    ),
    "failed": _tone(*_BAD, "Failed"),
    "on_hold": _tone("var(--primary)", "var(--primary-soft)", "var(--text-brand)", "Hold Requested"),
}


@register.inclusion_tag("approvals/partials/_status_badge.html")
def status_badge(status):
    """Render the editorial-status pill (dot + label) for *status*."""
    return {"meta": STATUS_META.get(status, STATUS_META["draft"])}
