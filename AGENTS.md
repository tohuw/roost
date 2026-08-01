# Agent notes for Roost

Guidance for AI coding agents working in this repository. Humans should read
[README.md](README.md) and [SPEC.md](SPEC.md) instead.

## What this project is

One shared status menu bar — a macOS menu bar item and a Windows system tray
item — for two long-running local daemons called *ravens*:
[Huginn](https://github.com/tohuw/huginn) and
[Muninn](https://github.com/tohuw/muninn). Pure Python, stdlib plus the two
platforms' tray toolkits, no build step.

**It is not a launcher.** It does not start, stop, supervise, register, or clean
up after anything. It reads status and draws it. If a change you are making needs
`Popen`, a PID file it writes itself, or a registry of apps, stop: that is the
architecture this repository deliberately removed, and reintroducing it is the
mistake to avoid.

## Architecture

Everything platform-neutral is shared; only the last mile is per-platform.

```
roost/ravens.py ── descriptors: discovery, validation, liveness
roost/menu_spec.py ── the menu-as-data parser
roost/raven_client.py ── bounded per-raven HTTP with token isolation
      │
roost/host.py ── host election (the lock) + menu aggregation
      │
roost/tray.py ── the platform-neutral row list: WHAT the menu contains
      ├── roost/menubar.py       (macOS, rumps)
      └── roost/windows_tray.py  (Windows, pystray)
```

All paths below are relative to `roost/`.

| File | Role |
|---|---|
| `ravens.py` | Descriptor path resolution, parsing, validation, PID-reuse-safe liveness |
| `menu_spec.py` | Parses and bounds a raven's menu payload |
| `raven_client.py` | Per-raven HTTP client; per-raven token isolation |
| `host.py` | `HostLock` (host election) and `build_model` (aggregation) |
| `tray.py` | Turns a model into a flat `Row` list — the only place that decides menu content |
| `menubar.py` | macOS rendering of those rows, plus the entry point |
| `windows_tray.py` | Windows rendering of the same rows |
| `windows_support.py` | Windows shortcuts, user PATH/environment, tray process |
| `help_server.py` | The one loopback listener: the Help page and `/api/status` |
| `icons.py` | Selectable tray icon, defaulting to the raven |
| `paths.py` | Roost's own state directory and the owner-only write helpers |
| `sanitize.py` | Strips escapes/controls/bidi from untrusted strings |
| `cli.py` | CLI: `install`, `uninstall`, `ui`, `ravens`, `icon` |
| `__init__.py` | The product name and slug; the package docstring explains why the package exists |
| `../examples/` | Two runnable reference ravens — documentation, not libraries |

`menubar.py` guards its macOS-only imports so it stays importable elsewhere.
`windows_support.py` imports Windows-only packages *inside functions* for the same
reason: its path and argument logic is exercised by the shared unit suite.

Everything is inside the `roost` package. That is not style — see
[Coexistence](#coexistence-is-a-constraint-not-a-preference) below.

## Testing

```bash
python -m pytest tests/unit -q     # what CI runs, on macOS and Windows
python -m pytest tests/windows -q  # Windows-only native runtime
python -m pytest tests/env -q      # the real install; needs a venv
```

`tests/unit` must pass on both platforms with no display and no real install. Both
tray suites fake their toolkit (`rumps`, `pystray`) before importing; keep that
pattern.

`tests/unit/test_tray_parity.py` renders one model through **both** trays and
compares labels, separators, clickability, and the icon marker. If you touch
either tray's rendering, that file is the one that catches the drift.

## Rules

- **The host never interprets a raven's data.** It draws labels and hands action
  ids back. No special case for any raven's name or id, ever — both trays had
  previously hardcoded the same one participant's id, independently of each other,
  and `test_tray.py` plus both tray suites now assert its absence by grepping the
  source.

- **Both trays render the same rows.** All menu-content decisions belong in
  `tray.py`. A tray file may only turn a `Row` into a widget. Two trays each
  assembling their own structure is the state this design replaced.

- **A descriptor is untrusted input.** It is a file another process wrote.
  Validate every field, never `eval`/`exec`, never let a control character or ANSI
  escape reach a menu or a log, and treat a malformed descriptor as an
  **unavailable raven with a reason** — never a crash, never a silent omission.
  Descriptor fields are *refused*; menu labels are *sanitised* (see
  [SPEC.md §4](SPEC.md#4-the-menu) for why the two differ).

- **Version compatibility is a range, not an equality.** `MIN_API_VERSION..API_VERSION`
  overlapping the raven's declared window. Exact matching is huginn issue #38,
  where a routine bump silently disabled every participant. An incompatibility must
  be reported *with both ranges named*.

- **Per-raven token isolation.** A token is read fresh from *that raven's own*
  `token_path` and sent only to *that raven's own* port. Never cache one, never
  share one between ravens, never mint one on a raven's behalf. Request headers are
  built per call from an allowlist — never copied from anything inbound.

- **Everything binds `127.0.0.1`, and nothing is relayed.** The previous hook proxy
  was an unauthenticated open relay that rewrote `Host` to a clean loopback value,
  laundering drive-by requests past a consumer's own `require_local_origin` check.
  Any HTTP surface here must validate `Host` is loopback, reject **any** `Origin`,
  build request *and* response headers from allowlists (never forward
  `Authorization`, `Cookie`, `Origin`, `Referer`, `X-*`; never relay `Set-Cookie`),
  guard `Content-Length` (`< 0` or `> cap`), cap response reads, bound every
  network call with a timeout, and keep the `nosniff`/CSP headers. **Do not add a
  proxy.**

- **Every call to a raven is bounded.** A raven is another process that can hang,
  and the client runs on the thread that builds the menu. Timeout, response cap
  enforced on the read (not on the declared `Content-Length`), no redirects. A hung
  raven must degrade to a disabled section, never to a frozen menu.

- **Roost's own state is owner-only.** Everything in Roost's state directory — the host
  lock, the help port file, the tray PID file, the icon config — is 0600 under a
  0700 directory, created with restrictive permissions rather than chmodded after.
  Use `paths.secure_dir` / `paths.atomic_write_text`; do not open state files
  directly.

- **A PID from a file is not trusted.** Refuse non-positive values, and verify
  identity (`create_time` for liveness, the command line before signalling) before
  acting. `os.kill(-1, ...)` signals every process the user can signal.

- **Update `VERSION` and `help.md` together with behaviour changes.** `help.md` is
  served to users at runtime by `help_server.render_help_page`, so it is **product
  copy**, not documentation. It uses a Markdown table, which needs the `tables`
  extension *and* the table tags in the `nh3` allowlist — a test pins both.

- **Keep `SPEC.md` and `examples/` in step.** SPEC.md is the contract both ravens
  implement; the examples are its executable form. A protocol change that lands in
  one and not the other is worse than no change.

- **Do not edit the huginn or muninn repositories from here.** The reference
  implementations live in `examples/` precisely so this repository can document the
  contract without reaching into its consumers.

- **The raven icon is a licence obligation.** *Raven* by Lorc, game-icons.net, CC
  BY 3.0. `roost/assets/CREDITS.md` must keep crediting it for as long as the art
  is here. If the art goes, the credit goes with it; if art is added, credit it
  before shipping.

- **No internal names.** This is a public repository derived from an internal one.
  The scrub list is in the commit history; grep before committing.

## Coexistence is a constraint, not a preference

This repository is named `appistry` and the project inside it is **Roost**. The
mismatch is deliberate: Roost was forked from a separate app launcher called
Appistry which is *still in use on the same machines*, and the two must be
installable and runnable at the same time. The repository name is history; the
runtime identity is not.

Concretely, this is why the code looks the way it does:

- **Everything lives in the `roost` package.** Both projects previously installed
  top-level `appistry`, `menubar`, `windows_support`, and `windows_tray` modules,
  so whichever was installed second overwrote the other's. Do not add a `.py` file
  at the repository root; a test fails if you do.

- **Every name Roost owns is prefixed.** The state directory, the lock, the port
  file, the config, the log, the PID file, the launchd label, the Windows shortcut
  and Start Menu folder, the console script, the distribution, the pystray tray
  name, and the `/api/status` service string. When you add a file to the state
  directory, name it for this project — not for the surface it serves. `menubar.log`
  and `menubar-http-port` were exactly that mistake, and both were names the other
  project had already taken.

- **Never read, move, or remove anything under `~/.appistry`.** It holds another
  live tool's `registry.toml`, `pids/`, and `secrets/`. There is no migration from
  it and no compatibility read, including for files an early Roost build wrote
  there — two of those filenames are ambiguous between the projects, so there is no
  safe way to tell whose a given file is. `roost/paths.py` documents the reasoning.

- **`tests/unit/test_coexistence.py` pins all of it.** The other project's
  well-known values are literal constants there, each commented with the file it
  came from, because that codebase is private and cannot be imported. If you rename
  something this project owns, that file is what tells you whether you just created
  a collision. Do not weaken an assertion in it to make a change pass.

- **The ravens descriptor directory is the exception.** `~/.local/state/ravens`
  and `%LOCALAPPDATA%\Ravens` are a cross-project contract that Huginn and Muninn
  write into. It is not Roost's to rename, and `ravens.state_dir()` must keep
  resolving exactly what it resolves today.
