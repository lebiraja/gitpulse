"""
sidebar.py — Repo list sidebar widget for GitPulse.

Displays all discovered repositories in a scrollable ListView with
color-coded status badges, branch names, relative time, and file counts.
Includes a search/filter input at the top and multi-select support for
bulk operations.
"""

from __future__ import annotations

from pathlib import Path

from textual.app import ComposeResult
from textual.message import Message
from textual.widgets import Static, ListView, ListItem, Input

try:
    from gitpulse.git_ops import RepoInfo, RepoStatus
    from gitpulse.utils import relative_time
except ImportError:
    from git_ops import RepoInfo, RepoStatus  # type: ignore[no-redef]
    from utils import relative_time  # type: ignore[no-redef]


# ---------------------------------------------------------------------------
# Badge markup (Rich)
# ---------------------------------------------------------------------------

def _make_badge(info: RepoInfo) -> str:
    """Build a Rich markup badge string with icon and optional file count."""
    count = info.modified_count
    if info.status == RepoStatus.CLEAN:
        return "[bold #3ddc84 on #0f2a1a] ✔ Clean [/]"
    elif info.status == RepoStatus.MODIFIED:
        label = f" ● {count} modified " if count else " ● Modified "
        return f"[bold #ffb74d on #2a1e00]{label}[/]"
    else:  # UNTRACKED
        label = f" ○ {count} untracked " if count else " ○ Untracked "
        return f"[bold #ff5252 on #2a0a0a]{label}[/]"


# ---------------------------------------------------------------------------
# Sparkline helper
# ---------------------------------------------------------------------------

_SPARK_CHARS = " ▁▂▃▄▅▆▇█"

def _sparkline(activity: list[int]) -> str:
    """Build a 7-char sparkline from weekly commit counts (oldest→newest)."""
    if not activity or len(activity) < 7:
        return "[dim #2a2a3a]▁▁▁▁▁▁▁[/]"
    mx = max(activity)
    if mx == 0:
        return "[dim #2a2a3a]▁▁▁▁▁▁▁[/]"
    chars = "".join(_SPARK_CHARS[min(8, int(v / mx * 8))] for v in activity)
    return f"[#ff2d4a]{chars}[/]"


# ---------------------------------------------------------------------------
# Repo list item — single Static with Rich markup
# ---------------------------------------------------------------------------

class RepoListItem(ListItem):
    """A single row in the sidebar representing one git repository."""

    DEFAULT_CSS = """
    RepoListItem {
        height: auto;
        padding: 0 1;
    }
    RepoListItem > Static {
        width: 100%;
        height: auto;
    }
    """

    def __init__(self, repo_info: RepoInfo, selected: bool = False, **kwargs) -> None:
        super().__init__(**kwargs)
        self.repo_info = repo_info
        self._selected = selected

    def compose(self) -> ComposeResult:
        info = self.repo_info
        badge = _make_badge(info)
        rel = relative_time(info.last_commit_ts)
        spark = _sparkline(info.commit_activity)

        # Selection checkbox prefix
        if self._selected:
            checkbox = "[bold #3ddc84][✓][/] "
        else:
            checkbox = "[dim #2a2a3a][ ][/] "

        # Shorten path for display
        path_str = str(info.path)
        home = str(Path.home())
        if path_str.startswith(home):
            path_str = "~" + path_str[len(home):]
        if len(path_str) > 38:
            path_str = "…" + path_str[-37:]

        # Line 1: checkbox + repo name + badge
        line1 = f"{checkbox}[bold #d4d4dc]{info.name}[/]  {badge}"
        # Line 2: branch  |  relative time  |  sparkline
        line2 = f"   [#e040fb]⎇ {info.branch}[/]  [dim #555568]⏱ {rel}[/]  {spark}"
        # Line 3: truncated last commit message for quick context
        commit_msg = info.last_commit_msg
        if len(commit_msg) > 36:
            commit_msg = commit_msg[:35] + "…"
        line3 = f"   [dim #555568]💬 {commit_msg}[/]" if commit_msg else "   [dim #2a2a3a]no commits[/]"
        # Line 4: truncated repo path for disambiguation
        line4 = f"   [dim #2a2a3a]{path_str}[/]"

        yield Static(f"{line1}\n{line2}\n{line3}\n{line4}", markup=True)


# ---------------------------------------------------------------------------
# Sidebar container
# ---------------------------------------------------------------------------

class RepoSidebar(Static):
    """
    Left sidebar panel: title + search input + scrollable list of repos.

    Posts a `RepoSidebar.RepoSelected` message when the user highlights
    a different repo, `RepoSidebar.SearchChanged` when the filter changes,
    and `RepoSidebar.SelectionChanged` when the multi-select set changes.
    """

    class RepoSelected(Message):
        """Fired when the user selects a repo from the list."""
        def __init__(self, repo_info: RepoInfo) -> None:
            super().__init__()
            self.repo_info = repo_info

    class SearchChanged(Message):
        """Fired when the search filter text changes."""
        def __init__(self, query: str) -> None:
            super().__init__()
            self.query = query

    class SelectionChanged(Message):
        """Fired when the multi-select set changes."""
        def __init__(self, count: int, paths: list[Path]) -> None:
            super().__init__()
            self.count = count
            self.paths = paths

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._selected: set[Path] = set()
        self._current_repos: list[RepoInfo] = []

    # ── Multi-select API ────────────────────────────────────────────────

    def is_selected(self, path: Path) -> bool:
        return path in self._selected

    def toggle(self, path: Path) -> None:
        if path in self._selected:
            self._selected.discard(path)
        else:
            self._selected.add(path)
        self.post_message(self.SelectionChanged(
            count=len(self._selected),
            paths=list(self._selected),
        ))

    def select_all_visible(self) -> None:
        for r in self._current_repos:
            self._selected.add(r.path)
        self.post_message(self.SelectionChanged(
            count=len(self._selected),
            paths=list(self._selected),
        ))
        self.populate(self._current_repos)

    def clear_selection(self) -> None:
        self._selected.clear()
        self.post_message(self.SelectionChanged(count=0, paths=[]))
        self.populate(self._current_repos)

    def selected_repos(self) -> list[RepoInfo]:
        return [r for r in self._current_repos if r.path in self._selected]

    # ── Compose ─────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Static(
            "⚡ [bold #ff2d4a]GitPulse[/]",
            id="sidebar-title",
            markup=True,
        )
        yield Input(
            placeholder="🔍 Filter repos...",
            id="search-input",
        )
        yield ListView(id="repo-list")

    def update_header(
        self,
        scanning: bool,
        count: int = 0,
        live: bool | None = None,
    ) -> None:
        """Update the title bar to show scanning state or repo count.

        *live* controls the watch indicator: True = green dot, False = dim dot,
        None = unchanged from last render.
        """
        title: Static = self.query_one("#sidebar-title", Static)
        if scanning:
            title.update("⚡ [bold #ff2d4a]GitPulse[/]  [dim #555568]scanning…[/]")
            return
        count_str = (
            f"[dim #555568]{count} repo{'s' if count != 1 else ''}[/]"
            if count else ""
        )
        if live is True:
            live_str = "  [bold #3ddc84]●live[/]"
        elif live is False:
            live_str = "  [dim #555568]○paused[/]"
        else:
            live_str = ""

        sel = len(self._selected)
        sel_str = f"  [bold #ffb74d][{sel} sel][/]" if sel > 0 else ""

        title.update(f"⚡ [bold #ff2d4a]GitPulse[/]{live_str}  {count_str}{sel_str}")

    def populate(self, repos: list[RepoInfo]) -> None:
        """Clear and re-populate the repo list."""
        self._current_repos = list(repos)
        list_view: ListView = self.query_one("#repo-list", ListView)
        list_view.clear()

        if not repos:
            from textual.widgets import ListItem as _LI
            list_view.append(_LI(Static(
                "[dim italic #555568]\n  📂  No repositories found\n"
                "      Try a different root or\n"
                "      press r to rescan\n[/]",
                markup=True,
            )))
            return

        for info in repos:
            list_view.append(RepoListItem(info, selected=info.path in self._selected))

        # Auto-select first item
        list_view.index = 0

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        """Forward the highlight event as a RepoSelected message."""
        if event.item is not None and isinstance(event.item, RepoListItem):
            self.post_message(self.RepoSelected(event.item.repo_info))

    def on_input_changed(self, event: Input.Changed) -> None:
        """Forward search input changes."""
        if event.input.id == "search-input":
            self.post_message(self.SearchChanged(event.value))

    def on_key(self, event) -> None:
        """Handle multi-select keys: Space toggles, * selects all."""
        if event.key == "space":
            lv: ListView = self.query_one("#repo-list", ListView)
            item = lv.highlighted_child
            if isinstance(item, RepoListItem):
                self.toggle(item.repo_info.path)
                self.populate(self._current_repos)
                event.stop()
        elif event.key == "asterisk":
            self.select_all_visible()
            event.stop()

    def focus_search(self) -> None:
        """Focus the search input."""
        self.query_one("#search-input", Input).focus()
