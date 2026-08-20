"""Coexistence with the separate internal Appistry.

This project was forked from an internal tool also called Appistry — an app
launcher, still in daily use, installed on the same machines and the same user
accounts. Both must be installable and runnable *at the same time*, which means
this project may not own any filesystem path, port file, lock, launch-agent
label, Windows shortcut or module identity, or console-script name that the other
one owns.

**Why the other project's values are literals here.** Appistry is not importable
from this repository and never will be: it is a separate, private codebase, and
the whole point of the fork was to stop depending on it. So its well-known values
are pinned as constants below, each with a comment naming where it came from. That
makes the guarantee testable rather than aspirational — if someone later moves this
project's state back to ``~/.appistry`` or renames the console script to
``appistry``, these tests fail even though neither codebase can see the other.

If the internal tool ever changes one of its own names, the constants below go
stale in the safe direction: they would describe a collision that no longer
exists, causing this suite to over-constrain rather than to miss a real clash.
"""

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from roost import cli
from roost import help_server
from roost import host
from roost import paths
from roost import birds
from roost import windows_support

REPO = Path(__file__).resolve().parents[2]


# ── The other project's well-known values ─────────────────────────────────────
# Each of these is a literal because the internal repository cannot be imported
# here. The trailing comment is the provenance.

#: registry.py: ``APPISTRY_DIR = Path.home() / ".appistry"``. Everything below
#: lives inside it.
APPISTRY_STATE_DIR = Path.home() / ".appistry"

#: registry.py: ``REGISTRY_PATH = APPISTRY_DIR / "registry.toml"`` — the app
#: catalog. Corrupting this would break the launcher outright.
APPISTRY_REGISTRY = APPISTRY_STATE_DIR / "registry.toml"

#: process.py: ``PIDS_DIR = APPISTRY_DIR / "pids"`` — one PID file per launched
#: app.
APPISTRY_PIDS_DIR = APPISTRY_STATE_DIR / "pids"

#: process.py: ``SECRETS_DIR = APPISTRY_DIR / "secrets"`` — per-launch secrets.
APPISTRY_SECRETS_DIR = APPISTRY_STATE_DIR / "secrets"

#: menubar.py: ``_HELP_PORT_PATH = registry.APPISTRY_DIR / "menubar-http-port"``,
#: also read by appistry.py and windows_support.py (``_CONTROL_PORT_FILE``).
APPISTRY_PORT_FILE = APPISTRY_STATE_DIR / "menubar-http-port"

#: hooks.py: ``HOOK_PORT_FILE = "stable-hook-port"``.
APPISTRY_HOOK_PORT_FILE = APPISTRY_STATE_DIR / "stable-hook-port"

#: windows_support.py: ``_TRAY_PID_FILE = "windows-tray.pid"``.
APPISTRY_TRAY_PID = APPISTRY_STATE_DIR / "windows-tray.pid"

#: windows_support.py / windows_tray.py: the tray log.
APPISTRY_LOG = APPISTRY_STATE_DIR / "menubar.log"

#: windows_support.py: ``registry.APPISTRY_DIR / "appistry_icon.ico"`` and the
#: ``shortcut-icons`` directory of per-app ICOs.
APPISTRY_ICON = APPISTRY_STATE_DIR / "appistry_icon.ico"
APPISTRY_SHORTCUT_ICONS = APPISTRY_STATE_DIR / "shortcut-icons"

#: menubar.py: ``_LOCK_PATH = HERE / ".menubar.lock"`` — its single-instance
#: flock. Note this one lives in its *install tree*, not in ``~/.appistry``, so it
#: is not a collision today. It is pinned anyway: a security review of this
#: project moved our lock out of the repo tree and into the state directory at
#: 0600, and the obvious same fix applied there would land a ``.menubar.lock`` in
#: a state directory. Our lock basename must already differ so that day is a
#: non-event.
APPISTRY_LOCK_NAME = ".menubar.lock"

#: Every path the other project is known to own, for the containment checks.
APPISTRY_OWNED_PATHS = (
    APPISTRY_REGISTRY,
    APPISTRY_PIDS_DIR,
    APPISTRY_SECRETS_DIR,
    APPISTRY_PORT_FILE,
    APPISTRY_HOOK_PORT_FILE,
    APPISTRY_TRAY_PID,
    APPISTRY_LOG,
    APPISTRY_ICON,
    APPISTRY_SHORTCUT_ICONS,
)

#: appistry.py: the launchd label, and therefore ``~/Library/LaunchAgents/
#: com.appistry.menubar.plist``. Also the tray bundle's CFBundleIdentifier.
APPISTRY_LAUNCHD_LABEL = "com.appistry.menubar"

#: pyproject.toml: ``[project.scripts] appistry = "appistry:main"``, symlinked by
#: its installer into /usr/local/bin or ~/.local/bin.
APPISTRY_CONSOLE_SCRIPT = "appistry"

#: pyproject.toml: ``name = "appistry"``.
APPISTRY_DISTRIBUTION = "appistry"

#: pyproject.toml ``py-modules``: the top-level modules its wheel installs. A
#: distribution of ours shipping any of these same names would overwrite them.
APPISTRY_TOP_LEVEL_MODULES = frozenset({
    "appistry", "cleanup", "hooks", "launch", "menubar", "process", "registry",
    "windows_support", "windows_tray",
})

#: windows_support.py: ``NamedMutex(name=r"Local\\AppistryWindowsTray")`` — the
#: Windows single-instance guard.
APPISTRY_MUTEX = r"Local\AppistryWindowsTray"

#: windows_support.py: ``_STARTUP_SHORTCUT = "Appistry.lnk"``, installed into the
#: Startup folder and into a Start Menu folder called "Appistry".
APPISTRY_SHORTCUT = "Appistry.lnk"
APPISTRY_START_MENU_FOLDER = "Appistry"

#: hooks.py: ``DEFAULT_HOOK_PORT = 47658`` — the only fixed port either project
#: has ever had. We must not bind it, or anything else fixed.
APPISTRY_HOOK_PORT = 47658

#: hooks.py: ``HOOK_PORT_ENV = "APPISTRY_HOOK_PORT"``.
APPISTRY_ENV_PREFIX = "APPISTRY_"


# ── What this project owns ────────────────────────────────────────────────────

def _owned_state_paths() -> dict[str, Path]:
    """Every path Roost writes inside its own state directory.

    Resolved through the real production accessors, not restated, so a rename
    that forgets one of these cannot pass by leaving a literal behind.
    """
    return {
        "state directory": paths.STATE_DIR,
        "host lock": host.host_lock_path(),
        "help port file": help_server.port_file_path(),
        "log": paths.log_path(),
        "tray PID file": windows_support.tray_pid_path(),
    }


class TestStateDirectoryIsDisjoint:
    """Nothing this project owns may live inside the other project's directory."""

    @pytest.mark.parametrize("label", list(_owned_state_paths()))
    def test_owned_path_is_outside_the_appistry_directory(self, label):
        path = _owned_state_paths()[label]
        assert APPISTRY_STATE_DIR not in path.parents, (
            f"Roost's {label} ({path}) is inside {APPISTRY_STATE_DIR}, which "
            "belongs to the separate internal Appistry."
        )
        assert path != APPISTRY_STATE_DIR, label

    def test_the_state_directory_is_not_the_appistry_directory(self):
        assert paths.STATE_DIR != APPISTRY_STATE_DIR

    @pytest.mark.parametrize("other", APPISTRY_OWNED_PATHS,
                             ids=lambda p: p.name)
    def test_no_owned_path_equals_one_of_the_other_projects(self, other):
        for label, path in _owned_state_paths().items():
            assert path != other, f"Roost's {label} collides with {other}"

    def test_the_state_directory_does_not_contain_the_appistry_directory(self):
        """Containment in the other direction is a collision too.

        A state directory of ``~`` would technically be "not inside
        ~/.appistry" while owning it outright.
        """
        assert paths.STATE_DIR not in APPISTRY_STATE_DIR.parents
        assert paths.STATE_DIR != Path.home()

    def test_state_filenames_do_not_collide_even_if_a_dir_is_ever_shared(self):
        """Belt and braces: the *basenames* differ too.

        The directories are disjoint, so this is redundant today. It is asserted
        anyway because a filename is the thing most likely to be reused by a
        future change ("menubar.log" was), and a shared basename is what would
        turn any accidental directory sharing into data loss rather than mere
        clutter.
        """
        ours = {path.name for path in _owned_state_paths().values()}
        theirs = {path.name for path in APPISTRY_OWNED_PATHS}
        assert ours.isdisjoint(theirs), sorted(ours & theirs)

    def test_the_lock_basename_differs_from_the_other_projects(self):
        """Pinned even though that lock is not in a shared directory today.

        See :data:`APPISTRY_LOCK_NAME`: it currently sits in the other project's
        install tree, which is the arrangement a security review moved *our* lock
        away from. If the same fix is ever applied there, a bare basename match is
        all that would stand between two trays and one lock file.
        """
        assert host.HOST_LOCK_NAME != APPISTRY_LOCK_NAME
        assert host.HOST_LOCK_NAME.lstrip(".") != APPISTRY_LOCK_NAME.lstrip(".")


class TestNothingReachesIntoTheOtherProjectsState:
    """No migration, and no reads either. See roost/paths.py for the reasoning.

    An earlier build of this project did keep its state in ``~/.appistry``, so
    "migrate it forward" is a real temptation. It is refused: the only things there
    are an icon preference and derived state, while the directory also holds the
    internal tool's live ``registry.toml``, ``pids/``, and ``secrets/``. Two of the
    filenames (``menubar-http-port``, ``menubar.log``) were written by *both*
    projects under the same name, so a migration would have to guess whose a file
    is — and guessing wrong corrupts software in daily use.
    """

    def test_no_string_literal_names_the_other_projects_state(self):
        """Checked over the AST's string constants, not the raw text.

        Docstrings and comments in this package explain at length *why* it stays
        out of ``~/.appistry``, so a substring scan of the source would flag the
        documentation. What matters is that no **runtime string** names those
        paths: a path this code could actually open has to appear as a literal
        somewhere. Docstrings are excluded by walking the tree and skipping them
        explicitly.
        """
        import ast

        forbidden = (".appistry", "registry.toml", "shortcut-icons",
                     "appistry_icon", "stable-hook-port")
        package = Path(paths.__file__).parent
        for module in sorted(package.glob("*.py")):
            tree = ast.parse(module.read_text(encoding="utf-8"))
            docstrings = {
                node.body[0].value
                for node in ast.walk(tree)
                if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                     ast.AsyncFunctionDef))
                and node.body
                and isinstance(node.body[0], ast.Expr)
                and isinstance(node.body[0].value, ast.Constant)
                and isinstance(node.body[0].value.value, str)
            }
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Constant)
                    and isinstance(node.value, str)
                    and node not in docstrings
                ):
                    for name in forbidden:
                        assert name not in node.value, (
                            f"{module.name}:{node.lineno} has a runtime string "
                            f"naming {name!r}. Nothing here may read, move, or "
                            "remove anything under ~/.appistry."
                        )

    def test_no_migration_helper_exists(self):
        """A migration is the one way this project could damage the other."""
        package = Path(paths.__file__).parent
        for module in sorted(package.glob("*.py")):
            source = module.read_text(encoding="utf-8").lower()
            for forbidden in ("def migrate", "def _migrate", "shutil.move",
                              "os.rename("):
                assert forbidden not in source, f"{module.name}: {forbidden}"


class TestProcessAndOsIdentifiers:
    def test_the_launchd_label_differs(self):
        assert cli.LABEL != APPISTRY_LAUNCHD_LABEL

    def test_the_launchd_label_is_not_in_the_appistry_namespace(self):
        """``com.appistry.*`` is the other project's reverse-DNS namespace."""
        assert not cli.LABEL.startswith("com.appistry.")

    def test_the_plist_filenames_differ(self):
        """The label *is* the plist name, so a shared label overwrites the file."""
        agents = Path.home() / "Library" / "LaunchAgents"
        assert (agents / f"{cli.LABEL}.plist") != (
            agents / f"{APPISTRY_LAUNCHD_LABEL}.plist"
        )

    def test_the_console_script_differs(self):
        assert cli.COMMAND != APPISTRY_CONSOLE_SCRIPT

    def test_the_shell_shim_is_not_named_appistry(self):
        """The shim is symlinked into ~/.local/bin, where the other one lives."""
        assert (REPO / "bin" / cli.COMMAND).is_file()
        assert not (REPO / "bin" / APPISTRY_CONSOLE_SCRIPT).exists()

    def test_the_windows_tray_launch_token_differs(self):
        """``stop_tray`` signals on this token, and both ship windows_tray.py.

        The other project verifies a bare ``windows_tray.py`` argument. Ours must
        verify something that file path can never match, or a recycled PID landing
        in one tool's PID file could make it terminate the other's tray.
        """
        assert windows_support.TRAY_MODULE != "windows_tray.py"
        assert "." in windows_support.TRAY_MODULE

    def test_the_windows_shortcut_names_differ(self, monkeypatch, tmp_path):
        monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
        startup = windows_support.startup_dir() / windows_support._STARTUP_SHORTCUT
        assert startup.name != APPISTRY_SHORTCUT
        assert windows_support.shortcuts_dir().name != APPISTRY_START_MENU_FOLDER

    def test_no_windows_mutex_is_created_under_the_other_projects_name(self):
        """We elect a host with a lock file, not a named mutex.

        Asserted rather than assumed: adding a ``NamedMutex`` later and reaching
        for the obvious name would silently make each tray think the other was
        itself, and only on Windows.
        """
        for module in (windows_support, __import__("roost.windows_tray",
                                                   fromlist=["x"])):
            source = Path(module.__file__).read_text(encoding="utf-8")
            assert APPISTRY_MUTEX not in source
            assert "AppistryWindowsTray" not in source


class TestPorts:
    def test_the_help_server_binds_an_ephemeral_port(self):
        """No fixed port at all, so no fixed port can be the other one's.

        The other project's hook server defaults to 47658. Ours asks the kernel
        for a free port and records it, which is also why a page on a fixed
        loopback port cannot find it.
        """
        source = Path(help_server.__file__).read_text(encoding="utf-8")
        assert 'sock.bind(("127.0.0.1", 0))' in source
        assert str(APPISTRY_HOOK_PORT) not in source

    def test_nothing_binds_or_connects_to_a_literal_port(self):
        """No call site may name a port; they must all come from data.

        Scanning for four-digit *numbers* would flag the badge cap, the
        host_priority range, and a millisecond timeout, so this looks at the
        socket call sites instead: a ``bind``/``connect`` tuple or a URL, which is
        where a fixed port would actually have to appear to matter. A bird's port
        arrives from its descriptor and is interpolated, never written down.
        """
        package = Path(paths.__file__).parent
        literal_bind = re.compile(r"(?:bind|connect)\(\(\s*[^,]+,\s*([0-9]+)")
        literal_url = re.compile(r"127\.0\.0\.1:([0-9]+)")
        for module in sorted(package.glob("*.py")):
            source = module.read_text(encoding="utf-8")
            for port in literal_bind.findall(source):
                assert port == "0", (
                    f"{module.name} binds port {port}. Bind 0 and let the kernel "
                    "choose, so no page on a fixed loopback port can find it."
                )
            for port in literal_url.findall(source):
                pytest.fail(f"{module.name} names loopback port {port} literally")

    def test_the_other_projects_fixed_port_appears_nowhere(self):
        package = Path(paths.__file__).parent
        for module in sorted(package.glob("*.py")):
            source = module.read_text(encoding="utf-8")
            assert str(APPISTRY_HOOK_PORT) not in source, module.name


class TestImportSurface:
    def test_no_top_level_module_is_shipped(self):
        """A flat module would overwrite the other distribution's in one env."""
        import tomllib

        config = tomllib.loads(
            (REPO / "pyproject.toml").read_text(encoding="utf-8")
        )
        setuptools = config["tool"]["setuptools"]
        assert "py-modules" not in setuptools, (
            "Shipping top-level modules re-creates the collision this rename "
            "removed."
        )
        assert setuptools["packages"] == ["roost"]

    def test_the_distribution_name_differs(self):
        import tomllib

        config = tomllib.loads(
            (REPO / "pyproject.toml").read_text(encoding="utf-8")
        )
        assert config["project"]["name"] != APPISTRY_DISTRIBUTION

    def test_the_console_script_entry_point_is_ours(self):
        import tomllib

        config = tomllib.loads(
            (REPO / "pyproject.toml").read_text(encoding="utf-8")
        )
        scripts = config["project"]["scripts"]
        assert APPISTRY_CONSOLE_SCRIPT not in scripts
        assert scripts == {"roost": "roost.cli:main"}

    def test_the_package_claims_none_of_the_other_projects_module_names(self):
        """Our importable names are ``roost`` and ``roost.*`` only."""
        package = Path(paths.__file__).parent
        assert package.name not in APPISTRY_TOP_LEVEL_MODULES
        # The submodule names may overlap (both have a `menubar`); what matters is
        # that ours are only reachable under the package.
        for source in package.glob("*.py"):
            assert (package / source.name).exists()
        assert not list(REPO.glob("*.py")), (
            "A .py file at the repository root is installed as a top-level "
            f"module: {[p.name for p in REPO.glob('*.py')]}"
        )


class TestEnvironmentVariables:
    def test_no_appistry_environment_variable_is_read(self):
        """``APPISTRY_HOOK_PORT`` is the other project's; we read nothing of its."""
        package = Path(paths.__file__).parent
        for module in sorted(package.glob("*.py")):
            source = module.read_text(encoding="utf-8")
            assert APPISTRY_ENV_PREFIX not in source, module.name


class TestTheSharedBirdContractIsUnchanged:
    """The one directory that is deliberately *not* renamed.

    The descriptor directory is a cross-project contract: Huginn and Muninn write
    into it, so it is not ours to move. It is asserted here — in the coexistence
    suite — precisely because "rename everything you own" and "never rename this"
    are easy to confuse.
    """

    def test_the_descriptor_directory_keeps_its_shared_name(self, monkeypatch):
        monkeypatch.delenv("BIRDS_STATE_DIR", raising=False)
        monkeypatch.delenv("XDG_STATE_HOME", raising=False)
        monkeypatch.setattr(birds, "_IS_WINDOWS", False)
        assert birds.state_dir() == Path.home() / ".local" / "state" / "birds"

    def test_the_windows_descriptor_directory_keeps_its_shared_name(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.delenv("BIRDS_STATE_DIR", raising=False)
        monkeypatch.setattr(birds, "_IS_WINDOWS", True)
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
        assert birds.state_dir() == tmp_path / "Local" / "Birds"

    def test_the_descriptor_directory_is_not_roosts_own_state_directory(self):
        """Shared and private state are different things and must not merge.

        A bird's descriptor is written by another process; Roost's lock and port
        file are not. Putting them in one directory would mean a bird could
        overwrite the host's lock.
        """
        monkey_free_state = paths.STATE_DIR
        assert monkey_free_state != birds.state_dir()
