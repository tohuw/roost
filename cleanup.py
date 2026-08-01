"""
cleanup.py

Smart git-aware project cleanup for unregistered apps. Removes clean tracked
files while preserving gitignored files (user data, secrets, venvs), modified
tracked files, and untracked files.
"""

import os
import subprocess
from pathlib import Path


# Appistry runs `git` over directories it did not create (a registry entry names
# an arbitrary `cwd`), and git reads configuration *from those directories*.
# `core.fsmonitor` and `core.hooksPath` are command hooks that fire on plain
# read-only verbs like `ls-files` and `diff`, so a repo checked out by anyone
# else could turn "clean up this project" into arbitrary command execution.
# Neutralise every config source git would consult.
_GIT_SAFE_FLAGS = [
    "-c", "core.fsmonitor=false",
    "-c", f"core.hooksPath={os.devnull}",
]


def _git_safe_env() -> dict:
    """Return an env that stops git reading global/system config or prompting."""
    env = os.environ.copy()
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_SYSTEM"] = os.devnull
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def _git(cwd: Path, *args: str, text: bool = False) -> subprocess.CompletedProcess:
    """Run a read-only git command against an untrusted repository."""
    return subprocess.run(
        ["git", "-C", str(cwd), *_GIT_SAFE_FLAGS, *args],
        capture_output=True,
        text=text,
        env=_git_safe_env(),
    )


def _split_nul(payload: str) -> set[str]:
    """Split a git `-z` payload. Filenames may legally contain newlines."""
    return {item for item in payload.split("\0") if item}


def git_clean_project(cwd: Path) -> bool:
    """
    Delete clean tracked files from a git project directory.

    Preserved:
      - Gitignored files (user data, secrets, venvs, etc.)
      - Tracked files with uncommitted changes (staged or unstaged)
      - Untracked files

    Deleted:
      - Tracked files whose content matches HEAD

    Returns True if cleanup ran, False if no files were touched — which is the
    case when cwd is not a git repo with at least one commit, or when cwd is a
    *subdirectory* of a repository rather than its root. Refusing on a
    subdirectory is deliberate: this is a destructive path with no undo, and
    only a repository root can be reasoned about as "the app's project".
    """
    # Ensure it's a git repo with at least one commit
    r = _git(cwd, "rev-parse", "HEAD")
    if r.returncode != 0:
        return False

    # Only ever operate on a repository root. Paths reported by `ls-files` and
    # `diff` are anchored differently (worktree-relative vs. repo-root-relative
    # by default), and a subdirectory cwd is exactly the case where a mismatch
    # silently promotes "modified" files into the delete set.
    r = _git(cwd, "rev-parse", "--show-toplevel", text=True)
    if r.returncode != 0:
        return False
    toplevel = r.stdout.strip()
    if not toplevel:
        return False
    try:
        if Path(toplevel).resolve() != Path(cwd).resolve():
            return False
    except OSError:
        return False

    # All tracked files, NUL-separated so newlines in filenames are safe
    r = _git(cwd, "ls-files", "-z", text=True)
    if r.returncode != 0:
        return False
    tracked = _split_nul(r.stdout)

    # Files that differ from HEAD (staged or unstaged) — preserve these.
    # `--relative` anchors the output to the cwd so it is directly comparable
    # to `ls-files`; without it these two sets can fail to intersect at all,
    # which would make every modified file look clean and get deleted.
    r = _git(cwd, "diff", "HEAD", "--relative", "--name-only", "-z", text=True)
    modified = _split_nul(r.stdout) if r.returncode == 0 else None
    if modified is None:
        # Never guess. If we cannot enumerate modified files we cannot tell
        # which files are safe to delete, so touch nothing.
        return False

    # Delete clean tracked files
    for rel in tracked - modified:
        path = cwd / rel
        try:
            if path.is_file() or path.is_symlink():
                path.unlink()
        except OSError:
            pass

    # Remove empty directories, leaving .git and any dir that still has content
    for dirpath in sorted(cwd.rglob("*"), reverse=True):
        if dirpath.is_dir() and ".git" not in dirpath.parts:
            try:
                dirpath.rmdir()  # no-op unless empty
            except OSError:
                pass

    return True
