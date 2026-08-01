"""Native Windows system-tray UI for Appistry."""

from __future__ import annotations

import logging
import os
import signal
import threading
import time
import traceback
import webbrowser
from pathlib import Path

import cleanup
import launch
import menubar
import process
import registry
import windows_support


HERE = Path(__file__).resolve().parent
log = logging.getLogger(__name__)
_SHORTCUT_GRACE_SECONDS = 15


class AppistryWindowsTray:
    def __init__(self):
        import pystray
        from PIL import Image

        self._pystray = pystray
        self._stop_event = threading.Event()
        self._state_lock = threading.RLock()
        self._search_lock = threading.Lock()
        self._menu_signature = None
        self._known_shortcuts = {
            entry.id
            for entry in registry.load()
            if windows_support.registered_shortcut_path(entry).is_file()
        }
        self._shortcut_missing_since: dict[str, float] = {}
        with Image.open(HERE / "appistry_icon.png") as image:
            tray_image = image.convert("RGBA")
        self._icon = pystray.Icon(
            "appistry",
            tray_image,
            "Appistry",
            menu=self._build_menu(),
        )

    def _build_menu(self):
        Menu = self._pystray.Menu
        MenuItem = self._pystray.MenuItem
        records, signature = menubar._menu_state()
        items = []
        running = [(entry, about) for entry, is_running, about in records if is_running]

        if running:
            items.append(MenuItem("Running apps", None, enabled=False))
            items.append(MenuItem("Search running apps...", self._search_apps))
            for entry, about_path in running:
                submenu = [
                    MenuItem("Open", self._callback(self._open_app, entry.id)),
                    MenuItem("Stop", self._callback(self._stop_app, entry.id)),
                    MenuItem("Restart", self._callback(self._restart_app, entry)),
                ]
                if about_path is not None or entry.github_url:
                    submenu.append(Menu.SEPARATOR)
                if about_path is not None:
                    submenu.append(
                        MenuItem(
                            "About",
                            self._callback(self._open_about, entry.name, about_path),
                        )
                    )
                if entry.github_url:
                    submenu.append(
                        MenuItem(
                            "GitHub",
                            self._callback(webbrowser.open, entry.github_url),
                        )
                    )
                items.append(MenuItem(entry.name, Menu(*submenu)))
        else:
            items.append(MenuItem("No apps are running", None, enabled=False))

        items.append(Menu.SEPARATOR)
        yggdrasil = next((entry for entry, _, _ in records if entry.id == "yggdrasil"), None)
        if yggdrasil is not None:
            items.append(
                MenuItem("Browse Apps", self._callback(self._browse_apps, yggdrasil))
            )
            items.append(Menu.SEPARATOR)
        items.extend(
            [
                MenuItem("Help", self._open_help),
                Menu.SEPARATOR,
                MenuItem("Quit All", self._quit_all),
                MenuItem("Quit Appistry", self._quit_appistry),
            ]
        )
        self._menu_signature = signature
        return Menu(*items)

    @staticmethod
    def _callback(function, *bound_args):
        def invoke(_icon, _item):
            function(*bound_args)

        return invoke

    def _refresh_menu(self) -> None:
        with self._state_lock:
            self._icon.menu = self._build_menu()
            self._icon.update_menu()

    def _open_app(self, app_id: str) -> None:
        menubar._open_app_ui(app_id)

    def _stop_app(self, app_id: str) -> None:
        process.stop(app_id)
        self._refresh_menu()

    def _restart_app(self, entry: registry.AppEntry) -> None:
        # Mirror the macOS tray: in shell mode the old dedicated window self-closed
        # when the server stopped and the one-shot secret can't be reused, so open
        # a fresh window after restart. In browser mode keep the launch page.
        shell = launch.is_shell_mode()
        if not shell:
            menubar._open_launch_page(entry.id)
        process.stop(entry.id)
        process.start(entry)
        if shell:
            launch.open_dedicated_window(entry, block=False)
        self._refresh_menu()

    def _open_about(self, name: str, about_path: Path) -> None:
        menubar._make_about(name, about_path)(None)

    def _browse_apps(self, entry: registry.AppEntry) -> None:
        menubar._open_launch_page(entry.id)
        if not process.is_running(entry.id):
            process.start(entry)
            self._refresh_menu()

    def _open_help(self, _icon=None, _item=None) -> None:
        webbrowser.open(f"http://127.0.0.1:{menubar._help_server_start()}/")

    def _search_apps(self, _icon=None, _item=None) -> None:
        if not self._search_lock.acquire(blocking=False):
            return
        try:
            self._show_search_window()
        finally:
            self._search_lock.release()

    def _show_search_window(self) -> None:
        import tkinter as tk
        from tkinter import ttk

        entries = [entry for entry in registry.load() if process.is_running(entry.id)]
        root = tk.Tk()
        root.title("Search running apps")
        root.geometry("440x330")
        root.minsize(360, 240)
        root.attributes("-topmost", True)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(1, weight=1)

        query = tk.StringVar()
        search = ttk.Entry(root, textvariable=query)
        search.grid(row=0, column=0, padx=12, pady=(12, 8), sticky="ew")
        search.configure(takefocus=True)

        results = tk.Listbox(root, activestyle="dotbox", exportselection=False)
        results.grid(row=1, column=0, padx=12, pady=(0, 8), sticky="nsew")
        hint = ttk.Label(root, text="Enter opens the selected app. Esc closes search.")
        hint.grid(row=2, column=0, padx=12, pady=(0, 12), sticky="w")

        visible: list[registry.AppEntry] = []

        def refresh(*_args):
            visible.clear()
            visible.extend(entry for entry in entries if menubar._app_matches_search(entry, query.get()))
            results.delete(0, tk.END)
            for entry in visible:
                results.insert(tk.END, f"{entry.name}  ({entry.id})")
            if visible:
                results.selection_set(0)
                results.activate(0)

        def open_selected(_event=None):
            selection = results.curselection()
            if not selection:
                return
            entry = visible[selection[0]]
            root.destroy()
            menubar._open_launch_page(entry.id)

        query.trace_add("write", refresh)
        search.bind("<Return>", open_selected)
        results.bind("<Return>", open_selected)
        results.bind("<Double-Button-1>", open_selected)
        root.bind("<Escape>", lambda _event: root.destroy())
        refresh()
        search.focus_set()
        root.after(50, root.lift)
        root.mainloop()

    def _check_removed_shortcuts(self) -> None:
        now = time.monotonic()
        for entry in registry.load():
            shortcut = windows_support.registered_shortcut_path(entry)
            if shortcut.is_file():
                self._known_shortcuts.add(entry.id)
                self._shortcut_missing_since.pop(entry.id, None)
            elif entry.id in self._known_shortcuts:
                missing_since = self._shortcut_missing_since.setdefault(entry.id, now)
                if now - missing_since >= _SHORTCUT_GRACE_SECONDS:
                    self._handle_removed(entry)

    def _handle_removed(self, entry: registry.AppEntry) -> None:
        self._known_shortcuts.discard(entry.id)
        self._shortcut_missing_since.pop(entry.id, None)
        if process.is_running(entry.id):
            process.stop(entry.id)
        cleaned = cleanup.git_clean_project(Path(entry.cwd))
        windows_support.remove_registered_shortcut(entry)
        registry.remove(entry.id)
        message = (
            f"{entry.name} was removed and its default project files were cleaned up. "
            "Your data and changes were preserved."
            if cleaned
            else f"{entry.name} was removed. Its project was not a clean Git checkout, so it was left untouched."
        )
        self._icon.notify(message, "Appistry")

    def _poll(self) -> None:
        while not self._stop_event.wait(5):
            try:
                windows_support.refresh_user_environment()
                self._check_removed_shortcuts()
                state = menubar._menu_state()
                if state[1] != self._menu_signature:
                    self._refresh_menu()
            except Exception:
                log.error("Windows tray poll failed:\n%s", traceback.format_exc())

    def _setup(self, icon) -> None:
        icon.visible = True
        threading.Thread(target=self._poll, name="appistry-windows-poll", daemon=True).start()

    def _shutdown(self, *, stop_apps: bool) -> None:
        if self._stop_event.is_set():
            return
        self._stop_event.set()
        if stop_apps:
            for entry in registry.load():
                if process.is_running(entry.id):
                    process.stop(entry.id)
        menubar._hook_server_shutdown()
        menubar._help_server_shutdown()
        windows_support.tray_pid_path().unlink(missing_ok=True)
        self._icon.stop()

    def _quit_all(self, _icon=None, _item=None) -> None:
        self._shutdown(stop_apps=True)

    def _quit_appistry(self, _icon=None, _item=None) -> None:
        self._shutdown(stop_apps=False)

    def run(self) -> None:
        windows_support.refresh_user_environment()
        registry.APPISTRY_DIR.mkdir(parents=True, exist_ok=True)
        windows_support.tray_pid_path().write_text(str(os.getpid()), encoding="utf-8")
        menubar._hook_server_start()
        menubar._help_server_start()
        self._icon.run(setup=self._setup)


def main() -> None:
    if not windows_support.is_windows():
        raise SystemExit("windows_tray.py is only available on Windows")

    registry.APPISTRY_DIR.mkdir(parents=True, exist_ok=True)
    from logging.handlers import RotatingFileHandler

    handler = RotatingFileHandler(
        registry.APPISTRY_DIR / "menubar.log",
        maxBytes=512_000,
        backupCount=1,
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logging.getLogger().addHandler(handler)
    logging.getLogger().setLevel(logging.WARNING)

    mutex = windows_support.NamedMutex()
    if not mutex.acquire():
        return
    tray = None
    try:
        tray = AppistryWindowsTray()

        def stop_for_signal(_signum, _frame):
            if tray is not None:
                tray._quit_appistry()

        signal.signal(signal.SIGTERM, stop_for_signal)
        signal.signal(signal.SIGINT, stop_for_signal)
        tray.run()
    except Exception:
        logging.critical(traceback.format_exc())
        raise
    finally:
        windows_support.tray_pid_path().unlink(missing_ok=True)
        mutex.release()


if __name__ == "__main__":
    main()
