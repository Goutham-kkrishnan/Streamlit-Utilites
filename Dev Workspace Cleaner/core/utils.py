"""Formatting helpers. Sizes use decimal units (1 GB = 10^9 bytes), like Ubuntu's Files app."""
from __future__ import annotations

from datetime import datetime

KB = 1000
MB = 1000**2
GB = 1000**3
DAY = 86400

_UNITS = ("B", "KB", "MB", "GB", "TB", "PB")


def human_size(n: float | int | None) -> str:
    """Format a byte count, e.g. 5_700_000_000 -> '5.7 GB'."""
    if n is None:
        return "—"
    value = float(n)
    sign = "-" if value < 0 else ""
    value = abs(value)
    for unit in _UNITS:
        if value < 1000 or unit == _UNITS[-1]:
            return f"{sign}{value:.0f} B" if unit == "B" else f"{sign}{value:.1f} {unit}"
        value /= 1000
    return f"{sign}{value:.1f} PB"  # unreachable


def unit_for(max_bytes: float) -> tuple[float, str]:
    """One unit for a chart axis so every tick uses the same scale."""
    for divisor, unit in ((1000**4, "TB"), (GB, "GB"), (MB, "MB"), (KB, "KB")):
        if max_bytes >= divisor:
            return divisor, unit
    return 1, "B"


def human_duration(seconds: float | None) -> str:
    """e.g. 3725 -> '1 h 2 min'."""
    if seconds is None:
        return "—"
    s = int(seconds)
    if s < 60:
        return f"{s} s"
    if s < 3600:
        return f"{s // 60} min {s % 60} s" if s % 60 else f"{s // 60} min"
    if s < 86400:
        return f"{s // 3600} h {s % 3600 // 60} min"
    return f"{s // 86400} d {s % 86400 // 3600} h"


def display_path(path: str) -> str:
    """Make a path safe to render (undecodable filename bytes become U+FFFD)."""
    try:
        return path.encode("utf-8", "surrogateescape").decode("utf-8", "replace")
    except UnicodeEncodeError:  # Windows names can hold unpaired UTF-16 surrogates
        return path.encode("utf-8", "replace").decode("utf-8")


def fmt_time(ts: float | None) -> str:
    if not ts:
        return "—"
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
