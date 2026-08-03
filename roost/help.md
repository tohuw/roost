# Roost

One menu bar icon for your ravens.

**Huginn** watches what your AI agents are doing right now. **Muninn** remembers
what they did. Rather than give each one its own icon, Roost shows both in one
place — whichever of them happens to be running.

---

## The menu

Click the raven in your macOS menu bar or Windows system tray.

```
Huginn (2)                 ← the number is how many things want you
    Needs attention
      ● Approve: deploy to staging — claude
    Sessions
      Refactor the parser — codex
      ────────────
      Open Console
────────────────
Muninn
    Recent
      · Deployed staging — 12m ago
      · Merged #412 — yesterday
────────────────
Tray icon      ▸
Help
────────────────
Quit Roost
```

Each raven gets its own part of the menu, and each decides what goes in it.
Roost draws what they send and passes your clicks straight back — so when a
raven adds something new, it appears here without Roost needing an update.

- **The number beside a name** is that raven's own count of things wanting your
  attention. Zero, and it is not shown.
- **A ● row** is something that wants you. **A · row** is quieter — background
  detail rather than a request.
- **Clicking a row** tells that raven to act on it. Rows that open a page open it
  in your browser, always against that raven's own address. If a raven offers a
  row like *Quit* or *Restart*, that is the raven acting on itself — Roost just
  passes the click along.
- **Greyed-out rows** are headings, or something the raven has marked as not
  currently actionable.
- **Tray icon** changes the icon in your menu bar. The raven is the default, and
  the one called *Roost* is a leftover from the launcher this was forked from — it
  is a grid of squares that means nothing here. Pick it if you like it.
- **Help** opens this page.

---

## When a raven is missing

Only running ravens appear. If one is not there, it is not running — start it the
way you normally would, and it will show up within a few seconds.

**Roost cannot start it for you, and there is no menu item that will.** A raven
that is not running has taken its status file away, so Roost has no way to know it
was ever installed — there is nothing for it to offer you. Start it however you
normally start it (Huginn: `huginn serve`), or set it to start automatically when
you sign in, which is what `huginn install-agent` does. After that it is your
computer, not Roost, that keeps the raven running.

If a raven *is* there but greyed out, the menu says why underneath its name:

> **Muninn**
> Not running (its recorded process is gone).

That is deliberate. A raven that stopped, crashed, or cannot be reached stays
visible with a reason, because a name that silently vanished would look identical
to one you never installed.

Common reasons, and what they mean:

| What you see | What happened |
|---|---|
| Not running (its recorded process is gone) | It stopped or crashed. Start it again. |
| Is not answering on its recorded port | It is starting up, or wedged. Give it a moment, then restart it. |
| Did not answer in time | It is busy. This usually clears on its own. |
| Rejected the credential from its own token file | It restarted and changed its key. Restarting it again normally fixes this. |
| Needs raven API […]; this menu bar speaks […] | The raven and Roost are too far apart in version. Update whichever is older. |
| Descriptor is not valid JSON | Its status file is damaged. Restart the raven to rewrite it. |

For the same information in a terminal, run `roost ravens`.

---

## Quitting

**Quit Roost** closes the menu bar icon only.

Your ravens keep running. Roost watches them; it does not run them, so closing
it never stops them — it just means nothing is showing you what they are doing.
The icon comes back the next time you sign in, or immediately with `roost ui`.

To quit a *raven*, use its own row if it offers one — a **Quit Huginn** inside
Huginn's part of the menu stops Huginn and leaves Roost and any other raven
running. Note that if you have set that raven to start automatically at login, your
system may start it straight back up; that is the login setting doing its job, and
removing it is how you make a quit stick.

---

## Your data

Roost has no data of its own. It reads status from your ravens over your own
machine's local connection, and stores nothing but your chosen icon.

**Nothing leaves your computer.** Roost makes no network requests off your
machine at all — not for updates, not for analytics, not for anything. Each raven
keeps its own key, and Roost only ever passes a raven its own.

Roost keeps its own small state — your icon choice, a lock, a log — in one
directory of its own:

- **macOS and Linux:** `~/.local/state/roost`
- **Windows:** `%LOCALAPPDATA%\Roost`

---

## If you also have Appistry

Roost began as a fork of a separate app launcher called **Appistry**, and the two
are unrelated tools today. If you use both, they run side by side and neither
interferes with the other.

Roost touches only its own directory above, plus the shared folder your ravens
publish their status into. It never reads or writes anything belonging to
Appistry, and it installs its own `roost` command rather than replacing
`appistry`. You can install, start, stop, or remove either one without affecting
the other.
