# Agent notes for Appistry

Guidance for AI coding agents working in this repository. Humans should read
[README.md](README.md) and [SPEC.md](SPEC.md) instead.

## What this project is

A macOS menu bar / Windows system tray launcher for local apps. Pure Python, no
build step. `menubar.py` holds the shared loopback servers and menu-state helpers
and is imported by `windows_tray.py`, so it must stay importable on Windows —
macOS-only imports (`rumps`, `objc`, `AppKit`, `fcntl`) are guarded behind
`_IS_MACOS`.

Module map:

| File                      | Role                                                   |
|---------------------------|--------------------------------------------------------|
| `appistry.py`             | CLI entry point and install/uninstall                  |
| `registry.py`             | `~/.appistry/registry.toml` read/write and validation   |
| `process.py`              | Start/stop/probe app processes, per-launch secrets      |
| `menubar.py`              | Shared loopback servers + macOS menu bar UI            |
| `windows_tray.py`         | Windows system tray UI                                 |
| `windows_support.py`      | Windows shortcuts, environment, single-instance mutex   |
| `launch.py`               | Launch-mode resolution and dedicated-window launching  |
| `ygg_shell.py`            | pywebview host for the dedicated native window         |
| `hooks.py`                | Stable hook URL construction                           |
| `cleanup.py`              | Safe project cleanup on app removal                    |
| `appistry_integration.py` | Drop-in module apps copy to integrate with Appistry    |

## Testing

```bash
python -m pytest tests/unit -q          # the suite CI runs on macOS and Windows
python -m pytest tests/windows -q       # Windows-only native runtime
python -m pytest tests/env -q           # real installed environment; needs a venv
```

`tests/unit` must pass on both macOS and Windows without a display or a real
install. It fakes `rumps` before importing `menubar`; keep that pattern when
adding tests that touch the macOS UI path.

## Rules

- **Everything binds to `127.0.0.1`.** Never introduce a non-loopback listener,
  and do not add outbound network calls without an explicit decision.
- **Registry values are untrusted input.** `id`, `name`, `cwd`, `command`,
  `icon`, and `github_url` come from whatever called `appistry register`. They
  are interpolated into filesystem paths, shell launcher scripts, notification
  text, and rendered HTML. Validate and escape at every use; see
  `registry.validate_app_id`, `registry.bundle_name_for`, and
  `process._validate_command`.
- **An app's `about.md` is untrusted HTML.** It is sanitized with `nh3` before
  being written to disk and opened. Do not widen the allowed tag/attribute set.
- **Never destroy user work.** The removal path deletes files automatically, with
  no confirmation and no undo, so `cleanup.git_clean_project` is the highest-risk
  code in the repo. It only ever runs at a repository **root** (verified against
  `git rev-parse --show-toplevel`) and only deletes tracked files that match
  `HEAD`; modified, untracked, and gitignored files are always kept. When
  changing it, remember that `ls-files` and `diff` anchor their output
  differently — the two path sets must be comparable or clean-looking files are
  really modified ones. If a file list cannot be obtained, delete nothing.
  Preserve that guarantee.
- **`git` runs over directories Appistry did not create.** A registry `cwd` can
  name any repo, and git reads command hooks (`core.fsmonitor`, `core.hooksPath`)
  from that repo's own config. Every `git` invocation must go through
  `cleanup._GIT_SAFE_FLAGS` and `cleanup._git_safe_env()`.
- **Update `VERSION` and `help.md` together with behaviour changes.** `help.md`
  is served to users at runtime by `menubar._render_help_page`, so it is product
  copy, not just documentation.
- Keep `SPEC.md` accurate — it is the contract other projects integrate against.
