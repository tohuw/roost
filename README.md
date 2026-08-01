# Appistry

One shared status menu bar for the ravens.

[Huginn](https://github.com/tohuw/huginn) is an AI agent activity console.
[Muninn](https://github.com/tohuw/muninn) is its agent-history companion. Thought
and Memory. Each runs as a long-lived local daemon, and each wants somewhere in
the desktop shell to say what it is doing — but two separate menu bar icons for
two halves of one thing is two icons too many.

So Appistry is a single macOS menu bar / Windows system tray item that renders
whichever ravens are running. It reports status; it does not launch anything.

## How it works

A raven publishes a small JSON **descriptor** into a shared directory saying
where it is listening and where its token lives. Appistry reads whatever
descriptors it finds, fetches each raven's menu over loopback, and draws it.

That is the whole coupling. Appistry has no list of known ravens, no
configuration naming one, and no code that treats any particular raven
specially — a raven that is not running simply has no descriptor, and a third
raven would need no change here at all.

Crucially, Appistry **renders a raven's menu without interpreting it**. It draws
labels and hands action ids back to the raven that published them. It does not
know what `focus:abc123` means and never needs to, which is what lets either
raven change its own menu with no change to Appistry.

```
[icon]
  Huginn (2)
    Needs attention
      ● Approve: deploy to staging — claude
    Sessions
      Refactor the parser — codex
      ──────────
      Open Console
  ──────────────────
  Muninn
    Recent
      · Deployed staging — 12m ago
      · Merged #412 — yesterday
  ──────────────────
  Tray icon      ▸
  Help
  ──────────────────
  Quit Appistry
```

## What it does

- Shows every running raven, ordered by the priority each one declares
- Forwards clicks back to the raven that published them, under that raven's own
  credential
- Renders an unreachable, stopped, or malformed raven as a **disabled section
  with a visible reason** — never as a silent omission and never as a crash
- Elects exactly one host process by a single exclusive lock, released by the
  kernel if that process dies
- Lets you pick the tray icon, defaulting to the raven
- Starts after login via launchd on macOS or the Startup folder on Windows

Everything binds to `127.0.0.1`. Appistry makes no outbound network requests and
holds no credential of its own.

## Security posture

Two invariants are worth stating up front, because they are the reason the design
looks the way it does.

**Per-raven token isolation.** Each raven owns its token. Appistry reads a token
from the `token_path` in *that raven's own descriptor* and sends it only to *that
raven's own port*, under the header that raven asked for. It never caches a token
across ravens, never sends one raven's credential to another, and never mints one
on a raven's behalf.

**No relay, by construction.** Appistry serves one loopback page (Help) and
forwards nothing. A fixed loopback port is reachable by any web page the user has
open, so that page refuses a foreign `Host`, refuses **any** `Origin`, takes no
request body, and builds every response header itself. An earlier design in this
repository proxied requests to app servers and rewrote `Host` to a clean loopback
value, which laundered drive-by requests past the upstream app's own origin
check; the fix was to remove the upstream entirely.

A descriptor is a file written by another process and is treated as untrusted
input: every field is range- and type-checked, nothing is ever `eval`'d, control
characters and ANSI escapes are stripped before anything reaches a menu or a log,
and a malformed descriptor becomes an unavailable raven with a reason rather than
an exception.

See [SECURITY.md](SECURITY.md) to report a vulnerability.

## Installation

Requires Python 3.10 or newer.

```bash
git clone https://github.com/tohuw/appistry.git
cd appistry
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python appistry.py install
```

On Windows (PowerShell):

```powershell
git clone https://github.com/tohuw/appistry.git
Set-Location appistry
py -3 appistry.py install
```

`install` creates the virtualenv, registers login startup (a launchd agent on
macOS, a Startup shortcut on Windows), installs the `appistry` CLI, and starts
the tray. It is safe to re-run.

## CLI

```
appistry ravens        Show what the tray sees, and why
appistry icon list     List the selectable tray icons
appistry icon set X    Choose a built-in name or an absolute PNG/ICO path
appistry icon reset    Revert to the default
appistry ui            Start the menu bar or system tray
appistry install       First-time setup
appistry uninstall     Remove login startup and CLI integration
```

There is no `register`, `start`, or `stop`: the ravens run themselves. If a raven
is not in the menu, `appistry ravens` will say why.

```
$ appistry ravens
Descriptor directory: /Users/alice/.local/state/ravens

  ● Huginn (huginn)
      port     56713
      pid      41213
      api      1..1
      priority 100
      token    yes
  ○ Muninn (muninn)
      Not running (its recorded process is gone).
```

## Writing a raven

The contract is in [SPEC.md](SPEC.md), and two complete runnable implementations
of it are in [`examples/`](examples/) — one with a token and per-item actions, one
with neither. Start either and the tray will show it.

Two rules cause most of the trouble:

- **Declare a version range, not a version.** Appistry accepts any raven whose
  declared window overlaps its own. Exact matching is the bug behind huginn issue
  #38, where one routine bump silently disabled every participant.
- **Publish the descriptor after binding the port, and remove it on exit.** A
  descriptor naming a port that is not yet listening reads as an unreachable
  raven. A crash that skips removal is still handled — Appistry checks the
  recorded PID before trusting the file.

## Uninstalling

`appistry uninstall` removes login startup and the CLI symlink. It does not touch
the ravens: they are separate daemons with their own lifecycles, and Appistry has
never owned them.

## Contributing

Issues and pull requests are welcome. Run the tests before opening a PR:

```bash
python -m pytest tests/unit -q
```

`tests/unit` must pass on both macOS and Windows with no display and no real
install. See [AGENTS.md](AGENTS.md) for the module map and the rules that hold
this design together.

## License

Apache License 2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE).

The tray's raven icon is *Raven* by Lorc from
[game-icons.net](https://game-icons.net/), licensed CC BY 3.0. See
[`assets/CREDITS.md`](assets/CREDITS.md).
