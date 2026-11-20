"""Findings: what an app reports, each with a suggested command that the app itself never runs.

`command` is the hook for a possible future "actions" feature: it would run a finding's command
only after the user explicitly confirms it. Until then commands are shown for the user to review.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

# Levels, mildest first. Cleanup-style apps use Safe/Low/Review; audit-style apps Info/Low/Medium/High.
LEVELS = ["Info", "Safe", "Low", "Medium", "Review", "High"]
LEVEL_LABEL = {"Info": "ℹ️ Info", "Safe": "🟢 Safe", "Low": "🟡 Low", "Medium": "🟠 Medium", "Review": "🔍 Review",
               "High": "🔴 High"}
LEVEL_HELP = {
    "Info": "For your information; nothing to do.",
    "Safe": "Rebuilds or re-downloads itself automatically.",
    "Low": "Low risk, but costs some time or bandwidth to get back.",
    "Medium": "Worth fixing when convenient.",
    "Review": "May hold data or settings you still want. Look before acting.",
    "High": "Fix soon.",
}


@dataclass
class Finding:
    title: str
    level: str  # one of LEVELS
    category: str = ""
    location: str = ""
    size: Optional[int] = None  # bytes, when the finding is about space
    note: str = ""
    command: str = ""  # suggested; shown to the user, never executed by the app
    details: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.level not in LEVELS:
            raise ValueError(f"unknown level {self.level!r}")


def sort_findings(items: list[Finding]) -> list[Finding]:
    """Most serious first, then largest."""
    return sorted(items, key=lambda f: (-LEVELS.index(f.level), -(f.size or 0), f.title))
