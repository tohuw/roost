# Appistry

A menu bar and Windows system tray launcher for your local apps.

---

## The menu

Click the Appistry icon in your macOS menu bar or Windows system tray to see your running apps.

```
Search running apps…       ← Windows opens a native search window
● App One
    Open ↗
    Stop          ← becomes Restart when Option is held on macOS
    ────────────
    About         ← GitHub is explicit on Windows; hold Option on macOS
● App Two
    Open ↗
    Stop
────────────────
Browse Apps ↗
────────────────
Help
────────────────
Quit All
Quit Appistry
```

Only **running** apps appear in the menu. To start a stopped app, launch its `.app`
from `/Applications` on macOS, or choose its shortcut from the Windows Start Menu's
`Appistry` folder.

- **Open ↗** — opens the app in your browser, or in a dedicated window if window mode is enabled.
- **Stop** — shuts the app down.
- **Restart** — on macOS, hold Option while the menu is open to replace **Stop** with **Restart**. On Windows, choose the explicit **Restart** action.
- **Search running apps** — filters by app name or ID and opens the selected app.
- **About** — opens the app's about page (shown only if the app provides one).
- **GitHub ↗** — opens the app's repository. It is an explicit Windows action; on macOS, hold Option over **About**.
- **Browse Apps ↗** — opens your app catalog, if one is registered. Starts it first if it isn't running.
- **Help** — opens this page.

---

## Quitting

**Quit All** stops every running app and then closes Appistry itself.

**Quit Appistry** closes the tray icon only; your apps keep running in the background.

---

## Removing an app

On macOS, drag the app's icon from `/Applications` to the Trash. On Windows, remove
the app's shortcut from the Start Menu's `Appistry` folder. Within a few seconds,
Appistry will notice, stop the app if it was running, clean up its default installed
files, and remove it from the menu.

> **Your own work is always safe.** Only the app's default installed files are removed.
> Anything you've changed, created, or that the app stored as data is left exactly where it is.
