"""
git_ops.py — Git operations for GitPulse.

Provides data classes and helper functions to query repository status,
recent commits, diffs, branches, stashes, remotes, tags, and to switch
branches. Uses GitPython for interfacing with local git repositories.
"""

from __future__ import annotations

import enum
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from git import Repo, InvalidGitRepositoryError, GitCommandError

try:
    from gitpulse.utils import relative_time
except ImportError:
    from utils import relative_time  # type: ignore[no-redef]


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

class RepoStatus(enum.Enum):
    """High-level status of a repository's working tree."""
    CLEAN = "CLEAN"
    MODIFIED = "MODIFIED"
    UNTRACKED = "UNTRACKED"


@dataclass
class RepoInfo:
    """Summary information for a single git repository."""
    name: str
    path: Path
    branch: str
    status: RepoStatus
    last_commit_ts: float = 0.0        # Unix timestamp of most recent commit
    last_commit_msg: str = ""           # First line of most recent commit message
    modified_count: int = 0             # Number of modified + staged + untracked files
    total_commits: int = 0              # Total commit count on current branch
    contributor_count: int = 0          # Unique author count
    commit_activity: list[int] = field(default_factory=list)  # Commits per week, 7 weeks oldest→newest
    ahead: int = 0                       # Commits ahead of upstream
    behind: int = 0                      # Commits behind upstream
    stash_count: int = 0                 # Number of stash entries
    has_stale_branches: bool = False     # True if any branch is older than stale threshold


@dataclass
class FileStatus:
    """Categorised lists of files returned by `git status`."""
    staged: list[str] = field(default_factory=list)
    unstaged: list[str] = field(default_factory=list)
    untracked: list[str] = field(default_factory=list)


@dataclass
class CommitInfo:
    """A single commit entry."""
    short_hash: str
    author: str
    date: str
    message: str
    files_changed: int = 0
    insertions: int = 0
    deletions: int = 0


@dataclass
class AuthorCommit:
    """A single commit attributed to a specific author, used by digest mode."""
    short_hash: str
    ts: float
    message: str
    insertions: int = 0
    deletions: int = 0
    files_changed: int = 0


@dataclass
class BranchInfo:
    """Information about a local branch."""
    name: str
    is_current: bool


@dataclass
class BranchDetail:
    """Rich branch information used by stale-branch analysis."""
    repo_path: Path
    repo_name: str
    name: str
    last_commit_ts: float
    last_commit_msg: str
    is_current: bool
    has_upstream: bool
    is_merged_into_default: bool
    is_wip: bool         # subject matches ^(wip|fixup!|squash!|tmp)\b
    age_days: int


@dataclass
class StashEntry:
    """A single stash entry."""
    index: int
    message: str


@dataclass
class RemoteInfo:
    """Information about a git remote."""
    name: str
    url: str
    ahead: int = 0
    behind: int = 0


@dataclass
class TagInfo:
    """Information about a git tag."""
    name: str
    date: str
    message: str
    tagger: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _open_repo(path: Path) -> Repo:
    """Open a git.Repo, raising on failure."""
    return Repo(str(path))


def _determine_status(repo: Repo) -> tuple[RepoStatus, int]:
    """Determine the high-level status of a repository and file count."""
    try:
        staged = list(repo.index.diff("HEAD"))
    except Exception:
        staged = []
    unstaged = list(repo.index.diff(None))
    untracked = repo.untracked_files
    count = len(staged) + len(unstaged) + len(untracked)

    if staged or unstaged:
        return RepoStatus.MODIFIED, count
    elif untracked:
        return RepoStatus.UNTRACKED, count
    return RepoStatus.CLEAN, 0


# Re-export so existing callers of `from git_ops import relative_time` keep working.
__all__ = ["relative_time"]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_repo_info(path: Path) -> RepoInfo:
    """
    Return a RepoInfo summary for the repository at `path`.

    Includes last commit timestamp for sorting, file counts, and summary stats.
    """
    try:
        repo = _open_repo(path)
        branch = repo.active_branch.name if not repo.head.is_detached else "(detached)"
        status, mod_count = _determine_status(repo)

        # Last commit info
        last_ts = 0.0
        last_msg = ""
        total = 0
        contributor_count = 0
        activity: list[int] = [0] * 7
        try:
            head_commit = repo.head.commit
            last_ts = float(head_commit.committed_date)
            last_msg = head_commit.message.strip().split("\n")[0]

            # Total commit count — one fast plumbing command
            try:
                count_out = repo.git.rev_list("HEAD", count=True).strip()
                total = int(count_out) if count_out.isdigit() else 0
            except Exception:
                total = 0

            # Contributor count via git shortlog (single command, no iteration)
            try:
                shortlog = repo.git.shortlog("-sn", "--no-merges", "HEAD")
                contributor_count = len(shortlog.strip().splitlines()) if shortlog.strip() else 0
            except Exception:
                contributor_count = 0

            # Commit activity: commits per week for the last 7 weeks (sparkline buckets)
            try:
                now_ts = time.time()
                for c in repo.iter_commits(max_count=200):
                    age_days = (now_ts - float(c.committed_date)) / 86400
                    week_idx = int(age_days // 7)
                    if 0 <= week_idx < 7:
                        activity[week_idx] += 1
                activity.reverse()  # oldest week first
            except Exception:
                activity = [0] * 7
        except Exception:
            pass

        # Ahead/behind relative to upstream (skip for detached HEAD)
        ahead = 0
        behind = 0
        try:
            if branch != "(detached)":
                _br = repo.active_branch
                tracking = _br.tracking_branch()
                if tracking:
                    ahead = len(list(repo.iter_commits(f"{tracking.name}..{_br.name}")))
                    behind = len(list(repo.iter_commits(f"{_br.name}..{tracking.name}")))
        except Exception:
            pass

        # Stash count
        stash_count = 0
        try:
            sl = repo.git.stash("list")
            stash_count = len(sl.strip().splitlines()) if sl.strip() else 0
        except Exception:
            pass

        # Stale-branch quick check (any local branch older than 8 weeks, not current)
        has_stale = False
        try:
            _stale_cutoff = time.time() - (8 * 7 * 86400)
            ref_out = repo.git.for_each_ref(
                "refs/heads/",
                format="%(refname:short) %(committerdate:unix)",
            )
            for _line in ref_out.strip().splitlines():
                _parts = _line.rsplit(" ", 1)
                if len(_parts) == 2:
                    _bname, _ts_str = _parts
                    try:
                        if float(_ts_str) < _stale_cutoff and _bname != branch:
                            has_stale = True
                            break
                    except ValueError:
                        pass
        except Exception:
            pass

    except (InvalidGitRepositoryError, Exception):
        branch = "unknown"
        status = RepoStatus.CLEAN
        mod_count = 0
        last_ts = 0.0
        last_msg = ""
        total = 0
        contributor_count = 0
        activity = []
        ahead = 0
        behind = 0
        stash_count = 0
        has_stale = False

    return RepoInfo(
        name=path.name,
        path=path,
        branch=branch,
        status=status,
        last_commit_ts=last_ts,
        last_commit_msg=last_msg,
        modified_count=mod_count,
        total_commits=total,
        contributor_count=contributor_count,
        commit_activity=activity,
        ahead=ahead,
        behind=behind,
        stash_count=stash_count,
        has_stale_branches=has_stale,
    )


def get_status(path: Path) -> FileStatus:
    """
    Return categorised file lists (staged, unstaged, untracked) for a repo.
    """
    repo = _open_repo(path)
    fs = FileStatus()

    # Staged (index vs HEAD)
    try:
        diffs_staged = repo.index.diff("HEAD")
        fs.staged = [d.a_path or d.b_path for d in diffs_staged]
    except Exception:
        fs.staged = []

    # Unstaged (working tree vs index)
    diffs_unstaged = repo.index.diff(None)
    fs.unstaged = [d.a_path or d.b_path for d in diffs_unstaged]

    # Untracked
    fs.untracked = repo.untracked_files

    return fs


def get_commits(path: Path, n: int = 10) -> list[CommitInfo]:
    """
    Return the last `n` commits from the current branch, including diff stats.
    """
    repo = _open_repo(path)
    commits: list[CommitInfo] = []

    try:
        for commit in repo.iter_commits(max_count=n):
            # Get diffstat for each commit
            files_changed = 0
            insertions = 0
            deletions = 0
            try:
                stats = commit.stats.total
                files_changed = stats.get("files", 0)
                insertions = stats.get("insertions", 0)
                deletions = stats.get("deletions", 0)
            except Exception:
                pass

            commits.append(
                CommitInfo(
                    short_hash=commit.hexsha[:7],
                    author=str(commit.author),
                    date=datetime.fromtimestamp(commit.committed_date).strftime(
                        "%Y-%m-%d %H:%M"
                    ),
                    message=commit.message.strip().split("\n")[0],
                    files_changed=files_changed,
                    insertions=insertions,
                    deletions=deletions,
                )
            )
    except Exception:
        pass

    return commits


def get_diff(path: Path) -> str:
    """
    Return the uncommitted diff (working tree vs index) as a string.
    """
    repo = _open_repo(path)

    try:
        diff_text = repo.git.diff()
        if not diff_text:
            staged_diff = repo.git.diff("--cached")
            if staged_diff:
                return staged_diff
            return "No uncommitted changes."
        return diff_text
    except Exception as exc:
        return f"Error getting diff: {exc}"


def get_branches(path: Path) -> list[BranchInfo]:
    """
    Return a list of all local branches, indicating which is current.
    """
    repo = _open_repo(path)
    branches: list[BranchInfo] = []

    try:
        current = repo.active_branch.name if not repo.head.is_detached else None
    except Exception:
        current = None

    for branch in repo.branches:
        branches.append(
            BranchInfo(name=branch.name, is_current=(branch.name == current))
        )

    return branches


def switch_branch(path: Path, branch_name: str) -> str:
    """
    Checkout a different branch.
    """
    repo = _open_repo(path)
    try:
        repo.git.checkout(branch_name)
        return f"Switched to branch '{branch_name}'"
    except GitCommandError as exc:
        return f"Error switching branch: {exc}"


def get_stashes(path: Path) -> list[StashEntry]:
    """
    Return a list of stash entries for the repo.
    """
    repo = _open_repo(path)
    stashes: list[StashEntry] = []
    try:
        stash_output = repo.git.stash("list")
        if stash_output.strip():
            for line in stash_output.strip().split("\n"):
                # Format: stash@{0}: WIP on main: abc1234 commit message
                parts = line.split(":", 1)
                idx_str = parts[0].strip()
                msg = parts[1].strip() if len(parts) > 1 else ""
                # Extract index number
                try:
                    idx = int(idx_str.split("{")[1].rstrip("}"))
                except (IndexError, ValueError):
                    idx = 0
                stashes.append(StashEntry(index=idx, message=msg))
    except Exception:
        pass
    return stashes


def get_remotes(path: Path) -> list[RemoteInfo]:
    """
    Return remote info including ahead/behind counts relative to tracking branch.
    """
    repo = _open_repo(path)
    remotes: list[RemoteInfo] = []

    for remote in repo.remotes:
        url = ""
        try:
            url = remote.url
        except Exception:
            pass

        ahead = 0
        behind = 0
        try:
            # Get ahead/behind for current branch vs remote tracking branch
            branch = repo.active_branch
            tracking = branch.tracking_branch()
            if tracking:
                rev_range_ahead = f"{tracking.name}..{branch.name}"
                rev_range_behind = f"{branch.name}..{tracking.name}"
                ahead = len(list(repo.iter_commits(rev_range_ahead)))
                behind = len(list(repo.iter_commits(rev_range_behind)))
        except Exception:
            pass

        remotes.append(RemoteInfo(
            name=remote.name,
            url=url,
            ahead=ahead,
            behind=behind,
        ))

    return remotes


def get_tags(path: Path, n: int = 15) -> list[TagInfo]:
    """
    Return the most recent `n` tags, sorted by date descending.
    """
    repo = _open_repo(path)
    tags: list[TagInfo] = []

    try:
        for tag_ref in repo.tags:
            try:
                # Annotated tag
                tag_obj = tag_ref.tag
                if tag_obj:
                    ts = tag_obj.tagged_date
                    date_str = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
                    message = (tag_obj.message or "").strip().split("\n")[0]
                    tagger = str(tag_obj.tagger) if tag_obj.tagger else ""
                else:
                    # Lightweight tag — use commit date
                    commit = tag_ref.commit
                    ts = commit.committed_date
                    date_str = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
                    message = commit.message.strip().split("\n")[0]
                    tagger = str(commit.author)

                tags.append(TagInfo(
                    name=tag_ref.name,
                    date=date_str,
                    message=message,
                    tagger=tagger,
                ))
            except Exception:
                tags.append(TagInfo(name=tag_ref.name, date="", message="", tagger=""))
    except Exception:
        pass

    # Sort by date descending, take first n
    tags.sort(key=lambda t: t.date, reverse=True)
    return tags[:n]


def stage_files(path: Path, files: list[str]) -> str:
    """
    Stage (git add) a list of files.
    Returns a success/error message.
    """
    repo = _open_repo(path)
    try:
        repo.index.add(files)
        return f"Staged {len(files)} file(s)"
    except Exception as exc:
        return f"Error staging files: {exc}"


def unstage_files(path: Path, files: list[str]) -> str:
    """
    Unstage (git reset HEAD) a list of files.
    Returns a success/error message.
    """
    repo = _open_repo(path)
    try:
        repo.index.reset("HEAD", paths=files)
        return f"Unstaged {len(files)} file(s)"
    except Exception as exc:
        return f"Error unstaging files: {exc}"


def stage_all(path: Path) -> str:
    """Stage all modified & untracked files (git add -A)."""
    repo = _open_repo(path)
    try:
        repo.git.add("-A")
        return "Staged all changes"
    except Exception as exc:
        return f"Error staging all: {exc}"


def unstage_all(path: Path) -> str:
    """Unstage everything (git reset HEAD)."""
    repo = _open_repo(path)
    try:
        repo.git.reset("HEAD")
        return "Unstaged all changes"
    except Exception as exc:
        return f"Error unstaging all: {exc}"


def commit_changes(path: Path, message: str) -> str:
    """
    Commit currently staged files with `message`.
    Returns a success/error message.
    """
    repo = _open_repo(path)
    try:
        if not message.strip():
            return "Error: commit message cannot be empty"
        # Check there is something staged
        try:
            staged = list(repo.index.diff("HEAD"))
        except Exception:
            staged = []
        if not staged:
            return "Nothing staged to commit. Stage files first."
        commit_obj = repo.index.commit(message.strip())
        short = commit_obj.hexsha[:7]
        first_line = message.strip().split("\n")[0][:60]
        return f"[{repo.active_branch.name} {short}] {first_line}"
    except Exception as exc:
        return f"Error committing: {exc}"


def create_branch(path: Path, name: str, checkout: bool = True) -> str:
    """
    Create a new local branch. If `checkout` is True, also switch to it.
    """
    repo = _open_repo(path)
    try:
        branch = repo.create_head(name)
        if checkout:
            branch.checkout()
            return f"Created and switched to branch '{name}'"
        return f"Created branch '{name}'"
    except Exception as exc:
        return f"Error creating branch: {exc}"


def delete_branch(path: Path, name: str, force: bool = False) -> str:
    """
    Delete a local branch. Pass `force=True` to force-delete unmerged branch.
    """
    repo = _open_repo(path)
    try:
        # Cannot delete the currently checked-out branch
        current = None
        try:
            current = repo.active_branch.name
        except Exception:
            pass
        if current == name:
            return f"Cannot delete the currently checked-out branch '{name}'"
        flag = "-D" if force else "-d"
        repo.git.branch(flag, name)
        return f"Deleted branch '{name}'"
    except Exception as exc:
        return f"Error deleting branch: {exc}"


def get_commit_diff(path: Path, commit_hash: str) -> str:
    """
    Return the full diff introduced by a specific commit (vs its parent).
    """
    repo = _open_repo(path)
    try:
        commit = repo.commit(commit_hash)
        if commit.parents:
            diff_text = repo.git.diff(commit.parents[0].hexsha, commit.hexsha)
        else:
            # Initial commit — diff against empty tree
            diff_text = repo.git.show("--format=", commit.hexsha)
        return diff_text if diff_text else "No changes in this commit."
    except Exception as exc:
        return f"Error getting commit diff: {exc}"


def get_file_diff(path: Path, filepath: str, staged: bool = False) -> str:
    """
    Return the diff for a single file.
    `staged=True` compares index vs HEAD; `staged=False` compares working tree vs index.
    """
    repo = _open_repo(path)
    try:
        if staged:
            diff = repo.git.diff("--cached", "--", filepath)
        else:
            diff = repo.git.diff("--", filepath)
        return diff if diff else f"No diff for {filepath}"
    except Exception as exc:
        return f"Error getting file diff: {exc}"


def get_changed_files(path: Path) -> dict[str, list[str]]:
    """
    Return a dict with keys 'staged', 'unstaged', 'untracked' — lists of file paths.
    Similar to get_status() but returns a plain dict for easier consumption.
    """
    repo = _open_repo(path)
    result: dict[str, list[str]] = {"staged": [], "unstaged": [], "untracked": []}
    try:
        try:
            result["staged"] = [d.a_path or d.b_path for d in repo.index.diff("HEAD")]
        except Exception:
            result["staged"] = []
        result["unstaged"] = [d.a_path or d.b_path for d in repo.index.diff(None)]
        result["untracked"] = list(repo.untracked_files)
    except Exception:
        pass
    return result


def get_file_tree(path: Path) -> "rich.tree.Tree":
    """
    Build a Rich Tree representing the repository's tracked file structure.

    Uses `git ls-files` to get only files tracked by git (respects .gitignore).
    Directories are shown in blue, files in the default colour.

    Args:
        path: Absolute path to the repository root.

    Returns:
        A rich.tree.Tree object ready to be rendered in a Static widget.
    """
    from rich.tree import Tree as RichTree
    from rich.text import Text

    repo = _open_repo(path)

    # Get all tracked files as relative paths
    try:
        ls_output = repo.git.ls_files()
        file_paths = [p for p in ls_output.splitlines() if p.strip()]
    except Exception:
        file_paths = []

    # Build a nested dict tree structure
    # e.g. {"src": {"main.py": None, "utils.py": None}, "README.md": None}
    def insert(tree_dict: dict, parts: list[str]) -> None:
        if not parts:
            return
        head, *tail = parts
        if tail:
            tree_dict.setdefault(head, {})
            if isinstance(tree_dict[head], dict):
                insert(tree_dict[head], tail)
        else:
            tree_dict[head] = None  # Leaf = file

    root_dict: dict = {}
    for fp in sorted(file_paths):
        parts = fp.replace("\\", "/").split("/")
        insert(root_dict, parts)

    # Render the dict structure into a Rich Tree
    repo_name = path.name
    rich_tree = RichTree(
        f"[bold #ff2d4a]📁 {repo_name}[/]",
        guide_style="#2a2a3a",
    )

    def build_tree(node: "RichTree", d: dict) -> None:
        # Sort: directories first, then files
        dirs = sorted(k for k, v in d.items() if isinstance(v, dict))
        files = sorted(k for k, v in d.items() if v is None)

        for name in dirs:
            branch = node.add(f"[bold #e040fb]📂 {name}[/]")
            build_tree(branch, d[name])

        for name in files:
            # Color-code by extension
            ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
            if ext in ("py", "js", "ts", "go", "rs", "c", "cpp", "java"):
                label = f"[#3ddc84]  {name}[/]"
            elif ext in ("md", "rst", "txt"):
                label = f"[#ffb74d]  {name}[/]"
            elif ext in ("json", "yaml", "yml", "toml", "ini", "cfg", "env"):
                label = f"[#4dd0e1]  {name}[/]"
            elif ext in ("sh", "bash", "zsh"):
                label = f"[#ff5252]  {name}[/]"
            else:
                label = f"[#d4d4dc]  {name}[/]"
            node.add(label)

    build_tree(rich_tree, root_dict)
    return rich_tree


# ===================================================================
# Remote operations
# ===================================================================

def git_fetch(path: Path, remote_name: str = "") -> str:
    """
    Fetch from remote(s). If *remote_name* is empty, fetches all remotes.
    Returns a human-readable status message.
    """
    repo = _open_repo(path)
    try:
        if remote_name:
            repo.git.fetch(remote_name)
            return f"Fetched from '{remote_name}' ✓"
        else:
            repo.git.fetch("--all")
            return "Fetched from all remotes ✓"
    except Exception as exc:
        return f"Error fetching: {exc}"


def git_pull(path: Path) -> str:
    """
    Pull from the current tracking branch.
    Returns the git output or a friendly message.
    """
    repo = _open_repo(path)
    try:
        result = repo.git.pull()
        return result.strip() if result.strip() else "Already up to date ✓"
    except Exception as exc:
        return f"Error pulling: {exc}"


def git_push(path: Path) -> str:
    """
    Push the current branch to its tracking remote.
    Returns the git output or a friendly message.
    """
    repo = _open_repo(path)
    try:
        result = repo.git.push()
        return result.strip() if result.strip() else "Push successful ✓"
    except Exception as exc:
        return f"Error pushing: {exc}"


def git_gc(path: Path) -> str:
    """Run git gc --auto to prune loose objects."""
    repo = _open_repo(path)
    try:
        repo.git.gc("--auto")
        return "gc --auto completed ✓"
    except Exception as exc:
        return f"Error running gc: {exc}"


def git_remote_prune(path: Path) -> str:
    """Run git remote prune origin to remove stale remote-tracking refs."""
    repo = _open_repo(path)
    try:
        result = repo.git.remote("prune", "origin")
        return result.strip() if result.strip() else "remote prune origin completed ✓"
    except Exception as exc:
        return f"Error pruning: {exc}"


def git_clean_dry(path: Path) -> str:
    """Run git clean -nd (dry run) and return untracked file list."""
    repo = _open_repo(path)
    try:
        result = repo.git.clean("-nd")
        return result.strip() if result.strip() else "Nothing to clean ✓"
    except Exception as exc:
        return f"Error running clean: {exc}"


# ===================================================================
# Stash operations
# ===================================================================

def stash_create(path: Path, message: str = "") -> str:
    """
    Create a new stash from the current working-tree changes.
    Returns a status message.
    """
    repo = _open_repo(path)
    try:
        if message.strip():
            repo.git.stash("push", "-m", message.strip())
        else:
            repo.git.stash("push")
        return "Changes stashed ✓"
    except Exception as exc:
        return f"Error creating stash: {exc}"


def stash_pop(path: Path) -> str:
    """
    Apply and remove the top stash entry.
    Returns the git output or a friendly message.
    """
    repo = _open_repo(path)
    try:
        result = repo.git.stash("pop")
        return result.strip() if result.strip() else "Stash applied and removed ✓"
    except Exception as exc:
        return f"Error popping stash: {exc}"


# ===================================================================
# Commit graph
# ===================================================================

def get_commit_graph(path: Path, n: int = 40) -> str:
    """
    Return the ASCII commit graph produced by:
        git log --graph --oneline --decorate -nN

    Suitable for display in a Static / ScrollableContainer widget.
    """
    repo = _open_repo(path)
    try:
        return repo.git.log(
            "--graph", "--oneline", "--decorate",
            "--color=never", f"-n{n}",
        )
    except Exception as exc:
        return f"Error loading commit graph: {exc}"


# ===================================================================
# File contents
# ===================================================================

def get_file_contents(path: Path, filepath: str) -> str:
    """
    Read the current working-tree content of a tracked file.
    Returns the file text, or an error message if unreadable.
    """
    full_path = path / filepath
    try:
        return full_path.read_text(errors="replace")
    except Exception as exc:
        return f"Error reading '{filepath}': {exc}"


def get_tracked_files(path: Path) -> list[str]:
    """
    Return a sorted list of all file paths tracked by git in the repo.
    Uses ``git ls-files`` so .gitignore is automatically respected.
    """
    repo = _open_repo(path)
    try:
        ls_output = repo.git.ls_files()
        return sorted(p for p in ls_output.splitlines() if p.strip())
    except Exception:
        return []


# ===================================================================
# Author / Digest operations
# ===================================================================

def default_branch_for(repo, candidates: list[str] | None = None) -> str | None:
    """Return the first candidate branch that exists locally, else None."""
    _candidates = candidates or ["main", "master", "develop", "trunk"]
    local_names = {b.name for b in repo.branches}
    for name in _candidates:
        if name in local_names:
            return name
    return None


def get_branch_details(
    path: Path,
    default_branches: list[str] | None = None,
) -> list[BranchDetail]:
    """Return rich details for every local branch in the repo."""
    import time as _time
    repo = _open_repo(path)
    details: list[BranchDetail] = []

    try:
        current = repo.active_branch.name if not repo.head.is_detached else None
    except Exception:
        current = None

    default = default_branch_for(repo, default_branches)

    # Collect merged branches (names only)
    merged: set[str] = set()
    if default:
        try:
            merged_out = repo.git.branch("--merged", default)
            for line in merged_out.splitlines():
                merged.add(line.strip().lstrip("* "))
        except Exception:
            pass

    _wip_re = re.compile(r"^(wip|fixup!|squash!|tmp)\b", re.IGNORECASE)
    now = _time.time()

    try:
        raw = repo.git.for_each_ref(
            "refs/heads/",
            format="%(refname:short)%00%(committerdate:unix)%00%(contents:subject)%00%(upstream:short)%00%(HEAD)",
        )
    except Exception:
        return details

    for line in raw.strip().splitlines():
        parts = line.split("\x00")
        if len(parts) < 5:
            continue
        bname, ts_str, subject, upstream, head_marker = parts[0], parts[1], parts[2], parts[3], parts[4]

        try:
            ts = float(ts_str)
        except ValueError:
            ts = 0.0

        age_days = int((now - ts) / 86400) if ts else 0

        details.append(BranchDetail(
            repo_path=path,
            repo_name=path.name,
            name=bname,
            last_commit_ts=ts,
            last_commit_msg=subject.strip()[:80],
            is_current=(head_marker.strip() == "*" or bname == current),
            has_upstream=bool(upstream.strip()),
            is_merged_into_default=(bname in merged),
            is_wip=bool(_wip_re.match(subject.strip())),
            age_days=age_days,
        ))

    return details


def get_author_email(path: Path) -> str:
    """Return the configured git user.email for this repo, or empty string."""
    repo = _open_repo(path)
    try:
        return repo.git.config("user.email").strip()
    except Exception:
        return ""


def get_author_commits(
    path: Path,
    since_ts: float,
    author_pattern: str,
) -> list[AuthorCommit]:
    """Return commits by *author_pattern* in *path* since *since_ts*.

    Uses ``--all`` so commits on any branch are credited. Parses shortstat
    blocks to extract insertions/deletions without iterating commit objects.
    """
    repo = _open_repo(path)
    commits: list[AuthorCommit] = []
    try:
        # Request log with NUL-separated records: hash\x1fts\x1fsubject
        # then a blank line and the shortstat
        raw = repo.git.log(
            "--all",
            "--no-merges",
            f"--since=@{int(since_ts)}",
            f"--author={author_pattern}",
            "--pretty=format:\x1e%H\x1f%ct\x1f%s",
            "--shortstat",
        )
        if not raw.strip():
            return []

        # Split on the record separator \x1e (prepended to each commit header)
        records = [r for r in raw.split("\x1e") if r.strip()]
        for record in records:
            lines = record.strip().splitlines()
            if not lines:
                continue
            header_line = lines[0]
            parts = header_line.split("\x1f", 2)
            if len(parts) < 3:
                continue
            full_hash, ts_str, subject = parts[0].strip(), parts[1], parts[2]
            try:
                ts = float(ts_str)
            except ValueError:
                continue

            insertions = 0
            deletions = 0
            files_changed = 0
            # shortstat line looks like: "3 files changed, 42 insertions(+), 5 deletions(-)"
            stat_lines = [l for l in lines[1:] if "changed" in l or "insertion" in l or "deletion" in l]
            for stat in stat_lines:
                m_files = re.search(r"(\d+) file", stat)
                m_ins   = re.search(r"(\d+) insertion", stat)
                m_del   = re.search(r"(\d+) deletion", stat)
                if m_files: files_changed = int(m_files.group(1))
                if m_ins:   insertions    = int(m_ins.group(1))
                if m_del:   deletions     = int(m_del.group(1))

            commits.append(AuthorCommit(
                short_hash=full_hash[:7],
                ts=ts,
                message=subject.strip(),
                insertions=insertions,
                deletions=deletions,
                files_changed=files_changed,
            ))
    except Exception:
        pass
    return commits
