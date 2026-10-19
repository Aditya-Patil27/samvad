# OWNER: P4 -- see docs/WORK.md. Do not edit if you are not P4.
"""Three static chart forms, as SVG text, from plain numbers.

Stdlib only. CI installs `.[dev]` and nothing else, and a plotting dependency
for five report figures is the wrong trade: SVG is text, so a regenerated
figure diffs in review like any other file, and GitHub renders it inline.

The look is fixed here so every figure in the report reads as one system:
categorical hues in a fixed order (never cycled, never by rank), 2px lines,
markers with a surface ring, bars capped at 16px with a rounded data end and a
square baseline, hairline grid, and text in ink -- never in the series colour.
Dark mode is its own set of steps, not an inversion. Every mark carries a
`<title>`, so hovering a figure opened on its own shows the exact value.
"""
from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from html import escape

WIDTH = 720
FONT = 'system-ui, -apple-system, "Segoe UI", sans-serif'

#: Categorical slots 1-3, light then dark. Three is the cap: past it the
#: palette stops validating all-pairs, so a fourth series would be a facet.
_SERIES_LIGHT = ("#2a78d6", "#eb6834", "#1baf7a")
_SERIES_DARK = ("#3987e5", "#d95926", "#199e70")

_STYLE = f"""
svg {{ font-family: {FONT}; }}
.bg {{ fill: #fcfcfb; }}
.ink {{ fill: #0b0b0b; }} .ink2 {{ fill: #52514e; }} .muted {{ fill: #898781; }}
.grid {{ stroke: #e1e0d9; stroke-width: 1; }} .axis {{ stroke: #c3c2b7; stroke-width: 1; }}
{" ".join(f".s{i + 1} {{ fill: {c}; stroke: {c}; }}" for i, c in enumerate(_SERIES_LIGHT))}
@media (prefers-color-scheme: dark) {{
  .bg {{ fill: #1a1a19; }}
  .ink {{ fill: #f0efec; }} .ink2 {{ fill: #c3c2b7; }}
  .grid {{ stroke: #2c2c2a; }} .axis {{ stroke: #383835; }}
  {" ".join(f".s{i + 1} {{ fill: {c}; stroke: {c}; }}" for i, c in enumerate(_SERIES_DARK))}
}}
/* Mark shape after both colour blocks: same specificity, so order decides. */
.line {{ fill: none; stroke-width: 2; stroke-linejoin: round; stroke-linecap: round; }}
.bar {{ stroke: none; }}
.ring {{ stroke: #fcfcfb; stroke-width: 2; }}
.halo {{ paint-order: stroke; stroke: #fcfcfb; stroke-width: 4; stroke-linejoin: round; }}
@media (prefers-color-scheme: dark) {{ .ring, .halo {{ stroke: #1a1a19; }} }}
"""


@dataclass(frozen=True)
class Series:
    name: str
    values: Sequence[float | None]   # one per category / x point; None is not drawn


def _fmt(value: float) -> str:
    if value == 0:
        return "0"
    if abs(value) >= 1000:
        return f"{value / 1000:.3g}K" if abs(value) < 1e6 else f"{value / 1e6:.3g}M"
    return f"{value:.3g}"


def _nice_max(value: float) -> float:
    """Smallest 1/2/5 x 10^k at or above `value`, so ticks land on round numbers."""
    if value <= 0:
        return 1.0
    exp = 10 ** math.floor(math.log10(value))
    for step in (1, 2, 5, 10):
        if step * exp >= value:
            return step * exp
    return 10 * exp


def _text(x: float, y: float, s: str, cls: str = "ink2", size: int = 12,
          anchor: str = "start", weight: int = 400) -> str:
    return (f'<text x="{x:.1f}" y="{y:.1f}" class="{cls}" font-size="{size}" '
            f'text-anchor="{anchor}" font-weight="{weight}">{escape(s)}</text>')


def _frame(height: float, title: str, subtitle: str, caption: str, body: list[str],
           legend: Sequence[str] = ()) -> str:
    """Title, subtitle, legend row, plot body, then the provenance caption."""
    parts = [
        (f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{height:.0f}" '
         f'viewBox="0 0 {WIDTH} {height:.0f}" role="img" aria-label="{escape(title)}">'),
        f"<title>{escape(title)}</title><desc>{escape(subtitle.rstrip('.'))}. {escape(caption)}</desc>",
        f"<style>{_STYLE}</style>",
        f'<rect class="bg" width="{WIDTH}" height="{height:.0f}" rx="8"/>',
        _text(24, 32, title, "ink", 16, weight=600),
        _text(24, 52, subtitle, "ink2", 12),
    ]
    x = 24.0
    for i, name in enumerate(legend):
        parts.append(f'<rect class="s{i + 1} bar" x="{x:.1f}" y="64" width="10" height="10" rx="2"/>')
        parts.append(_text(x + 15, 73, name, "ink2", 12))
        x += 15 + 7.2 * len(name) + 18
    parts.extend(body)
    parts.append(_text(24, height - 14, caption, "muted", 11))
    parts.append("</svg>")
    return "\n".join(parts) + "\n"


def _hbar(x0: float, x1: float, y: float, h: float) -> str:
    """Horizontal bar path: square at the baseline, 4px round at the data end."""
    w = x1 - x0
    if w <= 0:
        return ""
    r = min(4.0, w / 2, h / 2)
    return (f"M{x0:.1f},{y:.1f} H{x1 - r:.1f} A{r},{r} 0 0 1 {x1:.1f},{y + r:.1f} "
            f"V{y + h - r:.1f} A{r},{r} 0 0 1 {x1 - r:.1f},{y + h:.1f} H{x0:.1f} Z")


def bar_chart(*, title: str, subtitle: str, caption: str, categories: Sequence[str],
              series: Sequence[Series], unit: Callable[[float], str] = _fmt,
              x_max: float | None = None, reference: tuple[float, str] | None = None) -> str:
    """Grouped horizontal bars from one zero baseline.

    `reference` draws one labelled vertical rule (e.g. 100% = the baseline
    condition), which is how two measures of different scale share one axis:
    index both to it, rather than give either its own.
    """
    if len(series) > len(_SERIES_LIGHT):
        raise ValueError(f"at most {len(_SERIES_LIGHT)} series; facet the rest")
    bar_h, gap, group_gap = 16.0, 2.0, 18.0
    label_w, left, right = 150.0, 24.0, 70.0
    top = 92.0 if len(series) > 1 else 72.0
    group_h = len(series) * bar_h + (len(series) - 1) * gap
    plot_h = len(categories) * group_h + (len(categories) - 1) * group_gap
    x0, x1 = left + label_w, WIDTH - right
    peak = max([v for s in series for v in s.values if v is not None] + [1e-12]
               + ([reference[0]] if reference else []))
    top_val = x_max if x_max is not None else _nice_max(peak)
    sx = lambda v: x0 + (x1 - x0) * v / top_val

    body: list[str] = []
    for i in range(5):
        v = top_val * i / 4
        body.append(f'<line class="grid" x1="{sx(v):.1f}" y1="{top - 6:.1f}" '
                    f'x2="{sx(v):.1f}" y2="{top + plot_h + 4:.1f}"/>')
        body.append(_text(sx(v), top + plot_h + 20, unit(v), "muted", 11, "middle"))
    if reference:
        rv, rlabel = reference
        body.append(f'<line class="axis" x1="{sx(rv):.1f}" y1="{top - 10:.1f}" '
                    f'x2="{sx(rv):.1f}" y2="{top + plot_h + 4:.1f}" stroke-width="2"/>')
        # Right of the rule when it fits, left of it when that would run off.
        fits = sx(rv) + 4 + 6.2 * len(rlabel) <= WIDTH - 12
        body.append(_text(sx(rv) + (4 if fits else -4), top - 12, rlabel, "ink2", 11,
                          "start" if fits else "end"))
    for c, category in enumerate(categories):
        gy = top + c * (group_h + group_gap)
        body.append(_text(x0 - 10, gy + group_h / 2 + 4, category, "ink", 12, "end"))
        for s_i, s in enumerate(series):
            v = s.values[c]
            if v is None:
                continue
            y = gy + s_i * (bar_h + gap)
            body.append(f'<path class="s{s_i + 1} bar" d="{_hbar(x0, sx(v), y, bar_h)}">'
                        f"<title>{escape(f'{category} · {s.name}: {unit(v)}')}</title></path>")
            # Haloed: a value near the reference rule reads over it, not through it.
            body.append(_text(sx(v) + 6, y + bar_h - 4, unit(v), "ink2 halo", 11))
    body.append(f'<line class="axis" x1="{x0:.1f}" y1="{top - 6:.1f}" '
                f'x2="{x0:.1f}" y2="{top + plot_h + 4:.1f}"/>')
    height = top + plot_h + 56
    legend = [s.name for s in series] if len(series) > 1 else ()
    return _frame(height, title, subtitle, caption, body, legend)


def line_chart(*, title: str, subtitle: str, caption: str, x: Sequence[float],
               series: Sequence[Series], x_label: str, unit: Callable[[float], str] = _fmt) -> str:
    """Lines with a marker on every measured point, labelled at the right end.

    Every point is drawn as a marker because these are a handful of measured
    values, not a sampled curve: the eye must not read between them.
    """
    if len(series) > len(_SERIES_LIGHT):
        raise ValueError(f"at most {len(_SERIES_LIGHT)} series; facet the rest")
    left, right, top, plot_h = 64.0, 110.0, 96.0, 260.0
    x0, x1 = left, WIDTH - right
    lo, hi = min(x), max(x)
    peak = max([v for s in series for v in s.values if v is not None] + [1e-12])
    top_val = _nice_max(peak)
    sx = lambda v: x0 + (x1 - x0) * ((v - lo) / (hi - lo) if hi > lo else 0.5)
    sy = lambda v: top + plot_h - plot_h * v / top_val

    body: list[str] = []
    for i in range(5):
        v = top_val * i / 4
        body.append(f'<line class="grid" x1="{x0:.1f}" y1="{sy(v):.1f}" x2="{x1:.1f}" y2="{sy(v):.1f}"/>')
        body.append(_text(x0 - 8, sy(v) + 4, unit(v), "muted", 11, "end"))
    for xv in x:
        body.append(_text(sx(xv), top + plot_h + 20, _fmt(xv), "muted", 11, "middle"))
    body.append(_text((x0 + x1) / 2, top + plot_h + 40, x_label, "ink2", 12, "middle"))
    body.append(f'<line class="axis" x1="{x0:.1f}" y1="{sy(0):.1f}" x2="{x1:.1f}" y2="{sy(0):.1f}"/>')

    # Series that share a point share a marker: the last one drawn hides the rest.
    meet: dict[tuple[int, float], list[str]] = {}
    for s in series:
        for j, v in enumerate(s.values):
            if v is not None:
                meet.setdefault((j, v), []).append(s.name)

    ends: list[tuple[float, str]] = []
    for s_i, s in enumerate(series):
        pts = [(sx(xv), sy(v), xv, v, j) for j, (xv, v) in enumerate(zip(x, s.values)) if v is not None]
        if not pts:
            continue
        d = " ".join(f"{'M' if k == 0 else 'L'}{px:.1f},{py:.1f}" for k, (px, py, *_) in enumerate(pts))
        body.append(f'<path class="s{s_i + 1} line" d="{d}"/>')
        for px, py, xv, v, j in pts:
            who = " = ".join(meet[(j, v)])
            body.append(f'<circle class="s{s_i + 1} ring" cx="{px:.1f}" cy="{py:.1f}" r="4.5">'
                        f"<title>{escape(f'{who}, {x_label} {_fmt(xv)}: {unit(v)}')}</title></circle>")
        ends.append((pts[-1][1], f"{s.name}  {unit(pts[-1][3])}"))

    # Name a shared point where it is, on the side its lines leave clear. The
    # last x is left to the end labels, which already name every series.
    for (j, v), names in meet.items():
        if len(names) < 2 or j == len(x) - 1:
            continue
        k = j + 1 if j == 0 else j - 1  # the neighbouring point the lines run to
        others = [s.values[k] for s in series if s.name in names and s.values[k] is not None]
        if j == 0:
            dy = 18 if all(o >= v for o in others) else -10
            body.append(_text(sx(x[j]) + 8, sy(v) + dy, " = ".join(names), "muted halo", 11))
        else:
            dy = -10 if all(o <= v for o in others) else 18
            body.append(_text(sx(x[j]) - 8, sy(v) + dy, " = ".join(names), "muted halo", 11, "end"))
    # End labels never stack: push apart to a 14px minimum, top down.
    ends.sort()
    placed: list[float] = []
    for y, label in ends:
        y = max(y, placed[-1] + 14) if placed else y
        placed.append(y)
        body.append(_text(x1 + 10, y + 4, label, "ink2", 12))
    return _frame(top + plot_h + 74, title, subtitle, caption, body, [s.name for s in series])


def range_chart(*, title: str, subtitle: str, caption: str, categories: Sequence[str],
                low: Series, high: Series, unit: Callable[[float], str] = _fmt) -> str:
    """Median-to-p95 per row on a log axis: a dot at each end, a rule between.

    Log because the conditions differ by orders of magnitude and that ratio is
    the result. Dots, not bars: a bar's length on a log axis means nothing.
    """
    left, right, top = 174.0, 60.0, 96.0
    row_h = 34.0
    plot_h = row_h * len(categories)
    x0, x1 = left, WIDTH - right
    vals = [v for s in (low, high) for v in s.values if v is not None and v > 0]
    lo_dec = math.floor(math.log10(min(vals)))
    hi_dec = math.ceil(math.log10(max(vals)))
    if hi_dec == lo_dec:
        hi_dec += 1
    sx = lambda v: x0 + (x1 - x0) * (math.log10(v) - lo_dec) / (hi_dec - lo_dec)

    body: list[str] = []
    for d in range(lo_dec, hi_dec + 1):
        v = 10.0 ** d
        body.append(f'<line class="grid" x1="{sx(v):.1f}" y1="{top - 6:.1f}" '
                    f'x2="{sx(v):.1f}" y2="{top + plot_h:.1f}"/>')
        body.append(_text(sx(v), top + plot_h + 18, unit(v), "muted", 11, "middle"))
    for c, category in enumerate(categories):
        cy = top + c * row_h + row_h / 2
        body.append(_text(x0 - 12, cy + 4, category, "ink", 12, "end"))
        a, b = low.values[c], high.values[c]
        if a and b:
            body.append(f'<line class="axis" x1="{sx(a):.1f}" y1="{cy:.1f}" '
                        f'x2="{sx(b):.1f}" y2="{cy:.1f}" stroke-width="2"/>')
        for s_i, (s, v) in enumerate(((low, a), (high, b))):
            if not v:
                continue
            body.append(f'<circle class="s{s_i + 1} ring" cx="{sx(v):.1f}" cy="{cy:.1f}" r="5">'
                        f"<title>{escape(f'{category} · {s.name}: {unit(v)}')}</title></circle>")
        # Values ride above their dots; when the two sit too close to read
        # apart, the median keeps its label and the p95 is left to its title.
        if a:
            body.append(_text(sx(a), cy - 10, unit(a), "ink2", 11, "middle"))
        if b and (not a or sx(b) - sx(a) > 44):
            body.append(_text(sx(b), cy - 10, unit(b), "ink2", 11, "middle"))
    return _frame(top + plot_h + 50, title, subtitle, caption, body, [low.name, high.name])
