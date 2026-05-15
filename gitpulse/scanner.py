"""
scanner.py — Recursive Git repository discovery.

Walks a root directory tree and finds all folders containing a .git directory.
Skips common non-project directories for performance.
"""

from pathlib import Path

# Directories to skip during recursive scanning
SKIP_DIRS = {
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    "env",
    ".tox",
    ".mypy_cache",
    ".pytest_cache",
    "dist",
    "build",
    ".eggs",
    "site-packages",
}


def scan_repos(root: Path) -> list[Path]:
    """
    Recursively scan `root` for directories that contain a .git folder.

    Returns a sorted list of absolute paths to discovered repositories.
    Once a .git directory is found inside a folder, we do NOT recurse deeper
    into that folder (avoids picking up submodules or nested repos).

    Args:
        root: The top-level directory to begin scanning from.

    Returns:
        A sorted list of Path objects pointing to each discovered repo root.
    """
    root = root.expanduser().resolve()
    if not root.is_dir():
        return []

    repos: list[Path] = []
    _walk(root, repos)
    # No alphabetical sort here — the caller (_scan_worker) re-sorts by
    # commit timestamp, making a pre-sort wasted work.
    return repos


def _walk(directory: Path, repos: list[Path]) -> None:
    """
    Internal recursive walker.

    If `directory` itself contains a .git folder, add it to `repos`
    and stop recursing deeper. Otherwise, iterate children.
    """
    try:
        children = sorted(directory.iterdir())
    except PermissionError:
        return

    # Check if this directory is a git repo
    if (directory / ".git").is_dir():
        repos.append(directory)
        return  # Don't recurse into sub-repos

    for child in children:
        if child.is_dir() and child.name not in SKIP_DIRS and not child.name.startswith("."):
            _walk(child, repos)
