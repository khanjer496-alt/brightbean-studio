"""Static SVG charts for reports.

One set of charts serves the web page, the PDF and the emailed copy, so they are
plain SVG with attributes only (no CSS, no script): the PDF builder embeds the
same string the browser shows. Colours are passed in; text stays in neutral ink,
and the previous period is told apart by a dashed line as well as by colour.
"""

from __future__ import annotations

from datetime import date
from xml.sax.saxutils import escape

INK = "#1f2937"
INK_MUTED = "#6b7280"
GRID = "#e5e7eb"
PREVIOUS = "#6b7280"
WIDTH = 640
HEIGHT = 190
PAD_LEFT = 46
PAD_RIGHT = 12
PAD_TOP = 12
PAD_BOTTOM = 26


def _trim(number: float) -> str:
    """One decimal at most, and none when it would be ``.0`` ("2.5", "2", not "2.0")."""
    return f"{number:.1f}".rstrip("0").rstrip(".")


def compact(value: float) -> str:
    if abs(value) >= 1_000_000:
        return f"{_trim(value / 1_000_000)}M"
    if abs(value) >= 1_000:
        return f"{_trim(value / 1_000)}k"
    # Gridlines can fall on halves (a top of 1 or 5): label them as such, never rounded away.
    return _trim(value)


def _nice_max(value: float) -> float:
    """Round up to 1, 2, 2.5 or 5 x 10^n so gridlines land on tidy numbers."""
    if value <= 0:
        return 1.0
    magnitude = 10 ** (len(str(int(value))) - 1) if value >= 1 else 1
    for step in (1, 2, 2.5, 5, 10):
        if value <= step * magnitude:
            return step * magnitude
    return 10 * magnitude


def _segments(values: list[float | None], x_at, y_at) -> list[str]:
    """``M x y L x y …`` path pieces, split wherever a day has no value."""
    out: list[str] = []
    current: list[str] = []
    for i, value in enumerate(values):
        if value is None:
            if current:
                out.append(" ".join(current))
                current = []
            continue
        current.append(f"{'L' if current else 'M'}{x_at(i):.1f} {y_at(value):.1f}")
    if current:
        out.append(" ".join(current))
    return out


def _frame(title: str, description: str, body: str) -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {WIDTH} {HEIGHT}" width="{WIDTH}" height="{HEIGHT}" '
        f'role="img" aria-label="{escape(title, {chr(34): "&quot;"})}">'
        f"<title>{escape(title)}</title><desc>{escape(description)}</desc>{body}</svg>"
    )


def _axes(top: float, labels: list[str], x_at=None) -> str:
    """Gridlines with their values, and the first, middle and last day labels.

    ``x_at(i)`` places day ``i`` (a column's centre); by default days run edge to edge, as a line's points do.
    """
    plot_h = HEIGHT - PAD_TOP - PAD_BOTTOM
    parts = []
    for i in range(3):
        frac = i / 2
        y = PAD_TOP + plot_h * (1 - frac)
        parts.append(
            f'<line x1="{PAD_LEFT}" y1="{y:.1f}" x2="{WIDTH - PAD_RIGHT}" y2="{y:.1f}" stroke="{GRID}" stroke-width="1"/>'
        )
        parts.append(
            f'<text x="{PAD_LEFT - 6}" y="{y + 3.5:.1f}" text-anchor="end" font-size="10" fill="{INK_MUTED}" '
            f'font-family="Helvetica, Arial, sans-serif">{compact(top * frac)}</text>'
        )
    n = len(labels)
    if n:
        plot_w = WIDTH - PAD_LEFT - PAD_RIGHT
        for idx in sorted({0, n // 2, n - 1}):
            if x_at is not None:
                x, anchor = x_at(idx), "middle"
            else:
                x = PAD_LEFT + (plot_w * idx / (n - 1) if n > 1 else plot_w / 2)
                anchor = "start" if idx == 0 and n > 1 else "end" if idx == n - 1 and n > 1 else "middle"
            parts.append(
                f'<text x="{x:.1f}" y="{HEIGHT - 8}" text-anchor="{anchor}" font-size="10" fill="{INK_MUTED}" '
                f'font-family="Helvetica, Arial, sans-serif">{escape(labels[idx])}</text>'
            )
    return "".join(parts)


def _short(day_iso: str) -> str:
    d = date.fromisoformat(day_iso)
    return f"{d:%b} {d.day}"


def line_chart(title: str, labels: list[str], current: list[float | None], previous: list[float | None], accent: str):
    """Daily line for this period, dashed grey for the previous one aligned by day."""
    known = [v for v in (*current, *previous) if v is not None]
    top = _nice_max(max(known, default=0))
    plot_w, plot_h = WIDTH - PAD_LEFT - PAD_RIGHT, HEIGHT - PAD_TOP - PAD_BOTTOM
    n = len(labels)

    def x_at(i: int) -> float:
        return PAD_LEFT + (plot_w * i / (n - 1) if n > 1 else plot_w / 2)

    def y_at(v: float) -> float:
        return PAD_TOP + plot_h * (1 - v / top)

    body = [_axes(top, [_short(d) for d in labels])]
    if previous and any(v is not None for v in previous):
        for seg in _segments(previous, x_at, y_at):
            body.append(
                f'<path d="{seg}" fill="none" stroke="{PREVIOUS}" stroke-width="2" stroke-dasharray="5 4" '
                f'stroke-linecap="round" stroke-linejoin="round"/>'
            )
    for seg in _segments(current, x_at, y_at):
        body.append(
            f'<path d="{seg}" fill="none" stroke="{accent}" stroke-width="2.5" stroke-linecap="round" '
            f'stroke-linejoin="round"/>'
        )
    # A single reported day has no line to draw, so it gets a marker.
    reported = [i for i, v in enumerate(current) if v is not None]
    if len(reported) == 1:
        i = reported[0]
        body.append(
            f'<circle cx="{x_at(i):.1f}" cy="{y_at(current[i]):.1f}" r="4" fill="{accent}" stroke="#ffffff" stroke-width="2"/>'
        )
    total = sum(v for v in current if v is not None)
    description = (
        f"Daily values from {_short(labels[0])} to {_short(labels[-1])}; total {compact(total)}." if labels else ""
    )
    return _frame(title, description, "".join(body))


def column_chart(title: str, labels: list[str], values: list[float], accent: str):
    """One column per day (posts published)."""
    top = _nice_max(max(values, default=0))
    plot_w, plot_h = WIDTH - PAD_LEFT - PAD_RIGHT, HEIGHT - PAD_TOP - PAD_BOTTOM
    n = max(len(values), 1)
    slot = plot_w / n
    bar_w = max(min(slot - 2, 22), 1)
    body = [_axes(top, [_short(d) for d in labels], lambda i: PAD_LEFT + slot * (i + 0.5))]
    for i, value in enumerate(values):
        if value <= 0:
            continue
        h = plot_h * value / top
        x = PAD_LEFT + slot * i + (slot - bar_w) / 2
        body.append(
            f'<rect x="{x:.1f}" y="{PAD_TOP + plot_h - h:.1f}" width="{bar_w:.1f}" height="{h:.1f}" fill="{accent}"/>'
        )
    description = f"{round(sum(values))} posts across {len(values)} days."
    return _frame(title, description, "".join(body))
