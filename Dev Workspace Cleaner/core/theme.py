"""Chart styling shared by every app: a colour-blind-safe palette with light and dark steps."""
from __future__ import annotations

import plotly.graph_objects as go

from .utils import human_size, unit_for

FONT = 'system-ui, -apple-system, "Segoe UI", Roboto, sans-serif'

PALETTES = {
    "light": {
        "surface": "#fcfcfb", "text": "#0b0b0b", "text2": "#52514e", "muted": "#898781",
        "grid": "#e1e0d9", "axis": "#c3c2b7", "free": "#e1e0d9", "other": "#b9b8ae",
        "series": ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"],
        "ordinal": ["#86b6ef", "#6da7ec", "#5598e7", "#3987e5", "#2a78d6", "#256abf", "#1c5cab", "#184f95"],
        "good": "#1baf7a", "warn": "#eda100", "bad": "#e34948",
    },
    "dark": {
        "surface": "#1a1a19", "text": "#f0efec", "text2": "#c3c2b7", "muted": "#898781",
        "grid": "#2c2c2a", "axis": "#383835", "free": "#383835", "other": "#5f5e59",
        "series": ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9", "#e66767"],
        "ordinal": ["#184f95", "#1c5cab", "#256abf", "#2a78d6", "#3987e5", "#5598e7", "#6da7ec", "#86b6ef"],
        "good": "#199e70", "warn": "#c98500", "bad": "#e66767",
    },
}


def pal(mode: str) -> dict:
    return PALETTES.get(mode, PALETTES["light"])


def ramp(mode: str, n: int) -> list[str]:
    """n steps of the ordinal blue ramp, low -> high."""
    steps = pal(mode)["ordinal"]
    if n <= 1:
        return [steps[4]]
    return [steps[round(i * (len(steps) - 1) / (n - 1))] for i in range(n)]


def style(fig: go.Figure, mode: str, height: int, margin: dict | None = None) -> go.Figure:
    """Apply the shared look (transparent background, muted grid, system font)."""
    p = pal(mode)
    m = dict(l=8, r=16, t=16, b=8)
    m.update(margin or {})
    fig.update_layout(
        height=height, margin=m, paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        font=dict(family=FONT, color=p["text2"], size=13),
        hoverlabel=dict(font=dict(family=FONT, size=13, color=p["text"]), bgcolor=p["surface"], bordercolor=p["axis"]),
        barcornerradius=4,
    )
    axis = dict(automargin=True, gridcolor=p["grid"], gridwidth=1, zeroline=False, linecolor=p["axis"],
                title_font=dict(color=p["text2"], size=12))
    fig.update_xaxes(**axis)
    fig.update_yaxes(**axis)
    if fig.layout.xaxis.tickfont.color is None:
        fig.update_xaxes(tickfont=dict(color=p["muted"]))
    if fig.layout.yaxis.tickfont.color is None:
        fig.update_yaxes(tickfont=dict(color=p["muted"]))
    return fig


def ranked_bars(labels: list[str], values: list[float], mode: str, *, as_bytes: bool = True,
                hover: list[str] | None = None, unit: str = "", height: int | None = None) -> go.Figure:
    """Horizontal bars, largest on top, one colour, value label at each bar end."""
    p = pal(mode)
    if as_bytes:
        div, unit = unit_for(max(values) if values else 1)
        text = [human_size(v) for v in values]
    else:
        div = 1
        text = [f"{v:,.0f}{(' ' + unit) if unit else ''}" if float(v).is_integer() else f"{v:,.1f} {unit}".strip()
                for v in values]
    fig = go.Figure(go.Bar(
        x=[v / div for v in values], y=labels, orientation="h", marker=dict(color=p["series"][0]),
        text=text, textposition="outside", cliponaxis=False, textfont=dict(color=p["text2"]),
        customdata=[[t, h] for t, h in zip(text, hover or [""] * len(values))],
        hovertemplate="<b>%{y}</b><br>%{customdata[0]}<br>%{customdata[1]}<extra></extra>",
    ))
    fig.update_yaxes(autorange="reversed", showgrid=False, ticks="", tickfont=dict(color=p["text2"]))
    top = max(values) / div if values else 1
    fig.update_xaxes(title_text=unit, range=[0, (top or 1) * 1.22])
    fig.update_layout(bargap=0.35, showlegend=False)
    return style(fig, mode, height or max(180, 30 * len(labels) + 60))


def time_lines(series: dict[str, tuple[list, list]], mode: str, *, y_title: str = "", height: int = 300,
               y_range: list | None = None) -> go.Figure:
    """One line per series: {name: (times, values)}. Up to 8 fixed colours."""
    p = pal(mode)
    fig = go.Figure()
    for i, (name, (xs, ys)) in enumerate(series.items()):
        fig.add_scatter(x=xs, y=ys, name=name, mode="lines", line=dict(color=p["series"][i % 8], width=2),
                        hovertemplate=f"<b>{name}</b> %{{y:.1f}}<extra></extra>")
    fig.update_yaxes(title_text=y_title, rangemode="tozero", range=y_range)
    fig.update_layout(hovermode="x unified", showlegend=len(series) > 1,
                      legend=dict(orientation="h", y=1.02, yanchor="bottom", x=0, font=dict(color=p["text2"])))
    return style(fig, mode, height, margin=dict(t=36 if len(series) > 1 else 16))
