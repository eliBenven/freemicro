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

#: launchd label and the plist that carries it.
LABEL = "com.freemicro.daemon"

#: Cap for the log file. launchd never rotates; we do it ourselves, keeping the
#: newest half. A log that grows without bound on a laptop is a bug.
LOG_CAP_BYTES = 1 * 1024 * 1024

#: launchd will not restart a job faster than this, so a crash-loop costs one
#: line every 10s instead of pinning a core.
THROTTLE_SECONDS = 10


def agents_dir() -> Path:
    return Path.home() / "Library" / "LaunchAgents"


def plist_path() -> Path:
    return agents_dir() / f"{LABEL}.plist"


def log_path() -> Path:
    return config_home() / "logs" / "daemon.log"


def lock_path() -> Path:
    return config_home() / "pad.lock"


def service_target() -> str:
    return f"gui/{os.getuid()}/{LABEL}"


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


def build_plist(argv: Optional[List[str]] = None) -> Dict[str, Any]:
    argv = argv or daemon_argv()
    log = log_path()
    env = {
        # Enough PATH for the shell-action bindings to find ordinary tools.
        "PATH": "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin",
    }
    # Honour a relocated config dir, or the daemon would write somewhere else
    # than the CLI reads.
    if os.environ.get("FREEMICRO_HOME"):
        env["FREEMICRO_HOME"] = os.environ["FREEMICRO_HOME"]
    return {
        "Label": LABEL,
        "ProgramArguments": argv,
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": THROTTLE_SECONDS,
        "ProcessType": "Interactive",
        "EnvironmentVariables": env,
        "StandardOutPath": str(log),
        "StandardErrorPath": str(log),
        "WorkingDirectory": str(Path.home()),
    }


def render_plist(argv: Optional[List[str]] = None) -> bytes:
    return plistlib.dumps(build_plist(argv))


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


def is_installed() -> bool:
    return plist_path().exists()


def _bootout() -> Tuple[int, str]:
    """Stop and unregister the job, on new and old launchctl alike."""
    code, out = _launchctl("bootout", service_target())
    if code == 0 or "No such process" in out or "not find" in out.lower():
        return 0, out
    # Pre-Yosemite-style fallback; harmless if the modern call already worked.
    legacy_code, legacy_out = _launchctl("unload", "-w", str(plist_path()))
    if legacy_code == 0:
        return 0, legacy_out
    return code, out


def install(
    argv: Optional[List[str]] = None, force: bool = False
) -> Dict[str, Any]:
    """Write the plist and (re)start the job. Idempotent.

    Refuses up front if the binary sits somewhere launchd cannot read it.
    ``KeepAlive`` plus a doomed executable is a job that respawns forever and
    never runs a line of Python - installing that and calling it success would
    leave a machine quietly burning a process every ten seconds. ``force``
    overrides, for anyone who has arranged the grant some other way.
    """
    folder = protected_location(Path(argv[0]) if argv else None)
    if folder and not force:
        return {
            "ok": False,
            "path": str(plist_path()),
            "replaced": False,
            "warning": folder,
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

    path = plist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    log_path().parent.mkdir(parents=True, exist_ok=True)
    payload = render_plist(argv)
    replaced = path.exists()
    # Always bootout first: launchd caches the plist at bootstrap time, so
    # rewriting the file alone leaves the old command running.
    if replaced:
        _bootout()
    path.write_bytes(payload)

    code, out = _launchctl("bootstrap", f"gui/{os.getuid()}", str(path))
    if code != 0 and "already bootstrapped" not in out.lower():
        legacy, legacy_out = _launchctl("load", "-w", str(path))
        if legacy != 0:
            return {
                "ok": False, "path": str(path), "replaced": replaced,
                "error": out or legacy_out,
            }
    _launchctl("enable", service_target())
    _launchctl("kickstart", "-k", service_target())
    return {
        "ok": True,
        "path": str(path),
        "replaced": replaced,
        "error": "",
        "warning": protected_location(),
    }


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


def uninstall() -> Dict[str, Any]:
    """Stop the job, unregister it, and delete the plist. Verifies afterwards."""
    path = plist_path()
    existed = path.exists()
    code, out = _bootout()
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
    still = launchctl_state()
    if still.get("loaded"):
        return {
            "ok": False, "existed": existed, "removed": removed,
            "error": f"launchd still has {LABEL} loaded: {out}",
        }
    return {"ok": True, "existed": existed, "removed": removed, "error": ""}


def launchctl_state() -> Dict[str, Any]:
    """What launchd thinks of our job right now."""
    code, out = _launchctl("print", service_target())
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
    }


__all__ = [
    "LABEL",
    "LOG_CAP_BYTES",
    "PadLock",
    "build_plist",
    "daemon_argv",
    "describe_holder",
    "install",
    "is_installed",
    "is_running",
    "launchctl_state",
    "lock_holder",
    "lock_path",
    "log_path",
    "plist_path",
    "read_log",
    "render_plist",
    "rotate_log",
    "status",
    "uninstall",
]
