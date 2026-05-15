"""
fleet_status.py — Cross-repo fleet status bar for GitPulse.

Shows live counters (dirty, behind, unpushed, stashes, stale branches) across
all scanned repositories. Each chip is clickable and posts a FilterRequested
message so the sidebar can narrow to just the matching repos.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Static

try:
    from gitpulse.git_ops import RepoInfo, RepoStatus
except ImportError:
    from git_ops import RepoInfo, RepoStatus  # type: ignore[no-redef]


class FleetChip(Static):
    """A single clickable counter chip in the fleet status bar."""

    DEFAULT_CSS = """
    FleetChip {
        width: auto;
        height: 1;
        padding: 0 1;
        margin: 0 1;
        content-align: center middle;
    }
    FleetChip:hover {
        background: #2d1520;
    }
    """

    def __init__(self, category: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self.category = category

    def on_click(self) -> None:
        self.post_message(FleetStatus.FilterRequested(self.category))


class FleetStatus(Widget):
    """
    Horizontal bar pinned above the sidebar showing cross-repo health counters.

    Counters:
    - dirty     — repos with uncommitted changes
    - behind    — total commits behind upstream (sum)
    - ahead     — repos with unpushed commits
    - stashes   — total stash entries (sum)
    - stale     — repos with stale local branches
    - all       — reset chip to clear any active filter
    """

    DEFAULT_CSS = """
    FleetStatus {
        height: 3;
        background: #1a1a24;
        border-bottom: heavy #2a2a3a;
        layout: horizontal;
        align: left middle;
        padding: 0 1;
    }
    FleetStatus > Static#fleet-label {
        width: auto;
        color: #555568;
        margin-right: 1;
    }
    """

    class FilterRequested(Message):
        """Posted when a chip is clicked; carries the category to filter by."""

        def __init__(self, category: str) -> None:
            super().__init__()
            self.category = category

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._active_filter: str = ""

    def compose(self) -> ComposeResult:
        yield Static("fleet:", id="fleet-label", markup=False)
        yield FleetChip("dirty",   id="chip-dirty")
        yield FleetChip("behind",  id="chip-behind")
        yield FleetChip("ahead",   id="chip-ahead")
        yield FleetChip("stashes", id="chip-stashes")
        yield FleetChip("stale",   id="chip-stale")
        yield FleetChip("all",     id="chip-all")

    def on_mount(self) -> None:
        self._set_chip("chip-dirty",   "◆ dirty",   0, "#ff5252")
        self._set_chip("chip-behind",  "↓ behind",  0, "#ff5252")
        self._set_chip("chip-ahead",   "↑ ahead",   0, "#ffb74d")
        self._set_chip("chip-stashes", "⊞ stash",   0, "#4dd0e1")
        self._set_chip("chip-stale",   "☠ stale",   0, "#e040fb")
        chip: FleetChip = self.query_one("#chip-all", FleetChip)
        chip.update("[dim #555568]· all[/]")

    def update_counters(self, repos: list[RepoInfo]) -> None:
        """Recompute all chips from the current repo list."""
        n_dirty        = sum(1 for r in repos if r.status != RepoStatus.CLEAN)
        total_behind   = sum(r.behind for r in repos)
        n_ahead        = sum(1 for r in repos if r.ahead > 0)
        total_stashes  = sum(r.stash_count for r in repos)
        n_stale        = sum(1 for r in repos if r.has_stale_branches)

        self._set_chip("chip-dirty",   "◆ dirty",   n_dirty,       "#ff5252")
        self._set_chip("chip-behind",  "↓ behind",  total_behind,  "#ff5252")
        self._set_chip("chip-ahead",   "↑ ahead",   n_ahead,       "#ffb74d")
        self._set_chip("chip-stashes", "⊞ stash",   total_stashes, "#4dd0e1")
        self._set_chip("chip-stale",   "☠ stale",   n_stale,       "#e040fb")

    def set_active_filter(self, category: str) -> None:
        """Highlight the active filter chip and dim the rest."""
        self._active_filter = category
        chip_map = {
            "dirty": "chip-dirty", "behind": "chip-behind",
            "ahead": "chip-ahead", "stashes": "chip-stashes",
            "stale": "chip-stale", "all": "chip-all",
        }
        for cat, cid in chip_map.items():
            chip: FleetChip = self.query_one(f"#{cid}", FleetChip)
            if cat == category and category not in ("all", ""):
                chip.add_class("-active-filter")
            else:
                chip.remove_class("-active-filter")

    def _set_chip(self, widget_id: str, label: str, count: int, color: str) -> None:
        chip: FleetChip = self.query_one(f"#{widget_id}", FleetChip)
        if count == 0:
            chip.update(f"[dim #2a2a3a]{label}: 0[/]")
        else:
            chip.update(f"[bold {color}]{label}: {count}[/]")
