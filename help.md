# Appistry

One menu bar icon for your ravens.

**Huginn** watches what your AI agents are doing right now. **Muninn** remembers
what they did. Rather than give each one its own icon, Appistry shows both in one
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
Quit Appistry
```

Each raven gets its own part of the menu, and each decides what goes in it.
Appistry draws what they send and passes your clicks straight back — so when a
raven adds something new, it appears here without Appistry needing an update.

- **The number beside a name** is that raven's own count of things wanting your
  attention. Zero, and it is not shown.
- **A ● row** is something that wants you. **A · row** is quieter — background
  detail rather than a request.
- **Clicking a row** tells that raven to act on it. Rows that open a page open it
  in your browser, always against that raven's own address.
- **Greyed-out rows** are headings, or something the raven has marked as not
  currently actionable.
- **Tray icon** changes the icon in your menu bar. The raven is the default.
- **Help** opens this page.

---

## When a raven is missing

Only running ravens appear. If one is not there, it is not running — start it the
way you normally would, and it will show up within a few seconds.

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
| Needs raven API […]; this menu bar speaks […] | The raven and Appistry are too far apart in version. Update whichever is older. |
| Descriptor is not valid JSON | Its status file is damaged. Restart the raven to rewrite it. |

For the same information in a terminal, run `appistry ravens`.

---

## Quitting

**Quit Appistry** closes the menu bar icon only.

Your ravens keep running. Appistry watches them; it does not run them, so closing
it never stops them — it just means nothing is showing you what they are doing.
The icon comes back the next time you sign in, or immediately with `appistry ui`.

---

## Your data

Appistry has no data of its own. It reads status from your ravens over your own
machine's local connection, and stores nothing but your chosen icon.

**Nothing leaves your computer.** Appistry makes no network requests off your
machine at all — not for updates, not for analytics, not for anything. Each raven
keeps its own key, and Appistry only ever passes a raven its own.
