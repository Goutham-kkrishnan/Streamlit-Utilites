"""Plotly chart builders.

Colours come from a validated, colour-blind-safe palette with separate light and
dark steps: one colour for single-series bars, an ordinal blue ramp for ordered
buckets (age, size), blue <-> red for shrink/growth, and fixed categorical slots
for the treemap's top-level folders.
"""
from __future__ import annotations

import os

import plotly.graph_objects as go

from .scanner import ScanResult
from .utils import display_path, human_size, unit_for

FONT = 'system-ui, -apple-system, "Segoe UI", Roboto, sans-serif'

PALETTES = {
    "light": {
        "surface": "#fcfcfb", "text": "#0b0b0b", "text2": "#52514e", "muted": "#898781",
        "grid": "#e1e0d9", "axis": "#c3c2b7", "free": "#e1e0d9", "other": "#b9b8ae",
        "series": ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"],
        # ordinal blue, low -> high
        "ordinal": ["#86b6ef", "#6da7ec", "#5598e7", "#3987e5", "#2a78d6", "#256abf", "#1c5cab", "#184f95"],
        "shrink": "#2a78d6", "grow": "#e34948",
    },
    "dark": {
        "surface": "#1a1a19", "text": "#f0efec", "text2": "#c3c2b7", "muted": "#898781",
        "grid": "#2c2c2a", "axis": "#383835", "free": "#383835", "other": "#5f5e59",
        "series": ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9", "#e66767"],
        "ordinal": ["#184f95", "#1c5cab", "#256abf", "#2a78d6", "#3987e5", "#5598e7", "#6da7ec", "#86b6ef"],
        "shrink": "#3987e5", "grow": "#e66767",
    },
}


def pal(mode: str) -> dict:
    return PALETTES.get(mode, PALETTES["light"])


def _ramp(mode: str, n: int) -> list[str]:
    steps = pal(mode)["ordinal"]
    if n <= 1:
        return [steps[4]]
    return [steps[round(i * (len(steps) - 1) / (n - 1))] for i in range(n)]


def _style(fig: go.Figure, mode: str, height: int, margin: dict | None = None) -> go.Figure:
    p = pal(mode)
    m = dict(l=8, r=16, t=16, b=8)
    m.update(margin or {})
    fig.update_layout(
        height=height,
        margin=m,
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(family=FONT, color=p["text2"], size=13),
        hoverlabel=dict(font=dict(family=FONT, size=13, color=p["text"]), bgcolor=p["surface"],
                        bordercolor=p["axis"]),
        barcornerradius=4,
    )
    axis = dict(automargin=True, gridcolor=p["grid"], gridwidth=1, zeroline=False, linecolor=p["axis"],
                title_font=dict(color=p["text2"], size=12))
    fig.update_xaxes(**axis)
    fig.update_yaxes(**axis)
    # Muted tick labels unless a chart already chose a stronger colour (e.g. category names).
    if fig.layout.xaxis.tickfont.color is None:
        fig.update_xaxes(tickfont=dict(color=p["muted"]))
    if fig.layout.yaxis.tickfont.color is None:
        fig.update_yaxes(tickfont=dict(color=p["muted"]))
    return fig


# --------------------------------------------------------------------------- bars

def ranked_bars(labels: list[str], values: list[int], mode: str, *, hover: list[str] | None = None,
                height: int | None = None) -> go.Figure:
    """Horizontal bars, largest on top, one colour, size label at each bar end."""
    p = pal(mode)
    div, unit = unit_for(max(values) if values else 1)
    fig = go.Figure(go.Bar(
        x=[v / div for v in values], y=[display_path(l) for l in labels], orientation="h",
        marker=dict(color=p["series"][0]),
        text=[human_size(v) for v in values], textposition="outside", cliponaxis=False,
        textfont=dict(color=p["text2"]),
        customdata=[[human_size(v), h] for v, h in zip(values, hover or [""] * len(values))],
        hovertemplate="<b>%{y}</b><br>%{customdata[0]}<br>%{customdata[1]}<extra></extra>",
    ))
    fig.update_yaxes(autorange="reversed", showgrid=False, ticks="",
                     tickfont=dict(color=p["text2"]))
    top = max(values) / div if values else 1
    fig.update_xaxes(title_text=unit, range=[0, top * 1.22])
    fig.update_layout(bargap=0.35, showlegend=False)
    return _style(fig, mode, height or max(180, 30 * len(labels) + 60))


def ordered_bars(labels: list[str], values: list[float], mode: str, *, as_bytes: bool = True,
                 extra: list[str] | None = None, height: int = 300) -> go.Figure:
    """Vertical bars for ordered buckets (age, size range) on an ordinal ramp."""
    p = pal(mode)
    if as_bytes:
        div, unit = unit_for(max(values) if values else 1)
        y = [v / div for v in values]
        text = [human_size(v) for v in values]
        ytitle = unit
    else:
        y, text, ytitle = values, [f"{int(v):,}" for v in values], "files"
    fig = go.Figure(go.Bar(
        x=labels, y=y, marker=dict(color=_ramp(mode, len(labels))),
        text=text, textposition="outside", cliponaxis=False, textfont=dict(color=p["text2"]),
        customdata=[[t, e] for t, e in zip(text, extra or [""] * len(labels))],
        hovertemplate="<b>%{x}</b><br>%{customdata[0]}<br>%{customdata[1]}<extra></extra>",
    ))
    fig.update_xaxes(showgrid=False, tickfont=dict(color=p["text2"]))
    fig.update_yaxes(title_text=ytitle, range=[0, (max(y) if y else 1) * 1.15])
    fig.update_layout(bargap=0.3, showlegend=False)
    return _style(fig, mode, height, margin=dict(t=24))


def change_bars(labels: list[str], deltas: list[int], mode: str) -> go.Figure:
    """Diverging bars: growth (red) vs freed space (blue)."""
    p = pal(mode)
    div, unit = unit_for(max((abs(d) for d in deltas), default=1))
    colors = [p["grow"] if d > 0 else p["shrink"] for d in deltas]
    fig = go.Figure(go.Bar(
        x=[d / div for d in deltas], y=[display_path(l) for l in labels], orientation="h",
        marker=dict(color=colors),
        text=[("+" if d > 0 else "") + human_size(d) for d in deltas], textposition="outside",
        cliponaxis=False, textfont=dict(color=p["text2"]),
        hovertemplate="<b>%{y}</b><br>%{text}<extra></extra>",
    ))
    fig.update_yaxes(autorange="reversed", showgrid=False, ticks="",
                     tickfont=dict(color=p["text2"]))
    lo = min([0.0] + [d / div for d in deltas])
    hi = max([0.0] + [d / div for d in deltas])
    pad = (hi - lo) * 0.25 or 1
    fig.update_xaxes(title_text=f"change ({unit})", range=[lo - (pad if lo < 0 else 0), hi + (pad if hi > 0 else 0)],
                     zeroline=True, zerolinecolor=p["axis"], zerolinewidth=1)
    fig.update_layout(bargap=0.35, showlegend=False)
    return _style(fig, mode, max(200, 30 * len(labels) + 70))


def partition_usage(parts: list[dict], mode: str) -> go.Figure:
    """Used / free share of each partition (100% bars, so small partitions stay readable)."""
    p = pal(mode)
    labels = [r["mountpoint"] for r in parts]
    used_pct = [100 * r["used"] / ((r["used"] + r["free"]) or 1) for r in parts]
    fig = go.Figure()
    fig.add_bar(name="Used", y=labels, x=used_pct, orientation="h",
                marker=dict(color=p["series"][0], line=dict(color=p["surface"], width=2)),
                text=[f"{u:.0f}% · {human_size(r['used'])}" for u, r in zip(used_pct, parts)],
                textposition="inside", insidetextanchor="start", textfont=dict(color="#ffffff"),
                customdata=[[human_size(r["used"]), human_size(r["total"])] for r in parts],
                hovertemplate="<b>%{y}</b><br>Used %{customdata[0]} of %{customdata[1]}<extra></extra>")
    fig.add_bar(name="Free", y=labels, x=[100 - u for u in used_pct], orientation="h",
                marker=dict(color=p["free"], line=dict(color=p["surface"], width=2)),
                text=[f"{human_size(r['free'])} free" for r in parts],
                textposition="inside", insidetextanchor="end", textfont=dict(color=p["text"]),
                customdata=[[human_size(r["free"])] for r in parts],
                hovertemplate="<b>%{y}</b><br>Free %{customdata[0]}<extra></extra>")
    fig.update_layout(barmode="stack", bargap=0.4, uniformtext=dict(minsize=11, mode="hide"),
                      showlegend=True, legend=dict(orientation="h", y=1.02, yanchor="bottom", x=0,
                                                   traceorder="normal", font=dict(color=p["text2"])))
    fig.update_yaxes(autorange="reversed", showgrid=False, ticks="", tickfont=dict(color=p["text"]))
    fig.update_xaxes(range=[0, 100], ticksuffix="%", dtick=25)
    return _style(fig, mode, max(170, 48 * len(parts) + 80), margin=dict(t=36))


def usage_trend(points: list[dict], mode: str) -> go.Figure:
    """Used space per mountpoint across saved snapshots (one line per partition, max 3 colours)."""
    p = pal(mode)
    by_mount: dict[str, list[dict]] = {}
    for pt in points:
        by_mount.setdefault(pt["mountpoint"], []).append(pt)
    mounts = sorted(by_mount, key=lambda m: -max(x["used"] for x in by_mount[m]))[:3]
    div, unit = unit_for(max((pt["used"] for pt in points), default=1))
    fig = go.Figure()
    for i, m in enumerate(mounts):
        pts = sorted(by_mount[m], key=lambda x: x["time"])
        fig.add_scatter(x=[x["time"] for x in pts], y=[x["used"] / div for x in pts], name=m,
                        mode="lines+markers", line=dict(color=p["series"][i], width=2),
                        marker=dict(size=8, line=dict(color=p["surface"], width=2)),
                        customdata=[human_size(x["used"]) for x in pts],
                        hovertemplate=f"<b>{m}</b><br>%{{x|%Y-%m-%d %H:%M}}<br>used %{{customdata}}<extra></extra>")
    fig.update_yaxes(title_text=f"used ({unit})", rangemode="tozero")
    fig.update_layout(hovermode="x unified", showlegend=True,
                      legend=dict(orientation="h", y=1.02, yanchor="bottom", x=0, font=dict(color=p["text2"])))
    return _style(fig, mode, 320, margin=dict(t=36))


# --------------------------------------------------------------------------- treemap / sunburst

def folder_map(res: ScanResult, top: str, metric: str, mode: str, *, depth: int = 3,
               kind: str = "treemap", min_frac: float = 0.004, height: int = 560) -> go.Figure:
    """Treemap or sunburst of `top`, colour-coded by top-level branch (8 fixed slots, rest grey)."""
    p = pal(mode)
    sizes, own = res.sizes(metric), res.own(metric)
    total = sizes.get(top, 0) or 1
    min_size = total * min_frac

    ids, labels, parents, values, colors, custom = [], [], [], [], [], []

    def add(node_id, label, parent, value, color, path, parent_value):
        ids.append(display_path(node_id))
        labels.append(display_path(label))
        parents.append(display_path(parent))
        values.append(value)
        colors.append(color)
        custom.append([human_size(value), f"{value / total:.1%}", display_path(path),
                       f"{value / (parent_value or 1):.1%}"])

    def walk(d, level, parent_id, color):
        if level >= depth:
            return
        shown = 0
        kids = sorted(res.children.get(d, []), key=lambda k: sizes.get(k, 0), reverse=True)
        for rank, k in enumerate(kids):
            v = sizes.get(k, 0)
            if v < min_size:
                break
            c = color
            if level == 0:
                c = p["series"][rank] if rank < len(p["series"]) else p["other"]
            add(k, os.path.basename(k), d, v, c, k, sizes.get(d, 0))
            walk(k, level + 1, k, c)
            shown += v
        files_here = own.get(d, 0)
        if files_here >= min_size:
            add(d.rstrip("/") + "//files", "files here", d, files_here, p["other"] if level == 0 else color,
                d + " (files directly here)", sizes.get(d, 0))
            shown += files_here
        rest = sizes.get(d, 0) - shown
        if rest > 0 and shown > 0:
            add(d.rstrip("/") + "//rest", "smaller items", d, rest, p["other"] if level == 0 else color,
                d + " (smaller items)", sizes.get(d, 0))

    add(top, os.path.basename(top) or top, "", sizes.get(top, 0), p["surface"], top, sizes.get(top, 0))
    walk(top, 0, top, p["other"])

    common = dict(
        ids=ids, labels=labels, parents=parents, values=values, branchvalues="total",
        marker=dict(colors=colors, line=dict(color=p["surface"], width=2)),
        customdata=custom,
        hovertemplate="<b>%{customdata[2]}</b><br>%{customdata[0]}<br>%{customdata[3]} of parent · "
                      "%{customdata[1]} of this view<extra></extra>",
        insidetextfont=dict(family=FONT),
    )
    if kind == "sunburst":
        trace = go.Sunburst(**common, texttemplate="%{label}", maxdepth=depth + 1, insidetextorientation="radial")
    else:
        trace = go.Treemap(**common, texttemplate="<b>%{label}</b><br>%{customdata[0]}", maxdepth=depth + 1,
                           pathbar=dict(visible=True, textfont=dict(family=FONT)), tiling=dict(pad=2))
    fig = go.Figure(trace)
    fig.update_layout(height=height, margin=dict(l=0, r=0, t=28 if kind == "treemap" else 0, b=0),
                      paper_bgcolor="rgba(0,0,0,0)", font=dict(family=FONT, color=p["text"]))
    return fig
