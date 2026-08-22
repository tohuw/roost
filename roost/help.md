# Roost

One menu bar icon for your birds.

**Huginn** watches what your AI agents are doing right now. **Muninn** remembers
what they did. Rather than give each one its own icon, Roost shows both in one
place — whichever of them happens to be running.

---

## The menu

Click the bird in your macOS menu bar or Windows system tray.

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
Help
────────────────
Quit Roost
```

Each bird gets its own part of the menu, and each decides what goes in it.
Roost draws what they send and passes your clicks straight back — so when a
bird adds something new, it appears here without Roost needing an update.

- **The number beside a name** is that bird's own count of things wanting your
  attention. Zero, and it is not shown.
- **A ● row** is something that wants you. **A · row** is quieter — background
  detail rather than a request. The moment a row newly becomes a ●, Roost fires
  one system notification for it — never a repeat while it stays that way, and
  never a burst of them for everything that already wanted you when Roost
  started.
- **Clicking that notification** does what clicking the row would have done, so
  a session asking for you is one click away from the front. On Windows the
  notification is also kept in the notification centre, under *Roost*, for one
  that appeared while you were away; a click there still works for as long as
  Roost has been running since. On macOS the notification is a banner only.
- **A bird that stops answering** does not clear its ● rows and does not
  re-announce them when it comes back. Silence is not "resolved".
- **Clicking a row** tells that bird to act on it. Rows that open a page open it
  in your browser, always against that bird's own address. If a bird offers a
  row like *Quit* or *Restart*, that is the bird acting on itself — Roost just
  passes the click along.
- **Greyed-out rows** are headings, or something the bird has marked as not
  currently actionable.
- **Help** opens this page.

---

## When a bird is missing

Only running birds appear. If one is not there, it is not running — start it the
way you normally would, and it will show up within a few seconds.

Roost used to call every participant a *raven*, back when there were two and both
were. They are **birds** now, and the folder they publish into was renamed to
match. Nothing you already have stops working: Roost reads the old folder as well
as the new one, so Huginn and Muninn keep appearing exactly as before while they
catch up. `roost birds` prints both locations if you want to see them.

**Roost cannot start it for you, and there is no menu item that will.** A bird
that is not running has taken its status file away, so Roost has no way to know it
was ever installed — there is nothing for it to offer you. Start it however you
normally start it (Huginn: `huginn serve`), or set it to start automatically when
you sign in, which is what `huginn install-agent` does. After that it is your
computer, not Roost, that keeps the bird running.

If a bird *is* there but greyed out, the menu says why underneath its name:

> **Muninn**
> Not running (its recorded process is gone).

That is deliberate. A bird that stopped, crashed, or cannot be reached stays
visible with a reason, because a name that silently vanished would look identical
to one you never installed.

If the bird told Roost how to start it, a **Start** row appears underneath the
reason. Clicking it asks your computer's own startup mechanism — the same one
`install-agent` set up — to run it again. Roost never runs anything the bird's
status file named; it only passes along the name of a service you already
installed. If no Start row appears, that bird has not said how, and you start it
the usual way.

Common reasons, and what they mean:

| What you see | What happened |
|---|---|
| Not running (its recorded process is gone) | It stopped or crashed. Use **Start** if it is offered. |
| Is not answering on its recorded port | It is starting up, or wedged. Give it a moment, then restart it. |
| Did not answer in time | It is busy. This usually clears on its own. |
| Rejected the credential from its own token file | It restarted and changed its key. Restarting it again normally fixes this. |
| Needs bird API […]; this menu bar speaks […] | The bird and Roost are too far apart in version. Update whichever is older. |
| Descriptor is not valid JSON | Its status file is damaged. Restart the bird to rewrite it. |

For the same information in a terminal, run `roost birds`.

---

## Quitting

**Quit Roost** closes the menu bar icon only.

Your birds keep running. Roost watches them; it does not run them, so closing
it never stops them — it just means nothing is showing you what they are doing.
The icon comes back the next time you sign in, or immediately with `roost ui`.

To quit a *bird*, use its own row if it offers one — a **Quit Huginn** inside
Huginn's part of the menu stops Huginn and leaves Roost and any other bird
running. Note that if you have set that bird to start automatically at login, your
system may start it straight back up; that is the login setting doing its job, and
removing it is how you make a quit stick.

---

## Your data

Roost has no data of its own. It reads status from your birds over your own
machine's local connection, and stores no preferences at all.

**Nothing leaves your computer.** Roost makes no network requests off your
machine at all — not for updates, not for analytics, not for anything. Each bird
keeps its own key, and Roost only ever passes a bird its own.

Roost keeps its own small state — a lock, a log — in one
directory of its own:

- **macOS and Linux:** `~/.local/state/roost`
- **Windows:** `%LOCALAPPDATA%\Roost`

---

## If you also have Appistry

Roost began as a fork of a separate app launcher called **Appistry**, and the two
are unrelated tools today. If you use both, they run side by side and neither
interferes with the other.

Roost touches only its own directory above, plus the shared folder your birds
publish their status into. It never reads or writes anything belonging to
Appistry, and it installs its own `roost` command rather than replacing
`appistry`. You can install, start, stop, or remove either one without affecting
the other.
