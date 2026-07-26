"""Keep FreeMicro running without a terminal window open.

Why this is not optional
------------------------
The pad emits no ordinary scancodes. When nothing is listening on its vendor
HID channel the hardware is simply **dead** - not degraded, dead. Requiring a
user to keep a terminal open forever so their keyboard types is not a product.

So: a launchd **LaunchAgent** that starts at login, restarts if it dies, logs
to a size-capped file, and reconnects on its own when the pad drops (the device
layer already treats disconnects as the normal case).

The one rule everything here obeys
----------------------------------
**Only one process can usefully hold the device.** Two owners means keys
arriving in one process and LEDs painted from another, with no way for a user
to see why half their pad works. Rather than race, every command that wants the
pad takes an exclusive :class:`PadLock` first and *says so plainly* if someone
else has it. The lock is an ``flock`` on a file under the config dir, so it is
released automatically if a process is killed - a stale pid file can never
wedge the pad.
"""

from __future__ import annotations

import json
import os
import plistlib
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from freemicro.config import config_home

#: launchd label and the plist that carries it. This is the login-time agent:
#: KeepAlive, RunAtLoad, one process that lives for the whole session.
LABEL = "com.freemicro.daemon"

#: The *other* launcher: an agent with no RunAtLoad that launchd starts only
#: when the pad's HID device appears (see :func:`build_onconnect_plist`). A
#: separate label so the two can be installed, seen and removed independently -
#: though they are never installed at the same time (see :func:`conflicting_label`).
ONCONNECT_LABEL = "com.freemicro.onconnect"

#: The Codex Micro's USB-IF identifiers, as integers (plists carry integers, not
#: ``0x`` strings). VID 0x303A is Espressif; PID 0x8360 is this pad. Over BLE the
#: device enumerates as an ``IOHIDDevice`` exposing ``VendorID``/``ProductID``;
#: over USB it also appears under ``IOUSBHostDevice`` as ``idVendor``/``idProduct``.
CODEX_MICRO_VID = 0x303A  # 12346
CODEX_MICRO_PID = 0x8360  # 33632

#: Cap for the log file. launchd never rotates; we do it ourselves, keeping the
#: newest half. A log that grows without bound on a laptop is a bug.
LOG_CAP_BYTES = 1 * 1024 * 1024

#: launchd will not restart a job faster than this, so a crash-loop costs one
#: line every 10s instead of pinning a core.
THROTTLE_SECONDS = 10


def agents_dir() -> Path:
    return Path.home() / "Library" / "LaunchAgents"


def plist_path(label: str = LABEL) -> Path:
    return agents_dir() / f"{label}.plist"


def log_path() -> Path:
    return config_home() / "logs" / "daemon.log"


def lock_path() -> Path:
    return config_home() / "pad.lock"


def service_target(label: str = LABEL) -> str:
    return f"gui/{os.getuid()}/{label}"


# ---------------------------------------------------------------------------
# The pad lock
# ---------------------------------------------------------------------------

class PadLock:
    """Exclusive ownership of the physical pad, one process at a time.

    ``flock`` rather than a bare pid file on purpose: the kernel drops the lock
    when the holder exits *however* it exits, so there is no such thing as a
    stale lock that needs clearing by hand. The pid written alongside is only
    so we can tell the user *who* has it.
    """

    def __init__(self, path: Optional[Path] = None, role: str = "run") -> None:
        self.path = Path(path) if path else lock_path()
        self.role = role
        self._fd: Optional[int] = None

    def acquire(self) -> bool:
        """Take the lock without blocking. ``False`` means someone else has it."""
        try:
            import fcntl
        except ImportError:  # pragma: no cover - non-POSIX
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self.path), os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        os.ftruncate(fd, 0)
        payload = json.dumps(
            {"pid": os.getpid(), "role": self.role, "started": time.time()}
        )
        os.write(fd, payload.encode("utf-8"))
        os.fsync(fd)
        self._fd = fd
        return True

    def release(self) -> None:
        if self._fd is None:
            return
        fd, self._fd = self._fd, None
        try:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_UN)
        except (ImportError, OSError):
            pass
        try:
            os.ftruncate(fd, 0)
        except OSError:
            pass
        os.close(fd)

    def __enter__(self) -> "PadLock":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.release()


def lock_holder() -> Optional[Dict[str, Any]]:
    """Who currently owns the pad, or ``None`` if nobody does.

    Determined by *trying* the lock, not by reading a pid: the file survives a
    crash, the lock does not.
    """
    path = lock_path()
    if not path.exists():
        return None
    try:
        import fcntl
    except ImportError:  # pragma: no cover - non-POSIX
        return None
    try:
        fd = os.open(str(path), os.O_RDWR)
    except OSError:
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        # Held. The contents tell us by whom.
        try:
            data = json.loads(path.read_text(encoding="utf-8") or "{}")
        except (OSError, ValueError):
            data = {}
        os.close(fd)
        return data if isinstance(data, dict) else {}
    # We got it, so it was free. Let go immediately.
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
    return None


def describe_holder(holder: Dict[str, Any]) -> str:
    role = str(holder.get("role") or "another FreeMicro process")
    pid = holder.get("pid")
    label = {
        "daemon": "the background daemon",
        "run": "`freemicro run`",
        "keys": "`freemicro keys`",
    }.get(role, role)
    age = ""
    started = holder.get("started")
    if isinstance(started, (int, float)):
        age = f", up {int(max(0, time.time() - started))}s"
    return f"{label}" + (f" (pid {pid}{age})" if pid else "")


# ---------------------------------------------------------------------------
# The log
# ---------------------------------------------------------------------------

def rotate_log(path: Optional[Path] = None, cap: int = LOG_CAP_BYTES) -> bool:
    """Keep the newest half of an oversized log. Returns whether we trimmed.

    launchd opens our log ``O_APPEND`` and keeps the descriptor for the life of
    the job, so renaming the file would send every later line into a deleted
    inode. Truncating in place and rewriting the tail is the one form of
    rotation that survives that.
    """
    path = Path(path) if path else log_path()
    try:
        size = path.stat().st_size
    except OSError:
        return False
    if size <= cap:
        return False
    keep = cap // 2
    try:
        with open(path, "rb") as fh:
            fh.seek(size - keep)
            fh.readline()  # don't start mid-line
            tail = fh.read()
        with open(path, "r+b") as fh:
            fh.seek(0)
            fh.write(b"--- log trimmed to the most recent %dKB ---\n" % (keep // 1024))
            fh.write(tail)
            fh.truncate()
    except OSError:
        return False
    return True


def read_log(lines: int = 50, path: Optional[Path] = None) -> str:
    path = Path(path) if path else log_path()
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return "\n".join(text.splitlines()[-lines:])


# ---------------------------------------------------------------------------
# The plist
# ---------------------------------------------------------------------------

def daemon_argv() -> List[str]:
    """The command launchd should run, as an absolute argv.

    Same resolution rules as the Claude Code hook: launchd starts jobs with an
    almost-empty environment, so ``PATH`` cannot be relied on for anything.
    """
    from freemicro.hooks_install import console_script

    script = console_script()
    if script is not None:
        return [str(script), "daemon", "run"]
    return [sys.executable, "-m", "freemicro", "daemon", "run"]


#: Folders macOS gates behind a per-app TCC grant. A LaunchAgent has no app to
#: attach that grant to, so it simply cannot read them.
_PROTECTED_DIRS = ("Desktop", "Documents", "Downloads")


def protected_location(path: Optional[Path] = None) -> Optional[str]:
    """Name of the TCC-protected folder ``path`` lives in, if any.

    A launchd agent runs with no TCC grants for the user's Desktop, Documents
    or Downloads. Point one at a virtualenv inside those and it dies before
    Python finishes starting - ``PermissionError: … pyvenv.cfg`` - with no hint
    that a *folder permission* is the cause. Worth catching at install time,
    because the log message is genuinely unguessable.
    """
    if path is None:
        argv = daemon_argv()
        path = Path(argv[0])
    try:
        resolved = Path(path).resolve()
        home = Path.home().resolve()
    except OSError:
        return None
    try:
        relative = resolved.relative_to(home)
    except ValueError:
        return None
    first = relative.parts[0] if relative.parts else ""
    return first if first in _PROTECTED_DIRS else None


def _plist_env() -> Dict[str, str]:
    """The environment both agents run under. launchd starts jobs near-empty."""
    env = {
        # Enough PATH for the shell-action bindings to find ordinary tools.
        "PATH": "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin",
    }
    # Honour a relocated config dir, or the daemon would write somewhere else
    # than the CLI reads.
    if os.environ.get("FREEMICRO_HOME"):
        env["FREEMICRO_HOME"] = os.environ["FREEMICRO_HOME"]
    return env


def build_plist(argv: Optional[List[str]] = None) -> Dict[str, Any]:
    argv = argv or daemon_argv()
    log = log_path()
    return {
        "Label": LABEL,
        "ProgramArguments": argv,
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": THROTTLE_SECONDS,
        "ProcessType": "Interactive",
        "EnvironmentVariables": _plist_env(),
        "StandardOutPath": str(log),
        "StandardErrorPath": str(log),
        "WorkingDirectory": str(Path.home()),
    }


def build_onconnect_plist(argv: Optional[List[str]] = None) -> Dict[str, Any]:
    """The launch-on-connect agent: start ``daemon run`` when the pad appears.

    The heart of it is ``LaunchEvents -> com.apple.iokit.matching``: launchd
    watches the IOKit registry and starts this job when a device matching one of
    the dictionaries below shows up. That is the native, poll-free way to do
    "launch when this device connects" - nothing runs while the pad is asleep,
    and the moment a button press wakes the BLE link and the HID device
    enumerates, launchd starts us.

    Two matchers, so one plist covers both transports the owner might use:

    * ``IOHIDDevice`` with ``VendorID``/``ProductID`` - the Bluetooth LE form the
      owner is actually on. A BLE keypress establishes the link and the HID
      device appears with these integer properties.
    * ``IOUSBHostDevice`` with ``idVendor``/``idProduct`` - the same pad on a USB
      cable, so plugging in also launches us.

    ``RunAtLoad`` is deliberately **False**: an on-connect agent that also ran at
    login would start every session whether or not the pad is anywhere near, which
    is exactly the always-on process this mode exists to avoid. ``KeepAlive`` is
    **False** too: the device-appeared event is the trigger, and ``daemon run``
    already reconnects on its own for the life of the process and exits cleanly
    (status 0) if another owner holds the pad - so there is nothing for launchd to
    respawn, and a respawn loop is precisely the failure ``KeepAlive`` would create
    if the pad were briefly unavailable. Start on the event, then get out of the
    way.

    Honest caveat, because it matters: ``com.apple.iokit.matching`` is well
    trodden for USB and less so for BLE HID. The plist here is correct and fully
    testable as data; whether launchd fires it on a *Bluetooth* connect is a
    property of the owner's macOS and hardware, and the install command prints the
    exact steps to confirm it on the real pad.
    """
    argv = argv or daemon_argv()
    log = log_path()
    return {
        "Label": ONCONNECT_LABEL,
        "ProgramArguments": argv,
        "LaunchEvents": {
            "com.apple.iokit.matching": {
                # Bluetooth LE (and USB HID interface): the form the owner uses.
                "com.freemicro.codexmicro-hid": {
                    "IOProviderClass": "IOHIDDevice",
                    "VendorID": CODEX_MICRO_VID,
                    "ProductID": CODEX_MICRO_PID,
                },
                # The same pad over a USB cable, matched at the USB device layer.
                "com.freemicro.codexmicro-usb": {
                    "IOProviderClass": "IOUSBHostDevice",
                    "idVendor": CODEX_MICRO_VID,
                    "idProduct": CODEX_MICRO_PID,
                },
            }
        },
        "RunAtLoad": False,
        "KeepAlive": False,
        "ProcessType": "Interactive",
        "EnvironmentVariables": _plist_env(),
        "StandardOutPath": str(log),
        "StandardErrorPath": str(log),
        "WorkingDirectory": str(Path.home()),
    }


def render_plist(argv: Optional[List[str]] = None) -> bytes:
    return plistlib.dumps(build_plist(argv))


def render_onconnect_plist(argv: Optional[List[str]] = None) -> bytes:
    return plistlib.dumps(build_onconnect_plist(argv))


# ---------------------------------------------------------------------------
# launchctl
# ---------------------------------------------------------------------------

def _launchctl(*args: str, timeout: float = 20.0) -> Tuple[int, str]:
    try:
        proc = subprocess.run(
            ["launchctl", *args], capture_output=True, text=True, timeout=timeout
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return -1, str(exc)
    return proc.returncode, ((proc.stdout or "") + (proc.stderr or "")).strip()


def is_installed(label: str = LABEL) -> bool:
    return plist_path(label).exists()


def conflicting_label(on_connect: bool) -> Optional[str]:
    """The label of the *other* launcher, if it is installed - else ``None``.

    Only one thing may drive the pad, and two launchers is two things trying to.
    They are lock-guarded so they cannot both actually hold the device at once,
    but a machine with both installed is a machine where "why did my pad start
    twice" has no clean answer. So installing either refuses while the other is
    present, and this is the check that says which one is in the way.
    """
    other = LABEL if on_connect else ONCONNECT_LABEL
    return other if is_installed(other) else None


def _launcher_name(label: str) -> str:
    return "on-connect agent" if label == ONCONNECT_LABEL else "login daemon"


def _bootout(label: str = LABEL) -> Tuple[int, str]:
    """Stop and unregister the job, on new and old launchctl alike."""
    code, out = _launchctl("bootout", service_target(label))
    if code == 0 or "No such process" in out or "not find" in out.lower():
        return 0, out
    # Pre-Yosemite-style fallback; harmless if the modern call already worked.
    legacy_code, legacy_out = _launchctl("unload", "-w", str(plist_path(label)))
    if legacy_code == 0:
        return 0, legacy_out
    return code, out


def _install(
    label: str,
    payload: bytes,
    argv: Optional[List[str]],
    force: bool,
    kickstart: bool,
    on_connect: bool,
) -> Dict[str, Any]:
    """Write ``label``'s plist and register it with launchd. Shared by both modes.

    Refuses up front on the two failures that would leave a launcher that can
    never work: a binary in a TCC-protected folder (launchd cannot read it), and
    the *other* launcher already installed (two owners for one pad). ``force``
    overrides both, for anyone who has arranged things some other way.
    """
    folder = protected_location(Path(argv[0]) if argv else None)
    if folder and not force:
        return {
            "ok": False,
            "path": str(plist_path(label)),
            "replaced": False,
            "warning": folder,
            "conflict": None,
            "error": (
                f"the freemicro binary is under ~/{folder}, which macOS does "
                "not let\n  background agents read - launchd would respawn it "
                "forever without it\n  ever starting. Install FreeMicro "
                "somewhere unprotected first:\n"
                "    pipx install freemicro\n"
                "  or move the clone out of "
                f"~/{folder} and reinstall it there."
            ),
        }

    other = conflicting_label(on_connect)
    if other and not force:
        this_name = _launcher_name(label)
        other_name = _launcher_name(other)
        return {
            "ok": False,
            "path": str(plist_path(label)),
            "replaced": False,
            "warning": None,
            "conflict": other,
            "error": (
                f"the {other_name} is already installed, and it and the "
                f"{this_name}\n  would both try to drive the pad. Pick one - "
                "remove the other first:\n"
                "    freemicro daemon uninstall\n"
                f"  then install the {this_name} again."
            ),
        }

    path = plist_path(label)
    path.parent.mkdir(parents=True, exist_ok=True)
    log_path().parent.mkdir(parents=True, exist_ok=True)
    replaced = path.exists()
    # Always bootout first: launchd caches the plist at bootstrap time, so
    # rewriting the file alone leaves the old command running.
    if replaced:
        _bootout(label)
    path.write_bytes(payload)

    code, out = _launchctl("bootstrap", f"gui/{os.getuid()}", str(path))
    if code != 0 and "already bootstrapped" not in out.lower():
        legacy, legacy_out = _launchctl("load", "-w", str(path))
        if legacy != 0:
            return {
                "ok": False, "path": str(path), "replaced": replaced,
                "warning": None, "conflict": None, "error": out or legacy_out,
            }
    _launchctl("enable", service_target(label))
    # Only the login daemon is force-started. The on-connect agent must wait for
    # its device event; kickstarting it would run it now, pad or no pad, which is
    # the always-on behaviour it exists to avoid.
    if kickstart:
        _launchctl("kickstart", "-k", service_target(label))
    return {
        "ok": True,
        "path": str(path),
        "replaced": replaced,
        "error": "",
        "warning": protected_location(),
        "conflict": None,
    }


def install(
    argv: Optional[List[str]] = None, force: bool = False
) -> Dict[str, Any]:
    """Write the login-time plist and (re)start the job. Idempotent.

    ``KeepAlive`` plus a doomed executable is a job that respawns forever and
    never runs a line of Python, so this refuses a binary launchd cannot read.
    """
    argv = argv or daemon_argv()
    return _install(
        LABEL, render_plist(argv), argv, force, kickstart=True, on_connect=False
    )


def install_on_connect(
    argv: Optional[List[str]] = None, force: bool = False
) -> Dict[str, Any]:
    """Write the launch-on-connect plist and register it. Idempotent.

    Does *not* start anything: the whole point is that launchd starts it when the
    pad's HID device appears. Same TCC-folder refusal as :func:`install`, and it
    refuses if the login daemon is already installed (see :func:`_install`).
    """
    argv = argv or daemon_argv()
    return _install(
        ONCONNECT_LABEL, render_onconnect_plist(argv), argv, force,
        kickstart=False, on_connect=True,
    )


def wait_until_running(timeout: float = 10.0, settle: float = 2.0) -> Optional[int]:
    """Wait for the daemon to be *stably* up. ``None`` means it never got there.

    Two traps this avoids, both of which produce a confident "installed!"
    followed by a pad that does nothing:

    * launchd reports a pid for a process that is already dying, so a single
      pid sighting proves nothing during a crash-loop. We require the same pid
      twice, ``settle`` seconds apart.
    * A daemon can be alive and still not have the device. The pad lock is only
      written *after* start-up succeeds, so a lock held with ``role=daemon`` is
      the strongest evidence available and short-circuits the wait.
    """
    deadline = time.time() + timeout
    seen_pid: Optional[int] = None
    seen_at = 0.0
    while time.time() < deadline:
        holder = lock_holder() or {}
        if holder.get("role") == "daemon" and holder.get("pid"):
            return int(holder["pid"])
        pid = launchctl_state().get("pid")
        if pid:
            if pid == seen_pid and time.time() - seen_at >= settle:
                return int(pid)
            if pid != seen_pid:
                seen_pid, seen_at = int(pid), time.time()
        time.sleep(0.4)
    return None


def diagnose() -> str:
    """Best guess at *why* the daemon isn't running, from its own log."""
    log = read_log(lines=40)
    folder = protected_location()
    if "pyvenv.cfg" in log or "Failed to import the site module" in log:
        where = folder or "a protected folder"
        return (
            f"The binary lives in ~/{where}, which macOS will not let a\n"
            "background agent read - launchd has no app identity to hang a\n"
            "Files-and-Folders grant on, so Python dies before it starts.\n"
            "Fix it by installing FreeMicro somewhere unprotected:\n"
            "  pipx install freemicro          (recommended - ~/.local)\n"
            "  or move the clone out of ~/Desktop and reinstall, then\n"
            "  freemicro daemon install"
        )
    if "Permission denied" in log or "not permitted" in log:
        return (
            "Something the daemon needs is blocked by macOS privacy settings.\n"
            "freemicro daemon logs      # the exact path it was refused"
        )
    if folder:
        return (
            f"Heads up: the binary is under ~/{folder}, which background\n"
            "agents cannot read on macOS. If it never starts, that is why."
        )
    return ""


def uninstall(label: str = LABEL) -> Dict[str, Any]:
    """Stop the job, unregister it, and delete the plist. Verifies afterwards."""
    path = plist_path(label)
    existed = path.exists()
    code, out = _bootout(label)
    removed = False
    try:
        path.unlink()
        removed = True
    except FileNotFoundError:
        pass
    except OSError as exc:
        return {
            "ok": False, "existed": existed, "removed": False,
            "error": f"could not delete {path}: {exc}",
        }
    # An uninstall that leaves the job loaded is worse than none at all, so
    # confirm rather than assume.
    still = launchctl_state(label)
    if still.get("loaded"):
        return {
            "ok": False, "existed": existed, "removed": removed,
            "error": f"launchd still has {label} loaded: {out}",
        }
    return {"ok": True, "existed": existed, "removed": removed, "error": ""}


def launchctl_state(label: str = LABEL) -> Dict[str, Any]:
    """What launchd thinks of our job right now."""
    code, out = _launchctl("print", service_target(label))
    if code != 0:
        return {"loaded": False, "pid": None, "last_exit": None, "raw": out}
    pid: Optional[int] = None
    last_exit: Optional[int] = None
    for line in out.splitlines():
        stripped = line.strip()
        if stripped.startswith("pid = "):
            try:
                pid = int(stripped.split("=", 1)[1].strip())
            except ValueError:
                pass
        elif stripped.startswith("last exit code = "):
            value = stripped.split("=", 1)[1].strip()
            try:
                last_exit = int(value)
            except ValueError:
                last_exit = None
    return {"loaded": True, "pid": pid, "last_exit": last_exit, "raw": out}


def is_running() -> bool:
    holder = lock_holder()
    if holder and holder.get("role") == "daemon":
        return True
    if not is_installed():
        # No plist means launchd has never heard of us; asking it costs a
        # subprocess to be told so.
        return False
    return bool(launchctl_state().get("pid"))


def status() -> Dict[str, Any]:
    """Everything ``freemicro daemon status`` needs, in one dict."""
    state = launchctl_state()
    holder = lock_holder() or {}
    log = log_path()
    return {
        "label": LABEL,
        "plist": str(plist_path()),
        "installed": is_installed(),
        "loaded": bool(state.get("loaded")),
        "pid": state.get("pid"),
        "last_exit": state.get("last_exit"),
        "log": str(log),
        "log_size": log.stat().st_size if log.exists() else 0,
        "lock_role": holder.get("role"),
        "lock_pid": holder.get("pid"),
        "command": " ".join(shlex.quote(a) for a in daemon_argv()),
        "protected_location": protected_location(),
        "onconnect_installed": is_installed(ONCONNECT_LABEL),
    }


def onconnect_status() -> Dict[str, Any]:
    """Everything ``freemicro daemon status --on-connect`` needs, in one dict.

    Deliberately thinner than :func:`status`: an on-connect agent is *meant* to
    be loaded-but-not-running whenever the pad is asleep, so "no pid" is the
    normal resting state, not a fault to diagnose.
    """
    state = launchctl_state(ONCONNECT_LABEL)
    holder = lock_holder() or {}
    return {
        "label": ONCONNECT_LABEL,
        "plist": str(plist_path(ONCONNECT_LABEL)),
        "installed": is_installed(ONCONNECT_LABEL),
        "loaded": bool(state.get("loaded")),
        "pid": state.get("pid"),
        "vendor_id": CODEX_MICRO_VID,
        "product_id": CODEX_MICRO_PID,
        "lock_role": holder.get("role"),
        "lock_pid": holder.get("pid"),
        "command": " ".join(shlex.quote(a) for a in daemon_argv()),
        "protected_location": protected_location(),
        "daemon_installed": is_installed(LABEL),
    }


__all__ = [
    "CODEX_MICRO_PID",
    "CODEX_MICRO_VID",
    "LABEL",
    "LOG_CAP_BYTES",
    "ONCONNECT_LABEL",
    "PadLock",
    "build_onconnect_plist",
    "build_plist",
    "conflicting_label",
    "daemon_argv",
    "describe_holder",
    "install",
    "install_on_connect",
    "is_installed",
    "is_running",
    "launchctl_state",
    "lock_holder",
    "lock_path",
    "log_path",
    "onconnect_status",
    "plist_path",
    "read_log",
    "render_onconnect_plist",
    "render_plist",
    "rotate_log",
    "status",
    "uninstall",
]
