"""Gathering a :class:`~freemicro.menubar.model.Snapshot`, without owning the pad.

The hard constraint here is the one the whole project is built around: **only
one process can usefully hold the Codex Micro.** The daemon (or ``freemicro
run``) normally owns it, and a menu bar that opens the device behind their back
would produce exactly the failure this project keeps warning about - two owners
repainting the same LEDs with no way for the user to tell which is at fault.

So this module reads, and only reads:

* **Agent state** comes from :class:`~freemicro.state.engine.StateStore`, which
  is just files on disk. No device involved, no contention possible.
* **Presence and transport** come from ``device_transport()``, an IOKit registry
  lookup that needs no open and no permission.
* **Permissions** come from :mod:`freemicro.permissions`, which asks macOS
  directly and never prompts.
* **Ownership** comes from the daemon's ``flock``, tested rather than assumed.
* **Battery and firmware** are the one thing that genuinely requires the device
  (``device.status``). We take that route *only when the lock proves nobody else
  has the pad*, at a slow cadence, and cache the answer in
  ``~/.freemicro/status.json`` so the reading survives the daemon taking the pad
  back. When the daemon owns the pad it is expected to refresh that same file -
  see "Wiring required" in ``docs/MENUBAR.md``.

Everything is wrapped so that a transient failure - and on a wireless pad they
are constant - degrades a field to "unknown" rather than raising.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from freemicro.config import config_home
from freemicro.menubar.model import Snapshot, StaleNote
from freemicro.state.engine import AgentState, default_store

#: Where the last successful ``device.status`` round trip is cached.
STATUS_FILENAME = "status.json"

#: How often each probe runs. Presence is cheap; ``pgrep`` and the device round
#: trip are not, so they get their own, slower clocks.
POLL_SECONDS = 2.0
PROCESS_POLL_SECONDS = 10.0
DEVICE_POLL_SECONDS = 60.0

#: Give up on a ``device.status`` reply after this. The pad answers in
#: milliseconds when it answers at all.
DEVICE_TIMEOUT = 2.0


def status_path() -> Path:
    """The battery/firmware cache. Shared with the daemon by convention."""
    return config_home() / STATUS_FILENAME


def read_status(path: Optional[Path] = None) -> Dict[str, Any]:
    """The cached ``device.status``, or ``{}`` if there is none we can read."""
    target = Path(path) if path else status_path()
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_status(data: Dict[str, Any], path: Optional[Path] = None) -> None:
    """Cache a ``device.status`` reply atomically. Failure is never fatal."""
    target = Path(path) if path else status_path()
    payload = dict(data)
    payload.setdefault("updated_at", time.time())
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(target.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh)
            os.replace(tmp, target)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except OSError:
        return


# ---------------------------------------------------------------------------
# The individual probes. Each one answers "unknown" rather than raising.
# ---------------------------------------------------------------------------

def resolved_state() -> Tuple[AgentState, int]:
    """``(state, live session count)`` from the on-disk state store.

    The store is :func:`~freemicro.state.engine.default_store`'s, not one built
    here: the menu bar saying ``idle`` while the pad two inches away shows blue
    is the same contradiction from a second angle, and it is what happened
    while this function constructed its own with two of the four TTLs.

    Still a throwaway store, on purpose - it is rebuilt on every poll so a
    config edit is picked up without a restart. That costs nothing it should
    not: the liveness probe is process-wide (:func:`default_liveness`), so the
    expensive half of "is that session still open?" is not re-paid per poll.
    """
    try:
        store = default_store()
        return store.resolved_state(), len(store.sessions())
    except Exception:  # noqa: BLE001 - a broken store must not blank the menu
        return AgentState.IDLE, 0


def presence() -> Tuple[bool, str, bool, str]:
    """``(supported, transport, connected, reason)`` - no open, no permission."""
    try:
        from freemicro.device import device_transport, is_supported, unsupported_reason

        if not is_supported():
            return False, "", False, unsupported_reason()
        transport = device_transport()
        return True, transport or "", transport is not None, ""
    except Exception as exc:  # noqa: BLE001
        return False, "", False, str(exc)


def lighting_enabled() -> Tuple[bool, str]:
    """``(enabled, config path)`` - from the pad config, defaulting to off."""
    from freemicro import padconfig

    try:
        pad = padconfig.load()
        return bool(pad.lighting.enabled), str(pad.source or "")
    except Exception:  # noqa: BLE001 - a broken keymap is the `doctor` row's job
        return False, str(padconfig.user_path())


def pad_owner() -> str:
    """Who holds the pad, described for a human, or "" if it is free."""
    try:
        from freemicro import daemon

        holder = daemon.lock_holder()
        if not holder or holder.get("pid") == os.getpid():
            return ""
        return daemon.describe_holder(holder)
    except Exception:  # noqa: BLE001
        return ""


def permissions_state() -> Tuple[Optional[bool], bool]:
    """``(input monitoring, accessibility)``. Neither call ever prompts."""
    try:
        from freemicro import permissions

        granted, _ = permissions.input_monitoring()
        access, _ = permissions.accessibility()
        return granted, access
    except Exception:  # noqa: BLE001
        return None, True


def chatgpt_running() -> bool:
    try:
        from freemicro import permissions

        return permissions.chatgpt_running()
    except Exception:  # noqa: BLE001
        return False


def bridge_state() -> Tuple[bool, Tuple[StaleNote, ...]]:
    """``(is anything driving the pad, what is out of date)``.

    Costs one ``ps`` and one walk of the package directory, so it runs on the
    poller's *slow* clock beside the ``pgrep`` for ChatGPT - never on the 2 s
    tick and never on the main thread.

    Ignorance is silence: anything unexpected here reports "listening, nothing
    stale" rather than painting warnings the user cannot act on.
    """
    from freemicro import staleness

    try:
        report = staleness.report()
    except Exception:  # noqa: BLE001 - a probe failure must not invent warnings
        return True, ()
    notes = []
    # Ourselves first. `report()` excludes the caller by design, and the menu
    # bar is exactly the long-lived reader that has to notice its own drift:
    # a menu bar from before an update once rejected a valid config outright.
    try:
        mine = staleness.self_staleness(staleness.ROLE_MENUBAR)
    except Exception:  # noqa: BLE001
        mine = None
    if mine is not None:
        notes.append(
            StaleNote(
                summary="FreeMicro was updated - this menu bar is out of date",
                fix=(
                    "It is still running the code and config it started with, "
                    "which is how a menu bar ends up rejecting a config that is "
                    "perfectly valid. Click to restart it."
                ),
                action="restart_menubar",
            )
        )
    for entry in report.stale:
        restartable = entry.process.role == staleness.ROLE_DAEMON
        notes.append(
            StaleNote(
                summary=f"Out of date: {entry.process.label}",
                fix=entry.summary() + f"\nFix: {entry.fix()}",
                action="restart_daemon" if restartable else "",
            )
        )
    return report.listener.listening, tuple(notes)


def install_daemon() -> Tuple[bool, str]:
    """Install the LaunchAgent - the durable answer to "nothing is listening".

    Returns ``(ok, one line for an alert)``. Installing is the honest fix to
    offer from a menu: a ``freemicro run`` lives and dies with somebody's
    terminal window, and launchd's is the only lifecycle a status item can
    truthfully promise.
    """
    try:
        from freemicro import daemon

        result = daemon.install()
        if not result.get("ok"):
            return False, str(result.get("error") or "launchd refused the job")
        pid = daemon.wait_until_running()
        if pid:
            return True, (
                f"The FreeMicro daemon is running (pid {pid}). It starts at "
                "login and restarts if it dies."
            )
        return False, (
            "Installed, but it did not come up. Run `freemicro daemon logs` "
            "to see what it printed."
        )
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def web_ui_available() -> bool:
    """Can we start the bundled config UI in-process?"""
    try:
        import importlib.util

        if importlib.util.find_spec("freemicro.webui.server") is None:
            return False
    except (ImportError, ValueError):
        return False
    from freemicro.webui import server as _server

    return (_server.STATIC_DIR / "index.html").exists()


def probe_device_status(timeout: float = DEVICE_TIMEOUT) -> Dict[str, Any]:
    """Ask the pad for battery and firmware - **only when nobody else has it**.

    Returns ``{}`` without touching the device if the lock is held, if the pad
    is absent, or if anything at all goes wrong. A ``device.status`` round trip
    is the only honest health check on this hardware (a wrongly framed write
    returns success and is silently discarded), but it costs an open, so it is
    the one thing here that has to ask permission of the rest of the system.
    """
    if pad_owner():
        return {}
    try:
        from freemicro.device import close_shared, shared_device

        device = shared_device()
        if device is None:
            return {}
        try:
            reply = device.self_test(timeout=timeout)
        finally:
            close_shared()
    except Exception:  # noqa: BLE001 - the pad dropping mid-probe is routine
        try:
            from freemicro.device import close_shared as _close

            _close()
        except Exception:  # noqa: BLE001
            pass
        return {}
    if not isinstance(reply, dict):
        return {}
    result = {
        "version": reply.get("version"),
        "battery": reply.get("battery"),
        "is_charging": bool(reply.get("is_charging")),
        "updated_at": time.time(),
        "source": "menubar",
    }
    write_status(result)
    return result


# ---------------------------------------------------------------------------
# Composition
# ---------------------------------------------------------------------------

def _battery_fields(
    cached: Dict[str, Any],
) -> Tuple[Optional[int], bool, str, Optional[float]]:
    battery = cached.get("battery")
    try:
        battery = None if battery is None else int(battery)
    except (TypeError, ValueError):
        battery = None
    firmware = str(cached.get("version") or "")
    updated = cached.get("updated_at")
    age = None
    if isinstance(updated, (int, float)):
        age = max(0.0, time.time() - float(updated))
    return battery, bool(cached.get("is_charging")), firmware, age


def snapshot(
    cached: Optional[Dict[str, Any]] = None,
    chatgpt: Optional[bool] = None,
    bridge: Optional[Tuple[bool, Tuple[StaleNote, ...]]] = None,
) -> Snapshot:
    """One complete reading of the world. Safe to call from any thread.

    ``cached`` lets a caller supply the ``device.status`` payload it already has
    (the poller does, on its own slower clock); by default we read whatever is
    in the cache file, which is what the daemon writes. ``chatgpt`` and
    ``bridge`` do the same for the two probes that cost a subprocess.
    """
    state, sessions = resolved_state()
    supported, transport, connected, reason = presence()
    enabled, config_path = lighting_enabled()
    monitoring, access = permissions_state()
    status = read_status() if cached is None else cached
    listening, stale = bridge_state() if bridge is None else bridge
    battery, charging, firmware, age = _battery_fields(status)
    # A reading from a pad that is not here now is history, not status.
    if not connected:
        battery, charging, firmware = None, False, ""
    return Snapshot(
        state=state,
        sessions=sessions,
        supported=supported,
        unsupported_reason=reason,
        connected=connected,
        transport=transport,
        battery=battery,
        charging=charging,
        firmware=firmware,
        reading_age=age,
        lighting_enabled=enabled,
        owner=pad_owner(),
        input_monitoring=monitoring,
        accessibility=access,
        chatgpt_running=chatgpt_running() if chatgpt is None else chatgpt,
        web_ui=web_ui_available(),
        config_path=config_path,
        pad_listening=listening,
        stale=stale,
    )


class Poller:
    """Keeps a current :class:`Snapshot` on a background thread.

    The menu bar must stay responsive while a ``pgrep`` runs or a pad that has
    just wandered out of Bluetooth range times out, so nothing here happens on
    the main thread. Each probe has its own cadence; the cheap ones run every
    couple of seconds and the expensive ones far less often.
    """

    def __init__(
        self,
        interval: float = POLL_SECONDS,
        process_interval: float = PROCESS_POLL_SECONDS,
        device_interval: float = DEVICE_POLL_SECONDS,
    ) -> None:
        self.interval = interval
        self.process_interval = process_interval
        self.device_interval = device_interval
        self._snapshot = Snapshot()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._chatgpt = False
        self._chatgpt_at = 0.0
        self._device_at = 0.0
        self._cached: Dict[str, Any] = {}
        self._bridge: Tuple[bool, Tuple[StaleNote, ...]] = (True, ())

    @property
    def current(self) -> Snapshot:
        with self._lock:
            return self._snapshot

    def start(self) -> None:
        """Publish a cheap first reading, then keep it current on a thread.

        The first reading deliberately skips the ``device.status`` round trip.
        That call can take up to two seconds against a pad that has just
        wandered out of Bluetooth range, and making the menu bar item appear two
        seconds late - every login - to show a battery percentage that will
        arrive on its own moments later is a bad trade.
        """
        if self._thread is not None:
            return
        try:
            self._publish(
                snapshot(cached=read_status(), chatgpt=False, bridge=(True, ()))
            )
        except Exception:  # noqa: BLE001 - an empty snapshot is a fine start
            pass
        self._thread = threading.Thread(
            target=self._loop, name="freemicro-menubar-poll", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 3.0) -> None:
        """Ask the poll thread to finish, and wait for it.

        Waiting matters: the thread may be part-way through a device probe, and
        letting it run on after we think we have stopped means a probe landing
        after the menu bar has released everything.
        """
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)

    def refresh(self) -> Snapshot:
        """Take one reading now and publish it."""
        now = time.time()
        if now - self._chatgpt_at >= self.process_interval:
            # Both process probes share one clock: they are the two things here
            # that fork, and a menu bar that shells out every two seconds is a
            # menu bar people uninstall.
            self._chatgpt_at = now
            self._chatgpt = chatgpt_running()
            self._bridge = bridge_state()
        if now - self._device_at >= self.device_interval:
            self._device_at = now
            fresh = probe_device_status()
            self._cached = fresh or read_status()
        elif not self._cached:
            self._cached = read_status()
        return self._publish(
            snapshot(
                cached=self._cached, chatgpt=self._chatgpt, bridge=self._bridge
            )
        )

    def _publish(self, snap: Snapshot) -> Snapshot:
        with self._lock:
            self._snapshot = snap
        return snap

    def _loop(self) -> None:
        while True:
            try:
                self.refresh()
            except Exception:  # noqa: BLE001 - the poller must never die
                pass
            if self._stop.wait(self.interval):
                return


# ---------------------------------------------------------------------------
# The one thing the menu bar writes
# ---------------------------------------------------------------------------

def set_lighting_enabled(enabled: bool, path: Optional[Path] = None) -> Path:
    """Persist ``lighting.enabled`` - the same opt-in ``freemicro lights`` sets.

    The menu bar deliberately edits the *config*, not the hardware. Turning the
    switch off while the daemon holds the pad has to stop the daemon lighting
    it, and the only thing both processes agree on is the file.
    """
    from freemicro import padconfig

    target = Path(path) if path else padconfig.user_path()
    if not target.exists():
        padconfig.write_starter(target)
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    lighting = data.setdefault("lighting", {})
    if not isinstance(lighting, dict):
        lighting = data["lighting"] = {}
    lighting["enabled"] = bool(enabled)
    target.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return target


def restart_pad_owner() -> str:
    """Make a running daemon re-read the config. Returns what we did, for the UI.

    ``_run_pipeline`` loads the pad config once at startup, so a config change
    is invisible to it until it restarts. Restarting a LaunchAgent is blunt, but
    it is honest and it takes under a second - and the alternative (a toggle
    that silently does nothing until next login) is worse. ``docs/MENUBAR.md``
    asks for a config watcher so this can go away.
    """
    try:
        from freemicro import daemon

        if not daemon.is_running():
            return ""
        subprocess.run(
            ["launchctl", "kickstart", "-k", daemon.service_target()],
            capture_output=True,
            text=True,
            timeout=20,
        )
        return "restarted the background daemon so it picks up the change"
    except (OSError, subprocess.SubprocessError, ImportError):
        return ""


__all__ = [
    "DEVICE_POLL_SECONDS",
    "POLL_SECONDS",
    "PROCESS_POLL_SECONDS",
    "STATUS_FILENAME",
    "Poller",
    "bridge_state",
    "chatgpt_running",
    "install_daemon",
    "lighting_enabled",
    "pad_owner",
    "permissions_state",
    "presence",
    "probe_device_status",
    "read_status",
    "restart_pad_owner",
    "resolved_state",
    "set_lighting_enabled",
    "snapshot",
    "status_path",
    "web_ui_available",
    "write_status",
]
