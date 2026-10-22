"""Tiny server-side SVG charts (single series, one y-axis) with hover hit targets.

Colors come from CSS custom properties in dash.css, so light/dark both work.
Hover: each data point gets an invisible full-height hit rect carrying
data-tip; static/dash.js shows the tooltip and crosshair.
"""
from markupsafe import Markup, escape


def _nice_max(v):
    if v <= 0:
        return 1
    mag = 10 ** (len(str(int(v))) - 1)
    for step in (1, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10):
        if v <= step * mag:
            return step * mag
    return 10 * mag


def money_short(v):
    a = abs(v)
    sign = "-" if v < 0 else ""
    if a >= 1_000_000:
        return f"{sign}${a / 1_000_000:.1f}M"
    if a >= 1_000:
        return f"{sign}${a / 1_000:.1f}K"
    return f"{sign}${a:,.0f}"


def _tick_count(y_max):
    for n in (5, 4, 6, 3):
        step = y_max / n
        mag = 10 ** (len(str(int(step))) - 1) if step >= 1 else 1
        if step == int(step) and step / mag in (1, 2, 2.5, 5):
            return n
    return 4


def _frame(width, height, pad, y_max):
    left, right, top, bottom = pad
    ticks = _tick_count(y_max)
    plot_h = height - top - bottom
    parts = []
    for i in range(ticks + 1):
        v = y_max * i / ticks
        y = top + plot_h - plot_h * i / ticks
        parts.append(f'<line class="grid" x1="{left}" x2="{width - right}" y1="{y:.1f}" y2="{y:.1f}"/>')
        parts.append(f'<text class="axis" x="{left - 8}" y="{y + 4:.1f}" text-anchor="end">'
                     f'{escape(money_short(v))}</text>')
    return parts


def line_chart(points, label, width=1100, height=300):
    """points: list of (x_label, value, tooltip). Single cumulative series."""
    if not points:
        return Markup('<p class="muted">No data yet.</p>')
    pad = (64, 16, 16, 28)
    left, right, top, bottom = pad
    values = [p[1] for p in points]
    y_max = _nice_max(max(max(values), 0))
    plot_w, plot_h = width - left - right, height - top - bottom
    step = plot_w / max(len(points) - 1, 1)

    def xy(i, v):
        return left + i * step, top + plot_h - plot_h * max(v, 0) / y_max

    parts = _frame(width, height, pad, y_max)
    coords = [xy(i, p[1]) for i, p in enumerate(points)]
    path = " ".join(("M" if i == 0 else "L") + f"{x:.1f},{y:.1f}" for i, (x, y) in enumerate(coords))
    parts.append(f'<path class="series-line" d="{path}"/>')
    # sparse x labels: first, middle, last
    for i in sorted({0, len(points) // 2, len(points) - 1}):
        x, _ = coords[i]
        parts.append(f'<text class="axis" x="{x:.1f}" y="{height - 8}" text-anchor="middle">'
                     f'{escape(points[i][0])}</text>')
    last_x, last_y = coords[-1]
    parts.append(f'<circle class="series-dot" cx="{last_x:.1f}" cy="{last_y:.1f}" r="4"/>')
    for i, ((x, y), p) in enumerate(zip(coords, points)):
        parts.append(f'<rect class="hit" x="{x - step / 2:.1f}" y="{top}" width="{max(step, 6):.1f}" '
                     f'height="{plot_h}" data-x="{x:.1f}" data-y="{y:.1f}" data-tip="{escape(p[2])}"/>')
    return _svg(width, height, label, parts)


def bar_chart(bars, label, width=1100, height=260, highlight_last=True):
    """bars: list of (x_label, value, tooltip)."""
    if not bars:
        return Markup('<p class="muted">No data yet.</p>')
    pad = (64, 16, 16, 28)
    left, right, top, bottom = pad
    y_max = _nice_max(max(max(b[1] for b in bars), 0))
    plot_w, plot_h = width - left - right, height - top - bottom
    slot = plot_w / len(bars)
    bar_w = min(slot - 2, max(slot * 0.62, 4))
    parts = _frame(width, height, pad, y_max)
    base = top + plot_h
    for i, (xl, v, tip) in enumerate(bars):
        h = plot_h * max(v, 0) / y_max
        x = left + i * slot + (slot - bar_w) / 2
        r = min(4, bar_w / 2, h)
        cls = "series-bar" + (" current" if highlight_last and i == len(bars) - 1 else "")
        if h > 0:
            parts.append(
                f'<path class="{cls}" d="M{x:.1f},{base:.1f} V{base - h + r:.1f} '
                f'Q{x:.1f},{base - h:.1f} {x + r:.1f},{base - h:.1f} H{x + bar_w - r:.1f} '
                f'Q{x + bar_w:.1f},{base - h:.1f} {x + bar_w:.1f},{base - h + r:.1f} V{base:.1f} Z"/>')
        parts.append(f'<text class="axis" x="{x + bar_w / 2:.1f}" y="{height - 8}" '
                     f'text-anchor="middle">{escape(xl)}</text>')
        parts.append(f'<rect class="hit" x="{left + i * slot:.1f}" y="{top}" width="{slot:.1f}" '
                     f'height="{plot_h}" data-x="{x + bar_w / 2:.1f}" data-y="{base - h:.1f}" '
                     f'data-tip="{escape(tip)}" data-nocross="1"/>')
    return _svg(width, height, label, parts)


def _svg(width, height, label, parts):
    return Markup(
        f'<div class="chart"><svg viewBox="0 0 {width} {height}" role="img" '
        f'aria-label="{escape(label)}">'
        f'<line class="crosshair" x1="0" x2="0" y1="16" y2="{height - 28}" visibility="hidden"/>'
        + "".join(parts) + '</svg><div class="tip" hidden></div></div>'
    )
