"""Tests for the git-aware project cleanup that runs when an app is removed.

This path deletes files automatically, with no confirmation and no undo, so
every test here is about what it must *refuse* to delete.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import cleanup


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env.update({
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.invalid",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.invalid",
    })
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True, text=True, env=env, check=True,
    )


def _repo(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q", "-b", "main")
    return root


def _commit_all(root: Path, message: str = "seed") -> None:
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", message)


pytestmark = pytest.mark.skipif(
    subprocess.run(["git", "--version"], capture_output=True).returncode != 0,
    reason="git is unavailable",
)


def test_clean_repo_root_deletes_only_unmodified_tracked_files(tmp_path):
    root = _repo(tmp_path / "project")
    (root / "committed.txt").write_text("original", encoding="utf-8")
    (root / "precious.txt").write_text("original", encoding="utf-8")
    (root / ".gitignore").write_text("data/\n", encoding="utf-8")
    _commit_all(root)
    (root / "precious.txt").write_text("MODIFIED", encoding="utf-8")
    (root / "untracked.txt").write_text("mine", encoding="utf-8")
    (root / "data").mkdir()
    (root / "data" / "db.sqlite").write_text("user data", encoding="utf-8")

    assert cleanup.git_clean_project(root) is True

    assert not (root / "committed.txt").exists()
    assert (root / "precious.txt").read_text(encoding="utf-8") == "MODIFIED"
    assert (root / "untracked.txt").exists()
    assert (root / "data" / "db.sqlite").exists()


def test_subdirectory_cwd_preserves_modified_files(tmp_path):
    """Regression: F-2 data loss.

    `ls-files` reports paths relative to the cwd while `diff HEAD --name-only`
    reports them relative to the repository root. On a subdirectory cwd the two
    sets could not intersect, so `tracked - modified` was *every* tracked file
    and uncommitted work was deleted.
    """
    root = _repo(tmp_path / "project")
    sub = root / "app"
    sub.mkdir()
    (sub / "precious.txt").write_text("original", encoding="utf-8")
    (sub / "clean.txt").write_text("original", encoding="utf-8")
    _commit_all(root)
    (sub / "precious.txt").write_text("MODIFIED", encoding="utf-8")

    cleanup.git_clean_project(sub)

    assert (sub / "precious.txt").exists(), "uncommitted work was deleted"
    assert (sub / "precious.txt").read_text(encoding="utf-8") == "MODIFIED"


def test_subdirectory_cwd_is_refused_outright(tmp_path):
    """A non-root cwd deletes nothing and reports that cleanup did not run."""
    root = _repo(tmp_path / "project")
    sub = root / "app"
    sub.mkdir()
    (sub / "clean.txt").write_text("original", encoding="utf-8")
    _commit_all(root)

    assert cleanup.git_clean_project(sub) is False
    assert (sub / "clean.txt").exists()


def test_diff_output_is_anchored_to_cwd(tmp_path):
    """`--relative` is what makes the two path sets comparable."""
    root = _repo(tmp_path / "project")
    sub = root / "app"
    sub.mkdir()
    (sub / "precious.txt").write_text("original", encoding="utf-8")
    _commit_all(root)
    (sub / "precious.txt").write_text("MODIFIED", encoding="utf-8")

    result = cleanup._git(sub, "diff", "HEAD", "--relative", "--name-only", "-z", text=True)

    assert cleanup._split_nul(result.stdout) == {"precious.txt"}


def test_filename_containing_a_newline_is_not_split(tmp_path):
    """NUL-delimited output means a newline in a filename yields one entry."""
    root = _repo(tmp_path / "project")
    weird = "two\nlines.txt"
    (root / weird).write_text("original", encoding="utf-8")
    _commit_all(root)
    (root / weird).write_text("MODIFIED", encoding="utf-8")

    listed = cleanup._split_nul(
        cleanup._git(root, "ls-files", "-z", text=True).stdout
    )
    modified = cleanup._split_nul(
        cleanup._git(root, "diff", "HEAD", "--relative", "--name-only", "-z", text=True).stdout
    )

    assert listed == {weird}
    assert modified == {weird}
    assert not (listed - modified), "a newline in a filename must not fake a clean file"

    assert cleanup.git_clean_project(root) is True
    assert (root / weird).read_text(encoding="utf-8") == "MODIFIED"


def test_non_repo_directory_is_left_untouched(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "file.txt").write_text("mine", encoding="utf-8")

    assert cleanup.git_clean_project(plain) is False
    assert (plain / "file.txt").exists()


def test_repo_without_commits_is_left_untouched(tmp_path):
    root = _repo(tmp_path / "project")
    (root / "file.txt").write_text("mine", encoding="utf-8")

    assert cleanup.git_clean_project(root) is False
    assert (root / "file.txt").exists()


def test_repo_config_command_hooks_do_not_execute(tmp_path):
    """Regression: F-8 command execution via attacker-influenced git config.

    `core.fsmonitor` is a command hook that git runs during `ls-files` and
    `diff`. A registry entry can point at any directory, so a repo the user did
    not create must not be able to run commands just by being cleaned up.
    """
    root = _repo(tmp_path / "project")
    (root / "file.txt").write_text("original", encoding="utf-8")
    _commit_all(root)
    marker = tmp_path / "pwned"
    hook = tmp_path / "fsmonitor.sh"
    hook.write_text(f"#!/bin/sh\ntouch {marker}\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)
    _git(root, "config", "core.fsmonitor", str(hook))

    cleanup.git_clean_project(root)

    assert not marker.exists(), "core.fsmonitor from the target repo was executed"


def test_git_invocation_neutralises_config_and_prompts():
    assert "-c" in cleanup._GIT_SAFE_FLAGS
    assert "core.fsmonitor=false" in cleanup._GIT_SAFE_FLAGS
    assert f"core.hooksPath={os.devnull}" in cleanup._GIT_SAFE_FLAGS

    env = cleanup._git_safe_env()
    assert env["GIT_CONFIG_GLOBAL"] == os.devnull
    assert env["GIT_CONFIG_SYSTEM"] == os.devnull
    assert env["GIT_TERMINAL_PROMPT"] == "0"


def test_unreadable_diff_deletes_nothing(tmp_path, monkeypatch):
    """If modified files cannot be enumerated, nothing is safe to delete."""
    root = _repo(tmp_path / "project")
    (root / "file.txt").write_text("original", encoding="utf-8")
    _commit_all(root)

    real_git = cleanup._git

    def fake_git(cwd, *args, **kwargs):
        if args and args[0] == "diff":
            return subprocess.CompletedProcess(args, 1, "", "boom")
        return real_git(cwd, *args, **kwargs)

    monkeypatch.setattr(cleanup, "_git", fake_git)

    assert cleanup.git_clean_project(root) is False
    assert (root / "file.txt").exists()


def _menubar_module():
    import types

    fake_rumps = types.ModuleType("rumps")
    fake_rumps.App = type("App", (), {})
    fake_rumps.timer = lambda _s: (lambda fn: fn)
    fake_rumps.MenuItem = object
    sys.modules.setdefault("rumps", fake_rumps)

    import menubar

    return menubar


def _removal_entry(cwd: Path):
    from registry import AppEntry

    return AppEntry(
        id="widget", name="Widget", cwd=str(cwd),
        command="node server.js", port=8009,
        github_url="https://github.com/example/widget",
    )


def _tray_app(menubar, monkeypatch):
    monkeypatch.setattr(menubar.process, "is_running", lambda _app_id: False)
    monkeypatch.setattr(menubar.registry, "remove", lambda _app_id: True)
    monkeypatch.setattr(menubar, "_osascript_notify", lambda *_: None)
    app = menubar.AppistryApp.__new__(menubar.AppistryApp)
    app._known_bundles = {"widget"}
    app._bundle_missing_since = {"widget": 0.0}
    return app


def test_automatic_removal_path_cleans_a_repo_root(tmp_path, monkeypatch):
    """The real trigger: cleanup runs unattended ~15s after a bundle is trashed."""
    menubar = _menubar_module()
    root = _repo(tmp_path / "project")
    (root / "shipped.txt").write_text("original", encoding="utf-8")
    (root / "precious.txt").write_text("original", encoding="utf-8")
    _commit_all(root)
    (root / "precious.txt").write_text("MY UNCOMMITTED WORK", encoding="utf-8")

    _tray_app(menubar, monkeypatch)._handle_removed(_removal_entry(root))

    assert not (root / "shipped.txt").exists(), "clean tracked file should be removed"
    assert (root / "precious.txt").read_text(encoding="utf-8") == "MY UNCOMMITTED WORK"


def test_automatic_removal_path_preserves_work_under_a_subdirectory_cwd(
    tmp_path, monkeypatch
):
    """Regression: F-2 end-to-end, with no attacker and no bad registry entry.

    A perfectly ordinary registry entry whose `cwd` is a subdirectory of a repo
    used to have every one of its tracked files deleted — uncommitted work
    included — automatically and with no undo.
    """
    menubar = _menubar_module()
    root = _repo(tmp_path / "project")
    sub = root / "app"
    sub.mkdir()
    (sub / "precious.txt").write_text("original", encoding="utf-8")
    _commit_all(root)
    (sub / "precious.txt").write_text("MY UNCOMMITTED WORK", encoding="utf-8")

    _tray_app(menubar, monkeypatch)._handle_removed(_removal_entry(sub))

    assert (sub / "precious.txt").exists(), "uncommitted work was deleted"
    assert (sub / "precious.txt").read_text(encoding="utf-8") == "MY UNCOMMITTED WORK"
