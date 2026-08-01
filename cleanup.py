"""
cleanup.py

Smart git-aware project cleanup for unregistered apps. Removes clean tracked
files while preserving gitignored files (user data, secrets, venvs), modified
tracked files, and untracked files.
"""

import subprocess
from pathlib import Path


def git_clean_project(cwd: Path) -> bool:
    """
    Delete clean tracked files from a git project directory.

    Preserved:
      - Gitignored files (user data, secrets, venvs, etc.)
      - Tracked files with uncommitted changes (staged or unstaged)
      - Untracked files

    Deleted:
      - Tracked files whose content matches HEAD

    Returns True if cleanup ran, False if cwd is not a git repo with at least
    one commit (in which case no files are touched).
    """
    # Ensure it's a git repo with at least one commit
    r = subprocess.run(
        ["git", "-C", str(cwd), "rev-parse", "HEAD"],
        capture_output=True,
    )
    if r.returncode != 0:
        return False

    # All tracked files
    r = subprocess.run(
        ["git", "-C", str(cwd), "ls-files"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        return False
    tracked = set(r.stdout.splitlines())

    # Files that differ from HEAD (staged or unstaged) — preserve these
    r = subprocess.run(
        ["git", "-C", str(cwd), "diff", "HEAD", "--name-only"],
        capture_output=True, text=True,
    )
    modified = set(r.stdout.splitlines()) if r.returncode == 0 else set()

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
