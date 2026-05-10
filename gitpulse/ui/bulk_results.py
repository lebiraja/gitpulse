"""
bulk_results.py — Live results screen for bulk git operations.

Shown after a bulk operation is dispatched; rows update as futures complete.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import ModalScreen
from textual.widgets import DataTable, Static
from textual.containers import Container

try:
    from gitpulse.git_ops import RepoInfo
except ImportError:
    from git_ops import RepoInfo  # type: ignore[no-redef]


class BulkResultsScreen(ModalScreen):
    """Modal showing per-repo results from a bulk operation."""

    BINDINGS = [Binding("escape,q", "close", "Close", show=True)]

    DEFAULT_CSS = """
    BulkResultsScreen {
        align: center middle;
    }
    #results-frame {
        width: 88%;
        height: 75%;
        background: #1a1a24;
        border: thick #e040fb;
    }
    #results-title {
        dock: top;
        height: 1;
        background: #242430;
        color: #e040fb;
        text-style: bold;
        padding: 0 1;
    }
    #results-table {
        width: 100%;
        height: 1fr;
    }
    #results-footer {
        dock: bottom;
        height: 1;
        background: #1a1a24;
        color: #555568;
        padding: 0 1;
        border-top: solid #2a2a3a;
    }
    """

    def __init__(self, action: str, total: int, **kwargs) -> None:
        super().__init__(**kwargs)
        self._action = action
        self._total = total
        self._completed = 0

    def compose(self) -> ComposeResult:
        with Container(id="results-frame"):
            yield Static(
                f" Bulk: {self._action}",
                id="results-title",
                markup=False,
            )
            yield DataTable(id="results-table")
            yield Static(
                f"  0/{self._total} complete · Esc/q to close",
                id="results-footer",
                markup=False,
            )

    def on_mount(self) -> None:
        table: DataTable = self.query_one("#results-table", DataTable)
        table.add_columns("Repo", "Status", "Output")
        table.cursor_type = "row"
        table.zebra_stripes = True

    def append_row(self, repo: RepoInfo, result: str | Exception) -> None:
        """Add a completed row. Safe to call from any thread via call_from_thread."""
        table: DataTable = self.query_one("#results-table", DataTable)
        self._completed += 1

        if isinstance(result, Exception):
            status = "[bold #ff5252]ERROR[/]"
            output = str(result)[:80]
        elif isinstance(result, str) and result.lower().startswith("error"):
            status = "[bold #ff5252]FAIL[/]"
            output = result[:80]
        else:
            status = "[bold #3ddc84]OK[/]"
            output = (str(result) if result else "done")[:80]

        table.add_row(repo.name, status, output)

        footer: Static = self.query_one("#results-footer", Static)
        done = self._completed == self._total
        suffix = "  (done — Esc/q to close)" if done else " · Esc/q to close"
        footer.update(
            f"  {self._completed}/{self._total} complete{suffix}",
            markup=False,
        )

    def action_close(self) -> None:
        self.dismiss()
