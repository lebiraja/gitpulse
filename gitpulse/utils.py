"""
utils.py — Shared utilities for GitPulse.

Contains helpers that are used across multiple modules so they don't
need to live inside domain-specific files (e.g. git_ops.py).
"""

from __future__ import annotations

import re
from datetime import datetime, timezone, timedelta

# ---------------------------------------------------------------------------
# Package version — single source of truth
# ---------------------------------------------------------------------------

__version__ = "1.2.3"


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------

def parse_since(spec: str) -> float:
    """Parse a time-window spec into a Unix timestamp (start of the window).

    Supported forms:
        Nd  — N days ago      (e.g. "1d", "7d")
        Nw  — N weeks ago     (e.g. "2w")
        Nh  — N hours ago     (e.g. "4h")
        yesterday             — start of the previous calendar day (midnight)
        today                 — start of the current calendar day (midnight)
        YYYY-MM-DD            — specific date (midnight)
        YYYY-MM-DDTHH:MM      — specific datetime

    Raises ValueError for unrecognised formats.
    """
    spec = spec.strip().lower()
    now = datetime.now(timezone.utc)

    if spec == "yesterday":
        d = (now - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        return d.timestamp()
    if spec == "today":
        d = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return d.timestamp()

    m = re.fullmatch(r"(\d+)([hdw])", spec)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        delta = {"h": timedelta(hours=n), "d": timedelta(days=n), "w": timedelta(weeks=n)}[unit]
        return (now - delta).timestamp()

    # ISO date or datetime
    for fmt in ("%Y-%m-%dt%H:%M", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(spec, fmt).replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except ValueError:
            pass

    raise ValueError(f"Unrecognised time spec: {spec!r}. Try '1d', '7d', 'yesterday', or 'YYYY-MM-DD'.")


def relative_time(ts: float) -> str:
    """Convert a Unix timestamp to a human-readable relative time string.

    Examples: "just now", "5m ago", "3h ago", "2d ago", "1w ago", "4mo ago", "2y ago"
    """
    if ts == 0:
        return "never"
    now = datetime.now(timezone.utc).timestamp()
    diff = now - ts
    if diff < 60:
        return "just now"
    elif diff < 3600:
        m = int(diff // 60)
        return f"{m}m ago"
    elif diff < 86400:
        h = int(diff // 3600)
        return f"{h}h ago"
    elif diff < 604800:
        d = int(diff // 86400)
        return f"{d}d ago"
    elif diff < 2592000:
        w = int(diff // 604800)
        return f"{w}w ago"
    elif diff < 31536000:
        mo = int(diff // 2592000)
        return f"{mo}mo ago"
    else:
        y = int(diff // 31536000)
        return f"{y}y ago"
