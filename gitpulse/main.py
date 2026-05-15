"""
main.py — GitPulse entry point.

Launches the Textual TUI application. Accepts CLI arguments to configure
the scan root, number of commits to show, and version output.
Repos are sorted by most recent commit date.

Scanning runs in a background worker thread so the UI stays responsive.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import Header, Footer, Input
from textual.containers import Horizontal, Vertical
from textual.worker import Worker, WorkerState

# Support both installed-package imports (gitpulse.scanner) and
# direct execution (python main.py) by trying package import first.
try:
    from gitpulse.scanner import scan_repos
    from gitpulse.git_ops import get_repo_info, switch_branch, RepoInfo
    from gitpulse.ui.sidebar import RepoSidebar
    from gitpulse.ui.tabs import MainPanel
    from gitpulse.ui.fleet_status import FleetStatus
    from gitpulse.ui.digest_screen import DigestScreen
    from gitpulse.ui.command_palette import CommandPaletteModal
    from gitpulse.ui.bulk_results import BulkResultsScreen
    from gitpulse.ui.stale_screen import StaleScreen
    from gitpulse.utils import __version__, parse_since
    from gitpulse import config as _config
    from gitpulse import watcher as _watcher
except ImportError:
    # Running directly: python main.py
    _THIS_DIR = Path(__file__).resolve().parent
    if str(_THIS_DIR) not in sys.path:
        sys.path.insert(0, str(_THIS_DIR))
    from scanner import scan_repos  # type: ignore[no-redef]
    from git_ops import get_repo_info, switch_branch, RepoInfo  # type: ignore[no-redef]
    from ui.sidebar import RepoSidebar  # type: ignore[no-redef]
    from ui.tabs import MainPanel  # type: ignore[no-redef]
    from ui.fleet_status import FleetStatus  # type: ignore[no-redef]
    from ui.digest_screen import DigestScreen  # type: ignore[no-redef]
    from ui.command_palette import CommandPaletteModal  # type: ignore[no-redef]
    from ui.bulk_results import BulkResultsScreen  # type: ignore[no-redef]
    from ui.stale_screen import StaleScreen  # type: ignore[no-redef]
    from utils import __version__, parse_since  # type: ignore[no-redef]
    import config as _config  # type: ignore[no-redef]
    import watcher as _watcher  # type: ignore[no-redef]


class GitPulseApp(App):
    """
    GitPulse — A developer-focused Git repository dashboard TUI.

    Scans a root directory for all local git repos and displays live
    status, recent commits, diffs, and branch management.
    Repos are sorted by most recent commit (most active first).
    """

    CSS_PATH = str(Path(__file__).parent / "ui" / "styles.tcss")

    TITLE = "GitPulse"
    SUB_TITLE = "Git Repo Dashboard"

    BINDINGS = [
        Binding("q", "quit", "Quit", show=True),
        Binding("r", "refresh", "Refresh", show=True),
        Binding("w", "toggle_watch", "Watch", show=True),
        Binding("d", "open_digest", "Digest", show=True),
        Binding("colon", "open_palette", "Actions", show=True),
        Binding("b", "open_stale", "Stale", show=True),
        Binding("slash", "search", "Search", show=True),
        Binding("escape", "clear_search", "Clear", show=False),
        Binding("tab", "focus_next", "Next", show=False),
        Binding("shift+tab", "focus_previous", "Prev", show=False),
    ]

    def __init__(
        self,
        root_dir: Path,
        commits: int = 10,
        watch: bool = True,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.root_dir = root_dir
        self.commits = commits          # How many commits to show in Commits tab
        self.repos: list[RepoInfo] = []
        self._all_repos: list[RepoInfo] = []  # Unfiltered master list
        self._selected_repo: RepoInfo | None = None
        self._scanning = False          # Guard against concurrent scans
        self._watch_enabled = watch     # Whether watch mode is on
        self._watch_paused = False      # Toggled by 'w' key
        self._signatures: dict = {}     # path → (HEAD mtime, index mtime, refs mtime, packed-refs mtime)

    # -----------------------------------------------------------------
    # Layout
    # -----------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="app-grid"):
            with Vertical(id="sidebar-column"):
                yield FleetStatus(id="fleet-status")
                yield RepoSidebar(id="sidebar-container")
            yield MainPanel(id="main-panel", commits=self.commits)
        yield Footer()

    # -----------------------------------------------------------------
    # Lifecycle
    # -----------------------------------------------------------------

    def on_mount(self) -> None:
        """Initial scan on startup; start watch-mode interval if enabled."""
        self._start_scan()
        if self._watch_enabled:
            cfg = _config.get()
            self.set_interval(cfg.watch.interval_seconds, self._tick_watch)
            self.sub_title = "watch: ● live"
        else:
            self.sub_title = "watch: off"
        # Focus the repo list so global letter bindings (w/d/b/r) work
        # without keystrokes being captured by the search Input.
        try:
            self.set_focus(self.query_one("#repo-list"))
        except Exception:
            pass

    # -----------------------------------------------------------------
    # Actions
    # -----------------------------------------------------------------

    def action_refresh(self) -> None:
        """Rescan all repositories (bound to 'r')."""
        if self._scanning:
            self.notify("Scan already in progress…", timeout=2)
            return
        self._start_scan()
        self.notify("Scanning repositories… ⚡", timeout=2)

    def action_open_digest(self) -> None:
        """Open the activity digest modal (bound to 'd')."""
        cfg = _config.get()
        self.push_screen(DigestScreen(
            repos=self._all_repos,
            author_patterns=cfg.author.emails or [],
            default_window=cfg.digest.default_window,
        ))

    def action_open_stale(self) -> None:
        """Open stale-branch cleanup modal (bound to 'b')."""
        cfg = _config.get()
        self.push_screen(StaleScreen(
            repo_paths=[r.path for r in self._all_repos],
            stale_weeks=cfg.stale.weeks,
            default_branches=cfg.stale.default_branches,
            max_workers=cfg.bulk.max_workers,
        ))

    def action_open_palette(self) -> None:
        """Open the bulk-action command palette (bound to ':')."""
        sidebar: RepoSidebar = self.query_one("#sidebar-container", RepoSidebar)
        sel_count = len(sidebar.selected_repos())

        async def _after_palette(result: tuple | None) -> None:
            if result is None:
                return
            action_key, scope = result
            if scope == "selected":
                target_repos = sidebar.selected_repos()
            elif scope == "all":
                target_repos = list(self._all_repos)
            else:
                target_repos = [self._selected_repo] if self._selected_repo else []

            if not target_repos:
                self.notify("No repos to act on", timeout=2)
                return

            # Push needs extra confirmation
            if action_key == "push":
                names = ", ".join(r.name for r in target_repos[:5])
                extra = f" +{len(target_repos) - 5} more" if len(target_repos) > 5 else ""
                self.notify(f"Pushing to: {names}{extra}", timeout=4)

            self._dispatch_bulk(action_key, target_repos)

        self.push_screen(CommandPaletteModal(selected_count=sel_count), _after_palette)

    def _dispatch_bulk(self, action_key: str, repos: list) -> None:
        """Fan out a bulk git operation over repos using a thread pool worker."""
        try:
            from gitpulse.git_ops import git_fetch, git_pull, git_push, git_gc, git_remote_prune, git_clean_dry, get_repo_info
            from gitpulse.parallel import run_parallel
        except ImportError:
            from git_ops import git_fetch, git_pull, git_push, git_gc, git_remote_prune, git_clean_dry, get_repo_info  # type: ignore[no-redef]
            from parallel import run_parallel  # type: ignore[no-redef]

        _ops = {
            "fetch":   lambda r: git_fetch(r.path),
            "pull":    lambda r: git_pull(r.path),
            "push":    lambda r: git_push(r.path),
            "gc":      lambda r: git_gc(r.path),
            "prune":   lambda r: git_remote_prune(r.path),
            "clean":   lambda r: git_clean_dry(r.path),
            "refresh": lambda r: get_repo_info(r.path),
        }
        op = _ops.get(action_key)
        if op is None:
            self.notify(f"Unknown action: {action_key}", severity="error", timeout=3)
            return

        cfg = _config.get()
        results_screen = BulkResultsScreen(action=action_key, total=len(repos))
        self.push_screen(results_screen)

        def _worker() -> None:
            def _progress(completed, total, repo, result):
                self.call_from_thread(results_screen.append_row, repo, result)

            run_parallel(op, repos, max_workers=cfg.bulk.max_workers, on_progress=_progress)
            # After bulk refresh, trigger a rescan to update sidebar
            if action_key in ("pull", "refresh"):
                self.call_from_thread(self._start_scan)

        self.run_worker(_worker, thread=True, group="bulk", exclusive=False)

    def action_toggle_watch(self) -> None:
        """Pause / resume watch mode (bound to 'w')."""
        self._watch_paused = not self._watch_paused
        sidebar: RepoSidebar = self.query_one("#sidebar-container", RepoSidebar)
        if self._watch_paused:
            sidebar.update_header(scanning=False, count=len(self._all_repos), live=False)
            self.sub_title = "watch: ○ paused"
            self.notify("⏸  Watch mode PAUSED — press w to resume", severity="warning", timeout=4)
        else:
            sidebar.update_header(scanning=False, count=len(self._all_repos), live=True)
            self.sub_title = "watch: ● live"
            self.notify("▶  Watch mode RESUMED — auto-refresh on", severity="information", timeout=3)

    def action_search(self) -> None:
        """Focus the search input (bound to '/')."""
        sidebar: RepoSidebar = self.query_one("#sidebar-container", RepoSidebar)
        sidebar.focus_search()

    def action_clear_search(self) -> None:
        """Clear search and refocus repo list."""
        inp = self.query_one("#search-input", Input)
        inp.value = ""
        self.query_one("#repo-list").focus()

    # -----------------------------------------------------------------
    # Background scan worker
    # -----------------------------------------------------------------

    def _start_scan(self) -> None:
        """Launch the repository scan in a background worker thread."""
        self._scanning = True
        try:
            sidebar: RepoSidebar = self.query_one("#sidebar-container", RepoSidebar)
            sidebar.update_header(scanning=True)
        except Exception:
            pass
        self.run_worker(self._scan_worker, thread=True, exclusive=True, group="scan")

    def _scan_worker(self) -> list[RepoInfo]:
        """Worker function: scan filesystem and collect RepoInfo objects.

        Runs in a thread — no UI calls allowed here.
        Returns the sorted list of RepoInfo for the main thread to consume.
        """
        paths = scan_repos(self.root_dir)
        infos = [get_repo_info(p) for p in paths]
        infos.sort(key=lambda r: r.last_commit_ts, reverse=True)
        return infos

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        """Called on the main thread when the worker finishes."""
        if event.state == WorkerState.SUCCESS and event.worker.result is not None:
            group = getattr(event.worker, "group", None)

            if group == "watch":
                # Single-repo refresh from watch tick
                updated: RepoInfo = event.worker.result
                self._refresh_single_repo(updated)
                return

            if group == "branch_switch":
                # Result is (switch_message, updated_RepoInfo)
                switch_msg, updated_info = event.worker.result
                self.notify(switch_msg, timeout=3)
                self._refresh_single_repo(updated_info)
                self._start_scan()
                return

            if group not in (None, "scan"):
                # Unknown group (e.g. git_op owned by MainPanel) — ignore here.
                return

            # Full scan result
            self._scanning = False
            infos: list[RepoInfo] = event.worker.result
            self._all_repos = infos
            self.repos = list(infos)

            # Snapshot signatures for watch mode
            self._signatures = _watcher.snapshot(infos)

            sidebar: RepoSidebar = self.query_one("#sidebar-container", RepoSidebar)
            live = self._watch_enabled and not self._watch_paused
            sidebar.update_header(scanning=False, count=len(infos), live=live)
            sidebar.populate(self.repos)

            fleet: FleetStatus = self.query_one("#fleet-status", FleetStatus)
            fleet.update_counters(infos)

            if self.repos:
                self._select_repo(self.repos[0])

        elif event.state == WorkerState.ERROR:
            self._scanning = False
            self.notify(f"Scan failed: {event.worker.error}", severity="error", timeout=5)

    def _tick_watch(self) -> None:
        """Called on a timer interval — check for changed repos and re-enrich them."""
        if self._watch_paused or not self._all_repos:
            return
        changed = _watcher.changed_repos(self._all_repos, self._signatures)
        for repo in changed:
            # Update signature immediately to avoid re-triggering before worker completes
            self._signatures[repo.path] = _watcher.repo_signature(repo.path)
            path = repo.path
            self.run_worker(
                lambda p=path: get_repo_info(p),
                thread=True,
                group="watch",
                exclusive=False,
            )

    def _refresh_single_repo(self, updated: RepoInfo) -> None:
        """Apply a single watch-refresh result without re-populating the whole list."""
        # Update master list in place
        for i, r in enumerate(self._all_repos):
            if r.path == updated.path:
                self._all_repos[i] = updated
                break
        else:
            self._all_repos.append(updated)

        # Re-sort by activity
        self._all_repos.sort(key=lambda r: r.last_commit_ts, reverse=True)
        self.repos = list(self._all_repos)

        sidebar: RepoSidebar = self.query_one("#sidebar-container", RepoSidebar)
        sidebar.populate(self.repos)

        fleet: FleetStatus = self.query_one("#fleet-status", FleetStatus)
        fleet.update_counters(self._all_repos)

        # If the updated repo is selected, refresh the main panel too
        if self._selected_repo and self._selected_repo.path == updated.path:
            self._selected_repo = updated
            main: MainPanel = self.query_one("#main-panel", MainPanel)
            main.load_repo(updated.path, updated)

    # -----------------------------------------------------------------
    # Internal helpers
    # -----------------------------------------------------------------

    def _select_repo(self, repo_info: RepoInfo) -> None:
        """Load a repo's data into the main panel."""
        self._selected_repo = repo_info
        # Update header subtitle to reflect the active repo + branch
        self.sub_title = f"{repo_info.name}  ·  {repo_info.branch}"
        main: MainPanel = self.query_one("#main-panel", MainPanel)
        main.load_repo(repo_info.path, repo_info)

    def _apply_filter(self, query: str) -> None:
        """Filter the repo list by name, re-populate sidebar."""
        q = query.strip().lower()
        if q:
            self.repos = [r for r in self._all_repos if q in r.name.lower()]
        else:
            self.repos = list(self._all_repos)

        sidebar: RepoSidebar = self.query_one("#sidebar-container", RepoSidebar)
        sidebar.populate(self.repos)
        if self.repos:
            self._select_repo(self.repos[0])

    def _apply_fleet_filter(self, category: str) -> None:
        """Filter sidebar to repos matching a fleet-status category."""
        from gitpulse.git_ops import RepoStatus  # avoid circular at module level
        _predicates = {
            "dirty":   lambda r: r.status != RepoStatus.CLEAN,
            "behind":  lambda r: r.behind > 0,
            "ahead":   lambda r: r.ahead > 0,
            "stashes": lambda r: r.stash_count > 0,
            "stale":   lambda r: r.has_stale_branches,
        }
        pred = _predicates.get(category)
        if pred is None:
            self.repos = list(self._all_repos)
        else:
            self.repos = [r for r in self._all_repos if pred(r)]

        sidebar: RepoSidebar = self.query_one("#sidebar-container", RepoSidebar)
        sidebar.populate(self.repos)

        if self.repos:
            self._select_repo(self.repos[0])

    # -----------------------------------------------------------------
    # Message handlers
    # -----------------------------------------------------------------

    def on_repo_sidebar_repo_selected(self, message: RepoSidebar.RepoSelected) -> None:
        """User navigated to a different repo in the sidebar."""
        self._select_repo(message.repo_info)

    def on_repo_sidebar_search_changed(self, message: RepoSidebar.SearchChanged) -> None:
        """User typed in the search bar."""
        self._apply_filter(message.query)

    def on_repo_sidebar_selection_changed(self, message: RepoSidebar.SelectionChanged) -> None:
        """Update the header when the multi-select set changes."""
        sidebar: RepoSidebar = self.query_one("#sidebar-container", RepoSidebar)
        live = self._watch_enabled and not self._watch_paused
        sidebar.update_header(scanning=False, count=len(self._all_repos), live=live)

    def on_fleet_status_filter_requested(self, message: FleetStatus.FilterRequested) -> None:
        """User clicked a fleet chip — filter sidebar to matching repos."""
        self._apply_fleet_filter(message.category)

    def on_main_panel_branch_switch_requested(
        self, message: MainPanel.BranchSwitchRequested
    ) -> None:
        """User pressed Enter on a branch in the Branches tab."""
        if self._selected_repo is None:
            return

        path = self._selected_repo.path
        branch_name = message.branch_name

        def _do_switch() -> tuple[str, RepoInfo]:
            msg = switch_branch(path, branch_name)
            info = get_repo_info(path)
            return msg, info

        self.run_worker(_do_switch, thread=True, group="branch_switch", exclusive=False)

    def on_main_panel_reload_requested(self, message: MainPanel.ReloadRequested) -> None:
        """Fired after a commit or branch operation — rescan to update sidebar."""
        if self._selected_repo is None:
            return
        self._start_scan()


# -----------------------------------------------------------------------
# CLI entry point
# -----------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        prog="gitpulse",
        description="GitPulse — Git Repo Dashboard TUI",
    )
    parser.add_argument(
        "--root",
        type=str,
        default=None,
        help="Root directory to scan for git repos (default: first entry in config scan.roots, or current directory)",
    )
    parser.add_argument(
        "--commits",
        type=int,
        default=10,
        metavar="N",
        help="Number of commits to display per repo (default: 10)",
    )
    parser.add_argument(
        "--config",
        type=str,
        default=None,
        metavar="PATH",
        help="Path to config.toml (default: ~/.config/gitpulse/config.toml)",
    )
    parser.add_argument(
        "--no-watch",
        action="store_true",
        default=False,
        help="Disable live watch mode (default: enabled)",
    )
    parser.add_argument(
        "--digest",
        action="store_true",
        default=False,
        help="Print activity digest as markdown and exit (no TUI)",
    )
    parser.add_argument(
        "--since",
        type=str,
        default=None,
        metavar="SPEC",
        help="Time window for --digest: 1d, 7d, 30d, yesterday, YYYY-MM-DD (default: 1d)",
    )
    parser.add_argument(
        "--author",
        action="append",
        dest="authors",
        metavar="EMAIL",
        default=None,
        help="Author email filter for --digest (repeatable; default: git config user.email)",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"gitpulse {__version__}",
    )
    return parser.parse_args()


def main() -> None:
    """Entry point — called by both `python main.py` and the `gitpulse` command."""
    args = parse_args()

    # Load config first so scan.roots can influence the default root.
    if args.config:
        _config.load(Path(args.config))
    cfg = _config.get()

    if args.root is not None:
        root = Path(args.root).expanduser().resolve()
    elif cfg.scan.roots:
        root = Path(cfg.scan.roots[0]).expanduser().resolve()
    else:
        root = Path(".").resolve()

    if not root.is_dir():
        print(f"Error: '{root}' is not a valid directory.", file=sys.stderr)
        sys.exit(1)

    if args.digest:
        # CLI digest mode — no TUI
        from gitpulse.scanner import scan_repos as _scan
        from gitpulse.git_ops import get_repo_info as _gri
        from gitpulse.digest import build_digest as _bd, render_markdown as _rm
        from gitpulse.utils import parse_since as _ps

        since_spec = args.since or cfg.digest.default_window
        try:
            since_ts = _ps(since_spec)
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)

        author_patterns = args.authors or cfg.author.emails or []
        paths = _scan(root)
        repos = [_gri(p) for p in paths]
        digest = _bd(repos, since_ts, author_patterns, max_workers=cfg.bulk.max_workers)
        print(_rm(digest))
        return

    watch_enabled = cfg.watch.enabled and not args.no_watch
    app = GitPulseApp(root_dir=root, commits=args.commits, watch=watch_enabled)
    app.run()


if __name__ == "__main__":
    main()
