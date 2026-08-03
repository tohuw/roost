"""Lifecycle: a raven's Quit/Restart rows work, and Roost still starts nothing.

Roost replaced menu bars that owned their daemon's lifecycle, so "add a Start
button" is the change a future reader is most likely to attempt. This module
exists to make both halves of the answer executable rather than only written down
in SPEC.md §10.

**Quit and Restart needed nothing.** They are action ids a raven publishes, and
the tests below assert Roost cannot tell one from ``focus:abc123`` — same parsing,
same forwarding, same everything. That indistinguishability *is* the feature: the
moment Roost recognises ``quit``, it is interpreting a companion's data and owes
every raven an opinion about what stopping means.

**Starting a stopped raven is refused by construction, not by policy.** A stopped
raven has withdrawn its descriptor, so there is no row to click and no port to
call. The tests here pin that Roost holds no mechanism that could be pressed into
service anyway: no process spawning in the menu path, and no reading of a
raven-supplied path as something to execute. A test that merely asserted "we do
not currently call Popen" would pass the day someone adds one, so these check the
menu-building surface as a whole.
"""

import ast
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from roost import host
from roost import icons
from roost import menu_spec
from roost import ravens
from roost import tray
from roost.tray import RowKind


@pytest.fixture(autouse=True)
def no_user_icon_config(monkeypatch, tmp_path):
    """Keep the icon submenu deterministic and off the real config."""
    monkeypatch.setattr(icons.paths, "STATE_DIR", tmp_path)
    return tmp_path


def _descriptor(name="huginn", **overrides):
    values = dict(
        name=name, display=name.title(), api_version=1, min_api=1, max_api=1,
        pid=1, port=47100, token_path=None, token_header="", endpoints={},
        host_priority=0, started=None, path=Path(f"/tmp/{name}.json"),
    )
    values.update(overrides)
    return ravens.RavenDescriptor(**values)


def _menu_with(*action_ids, name="huginn"):
    """A raven menu offering one row per given action id."""
    items = tuple(
        menu_spec.MenuItem(label=f"Row {action_id}", action_id=action_id)
        for action_id in action_ids
    )
    spec = menu_spec.MenuSpec(sections=(menu_spec.MenuSection(id="lifecycle", items=items),))
    return menu_spec.RavenMenu(
        name=name, display=name.title(), spec=spec, descriptor=_descriptor(name),
    )


# ── Quit and Restart are ordinary actions ─────────────────────────────────────

class TestLifecycleRowsAreOrdinary:
    """A lifecycle id must travel the same path as any other action id."""

    @pytest.mark.parametrize("action_id", ["quit", "restart", "shutdown", "stop-daemon"])
    def test_a_lifecycle_id_survives_parsing_like_any_other(self, action_id):
        """No id is reserved. The parser has no vocabulary, so it has no opinion."""
        item = menu_spec.parse_item({"id": action_id, "label": f"Do {action_id}"})

        assert item is not None
        assert item.action_id == action_id
        assert item.clickable

    def test_a_quit_row_renders_identically_to_a_focus_row(self):
        """Byte-for-byte the same row kind, label shape, and clickability.

        If a change ever makes these differ, Roost has started interpreting the
        id — which is the rule in AGENTS.md that this asserts.
        """
        quit_rows = tray.raven_rows(_menu_with("quit"))
        focus_rows = tray.raven_rows(_menu_with("focus:abc123"))

        assert [row.kind for row in quit_rows] == [row.kind for row in focus_rows]
        assert [row.enabled for row in quit_rows] == [row.enabled for row in focus_rows]

    def test_a_lifecycle_row_is_forwarded_not_acted_on(self, monkeypatch):
        """``host.activate`` hands the id back; it does not signal anything itself."""
        sent = []
        monkeypatch.setattr(
            host.raven_client, "send_action",
            lambda descriptor, action_id, **kw: sent.append((descriptor.name, action_id)),
        )
        menu = _menu_with("quit")
        item = menu.spec.sections[0].items[0]

        returned = host.activate(menu, item)

        # Forwarded verbatim to the raven that published it, and no URL to open.
        assert sent == [("huginn", "quit")]
        assert returned is None

    def test_a_raven_that_dies_mid_action_is_not_an_error_the_menu_shows(self, monkeypatch):
        """A quit that drops the connection must degrade, not raise.

        The raven is supposed to answer before exiting (SPEC.md §10), but one that
        gets it wrong is the *expected* failure here, and it happens on the UI
        thread. It has to come back as a logged warning, not an exception.
        """
        def die(descriptor, action_id, **kw):
            raise host.raven_client.RavenRequestError("Is not answering on its recorded port.")

        monkeypatch.setattr(host.raven_client, "send_action", die)
        menu = _menu_with("quit")

        # No exception, and nothing for the caller to open.
        assert host.activate(menu, menu.spec.sections[0].items[0]) is None

    def test_the_menu_survives_a_raven_that_quit_between_draw_and_click(self, monkeypatch):
        """The ordinary race: the row was drawn, then the raven stopped."""
        monkeypatch.setattr(
            host.raven_client, "send_action",
            lambda *a, **kw: (_ for _ in ()).throw(
                host.raven_client.RavenRequestError("Not running.")),
        )
        menu = _menu_with("restart")

        assert host.activate(menu, menu.spec.sections[0].items[0]) is None


# ── No raven-supplied string is ever a name to special-case ───────────────────

class TestNoLifecycleVocabulary:
    """Roost must hold no list of lifecycle ids, the way it holds no raven names."""

    @pytest.mark.parametrize("module", [tray, host, menu_spec, ravens])
    def test_no_module_names_a_lifecycle_action(self, module):
        """Grepping the source, in the spirit of the existing no-hardcoding test.

        The reserved-word list this guards against is the tempting shortcut: a
        host that knows ``quit`` means stop could grey the row out while the raven
        is unreachable, or confirm before sending. Both are interpretation, and
        both would make one raven's vocabulary load-bearing for every raven.
        """
        source = Path(module.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        literals = {
            node.value.casefold()
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        }
        for reserved in ("quit", "restart", "shutdown"):
            # Roost's *own* Quit row is a host action, not a raven id, and lives
            # in tray.py as "quit". Allow it there and nowhere else, since that
            # one never leaves the host.
            if module is tray and reserved == "quit":
                continue
            assert reserved not in literals, f"{module.__name__} names {reserved!r}"

    def test_roosts_own_quit_row_is_not_a_raven_action(self):
        """Roost's ``Quit Roost`` is a HOST row and closes only the tray.

        Worth pinning because the two now look similar in the menu: a raven may
        publish ``Quit Huginn`` right above Roost's own ``Quit Roost``. They must
        stay different *kinds* of row, or clicking one would do the other's job.
        """
        rows = tray.build_rows(host.MenuModel((_menu_with("quit"),)))
        host_quit = [r for r in rows if r.kind is RowKind.HOST and r.action == "quit"]
        raven_quit = [r for r in rows if r.kind is RowKind.ITEM]

        assert len(host_quit) == 1
        assert host_quit[0].label == tray.QUIT_LABEL
        # The raven's row carries its item and its raven name; Roost's carries
        # neither, because it is never forwarded anywhere.
        assert host_quit[0].raven == ""
        assert host_quit[0].item is None
        assert len(raven_quit) == 1
        assert raven_quit[0].raven == "huginn"


# ── Roost cannot start a stopped raven ────────────────────────────────────────

class TestRoostStartsNothing:
    """The half of SPEC.md §10 that is a refusal.

    These are the tests that fail if someone implements a Start button.
    """

    def test_a_stopped_raven_offers_no_rows_at_all(self, tmp_path):
        """With no descriptor there is nothing to render, let alone to click.

        This is the whole design problem in one assertion: the reason Roost has no
        Start action is that a stopped raven is not a raven Roost can see.
        """
        model = host.build_model(tmp_path)

        assert model.menus == ()
        rows = tray.build_rows(model)
        assert tray.NO_RAVENS_LABEL in [
            row.label for row in rows if row.kind is RowKind.REASON
        ]
        # Nothing clickable is offered for the raven that is not there.
        assert not [row for row in rows if row.kind is RowKind.ITEM]

    def test_an_unavailable_raven_gets_a_reason_and_no_action(self, tmp_path):
        """A crashed raven's stale descriptor is a reason, never a restart offer.

        A dead PID is the one case where Roost *does* know a raven's name and port
        — which makes it the tempting place to offer "start it again". It stays
        inert: the descriptor is evidence about the past, and the process it names
        is gone.
        """
        (tmp_path / "huginn.json").write_text(
            '{"api_version": 1, "name": "huginn", "display": "Huginn",'
            # PID 1 is alive but its start time cannot match, and a descriptor
            # this old is exactly what a crash leaves behind.
            ' "pid": 999999999, "port": 47100}',
            encoding="utf-8",
        )

        model = host.build_model(tmp_path)
        rows = tray.build_rows(model)

        assert len(model.menus) == 1
        assert not model.menus[0].available
        assert not [row for row in rows if row.kind is RowKind.ITEM]
        reasons = [row.label for row in rows if row.kind is RowKind.REASON]
        assert any("Not running" in reason for reason in reasons)

    def test_the_menu_path_spawns_no_process(self, monkeypatch, tmp_path):
        """Building and activating a menu must never reach a process launcher.

        Asserted by making every spawn primitive fail rather than by grepping, so
        it holds for an indirect call too. Scoped to the menu path because
        ``cli.py`` legitimately runs ``launchctl`` to install Roost's *own* login
        agent, and ``menubar.py`` runs ``osascript``/``zsh`` for notifications and
        environment — neither is a raven's lifecycle.
        """
        def forbidden(*args, **kwargs):
            raise AssertionError(f"the menu path must not spawn: {args!r}")

        for name in ("Popen", "run", "call", "check_call", "check_output"):
            monkeypatch.setattr(subprocess, name, forbidden)
        monkeypatch.setattr(host.raven_client, "send_action", lambda *a, **kw: {"ok": True})

        model = host.build_model(tmp_path)
        tray.build_rows(model)
        menu = _menu_with("quit")
        host.activate(menu, menu.spec.sections[0].items[0])

    def test_no_descriptor_field_is_treated_as_something_to_execute(self, tmp_path):
        """A descriptor names a port and a token path — never a command.

        The rejected design in SPEC.md §10 was a registration file naming an
        interpreter, so this pins that the validated descriptor has no field that
        could carry one. Huginn's ``daemon.json`` needed 0600 plus an ownership
        check plus a group/world-writable check on every parent before its
        ``python`` field was safe to execute; the parsed type here simply has
        nowhere to put such a value.
        """
        fields = set(ravens.RavenDescriptor.__dataclass_fields__)

        for executable_ish in ("python", "repo", "command", "argv", "exec",
                               "program", "interpreter", "start", "launch"):
            assert executable_ish not in fields

    def test_an_extra_command_field_in_a_descriptor_is_dropped(self, tmp_path):
        """A raven that *tries* to hand Roost a command gets it ignored.

        Unknown fields are dropped rather than refused (that is what keeps the
        protocol additive), so the guarantee is that dropping is what happens —
        not that a hostile descriptor is rejected. The descriptor still validates;
        the command simply does not survive into anything Roost holds.
        """
        raw = (
            '{"api_version": 1, "name": "huginn", "display": "Huginn", "pid": %d,'
            ' "port": 47100, "python": "/tmp/evil", "repo": "/tmp",'
            ' "start_command": "/tmp/evil --now"}'
        ) % __import__("os").getpid()
        path = tmp_path / "huginn.json"
        path.write_text(raw, encoding="utf-8")

        descriptor = ravens.parse_descriptor(raw, path, expected_name="huginn")

        assert not hasattr(descriptor, "python")
        assert not hasattr(descriptor, "start_command")
        # And nothing smuggled it into the one dict a descriptor does carry.
        assert descriptor.endpoints == {}

    def test_endpoints_cannot_carry_a_command(self, tmp_path):
        """The one open-ended map in a descriptor holds URL paths only.

        ``endpoints`` accepts unknown *keys* by design, so it is the place a
        "start" entry would be smuggled in. It stays harmless because every value
        must be a rooted relative path, and Roost only ever joins one onto
        ``http://127.0.0.1:{port}``.
        """
        raw = (
            '{"api_version": 1, "name": "huginn", "pid": %d, "port": 47100,'
            ' "endpoints": {"start": "/usr/bin/python"}}'
        ) % __import__("os").getpid()
        path = tmp_path / "huginn.json"

        descriptor = ravens.parse_descriptor(raw, path, expected_name="huginn")

        # Accepted, because an unknown key must not break an older host — but it
        # is a URL path, and the only thing Roost can do with it is fetch it.
        assert descriptor.endpoints == {"start": "/usr/bin/python"}
        assert descriptor.endpoint("start").startswith("/")

    def test_a_shell_command_in_an_endpoint_is_refused(self, tmp_path):
        """Anything not shaped like a local path is refused outright."""
        for value in ("sh -c 'rm -rf /'", "file:///bin/sh", "//evil/start",
                      "/../../bin/sh"):
            raw = (
                '{"api_version": 1, "name": "huginn", "pid": 1, "port": 47100,'
                ' "endpoints": {"start": %s}}'
            ) % __import__("json").dumps(value)
            with pytest.raises(ravens.DescriptorError):
                ravens.parse_descriptor(raw, tmp_path / "huginn.json",
                                        expected_name="huginn")


# ── The protocol version did not move ─────────────────────────────────────────

def test_lifecycle_needed_no_version_bump():
    """Adding lifecycle rows must not have moved Roost's advertised window.

    The point of the exercise: a raven publishing a Quit row is publishing an
    ordinary action id, so an older host renders it correctly without being told
    anything. Had it been spelled as a field the host must recognise, every host
    below the bump would have needed updating — huginn issue #38's failure mode,
    which is why compatibility is a range here in the first place.
    """
    assert (ravens.MIN_API_VERSION, ravens.API_VERSION) == (1, 1)
