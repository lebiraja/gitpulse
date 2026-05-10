"""
command_palette.py — Fuzzy-search command palette modal for GitPulse.

Opened with ':' to pick a bulk operation to run across selected (or all) repos.
Returns (action_key, scope) to the caller via dismiss().
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import ModalScreen
from textual.widgets import Input, ListItem, ListView, Static
from textual.containers import Container

# Available bulk actions: (key, label, description)
BULK_ACTIONS: list[tuple[str, str, str]] = [
    ("fetch",   "fetch",          "git fetch --all from each repo"),
    ("pull",    "pull",           "git pull from tracking branch"),
    ("push",    "push",           "git push to tracking branch (asks confirmation)"),
    ("gc",      "gc --auto",      "git gc --auto — prune loose objects"),
    ("prune",   "remote prune",   "git remote prune origin — remove stale remote refs"),
    ("clean",   "clean -nd",      "git clean -nd — dry-run show untracked files"),
    ("refresh", "status refresh", "Re-scan and refresh repo info"),
]


class CommandPaletteModal(ModalScreen):
    """Fuzzy command palette for selecting a bulk action."""

    BINDINGS = [Binding("escape", "close", "Cancel", show=True)]

    DEFAULT_CSS = """
    CommandPaletteModal {
        align: center middle;
    }
    #palette-frame {
        width: 60;
        height: auto;
        max-height: 24;
        padding: 1 2;
        background: #1a1a24;
        border: thick #ff2d4a;
    }
    #palette-title {
        text-style: bold;
        color: #ff2d4a;
        margin-bottom: 1;
        text-align: center;
        width: 100%;
        height: 1;
    }
    #palette-scope {
        color: #ffb74d;
        margin-bottom: 1;
        width: 100%;
        height: 1;
    }
    #palette-input {
        width: 100%;
        margin-bottom: 1;
    }
    #palette-list {
        width: 100%;
        height: auto;
        max-height: 12;
    }
    """

    def __init__(self, selected_count: int = 0, **kwargs) -> None:
        super().__init__(**kwargs)
        self._selected_count = selected_count
        self._filtered = list(BULK_ACTIONS)

    def compose(self) -> ComposeResult:
        scope_text = (
            f"{self._selected_count} selected repo{'s' if self._selected_count != 1 else ''}"
            if self._selected_count > 0
            else "current repo"
        )
        with Container(id="palette-frame"):
            yield Static("⚡ Bulk Action", id="palette-title", markup=False)
            yield Static(f"  Scope: {scope_text}", id="palette-scope", markup=False)
            yield Input(placeholder="Filter actions…", id="palette-input")
            yield ListView(id="palette-list")

    def on_mount(self) -> None:
        self._rebuild_list(self._filtered)
        self.query_one("#palette-input", Input).focus()

    def _rebuild_list(self, actions: list[tuple[str, str, str]]) -> None:
        lv: ListView = self.query_one("#palette-list", ListView)
        lv.clear()
        for key, label, desc in actions:
            lv.append(ListItem(
                Static(f"[bold #3ddc84]{label}[/]  [dim #555568]{desc}[/]", markup=True),
                id=f"action-{key}",
            ))
        if actions:
            lv.index = 0

    def on_input_changed(self, event: Input.Changed) -> None:
        q = event.value.strip().lower()
        if q:
            self._filtered = [
                a for a in BULK_ACTIONS
                if q in a[0] or q in a[1].lower() or q in a[2].lower()
            ]
        else:
            self._filtered = list(BULK_ACTIONS)
        self._rebuild_list(self._filtered)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._submit()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        self._submit()

    def _submit(self) -> None:
        lv: ListView = self.query_one("#palette-list", ListView)
        item = lv.highlighted_child
        if item is None and self._filtered:
            self._dispatch(self._filtered[0][0])
            return
        if item is not None and item.id and item.id.startswith("action-"):
            key = item.id.removeprefix("action-")
            self._dispatch(key)

    def _dispatch(self, key: str) -> None:
        scope = "selected" if self._selected_count > 0 else "current"
        self.dismiss((key, scope))

    def action_close(self) -> None:
        self.dismiss(None)
