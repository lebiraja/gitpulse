"""
stale_screen.py — Stale-branch cleanup modal for GitPulse.

Opened with 'B' to show branches across all repos matching stale/merged/WIP
criteria. Supports multi-select and bulk delete with a typed confirmation.
"""

from __future__ import annotations

from pathlib import Path

from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import ModalScreen
from textual.widgets import DataTable, Input, Static, Button, TabbedContent, TabPane
from textual.containers import Container, Vertical, Horizontal

try:
    from gitpulse.git_ops import BranchDetail, delete_branch
    from gitpulse.stale import gather_all_repos, categorize
    from gitpulse.parallel import run_parallel
    from gitpulse.utils import relative_time
except ImportError:
    from git_ops import BranchDetail, delete_branch  # type: ignore
    from stale import gather_all_repos, categorize  # type: ignore
    from parallel import run_parallel  # type: ignore
    from utils import relative_time  # type: ignore


class DeleteConfirmModal(ModalScreen):
    """Type-to-confirm modal for bulk branch deletion."""

    BINDINGS = [Binding("escape", "close", "Cancel", show=True)]

    DEFAULT_CSS = """
    DeleteConfirmModal { align: center middle; }
    #dconf-frame {
        width: 56;
        height: auto;
        padding: 1 2;
        background: #1a1a24;
        border: thick #ff5252;
    }
    #dconf-title { color: #ff5252; text-style: bold; margin-bottom: 1; }
    #dconf-info  { color: #ffb74d; margin-bottom: 1; }
    #dconf-input { width: 100%; margin-bottom: 1; }
    #dconf-btns  { layout: horizontal; width: 100%; height: 3; align: center middle; }
    """

    def __init__(self, count: int, **kwargs) -> None:
        super().__init__(**kwargs)
        self._count = count
        self._phrase = f"delete {count} branch{'es' if count != 1 else ''}"

    def compose(self) -> ComposeResult:
        with Container(id="dconf-frame"):
            yield Static(f"⚠ Delete {self._count} branch{'es' if self._count != 1 else ''}?", id="dconf-title", markup=False)
            yield Static(f'  Type exactly: {self._phrase}', id="dconf-info", markup=False)
            yield Input(placeholder=self._phrase, id="dconf-input")
            with Horizontal(id="dconf-btns"):
                yield Button("Delete", id="btn-confirm-del", variant="error")
                yield Button("Cancel", id="btn-cancel-del")

    def on_mount(self) -> None:
        self.query_one("#dconf-input", Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-confirm-del":
            self._try_confirm()
        else:
            self.dismiss(False)

    def on_input_submitted(self, _) -> None:
        self._try_confirm()

    def _try_confirm(self) -> None:
        val = self.query_one("#dconf-input", Input).value.strip()
        if val == self._phrase:
            self.dismiss(True)
        else:
            self.query_one("#dconf-info", Static).update(
                f"  ✗ Must match exactly: {self._phrase}", markup=False
            )

    def action_close(self) -> None:
        self.dismiss(False)


class StaleScreen(ModalScreen):
    """Full-screen stale-branch explorer and cleanup modal."""

    BINDINGS = [
        Binding("escape,q", "close", "Close", show=True),
        Binding("space", "toggle_row", "Select", show=True),
        Binding("asterisk", "select_all", "Select All", show=True),
        Binding("d", "delete_selected", "Delete", show=True),
    ]

    DEFAULT_CSS = """
    StaleScreen { align: center middle; }
    #stale-frame {
        width: 96%;
        height: 90%;
        background: #1a1a24;
        border: thick #e040fb;
    }
    #stale-header {
        dock: top;
        height: 1;
        background: #242430;
        color: #e040fb;
        text-style: bold;
        padding: 0 1;
    }
    #stale-tabs { height: 1fr; }
    #stale-footer {
        dock: bottom;
        height: 1;
        background: #1a1a24;
        color: #555568;
        padding: 0 1;
        border-top: solid #2a2a3a;
    }
    """

    def __init__(
        self,
        repo_paths: list[Path],
        stale_weeks: int = 8,
        default_branches: list[str] | None = None,
        max_workers: int = 8,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._repo_paths = repo_paths
        self._stale_weeks = stale_weeks
        self._default_branches = default_branches or ["main", "master", "develop", "trunk"]
        self._max_workers = max_workers
        self._categories: dict[str, list[BranchDetail]] = {}
        self._selected: set[tuple[str, str]] = set()  # (repo_name, branch_name)
        self._active_category = "stale"

    def compose(self) -> ComposeResult:
        with Container(id="stale-frame"):
            yield Static(" ☠ Stale Branches", id="stale-header", markup=False)
            with TabbedContent(id="stale-tabs"):
                for cat, label in [
                    ("stale",    f"Stale ({self._stale_weeks}w+)"),
                    ("merged",   "Merged"),
                    ("wip",      "WIP"),
                    ("unmerged", "Unmerged"),
                    ("all",      "All"),
                ]:
                    with TabPane(label, id=f"stale-tab-{cat}"):
                        yield DataTable(id=f"stale-table-{cat}")
            yield Static(
                "  Space=select  *=all  d=delete selected  Esc/q=close",
                id="stale-footer",
                markup=False,
            )

    def on_mount(self) -> None:
        for cat in ("stale", "merged", "wip", "unmerged", "all"):
            table: DataTable = self.query_one(f"#stale-table-{cat}", DataTable)
            table.add_columns("[ ]", "Repo", "Branch", "Age", "Last commit", "Flags")
            table.cursor_type = "row"
            table.zebra_stripes = True

        self._load_data()

    def _load_data(self) -> None:
        self.query_one("#stale-header", Static).update(
            " ☠ Stale Branches  [dim #555568]loading…[/]"
        )
        self.run_worker(self._fetch_worker, thread=True, group="stale")

    def _fetch_worker(self) -> dict[str, list[BranchDetail]]:
        return gather_all_repos(
            self._repo_paths,
            self._stale_weeks,
            self._default_branches,
            self._max_workers,
        )

    def on_worker_state_changed(self, event) -> None:
        from textual.worker import WorkerState
        if event.state == WorkerState.SUCCESS and event.worker.result is not None:
            self._categories = event.worker.result
            self._populate_tables()
            self.query_one("#stale-header", Static).update(" ☠ Stale Branches")
        elif event.state == WorkerState.ERROR:
            self.query_one("#stale-header", Static).update(
                f" ☠ Error: {event.worker.error}"
            )

    def _populate_tables(self) -> None:
        for cat, branches in self._categories.items():
            table: DataTable = self.query_one(f"#stale-table-{cat}", DataTable)
            table.clear()
            for b in sorted(branches, key=lambda x: x.age_days, reverse=True):
                sel_key = (b.repo_name, b.name)
                checkbox = "[bold #3ddc84]✓[/]" if sel_key in self._selected else "[ ]"
                flags = []
                if b.is_wip:                 flags.append("[#ffb74d]WIP[/]")
                if b.is_merged_into_default: flags.append("[#3ddc84]merged[/]")
                if b.is_current:             flags.append("[#ff2d4a]current[/]")
                if not b.has_upstream:       flags.append("[dim]no-remote[/]")
                flags_str = " ".join(flags) if flags else "[dim]—[/]"
                age_str = f"{b.age_days}d"
                msg = b.last_commit_msg[:40] + ("…" if len(b.last_commit_msg) > 40 else "")
                table.add_row(checkbox, b.repo_name, b.name, age_str, msg, flags_str)

    def _current_table_and_category(self) -> tuple[DataTable, str]:
        try:
            tc = self.query_one(TabbedContent)
            pane_id = str(tc.active) if tc.active else "stale-tab-stale"
            cat = pane_id.removeprefix("stale-tab-")
        except Exception:
            cat = "stale"
        table: DataTable = self.query_one(f"#stale-table-{cat}", DataTable)
        return table, cat

    def _branch_at_cursor(self) -> BranchDetail | None:
        table, cat = self._current_table_and_category()
        if table.cursor_row < 0:
            return None
        branches = self._categories.get(cat, [])
        sorted_branches = sorted(branches, key=lambda x: x.age_days, reverse=True)
        if table.cursor_row < len(sorted_branches):
            return sorted_branches[table.cursor_row]
        return None

    def action_toggle_row(self) -> None:
        b = self._branch_at_cursor()
        if b is None or b.is_current:
            return
        key = (b.repo_name, b.name)
        if key in self._selected:
            self._selected.discard(key)
        else:
            self._selected.add(key)
        self._populate_tables()

    def action_select_all(self) -> None:
        _, cat = self._current_table_and_category()
        for b in self._categories.get(cat, []):
            if not b.is_current:
                self._selected.add((b.repo_name, b.name))
        self._populate_tables()

    def action_delete_selected(self) -> None:
        if not self._selected:
            self.app.notify("Nothing selected — use Space to select branches", timeout=3)
            return

        async def _after_confirm(confirmed: bool) -> None:
            if not confirmed:
                return
            to_delete: list[BranchDetail] = []
            for cat_branches in self._categories.values():
                for b in cat_branches:
                    if (b.repo_name, b.name) in self._selected:
                        if b not in to_delete:
                            to_delete.append(b)

            def _do_delete(b: BranchDetail) -> str:
                return delete_branch(b.repo_path, b.name, force=True)

            results = run_parallel(_do_delete, to_delete, max_workers=self._max_workers)
            ok = sum(1 for _, r in results if not isinstance(r, Exception) and not str(r).startswith("Error"))
            fail = len(results) - ok
            self.app.notify(f"Deleted {ok} branch{'es' if ok != 1 else ''}" + (f", {fail} failed" if fail else ""), timeout=4)

            self._selected.clear()
            self._load_data()

        self.app.push_screen(DeleteConfirmModal(count=len(self._selected)), _after_confirm)

    def action_close(self) -> None:
        self.dismiss()
