"""Tests for Roost's own state directory and its owner-only write helpers."""

import os
import stat
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from roost import paths

_POSIX_ONLY = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX mode bits are not meaningful on Windows"
)


class TestSecureDir:
    def test_creates_nested_directories(self, tmp_path):
        target = tmp_path / "a" / "b" / "c"
        assert paths.secure_dir(target) == target
        assert target.is_dir()

    def test_is_idempotent(self, tmp_path):
        paths.secure_dir(tmp_path / "d")
        paths.secure_dir(tmp_path / "d")
        assert (tmp_path / "d").is_dir()

    @_POSIX_ONLY
    def test_directory_is_owner_only(self, tmp_path):
        target = paths.secure_dir(tmp_path / "state")
        mode = stat.S_IMODE(target.stat().st_mode)
        assert mode == 0o700, oct(mode)

    @_POSIX_ONLY
    def test_mode_is_applied_explicitly_not_left_to_umask(self, tmp_path):
        """mkdir(mode=) is masked by umask; the chmod must not be."""
        previous = os.umask(0o000)
        try:
            target = paths.secure_dir(tmp_path / "umask-test")
        finally:
            os.umask(previous)
        assert stat.S_IMODE(target.stat().st_mode) == 0o700


class TestAtomicWriteText:
    def test_writes_the_content(self, tmp_path):
        target = tmp_path / "state" / "port"
        paths.atomic_write_text(target, "47100")
        assert target.read_text(encoding="utf-8") == "47100"

    def test_replaces_existing_content(self, tmp_path):
        target = tmp_path / "port"
        paths.atomic_write_text(target, "1")
        paths.atomic_write_text(target, "2")
        assert target.read_text(encoding="utf-8") == "2"

    def test_creates_missing_parents(self, tmp_path):
        target = tmp_path / "deep" / "nest" / "file"
        paths.atomic_write_text(target, "x")
        assert target.is_file()

    def test_leaves_no_temporary_files_behind(self, tmp_path):
        paths.atomic_write_text(tmp_path / "file", "x")
        assert sorted(item.name for item in tmp_path.iterdir()) == ["file"]

    @_POSIX_ONLY
    def test_file_is_owner_only(self, tmp_path):
        target = tmp_path / "secret"
        paths.atomic_write_text(target, "value")
        mode = stat.S_IMODE(target.stat().st_mode)
        assert mode == 0o600, oct(mode)

    @_POSIX_ONLY
    def test_file_is_never_briefly_world_readable(self, tmp_path):
        """The chmod happens on the temp file, before it is moved into place."""
        target = tmp_path / "secret"
        previous = os.umask(0o000)
        try:
            paths.atomic_write_text(target, "value")
        finally:
            os.umask(previous)
        assert stat.S_IMODE(target.stat().st_mode) == 0o600

    @_POSIX_ONLY
    def test_rewriting_does_not_widen_the_mode(self, tmp_path):
        target = tmp_path / "secret"
        paths.atomic_write_text(target, "one")
        target.chmod(0o644)
        paths.atomic_write_text(target, "two")
        assert stat.S_IMODE(target.stat().st_mode) == 0o600

    def test_temporary_file_is_cleaned_up_on_failure(self, tmp_path, monkeypatch):
        def _boom(*_args, **_kwargs):
            raise OSError("replace failed")

        monkeypatch.setattr(paths.os, "replace", _boom)
        with pytest.raises(OSError):
            paths.atomic_write_text(tmp_path / "file", "x")
        assert list(tmp_path.iterdir()) == []


class TestStateDir:
    def test_returns_and_creates_the_state_directory(self, tmp_path, monkeypatch):
        monkeypatch.setattr(paths, "STATE_DIR", tmp_path / "roost")
        result = paths.ensure_state_dir()
        assert result == tmp_path / "roost"
        assert result.is_dir()


class TestRestrictToOwner:
    def test_missing_path_does_not_raise(self, tmp_path):
        """Best-effort by contract: a race that removes the file is not fatal."""
        paths.restrict_to_owner(tmp_path / "gone")
