"""Tests for appistry_integration.py — install-path safety.

APPISTRY_HOME is environment-controlled and may name any existing directory
(including the user's home directory), so _clone_appistry() must never
recursively delete an arbitrary existing path.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import appistry_integration as ai


class TestLooksLikeAppistryInstall:
    def test_true_when_all_markers_present(self, tmp_path):
        (tmp_path / ".git").mkdir()
        (tmp_path / "appistry").write_text("")
        (tmp_path / "appistry.py").write_text("")
        assert ai._looks_like_appistry_install(tmp_path) is True

    def test_false_when_markers_missing(self, tmp_path):
        assert ai._looks_like_appistry_install(tmp_path) is False

    def test_false_for_unrelated_directory(self, tmp_path):
        (tmp_path / "some_other_file.txt").write_text("hello")
        assert ai._looks_like_appistry_install(tmp_path) is False


class TestCloneAppistry:
    def test_refuses_to_touch_unrelated_existing_directory(self, tmp_path, monkeypatch):
        target = tmp_path / "not-appistry"
        target.mkdir()
        (target / "important-user-data.txt").write_text("do not delete me")
        monkeypatch.setattr(ai, "APPISTRY_PATH", target)

        rmtree = MagicMock()
        monkeypatch.setattr(ai.shutil, "rmtree", rmtree)

        assert ai._clone_appistry() is False
        rmtree.assert_not_called()
        assert (target / "important-user-data.txt").exists()

    def test_treats_existing_appistry_checkout_as_success(self, tmp_path, monkeypatch):
        target = tmp_path / "appistry-checkout"
        target.mkdir()
        (target / ".git").mkdir()
        (target / "appistry").write_text("")
        (target / "appistry.py").write_text("")
        monkeypatch.setattr(ai, "APPISTRY_PATH", target)

        rmtree = MagicMock()
        monkeypatch.setattr(ai.shutil, "rmtree", rmtree)
        run = MagicMock()
        monkeypatch.setattr(ai.subprocess, "run", run)

        assert ai._clone_appistry() is True
        rmtree.assert_not_called()
        run.assert_not_called()  # no clone needed — already installed

    def test_clones_when_path_does_not_exist(self, tmp_path, monkeypatch):
        target = tmp_path / "fresh-install"
        monkeypatch.setattr(ai, "APPISTRY_PATH", target)

        result = MagicMock(returncode=0, stderr="")
        run = MagicMock(return_value=result)
        monkeypatch.setattr(ai.subprocess, "run", run)

        assert ai._clone_appistry() is True
        run.assert_called_once()
        assert run.call_args.args[0][:2] == ["git", "clone"]

    def test_home_directory_is_never_deleted(self, tmp_path, monkeypatch):
        # Simulate the worst case: APPISTRY_HOME points at the user's home dir.
        home = tmp_path / "home" / "someuser"
        home.mkdir(parents=True)
        (home / "Documents").mkdir()
        monkeypatch.setattr(ai, "APPISTRY_PATH", home)

        rmtree = MagicMock()
        monkeypatch.setattr(ai.shutil, "rmtree", rmtree)

        assert ai._clone_appistry() is False
        rmtree.assert_not_called()
        assert (home / "Documents").exists()


def test_windows_install_bootstraps_with_current_python(tmp_path, monkeypatch):
    monkeypatch.setattr(ai, "APPISTRY_PATH", tmp_path)
    monkeypatch.setattr(ai.sys, "platform", "win32")
    result = MagicMock(returncode=0, stderr="")
    run = MagicMock(return_value=result)
    monkeypatch.setattr(ai.subprocess, "run", run)

    assert ai._install_appistry() is True

    assert run.call_args.args[0] == [
        ai.sys.executable,
        str(tmp_path / "appistry.py"),
        "install",
    ]
