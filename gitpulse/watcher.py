"""
watcher.py — Filesystem polling helpers for GitPulse watch mode.

Polls .git/HEAD, .git/index, and .git/refs/heads/ mtimes to detect
repository state changes without requiring external dependencies like watchdog.

Strategy: after a full scan, snapshot per-repo signatures. On each tick,
recheck signatures cheaply (N os.stat calls). Only changed repos trigger a
re-enrichment worker — no full rescan, no UI flicker.
"""

from __future__ import annotations

import os
from pathlib import Path

try:
    from gitpulse.git_ops import RepoInfo
except ImportError:
    from git_ops import RepoInfo  # type: ignore[no-redef]


def repo_signature(repo_path: Path) -> tuple[float, float, float, float]:
    """Return (HEAD mtime, index mtime, refs/heads mtime, packed-refs mtime).

    Any unreadable path contributes 0.0 so the tuple is always well-typed.
    packed-refs is checked alongside refs/heads because cloned or gc'd repos
    store references there rather than as individual files under refs/heads/.
    """
    def _mtime(p: Path) -> float:
        try:
            return os.stat(p).st_mtime
        except OSError:
            return 0.0

    git_dir = repo_path / ".git"
    return (
        _mtime(git_dir / "HEAD"),
        _mtime(git_dir / "index"),
        _mtime(git_dir / "refs" / "heads"),
        _mtime(git_dir / "packed-refs"),
    )


def snapshot(repos: list[RepoInfo]) -> dict[Path, tuple[float, float, float, float]]:
    """Build a path → signature map for all repos. O(N) stat calls."""
    return {r.path: repo_signature(r.path) for r in repos}


def changed_repos(
    repos: list[RepoInfo],
    previous: dict[Path, tuple[float, float, float, float]],
) -> list[RepoInfo]:
    """Return repos whose signature differs from *previous*.

    A repo absent from *previous* is considered changed (first tick after
    a new repo appears in the scan root).
    """
    changed: list[RepoInfo] = []
    for repo in repos:
        current_sig = repo_signature(repo.path)
        if previous.get(repo.path) != current_sig:
            changed.append(repo)
    return changed
