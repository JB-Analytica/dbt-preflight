"""The base branch, checked out somewhere dbt can parse it."""

from __future__ import annotations

import contextlib
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path


class GitError(RuntimeError):
    pass


def _git(repo_root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=repo_root, text=True, capture_output=True, check=check
    )


def ensure_ref(repo_root: Path, ref: str) -> str:
    """Make `ref` resolvable, fetching it shallowly from origin when it is a remote branch.

    Returns the commit sha. CI checkouts are usually depth 1, so `origin/main` is often
    absent until fetched.
    """
    probe = _git(repo_root, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", check=False)
    if probe.returncode == 0:
        return probe.stdout.strip()

    if ref.startswith("origin/"):
        branch = ref[len("origin/") :]
        fetch = _git(
            repo_root,
            "fetch",
            "--no-tags",
            "--depth=1",
            "origin",
            f"+refs/heads/{branch}:refs/remotes/origin/{branch}",
            check=False,
        )
        if fetch.returncode == 0:
            probe = _git(repo_root, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
            return probe.stdout.strip()
        raise GitError(f"Could not fetch {ref}: {fetch.stderr.strip()}")

    raise GitError(f"{ref} is not a commit in this repository.")


@contextlib.contextmanager
def base_worktree(repo_root: Path, ref: str, dest: Path) -> Iterator[Path]:
    """A detached worktree of `ref` at `dest`, removed on exit."""
    sha = ensure_ref(repo_root, ref)
    if dest.exists():
        _git(repo_root, "worktree", "remove", "--force", str(dest), check=False)
        shutil.rmtree(dest, ignore_errors=True)
    dest.parent.mkdir(parents=True, exist_ok=True)
    result = _git(repo_root, "worktree", "add", "--detach", str(dest), sha, check=False)
    if result.returncode != 0:
        raise GitError(f"Could not check out {ref}: {result.stderr.strip()}")
    try:
        yield dest
    finally:
        _git(repo_root, "worktree", "remove", "--force", str(dest), check=False)
        shutil.rmtree(dest, ignore_errors=True)


def paths_changed(repo_root: Path, ref: str, paths: list[Path]) -> list[Path]:
    """Which of `paths` differ between `ref` and the working tree (staged or not)."""
    changed: list[Path] = []
    for path in paths:
        try:
            rel = str(path.resolve().relative_to(repo_root.resolve()))
        except ValueError:
            continue
        result = _git(repo_root, "diff", "--quiet", ref, "--", rel, check=False)
        if result.returncode == 1:
            changed.append(path)
    return changed


def git_root(start: Path) -> Path:
    """The repository root containing `start`, or `start` itself outside any repository."""
    result = _git(start, "rev-parse", "--show-toplevel", check=False)
    if result.returncode == 0 and result.stdout.strip():
        return Path(result.stdout.strip())
    return start


def head_sha(repo_root: Path) -> str | None:
    result = _git(repo_root, "rev-parse", "--short", "HEAD", check=False)
    return result.stdout.strip() if result.returncode == 0 else None
