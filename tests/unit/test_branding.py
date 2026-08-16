"""What Windows calls the tray in Settings > Taskbar.

It was "Python". Windows names a notification-area entry after the
*executable's* ``FileDescription``, falling back to the filename when there is
none — and the tray runs on the base interpreter, whose FileDescription is
"Python". Confirmed by reading ``HKCU\\Control Panel\\NotifyIconSettings``, which
records the interpreter's path rather than Roost's, so nothing pystray sets
could have changed it.

The fix is a copy of that interpreter, named ``Roost.exe``, with its version
resource removed. Most of this is exercised on every platform; only the actual
PE surgery is Windows-only.
"""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from roost import windows_support


class TestTheHoverTooltip:
    """The other place the tray said something that was not its name.

    Read as source rather than imported: ``windows_tray`` needs pystray, which
    is a Windows-only dependency, and this identity is worth pinning on every
    platform's CI run.
    """

    SOURCE = (Path(__file__).resolve().parents[2]
              / "roost" / "windows_tray.py").read_text(encoding="utf-8")

    def test_it_names_the_app(self):
        assert 'TOOLTIP = "Roost"' in self.SOURCE

    def test_the_icon_is_given_the_tooltip_rather_than_a_literal(self):
        """It said "Ravens" — the menu's contents, which reads as another app.

        Walks the AST rather than the text: the comment explaining the old
        value names it, and a substring check cannot tell an explanation of
        the hazard from the hazard.
        """
        import ast

        assert "_tray_image(), TOOLTIP," in self.SOURCE
        literals = {
            node.value for node in ast.walk(ast.parse(self.SOURCE))
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        }
        assert "Ravens" not in literals


def _venv(tmp_path: Path, home: Path) -> Path:
    repo = tmp_path / "roost"
    (repo / ".venv" / "Scripts").mkdir(parents=True)
    (repo / ".venv" / "pyvenv.cfg").write_text(
        f"home = {home}\ninclude-system-site-packages = false\nversion = 3.14.2\n",
        encoding="utf-8",
    )
    return repo


class TestFindingTheRealInterpreter:
    def test_the_home_key_names_it(self, tmp_path):
        """uv's Scripts\\pythonw.exe is a trampoline that re-execs the base
        interpreter, and the *child* owns the tray icon — so renaming the
        trampoline would change nothing."""
        home = tmp_path / "Python314"
        home.mkdir()
        (home / "pythonw.exe").write_bytes(b"MZ")
        repo = _venv(tmp_path, home)
        assert windows_support._base_interpreter(repo) == home / "pythonw.exe"

    def test_a_home_that_does_not_exist_yields_none(self, tmp_path):
        repo = _venv(tmp_path, tmp_path / "gone")
        assert windows_support._base_interpreter(repo) is None

    def test_a_missing_config_yields_none(self, tmp_path):
        repo = tmp_path / "roost"
        (repo / ".venv").mkdir(parents=True)
        assert windows_support._base_interpreter(repo) is None


class TestLauncherSelection:
    def test_the_tray_prefers_the_branded_copy(self, tmp_path):
        branded = tmp_path / "Roost.exe"
        with patch.object(windows_support, "branded_launcher", return_value=branded):
            chosen = windows_support._venv_executable(tmp_path, windowed=True)
        assert chosen == branded

    def test_the_tray_falls_back_to_pythonw(self, tmp_path):
        """A tray that starts under the wrong name beats one that does not start."""
        with patch.object(windows_support, "branded_launcher", return_value=None):
            chosen = windows_support._venv_executable(tmp_path, windowed=True)
        assert chosen.name == "pythonw.exe"

    def test_the_console_path_is_untouched(self, tmp_path):
        """Only the tray has a shell identity worth branding."""
        with patch.object(windows_support, "branded_launcher") as branded:
            chosen = windows_support._venv_executable(tmp_path, windowed=False)
        assert chosen.name == "python.exe"
        branded.assert_not_called()

    def test_nothing_is_staged_off_windows(self, tmp_path):
        with patch.object(windows_support, "is_windows", return_value=False):
            assert windows_support.branded_launcher(tmp_path) is None


class TestTheVersionBlob:
    """The binary shape, checked without needing Windows to parse it.

    VS_VERSIONINFO is length-prefixed and 32-bit aligned throughout, and the
    alignment is measured from each structure's own start — including the
    two-byte length field. Get that wrong and the blob still writes, still
    reads back as *some* size, and the shell quietly declines to show a name.
    """

    def test_every_node_is_four_byte_aligned(self):
        blob = windows_support._version_resource("Roost", "2026.8.4")
        assert len(blob) % 4 == 0

    def test_the_root_declares_its_own_length(self):
        import struct

        blob = windows_support._version_resource("Roost", "2026.8.4")
        # wLength excludes any padding the caller adds after it, so it is the
        # blob's own length here.
        assert struct.unpack("<H", blob[:2])[0] == len(blob)

    def test_it_is_a_version_resource_at_all(self):
        blob = windows_support._version_resource("Roost", "2026.8.4")
        assert b"V\x00S\x00_\x00V\x00E\x00R\x00S\x00I\x00O\x00N" in blob
        assert b"\xbd\x04\xef\xfe" in blob        # the VS_FIXEDFILEINFO signature

    def test_the_description_is_the_one_asked_for(self):
        blob = windows_support._version_resource("Roost", "2026.8.4")
        assert "FileDescription".encode("utf-16-le") in blob
        assert "Roost".encode("utf-16-le") in blob

    def test_a_version_with_too_few_parts_is_padded_not_refused(self):
        """VERSION is CalVer with three parts; the fixed header wants four."""
        assert windows_support._version_resource("Roost", "2026.8")
        assert windows_support._version_resource("Roost", "")


def _repo_has_a_venv() -> bool:
    """Is this checkout laid out as a venv install?

    Branding stages a copy of the venv's *base* interpreter, which it finds
    through ``.venv/pyvenv.cfg``. CI installs into the system Python with no
    venv at all, so there is nothing to copy and ``branded_launcher`` correctly
    returns None -- a fact about the checkout rather than a regression. The
    class above already covers the selection logic on every platform; this is
    only the real PE surgery.
    """
    return windows_support._base_interpreter(
        Path(__file__).resolve().parents[2]) is not None


@pytest.mark.skipif(sys.platform != "win32", reason="PE resources are Windows-only")
@pytest.mark.skipif(not _repo_has_a_venv(), reason="no .venv in this checkout to brand")
class TestOnWindows:
    def test_a_staged_copy_says_its_name_outright(self, tmp_path):
        """Named, not merely unnamed — the actual PE surgery.

        Stripping the resource was the first attempt and it left the shell to
        fall back to the filename, which includes the extension: Taskbar
        settings read "Roost.exe". The only way to drop that is to stop relying
        on the fallback and write the name.

        Done on the test's own copy rather than the repo's launcher, because
        Windows holds a running executable's image open — a developer with the
        tray up would otherwise fail this for a reason that is not about the
        code.
        """
        import shutil

        repo = Path(__file__).resolve().parents[2]
        source = windows_support._base_interpreter(repo)
        assert source is not None
        copy = tmp_path / windows_support.BRANDED_LAUNCHER
        shutil.copy2(source, copy)
        assert windows_support.file_description(copy) == "Python"

        assert windows_support._write_version_resource(copy, "Roost", "2026.8.5")
        assert windows_support.file_description(copy) == "Roost"
        assert windows_support.file_description(copy) != "Roost.exe"

    def test_the_launcher_is_named_once_it_can_be_staged(self, tmp_path):
        repo = Path(__file__).resolve().parents[2]
        launcher = windows_support.branded_launcher(repo)
        assert launcher is not None
        assert launcher.name == windows_support.BRANDED_LAUNCHER
        if windows_support.file_description(launcher) != "Roost":
            pytest.skip("a running tray is holding the launcher open")

    def test_the_interpreter_it_copied_is_left_alone(self, tmp_path):
        """The rewrite must touch the copy, never the installed interpreter."""
        repo = Path(__file__).resolve().parents[2]
        source = windows_support._base_interpreter(repo)
        assert source is not None
        assert windows_support.file_description(source) == "Python"

    def test_it_is_not_restaged_every_call(self, tmp_path):
        repo = Path(__file__).resolve().parents[2]
        first = windows_support.branded_launcher(repo)
        stamp = first.stat().st_mtime_ns
        assert windows_support.branded_launcher(repo).stat().st_mtime_ns == stamp

    def test_the_copy_still_resolves_the_venv(self):
        """A copy in Scripts is how a classic venv is laid out, so CPython finds
        pyvenv.cfg beside it — the identity changes, what runs does not."""
        import subprocess

        repo = Path(__file__).resolve().parents[2]
        launcher = windows_support.branded_launcher(repo)
        out = subprocess.run(
            [str(launcher), "-c", "import sys, roost; print(sys.prefix)"],
            capture_output=True, text=True, timeout=120, cwd=str(repo),
        )
        assert out.returncode == 0, out.stderr
        assert str(repo / ".venv") in out.stdout
