"""The status item: a Cocoa shell around :mod:`freemicro.menubar.model`.

Everything decision-shaped lives in ``model.py`` and ``status.py``. This file
only knows how to draw a menu and how to turn a click into a call - which is
why the tests can cover the menu without a window server.

Three things worth knowing before editing:

**The run loop is ours.** Instead of ``[NSApp run]`` we pump
``nextEventMatchingMask:untilDate:…`` in a Python ``while``. That costs nothing,
keeps Ctrl-C working (``[NSApp run]`` blocks inside C, where a Python signal
handler never gets a chance), and gives us a natural place to hang the periodic
refresh without scheduling an ``NSTimer`` and another callback trampoline.

**Nothing slow happens on the main thread.** ``pgrep``, a ``device.status``
round trip against a pad that has just wandered out of range, a config write, a
daemon restart - all of it runs on a worker, and results come back through a
queue that the run loop drains. A menu bar that beach-balls is worse than no
menu bar.

**The menu is rebuilt on open, not on a timer.** ``menuNeedsUpdate:`` fires
immediately before the menu is shown, so what you see is what
:func:`~freemicro.menubar.model.build_menu` said at that instant, and there is
no work at all while it is closed.
"""

from __future__ import annotations

import ctypes
import os
import queue
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path
from typing import Any, Callable, List, Optional, Tuple

from freemicro.menubar import cocoa, status
from freemicro.menubar.model import MenuItem, Snapshot, bar_title, build_menu

#: How often the run loop wakes to refresh the title. Comfortably faster than
#: a human notices, slow enough to be free.
TICK_SECONDS = 0.4

#: Runtime name of the Objective-C class we install our callbacks on.
HANDLER_CLASS = "FreeMicroMenuBarHandler"


#: The app the runtime handler class forwards to. See ``_build_ui``.
_active: Optional["MenuBarApp"] = None


def _set_active(app: "MenuBarApp") -> None:
    global _active
    _active = app


def _dispatch_click(this: Any, cmd: Any, sender: Any) -> None:
    if _active is not None:
        _active._on_item_clicked(sender)


def _dispatch_menu_update(this: Any, cmd: Any, menu: Any) -> None:
    """Cocoa calls this immediately before the menu is shown."""
    if _active is not None:
        _active._rebuild_menu()


class SingleInstance:
    """An ``flock`` so a second menu bar cannot appear beside the first.

    Deliberately **not** the pad lock: this process never takes the device, and
    borrowing ``daemon.PadLock`` would tell every other FreeMicro command that
    the pad was busy when it is not.
    """

    def __init__(self, path: Optional[Path] = None) -> None:
        from freemicro.config import config_home

        self.path = Path(path) if path else config_home() / "menubar.lock"
        self._fd: Optional[int] = None

    def acquire(self) -> bool:
        try:
            import fcntl
        except ImportError:  # pragma: no cover - non-POSIX
            return True
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(str(self.path), os.O_RDWR | os.O_CREAT, 0o644)
        except OSError:
            return True  # never refuse to start over a lock file we can't make
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode("ascii"))
        self._fd = fd
        return True

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            os.close(fd)
        except OSError:
            pass


class MenuBarApp:
    """One status item, its menu, and the loop that keeps them current."""

    def __init__(self, poller: Optional[status.Poller] = None) -> None:
        self.poller = poller or status.Poller()
        self._running = False
        self._app: Any = None
        self._handler: Any = None
        self._status_item: Any = None
        self._menu: Any = None
        #: Parallel to the menu's item tags: (key, action, detail).
        self._targets: List[Tuple[str, str, str]] = []
        self._main_thread_work: "queue.Queue[Callable[[], None]]" = queue.Queue()
        self._last_title: Optional[Tuple[str, Any]] = None
        self._web_server: Any = None
        #: Set when the user asks for a restart into newer code. ``main`` acts
        #: on it *after* ``run`` has returned, so the status item, the poll
        #: thread and the single-instance lock are all released before the
        #: process is replaced.
        self.restart_requested = False

    # -- lifecycle --------------------------------------------------------

    def run(self) -> int:
        if not cocoa.is_available():
            print(cocoa.unavailable_reason(), file=sys.stderr)
            return 2
        self.poller.start()
        with cocoa.Pool():
            self._build_ui()
        self._running = True
        try:
            self._loop()
        except KeyboardInterrupt:
            pass
        finally:
            self._teardown()
        return 0

    def _build_ui(self) -> None:
        msg, cls = cocoa.msg, cocoa.cls
        self._app = msg(cls("NSApplication"), "sharedApplication")
        # Accessory: menu bar only. No Dock icon, no app menu, no window.
        msg(
            self._app,
            "setActivationPolicy:",
            cocoa.ACTIVATION_POLICY_ACCESSORY,
            restype=ctypes.c_bool,
            argtypes=(ctypes.c_long,),
        )

        # The Objective-C class is created once per *process* and its methods
        # are baked in at that moment, so they route through `_active` rather
        # than closing over `self`. Otherwise a second MenuBarApp in the same
        # process would silently deliver its clicks to the first one.
        _set_active(self)
        handler_class = cocoa.define_class(
            HANDLER_CLASS,
            {
                "itemClicked:": _dispatch_click,
                "menuNeedsUpdate:": _dispatch_menu_update,
            },
        )
        self._handler = msg(msg(handler_class, "alloc"), "init")
        msg(self._handler, "retain")

        bar = msg(cls("NSStatusBar"), "systemStatusBar")
        self._status_item = msg(
            bar,
            "statusItemWithLength:",
            ctypes.c_double(cocoa.VARIABLE_STATUS_ITEM_LENGTH),
            argtypes=(ctypes.c_double,),
        )
        msg(self._status_item, "retain")

        self._menu = msg(msg(cls("NSMenu"), "alloc"), "init")
        # We decide what is enabled; Cocoa's automatic enabling would grey out
        # every item because none of them are in a responder chain.
        msg(
            self._menu,
            "setAutoenablesItems:",
            False,
            argtypes=(ctypes.c_bool,),
        )
        msg(self._menu, "setDelegate:", self._handler, argtypes=(ctypes.c_void_p,))
        msg(
            self._status_item,
            "setMenu:",
            self._menu,
            argtypes=(ctypes.c_void_p,),
        )

        self._apply_title(self.poller.current)
        self._rebuild_menu()
        msg(self._app, "finishLaunching")

    def _loop(self) -> None:
        msg, cls = cocoa.msg, cocoa.cls
        mode = cocoa.constant("Foundation", "NSDefaultRunLoopMode")
        while self._running:
            with cocoa.Pool():
                until = msg(
                    cls("NSDate"),
                    "dateWithTimeIntervalSinceNow:",
                    ctypes.c_double(TICK_SECONDS),
                    argtypes=(ctypes.c_double,),
                )
                event = msg(
                    self._app,
                    "nextEventMatchingMask:untilDate:inMode:dequeue:",
                    ctypes.c_ulonglong(cocoa.EVENT_MASK_ANY),
                    until,
                    mode,
                    True,
                    argtypes=(
                        ctypes.c_ulonglong,
                        ctypes.c_void_p,
                        ctypes.c_void_p,
                        ctypes.c_bool,
                    ),
                )
                if event:
                    msg(self._app, "sendEvent:", event, argtypes=(ctypes.c_void_p,))
                self._pump()

    def _pump(self) -> None:
        """One tick: drain worker results, then refresh the bar glyph."""
        while True:
            try:
                work = self._main_thread_work.get_nowait()
            except queue.Empty:
                break
            try:
                work()
            except Exception:  # noqa: BLE001 - one bad callback, not a dead app
                continue
        try:
            self._apply_title(self.poller.current)
        except Exception:  # noqa: BLE001
            pass

    def _teardown(self) -> None:
        self._running = False
        self.poller.stop()
        server, self._web_server = self._web_server, None
        if server is not None:
            try:
                server.shutdown()
                server.server_close()
                server.api.close()
            except Exception:  # noqa: BLE001 - shutting down must not raise
                pass
        try:
            if self._status_item is not None:
                bar = cocoa.msg(cocoa.cls("NSStatusBar"), "systemStatusBar")
                cocoa.msg(
                    bar,
                    "removeStatusItem:",
                    self._status_item,
                    argtypes=(ctypes.c_void_p,),
                )
        except Exception:  # noqa: BLE001
            pass

    # -- drawing ----------------------------------------------------------

    def _apply_title(self, snap: Snapshot) -> None:
        title = bar_title(snap)
        signature = (title.text, title.color)
        if signature == self._last_title:
            return
        self._last_title = signature
        button = cocoa.msg(self._status_item, "button")
        if not button:
            return
        with cocoa.Pool():
            attributed = cocoa.attributed(title.text, title.color)
            if attributed:
                cocoa.msg(
                    button,
                    "setAttributedTitle:",
                    attributed,
                    argtypes=(ctypes.c_void_p,),
                )
            else:
                cocoa.msg(
                    button,
                    "setTitle:",
                    cocoa.nsstring(title.text),
                    argtypes=(ctypes.c_void_p,),
                )
            cocoa.msg(
                button,
                "setToolTip:",
                cocoa.nsstring(title.description),
                argtypes=(ctypes.c_void_p,),
            )

    def _rebuild_menu(self) -> None:
        msg, cls = cocoa.msg, cocoa.cls
        snap = self.poller.current
        with cocoa.Pool():
            msg(self._menu, "removeAllItems")
            self._targets = []
            for item in build_menu(snap):
                if item.separator:
                    msg(
                        self._menu,
                        "addItem:",
                        msg(cls("NSMenuItem"), "separatorItem"),
                        argtypes=(ctypes.c_void_p,),
                    )
                    continue
                msg(
                    self._menu,
                    "addItem:",
                    self._make_item(item),
                    argtypes=(ctypes.c_void_p,),
                )

    def _make_item(self, item: MenuItem) -> Any:
        msg, cls = cocoa.msg, cocoa.cls
        equivalent = "q" if item.key == "quit" else ""
        native = msg(
            msg(cls("NSMenuItem"), "alloc"),
            "initWithTitle:action:keyEquivalent:",
            cocoa.nsstring(item.label),
            None,
            cocoa.nsstring(equivalent),
            argtypes=(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p),
        )
        msg(native, "autorelease")

        # The state row is the one place colour is allowed to speak: a dot in
        # the state's colour, with the word itself in the ordinary menu colour.
        if item.key == "state":
            attributed = cocoa.attributed(f"●  {item.label}", item.color, length=1)
            if attributed:
                msg(
                    native,
                    "setAttributedTitle:",
                    attributed,
                    argtypes=(ctypes.c_void_p,),
                )

        if item.detail:
            msg(
                native,
                "setToolTip:",
                cocoa.nsstring(item.detail),
                argtypes=(ctypes.c_void_p,),
            )
        if item.checked is not None:
            msg(
                native,
                "setState:",
                cocoa.STATE_ON if item.checked else cocoa.STATE_OFF,
                argtypes=(ctypes.c_long,),
            )
        msg(native, "setEnabled:", bool(item.enabled), argtypes=(ctypes.c_bool,))
        if item.action:
            self._targets.append((item.key, item.action, item.detail))
            msg(
                native,
                "setTag:",
                len(self._targets) - 1,
                argtypes=(ctypes.c_long,),
            )
            msg(native, "setTarget:", self._handler, argtypes=(ctypes.c_void_p,))
            msg(
                native,
                "setAction:",
                cocoa.sel("itemClicked:"),
                argtypes=(ctypes.c_void_p,),
            )
        return native

    # -- actions ----------------------------------------------------------

    def _on_item_clicked(self, sender: Any) -> None:
        tag = cocoa.msg(sender, "tag", restype=ctypes.c_long)
        if not 0 <= tag < len(self._targets):
            return
        key, action, detail = self._targets[tag]
        handler = {
            "toggle_lighting": self._toggle_lighting,
            "open_config": self._open_config,
            "run_doctor": self._run_doctor,
            "open_input_monitoring": self._open_input_monitoring,
            "open_accessibility": self._open_accessibility,
            "explain_chatgpt": lambda d=detail: self._alert(
                "The ChatGPT app is driving the same LEDs", d
            ),
            "start_bridge": self._start_bridge,
            "restart_daemon": self._restart_daemon,
            "restart_menubar": self._restart_menubar,
            "quit": self._quit,
        }.get(action)
        if handler is not None:
            handler()

    def _quit(self) -> None:
        self._running = False

    def _toggle_lighting(self) -> None:
        """Flip ``lighting.enabled`` - the kill switch, and the opt-in.

        Writing the config rather than the hardware is deliberate: whoever holds
        the pad is a different process, and the file is the only thing both of
        us agree on.
        """
        snap = self.poller.current
        wanted = not snap.lighting_enabled
        if wanted and snap.chatgpt_running:
            self._alert(
                "Turn LED control on anyway?",
                "The ChatGPT desktop app is running and drives the same LEDs "
                "over the same channel. Turning FreeMicro's lighting on now "
                "gives you two owners repainting the same LEDs - quit ChatGPT "
                "first if the pad starts flickering.",
            )
        self._in_background(lambda: self._write_lighting(wanted))

    def _write_lighting(self, enabled: bool) -> None:
        try:
            status.set_lighting_enabled(enabled)
        except Exception as exc:  # noqa: BLE001
            self._report("Could not change LED control", str(exc))
            return
        # A running daemon read the config once, at startup. Nudge it, or the
        # switch would silently do nothing until the next login.
        status.restart_pad_owner()
        self.poller.refresh()

    def _start_bridge(self) -> None:
        """Nothing is driving the pad. Install the daemon that always will."""

        def work() -> None:
            ok, message = status.install_daemon()
            self.poller.refresh()
            self._report(
                "The pad has an owner again" if ok else "Could not start it",
                message,
            )

        self._in_background(work)

    def _restart_daemon(self) -> None:
        """Kick the daemon so it re-reads the code and the config on disk."""

        def work() -> None:
            done = status.restart_pad_owner()
            self.poller.refresh()
            self._report(
                "Restarted" if done else "Nothing to restart",
                done
                or "The background daemon is not running, so there is nothing "
                "here to bring up to date.",
            )

        self._in_background(work)

    def _restart_menubar(self) -> None:
        """Replace this process with the code that is installed right now.

        Verified before committing, exactly like the bridge: re-execing into a
        tree that will not import turns "slightly out of date" into "gone from
        your menu bar", which is a far worse outcome than the problem.
        """

        def work() -> None:
            from freemicro import staleness

            ok, detail = staleness.verify_new_code()
            if not ok:
                self._report(
                    "Not restarting yet",
                    f"The updated FreeMicro does not import ({detail}), so this "
                    "menu bar is staying on the code it has.",
                )
                return
            self.restart_requested = True
            self._on_main(self._quit)

        self._in_background(work)

    def _open_input_monitoring(self) -> None:
        from freemicro.permissions import PANE_INPUT_MONITORING

        self._open_pane(PANE_INPUT_MONITORING)

    def _open_accessibility(self) -> None:
        from freemicro.permissions import PANE_ACCESSIBILITY

        self._open_pane(PANE_ACCESSIBILITY)

    def _open_pane(self, url: str) -> None:
        from freemicro import permissions

        self._in_background(lambda: permissions.open_pane(url))

    def _open_config(self) -> None:
        """The web editor when we have one, the file in Finder when we do not."""
        if status.web_ui_available():
            self._in_background(self._open_web_ui)
            return
        self._in_background(self._reveal_config)

    def _open_web_ui(self) -> None:
        try:
            if self._web_server is None:
                from freemicro.webui.server import create_server

                server = create_server()
                threading.Thread(
                    target=server.serve_forever,
                    kwargs={"poll_interval": 0.2},
                    name="freemicro-webui",
                    daemon=True,
                ).start()
                self._web_server = server
            webbrowser.open(self._web_server.entry_url)
        except Exception:  # noqa: BLE001 - fall back rather than fail
            self._reveal_config()

    def _reveal_config(self) -> None:
        from freemicro import padconfig

        try:
            path = padconfig.user_path()
            if not path.exists():
                padconfig.write_starter(path)
            subprocess.run(
                ["open", "-R", str(path)], capture_output=True, timeout=15
            )
        except Exception as exc:  # noqa: BLE001
            self._report("Could not open the config", str(exc))

    def _run_doctor(self) -> None:
        def work() -> None:
            from freemicro.menubar import checks

            try:
                results = checks.run_checks()
            except Exception as exc:  # noqa: BLE001
                self._report("Doctor could not run", str(exc))
                return
            title = checks.summary(results)
            body = checks.report(results)
            self._on_main(lambda: self._alert(title, body, copyable=True))

        self._in_background(work)

    # -- plumbing ---------------------------------------------------------

    def _in_background(self, work: Callable[[], None]) -> None:
        threading.Thread(target=_shielded(work), daemon=True).start()

    def _on_main(self, work: Callable[[], None]) -> None:
        self._main_thread_work.put(work)

    def _report(self, title: str, body: str) -> None:
        """Show an alert from a worker thread.

        The message is captured as a plain string on purpose: Python clears the
        ``except … as exc`` name at the end of the block, so a lambda closing
        over it would raise ``NameError`` by the time the main thread ran it.
        """
        message = str(body)
        self._on_main(lambda: self._alert(title, message))

    def _alert(self, title: str, body: str, copyable: bool = False) -> None:
        """A modal sheet-less alert. Must be called on the main thread."""
        msg, cls = cocoa.msg, cocoa.cls
        with cocoa.Pool():
            alert = msg(msg(cls("NSAlert"), "alloc"), "init")
            msg(
                alert,
                "setMessageText:",
                cocoa.nsstring(title),
                argtypes=(ctypes.c_void_p,),
            )
            msg(
                alert,
                "setInformativeText:",
                cocoa.nsstring(body),
                argtypes=(ctypes.c_void_p,),
            )
            msg(
                alert,
                "addButtonWithTitle:",
                cocoa.nsstring("Done"),
                argtypes=(ctypes.c_void_p,),
            )
            if copyable:
                msg(
                    alert,
                    "addButtonWithTitle:",
                    cocoa.nsstring("Copy"),
                    argtypes=(ctypes.c_void_p,),
                )
            msg(
                self._app,
                "activateIgnoringOtherApps:",
                True,
                argtypes=(ctypes.c_bool,),
            )
            response = msg(alert, "runModal", restype=ctypes.c_long)
            msg(alert, "release")
        if copyable and response == cocoa.ALERT_FIRST_BUTTON + 1:
            cocoa.copy_to_clipboard(f"{title}\n\n{body}")


def _shielded(work: Callable[[], None]) -> Callable[[], None]:
    def run() -> None:
        try:
            work()
        except Exception:  # noqa: BLE001 - a worker must never kill the app
            pass

    return run


def main(argv: Optional[List[str]] = None) -> int:
    """Entry point for ``freemicro menubar`` and ``python -m freemicro.menubar``."""
    if argv:
        print(f"freemicro menubar takes no arguments (got {' '.join(argv)})",
              file=sys.stderr)
        return 2
    if not cocoa.is_available():
        print(cocoa.unavailable_reason(), file=sys.stderr)
        return 2
    instance = SingleInstance()
    if not instance.acquire():
        print(
            "A FreeMicro menu bar item is already running - look for the dot in "
            "your menu bar.",
            file=sys.stderr,
        )
        return 1
    app = MenuBarApp()
    try:
        code = app.run()
    finally:
        instance.release()
    if app.restart_requested:
        from freemicro import staleness

        # Everything is already released - status item, poll thread, lock - so
        # this is the safe point to be replaced. Same argv, so nothing about
        # how the user started us is lost.
        print("[update] freemicro changed on disk - restarting the menu bar…")
        staleness.CodeWatcher().restart()
    return code


__all__ = ["HANDLER_CLASS", "TICK_SECONDS", "MenuBarApp", "SingleInstance", "main"]
