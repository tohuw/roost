# Roost

One shared status menu bar for the ravens.

> **A note on the history.** Roost was forked from a separate app launcher called
> **Appistry**, which is still in use, and this repository carried that name until
> it was renamed to match what it installs: the `roost` command, the `roost`
> distribution, and its own state directory. The two are separate tools and can be
> installed and run at the same time — see
> [Coexistence](#coexistence-with-appistry). Links to `tohuw/appistry` still
> redirect here.

[Huginn](https://github.com/tohuw/huginn) is an AI agent activity console.
[Muninn](https://github.com/tohuw/muninn) is its agent-history companion. Thought
and Memory. Each runs as a long-lived local daemon, and each wants somewhere in
the desktop shell to say what it is doing — but two separate menu bar icons for
two halves of one thing is two icons too many.

So Roost is a single macOS menu bar / Windows system tray item that renders
whichever ravens are running. It reports status; it does not launch anything.

## How it works

A raven publishes a small JSON **descriptor** into a shared directory saying
where it is listening and where its token lives. Roost reads whatever
descriptors it finds, fetches each raven's menu over loopback, and draws it.

That is the whole coupling. Roost has no list of known ravens, no
configuration naming one, and no code that treats any particular raven
specially — a raven that is not running simply has no descriptor, and a third
raven would need no change here at all.

Crucially, Roost **renders a raven's menu without interpreting it**. It draws
labels and hands action ids back to the raven that published them. It does not
know what `focus:abc123` means and never needs to, which is what lets either
raven change its own menu with no change to Roost.

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
  Help
  ──────────────────
  Quit Roost
```

## What it does

- Shows every running raven, ordered by the priority each one declares
- Forwards clicks back to the raven that published them, under that raven's own
  credential — including a raven's own **Quit** or **Restart** row, which is an
  ordinary action id Roost passes back without knowing what it does
- Offers to **start** a stopped raven by asking the OS supervisor, never by
  executing anything a descriptor named: see [Lifecycle](#lifecycle-what-roost-does-and-does-not-own)
- Renders an unreachable, stopped, or malformed raven as a **disabled section
  with a visible reason** — never as a silent omission and never as a crash
- Elects exactly one host process by a single exclusive lock, released by the
  kernel if that process dies
- Wears one mark, the raven, so the tray item stays findable
- Starts after login via launchd on macOS or the Startup folder on Windows
- Identifies itself as *Roost* on Windows, not as *Python*: the tray runs from a
  copy of the interpreter named `RoostTray.exe`, carrying a version resource
  whose `FileDescription` is `Roost`, because Windows names a tray entry after
  that field and falls back to the filename only when it is absent. The copy
  lives in `Scripts` beside pip's console scripts and is deliberately **not**
  named `Roost.exe` — that is the same file as `roost.exe` on a case-insensitive
  filesystem, and staging it there overwrites the `roost` command itself

Everything binds to `127.0.0.1`. Roost makes no outbound network requests and
holds no credential of its own.

## Lifecycle: what Roost does and does not own

Roost replaces menu bars that owned their daemon's lifecycle, so it is worth being
exact about which parts came along.

**Quit and Restart did.** They are not features of Roost — they are rows a raven
publishes, with ordinary action ids, that Roost forwards like any other. A running
process can stop itself; it needs no help from the menu bar, and Roost stays
unaware that a row labelled *Quit Huginn* is different from one labelled *Approve*.
Adding them needed no change here and no protocol version bump.

**Starting a stopped raven came along later, and carefully.** It was left out at
first on the grounds that a stopped raven has withdrawn its descriptor, so Roost
cannot see it — but that is only true of a *clean* shutdown. A kill, a crash or a
power cut leaves the descriptor exactly where it was, and Roost duly rendered the
raven greyed out with "its recorded process is gone" and nothing to click. That
is the state a status menu is least useful in, and it was reachable by simply
force-quitting.

What was actually right in the original reasoning is narrower and still holds: a
file recording an interpreter and a checkout for Roost to run is a
write-then-execute path, and one of those in a *shared* host means one process
holding an exec path for every raven on the machine.

So a descriptor names an **identifier, never a command** — a launchd label, a
systemd user unit, or a `Run` value — and Roost hands it to the platform's own
supervisor. The command stays in the plist, the unit or the registry, put there
by an `install-agent` the user ran deliberately. Roost executes nothing it found
in a file, and the worst a forged descriptor achieves is starting a service that
is already installed. A raven that publishes no `launch` block gets no row, and
a `kind` that is not this machine's supervisor is ignored.

Starting at login is still the OS supervisor's job and still works without Roost:
`huginn install-agent` registers the launchd agent, systemd user unit, or Windows
`Run` key that this then asks for by name.

So: **the ravens run themselves, the OS starts them, and Roost asks and reports.**
[SPEC.md §10](SPEC.md#10-lifecycle-quitting-restarting-and-starting) states this
normatively and lists what else was considered.

## Security posture

Two invariants are worth stating up front, because they are the reason the design
looks the way it does.

**Per-raven token isolation.** Each raven owns its token. Roost reads a token
from the `token_path` in *that raven's own descriptor* and sends it only to *that
raven's own port*, under the header that raven asked for. It never caches a token
across ravens, never sends one raven's credential to another, and never mints one
on a raven's behalf.

**No relay, by construction.** Roost serves one loopback page (Help) and
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
git clone https://github.com/tohuw/roost.git
cd roost
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m roost.cli install
```

On Windows (PowerShell):

```powershell
git clone https://github.com/tohuw/roost.git
Set-Location roost
py -3 -m roost.cli install
```

`install` creates the virtualenv, registers login startup (a launchd agent on
macOS, a Startup shortcut on Windows), installs the `roost` CLI, and starts
the tray. It is safe to re-run.

## CLI

```
roost ravens        Show what the tray sees, and why
roost ui            Start the menu bar or system tray
roost install       First-time setup
roost uninstall     Remove login startup and CLI integration
```

There is no `register`, `start`, or `stop`: the ravens run themselves. If a raven
is not in the menu, `roost ravens` will say why.

```
$ roost ravens
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

- **Declare a version range, not a version.** Roost accepts any raven whose
  declared window overlaps its own. Exact matching is the bug behind huginn issue
  #38, where one routine bump silently disabled every participant.
- **Publish the descriptor after binding the port, and remove it on exit.** A
  descriptor naming a port that is not yet listening reads as an unreachable
  raven. A crash that skips removal is still handled — Roost checks the
  recorded PID before trusting the file.

## Uninstalling

`roost uninstall` removes login startup and the CLI symlink. It does not touch
the ravens: they are separate daemons with their own lifecycles, and Roost has
never owned them.

## Coexistence with Appistry

Roost was forked from **Appistry**, a local app-registry-and-launcher that is a
separate project, lives in a different repository, and is still in use. The two
share a git history and nothing else. They are designed to run **simultaneously**
on the same machine and the same user account, so installing one never displaces
the other.

This section is about that other tool, not about this repository's former name.
Renaming the repo changed nothing here: Appistry is still installed on the same
machines, still owns `~/.appistry`, and every collision below is still one Roost
has to avoid.

That works because Roost owns a distinct name for everything an OS can collide
on:

| | Appistry | Roost |
|---|---|---|
| State directory | `~/.appistry` | `~/.local/state/roost` (`%LOCALAPPDATA%\Roost`) |
| Console script | `appistry` | `roost` |
| Distribution | `appistry` | `roost` |
| Installed modules | `appistry`, `menubar`, `registry`, … (top-level) | `roost` (one package) |
| launchd label | `com.appistry.menubar` | `com.tohuw.roost` |
| Single-instance lock | `.menubar.lock` | `roost.lock` |
| Help port file | `menubar-http-port` | `roost-http-port` |
| Log | `menubar.log` | `roost.log` |
| Windows tray launch | `windows_tray.py` | `-m roost.windows_tray` |
| Windows shortcuts | `Appistry.lnk`, Start Menu `Appistry\` | `Roost.lnk`, Start Menu `Roost\` |
| Windows mutex | `Local\AppistryWindowsTray` | none (a lock file elects the host) |

**What Roost owns.** Its own state directory and nothing else. That directory
holds the host lock, the ephemeral help-port file, the tray
log, and on Windows the tray PID file — all 0600 inside a 0700 directory.

**What Roost never touches.** Anything under `~/.appistry`. Not `registry.toml`,
not `pids/`, not `secrets/`, and not the `menubar-http-port` or `menubar.log` that
both projects once wrote there. There is no migration and no compatibility read:
Roost does not know that directory exists. A test walks this package's AST to
prove no runtime string names it.

**If you ran an early build of Roost**, its state is still sitting in
`~/.appistry` and is simply ignored. Nothing is migrated, deliberately: Roost
stores no preferences to carry over, and a migration would mean deleting files
from inside a live tool's state directory. Two of the filenames were written by
both projects under the same name, so there is no way to be sure whose they are.
Delete the orphans by hand if they bother you.

Neither project binds a fixed port. Roost's help server asks the kernel for a
free one and records it owner-only, which is also why no web page can find it.

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
