"""
digest.py — Activity digest aggregation for GitPulse.

Collects commits authored by a set of email patterns across all scanned repos
within a time window, groups them by repo, and produces a Digest object.
Renders as markdown for stdout/clipboard use.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

try:
    from gitpulse.git_ops import RepoInfo, AuthorCommit, get_author_commits, get_author_email
    from gitpulse.parallel import run_parallel
    from gitpulse.utils import relative_time
except ImportError:
    from git_ops import RepoInfo, AuthorCommit, get_author_commits, get_author_email  # type: ignore
    from parallel import run_parallel  # type: ignore
    from utils import relative_time  # type: ignore


@dataclass
class RepoDigest:
    repo: RepoInfo
    commits: list[AuthorCommit]

    @property
    def insertions(self) -> int:
        return sum(c.insertions for c in self.commits)

    @property
    def deletions(self) -> int:
        return sum(c.deletions for c in self.commits)

    @property
    def files_changed(self) -> int:
        return sum(c.files_changed for c in self.commits)


@dataclass
class Digest:
    since_ts: float
    until_ts: float
    author_patterns: list[str]
    by_repo: list[RepoDigest] = field(default_factory=list)

    @property
    def total_commits(self) -> int:
        return sum(len(rd.commits) for rd in self.by_repo)

    @property
    def total_insertions(self) -> int:
        return sum(rd.insertions for rd in self.by_repo)

    @property
    def total_deletions(self) -> int:
        return sum(rd.deletions for rd in self.by_repo)

    @property
    def repos_active(self) -> int:
        return len(self.by_repo)


def _collect_for_repo(
    args: tuple[RepoInfo, float, list[str]],
) -> RepoDigest | None:
    """Worker target: fetch commits for one repo. Returns None if no commits."""
    repo, since_ts, author_patterns = args
    all_commits: list[AuthorCommit] = []
    for pattern in author_patterns:
        commits = get_author_commits(repo.path, since_ts, pattern)
        all_commits.extend(commits)

    if not all_commits:
        return None

    # De-duplicate by full hash (not short hash, which can collide in large repos)
    seen: set[str] = set()
    unique: list[AuthorCommit] = []
    for c in all_commits:
        key = c.full_hash or c.short_hash  # full_hash preferred
        if key not in seen:
            seen.add(key)
            unique.append(c)

    unique.sort(key=lambda c: c.ts, reverse=True)
    return RepoDigest(repo=repo, commits=unique)


def _resolve_author_patterns(
    repos: list[RepoInfo],
    explicit_patterns: list[str],
) -> list[str]:
    """If no explicit patterns given, try to read user.email from the first N repos."""
    if explicit_patterns:
        return explicit_patterns
    emails: set[str] = set()
    for repo in repos[:5]:
        email = get_author_email(repo.path)
        if email:
            emails.add(email)
    return list(emails) if emails else []


def build_digest(
    repos: list[RepoInfo],
    since_ts: float,
    author_patterns: list[str] | None = None,
    max_workers: int = 8,
) -> Digest:
    """Build a Digest aggregating all matching commits across *repos*."""
    patterns = _resolve_author_patterns(repos, author_patterns or [])
    until_ts = time.time()

    if not patterns:
        return Digest(since_ts=since_ts, until_ts=until_ts, author_patterns=[])

    args_list = [(repo, since_ts, patterns) for repo in repos]
    results = run_parallel(_collect_for_repo, args_list, max_workers=max_workers)

    by_repo = [
        result
        for _, result in results
        if result is not None and not isinstance(result, Exception)
    ]
    by_repo.sort(key=lambda rd: len(rd.commits), reverse=True)

    return Digest(
        since_ts=since_ts,
        until_ts=until_ts,
        author_patterns=patterns,
        by_repo=by_repo,
    )


def render_markdown(d: Digest) -> str:
    """Render a Digest as a markdown standup summary."""
    from datetime import datetime, timezone

    lines: list[str] = []
    since_str = datetime.fromtimestamp(d.since_ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    until_str = datetime.fromtimestamp(d.until_ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    authors_str = ", ".join(d.author_patterns) if d.author_patterns else "all authors"

    lines.append(f"# Activity digest — {since_str} → {until_str}")
    lines.append(f"**Author(s):** {authors_str}  ")
    lines.append(
        f"**Summary:** {d.total_commits} commit{'s' if d.total_commits != 1 else ''} "
        f"across {d.repos_active} repo{'s' if d.repos_active != 1 else ''} "
        f"· +{d.total_insertions} -{d.total_deletions} lines"
    )
    lines.append("")

    for rd in d.by_repo:
        lines.append(f"## {rd.repo.name} ({len(rd.commits)} commits · +{rd.insertions} -{rd.deletions})")
        for c in rd.commits:
            rel = relative_time(c.ts)
            stats = f"+{c.insertions}/-{c.deletions}" if c.insertions or c.deletions else ""
            stats_str = f" `{stats}`" if stats else ""
            lines.append(f"- `{c.short_hash}` {c.message}{stats_str} _{rel}_")
        lines.append("")

    return "\n".join(lines)
