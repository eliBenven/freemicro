"""Is what's running actually what's installed - and is anything running at all?

Every failure this module exists for looked like a bug and was not one. In a
single evening of real use the owner hit five of them:

1. ``freemicro run`` was not running, so the pad was inert. ``status`` happily
   listed live sessions and never mentioned that nobody was listening.
2. A ``menubar`` from before an update rejected the config with "unknown action
   'focus_session'" - an action that plainly existed, in the code on disk.
3. A web UI from before an update held ``pad.lock``, and the message told the
   user to go and delete a file.
4. A running bridge had loaded the config *before* an edit, so keys did the
   wrong thing with no indication why.
5. A running bridge predated a code fix, so Agent Keys still failed after the
   fix had landed.

None of them was a logic error. All of them were **invisible state**: correct
software, running from something other than what the user was looking at.

The answers here, in order of how much they are worth
----------------------------------------------------
* **Is anything driving the pad?** (:func:`pad_listener`) A pad nobody is
  listening to is dead hardware, and it is the most common broken state there
  is. Everything else in ``status`` is noise until this is answered.
* **Is a running process older than the code it loaded?**
  (:func:`process_started_before_code`) and older than the config it read?
  (:func:`config_changed_since`)
* **Fix it without asking.** (:class:`CodeWatcher`) A warning still leaves a
  process doing the wrong thing until a human notices. When the installed
  package changes underneath a long-lived bridge, the bridge re-execs itself -
  after verifying the new code imports, after the tree has stopped changing,
  after releasing the device, and never more than a few times in a row.

Everything returns **structured results, not printed strings**, so ``status``,
``doctor`` and the menu bar render one truth rather than three descriptions of
it. Every probe - the process list, the clock, the mtime source, even ``execv``
- is injectable, so the whole module is testable with no processes, no hardware
and no restarts.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

#: When this process started. Captured at import, which is as close to the
#: truth as we can get without asking ``ps`` about ourselves.
PROCESS_STARTED = time.time()

#: Slack allowed when comparing a process start time against a file mtime.
#: ``ps`` reports elapsed time to the second, so a start time is only ever
#: accurate to about that, and calling a process stale over rounding would be
#: worse than saying nothing.
GRACE_SECONDS = 2.0

# ---------------------------------------------------------------------------
# Roles
# ---------------------------------------------------------------------------

ROLE_RUN = "run"
ROLE_KEYS = "keys"
ROLE_DAEMON = "daemon"
ROLE_MENUBAR = "menubar"
ROLE_WEBUI = "web UI"

#: Roles that actually drive the pad. A menu bar or a web UI is a *reader*: it
#: can be stale, but its absence never makes the pad inert.
PAD_ROLES: Tuple[str, ...] = (ROLE_DAEMON, ROLE_RUN, ROLE_KEYS)

#: Roles that re-read the config file while they run, and so cannot be stale
#: about it. Reporting them anyway would leave a warning on screen forever
#: after an edit that the process had already picked up - which is exactly the
#: kind of untrue status line this module exists to delete.
RELOADING_ROLES: Tuple[str, ...] = (
    ROLE_RUN, ROLE_DAEMON, ROLE_MENUBAR, ROLE_WEBUI,
)

#: How each role is written for a human, and how it is restarted.
_ROLE_LABELS = {
    ROLE_RUN: "`freemicro run`",
    ROLE_KEYS: "`freemicro keys`",
    ROLE_DAEMON: "the background daemon",
    ROLE_MENUBAR: "the menu bar",
    ROLE_WEBUI: "the web config UI",
}

_ROLE_RESTART = {
    ROLE_RUN: "stop it (Ctrl-C) and run `freemicro run` again",
    ROLE_KEYS: "stop it (Ctrl-C) and run `freemicro keys` again",
    ROLE_DAEMON: "freemicro daemon install",
    ROLE_MENUBAR: "quit it from its menu and run `freemicro menubar` again",
    ROLE_WEBUI: "close the tab, stop it (Ctrl-C), run `freemicro config --web`",
}

# Order matters: "daemon run" contains "run", and the menu bar is spawned as
# both `freemicro menubar` and `python -m freemicro.menubar`.
#
# ``(?<![-\w])`` is load-bearing: without it ``freemicro keys --dry-run`` reads
# as a ``run``, and every message about it would then name the wrong process
# and offer the wrong restart command.
_VERB = r"(?<![-\w])"
_ROLE_PATTERNS: Tuple[Tuple[str, "re.Pattern[str]"], ...] = (
    (ROLE_DAEMON, re.compile(rf"freemicro\b.*{_VERB}daemon\b\s+run\b")),
    (ROLE_MENUBAR, re.compile(rf"freemicro[.\s]+{_VERB}menubar\b")),
    (ROLE_WEBUI, re.compile(rf"freemicro\b.*{_VERB}config\b.*--web\b")),
    (ROLE_RUN, re.compile(rf"freemicro\b[^|]*{_VERB}run\b")),
    (ROLE_KEYS, re.compile(rf"freemicro\b[^|]*{_VERB}keys\b")),
)


def role_label(role: str) -> str:
    """How a role is named in a sentence."""
    return _ROLE_LABELS.get(role, role or "a FreeMicro process")


def restart_hint(role: str) -> str:
    """The exact thing to do to make that process pick up new code."""
    return _ROLE_RESTART.get(role, "restart it")


def _role_of(command: str) -> str:
    for role, pattern in _ROLE_PATTERNS:
        if pattern.search(command):
            return role
    return ""


# ---------------------------------------------------------------------------
# The code on disk
# ---------------------------------------------------------------------------

def package_root() -> Path:
    """Directory of the *installed* ``freemicro`` package we are running from."""
    import freemicro

    return Path(freemicro.__file__).resolve().parent


def package_mtime(root: Optional[Path] = None) -> float:
    """Newest mtime anywhere in the installed package. ``0.0`` if unreadable.

    ``__pycache__`` is skipped deliberately: CPython writes a ``.pyc`` the first
    time a fresh process imports a module, so counting it would make every
    process look older than its own code within a second of starting - a
    restart loop built out of nothing.
    """
    base = Path(root) if root is not None else package_root()
    newest = 0.0
    try:
        for dirpath, dirnames, filenames in os.walk(str(base)):
            dirnames[:] = [d for d in dirnames if d != "__pycache__"]
            for name in filenames:
                if name.endswith((".pyc", ".pyo")):
                    continue
                try:
                    stamp = os.stat(os.path.join(dirpath, name)).st_mtime
                except OSError:
                    continue
                if stamp > newest:
                    newest = stamp
    except OSError:
        return 0.0
    return newest


# ---------------------------------------------------------------------------
# The processes
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ProcessInfo:
    """One running FreeMicro process, and when it started."""

    pid: int
    role: str
    started: float
    command: str = ""

    @property
    def label(self) -> str:
        return role_label(self.role)

    @property
    def drives_pad(self) -> bool:
        return self.role in PAD_ROLES

    def describe(self) -> str:
        return f"{self.label} (pid {self.pid})"

    def restart_hint(self) -> str:
        return restart_hint(self.role)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "pid": self.pid,
            "role": self.role,
            "started": self.started,
            "command": self.command,
        }


def _parse_etime(text: str) -> Optional[float]:
    """``[[dd-]hh:]mm:ss`` - BSD ``ps``'s elapsed time - as seconds.

    Not ``etimes``: that keyword is Linux-only, and macOS ``ps`` refuses the
    whole command when it sees it, which would silently blind every check here.
    """
    text = text.strip()
    if not text:
        return None
    days = 0
    if "-" in text:
        head, _, text = text.partition("-")
        try:
            days = int(head)
        except ValueError:
            return None
    parts = text.split(":")
    if not 1 <= len(parts) <= 3:
        return None
    try:
        values = [float(p) for p in parts]
    except ValueError:
        return None
    seconds = 0.0
    for value in values:
        seconds = seconds * 60 + value
    return seconds + days * 86400


def process_rows(timeout: float = 5.0) -> Tuple[Tuple[int, float, str], ...]:
    """``(pid, elapsed seconds, command)`` for every process. One ``ps``.

    One call, parsed once, rather than a ``pgrep`` per question - which is why
    everything downstream takes an already-gathered list. Never raises: a
    machine that will not tell us about its processes degrades to "we saw
    nothing", not to a traceback in ``status``.
    """
    try:
        proc = subprocess.run(
            ["ps", "-axo", "pid=,etime=,command="],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return ()
    if proc.returncode != 0:
        return ()
    rows: List[Tuple[int, float, str]] = []
    for line in (proc.stdout or "").splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 3:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        elapsed = _parse_etime(parts[1])
        if elapsed is None:
            continue
        rows.append((pid, elapsed, parts[2]))
    return tuple(rows)


def find_freemicro_processes(
    rows: Optional[Sequence[Tuple[int, float, str]]] = None,
    exclude: Optional[Iterable[int]] = None,
    clock: Callable[[], float] = time.time,
) -> Tuple[ProcessInfo, ...]:
    """Long-lived FreeMicro processes, with their start times and roles.

    Only the roles that *stay running* are matched, so a ``freemicro status``
    asking this question does not find itself, and neither does the ``grep`` in
    somebody's pipeline. The caller's own pid is excluded by default for the
    same reason: a running bridge checking on its peers must not report itself
    as a peer.
    """
    listing = process_rows() if rows is None else rows
    skip = set(exclude) if exclude is not None else {os.getpid()}
    now = clock()
    found: List[ProcessInfo] = []
    for pid, elapsed, command in listing:
        if pid in skip:
            continue
        role = _role_of(command)
        if not role:
            continue
        found.append(
            ProcessInfo(
                pid=pid, role=role, started=now - float(elapsed), command=command
            )
        )
    # Pad drivers first: they are what a user is actually asking about.
    order = {role: index for index, role in enumerate(PAD_ROLES)}
    found.sort(key=lambda p: (order.get(p.role, len(order)), p.pid))
    return tuple(found)


def process_start_time(
    pid: int,
    rows: Optional[Sequence[Tuple[int, float, str]]] = None,
    clock: Callable[[], float] = time.time,
) -> Optional[float]:
    """When ``pid`` started, in epoch seconds, or ``None`` if it is not there."""
    listing = process_rows() if rows is None else rows
    now = clock()
    for other, elapsed, _command in listing:
        if other == pid:
            return now - float(elapsed)
    return None


def process_started_before_code(
    pid: int,
    rows: Optional[Sequence[Tuple[int, float, str]]] = None,
    mtime: Optional[float] = None,
    clock: Callable[[], float] = time.time,
) -> bool:
    """Is that process running code older than what is installed right now?"""
    started = process_start_time(pid, rows=rows, clock=clock)
    if started is None:
        return False
    newest = package_mtime() if mtime is None else mtime
    return bool(newest) and started + GRACE_SECONDS < newest


def config_changed_since(
    pid: int,
    config_path: Optional[Path] = None,
    rows: Optional[Sequence[Tuple[int, float, str]]] = None,
    clock: Callable[[], float] = time.time,
    started: Optional[float] = None,
) -> bool:
    """Was the config edited after that process read it?

    This is incident 4 in one function: a bridge loads the config once, you
    edit it, and every key afterwards does the old thing with nothing anywhere
    saying why.
    """
    path = active_config_path() if config_path is None else Path(config_path)
    if path is None:
        return False
    try:
        edited = os.stat(str(path)).st_mtime
    except OSError:
        return False
    if started is None:
        started = process_start_time(pid, rows=rows, clock=clock)
    if started is None:
        return False
    return started + GRACE_SECONDS < edited


def active_config_path() -> Optional[Path]:
    """The user's pad config, or ``None`` when they are on the shipped default.

    The packaged ``default_keymap.json`` is deliberately excluded even though
    it is last in the search order: it ships *inside* the package, so every
    update rewrites it, and counting it here would report "the config it read
    changed" after every upgrade - a warning that is true of nothing the user
    did and that they could not act on.
    """
    try:
        from freemicro import padconfig

        for candidate in padconfig.search_paths():
            if candidate == padconfig.DEFAULT_CONFIG_PATH:
                continue
            if candidate.exists():
                return candidate
    except Exception:  # noqa: BLE001 - a status line may not depend on config
        return None
    return None


# ---------------------------------------------------------------------------
# Is anything listening?
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PadListener:
    """Whether *anything* is driving the pad, and how we know.

    The single most valuable answer in this module. A pad that is plugged in,
    permitted and configured but that nobody is listening to emits no
    scancodes at all - it is not degraded, it is dead - and until now every
    surface reported that state as all-green.
    """

    role: str = ""
    pid: Optional[int] = None
    #: ``"lock"`` (it holds ``pad.lock``) or ``"process"`` (we saw it running).
    source: str = ""

    @property
    def listening(self) -> bool:
        return bool(self.role)

    def summary(self) -> str:
        if not self.listening:
            return "No bridge running - the pad is inert."
        who = role_label(self.role)
        return f"Pad driven by {who}" + (f" (pid {self.pid})" if self.pid else "")

    def fix(self) -> str:
        """What to type. Empty when nothing needs doing."""
        if self.listening:
            return ""
        return (
            "Run: freemicro run\n"
            "Or, so it is always running:  freemicro daemon install"
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "listening": self.listening,
            "role": self.role,
            "pid": self.pid,
            "source": self.source,
        }


def pad_listener(
    processes: Optional[Sequence[ProcessInfo]] = None,
    holder: Optional[Callable[[], Optional[Dict[str, Any]]]] = None,
    rows: Optional[Sequence[Tuple[int, float, str]]] = None,
) -> PadListener:
    """Is any process actually driving the pad?

    The lock is the strongest evidence - a process that holds ``pad.lock`` has
    the device - so it is asked first. A running ``run``/``keys``/daemon that
    started with ``--take-pad`` holds no lock but is very much listening, which
    is why the process list is consulted too.
    """
    probe = holder or _default_lock_holder
    try:
        held = probe()
    except Exception:  # noqa: BLE001
        held = None
    if held:
        pid = held.get("pid")
        return PadListener(
            role=str(held.get("role") or ROLE_RUN),
            pid=int(pid) if isinstance(pid, int) else None,
            source="lock",
        )
    found = (
        find_freemicro_processes(rows=rows) if processes is None else tuple(processes)
    )
    for process in found:
        if process.drives_pad:
            return PadListener(
                role=process.role, pid=process.pid, source="process"
            )
    return PadListener()


def _default_lock_holder() -> Optional[Dict[str, Any]]:
    from freemicro import daemon

    return daemon.lock_holder()


# ---------------------------------------------------------------------------
# Staleness, per process
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Staleness:
    """One running process, and every way it has drifted from the truth."""

    process: ProcessInfo
    code_stale: bool = False
    config_stale: bool = False
    config_path: str = ""

    @property
    def stale(self) -> bool:
        return self.code_stale or self.config_stale

    def summary(self) -> str:
        what = []
        if self.code_stale:
            what.append("the code it loaded")
        if self.config_stale:
            what.append("the config it read")
        if not what:
            return f"{self.process.describe()} is up to date"
        return (
            f"{self.process.describe()} started before "
            + " and ".join(what)
            + " changed"
        )

    def fix(self) -> str:
        return self.process.restart_hint()

    def to_dict(self) -> Dict[str, Any]:
        data = self.process.to_dict()
        data.update(
            {
                "code_stale": self.code_stale,
                "config_stale": self.config_stale,
                "config_path": self.config_path,
                "stale": self.stale,
            }
        )
        return data


def self_staleness(
    role: str,
    mtime: Optional[float] = None,
    config_path: Optional[Path] = None,
    started: Optional[float] = None,
    clock: Callable[[], float] = time.time,
) -> Optional[Staleness]:
    """Is *this* process out of date? ``None`` when it is current.

    Deliberately separate from :func:`report`, which excludes the caller: a
    ``status`` command has no business calling itself stale, but a long-lived
    reader - the menu bar - absolutely must be able to. That was incident 2: a
    menu bar from before an update rejected a perfectly valid config, and the
    one process that could have said so was the one not looking.

    Uses :data:`PROCESS_STARTED` rather than ``ps``, so it costs a directory
    walk and no subprocess at all.
    """
    begin = PROCESS_STARTED if started is None else started
    newest = package_mtime() if mtime is None else mtime
    code_stale = bool(newest) and begin + GRACE_SECONDS < newest
    path = active_config_path() if config_path is None else Path(config_path)
    config_stale = role not in RELOADING_ROLES and config_changed_since(
        os.getpid(), config_path=path, started=begin, clock=clock
    )
    if not (code_stale or config_stale):
        return None
    return Staleness(
        process=ProcessInfo(pid=os.getpid(), role=role, started=begin),
        code_stale=code_stale,
        config_stale=config_stale,
        config_path=str(path) if path is not None else "",
    )


@dataclass(frozen=True)
class Report:
    """One reading of "what is running, and is any of it out of date?"."""

    listener: PadListener
    processes: Tuple[ProcessInfo, ...] = ()
    stale: Tuple[Staleness, ...] = ()

    @property
    def anything_stale(self) -> bool:
        return bool(self.stale)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "listener": self.listener.to_dict(),
            "processes": [p.to_dict() for p in self.processes],
            "stale": [s.to_dict() for s in self.stale],
        }


def report(
    rows: Optional[Sequence[Tuple[int, float, str]]] = None,
    holder: Optional[Callable[[], Optional[Dict[str, Any]]]] = None,
    config_path: Optional[Path] = None,
    mtime: Optional[float] = None,
    clock: Callable[[], float] = time.time,
) -> Report:
    """Everything ``status``, ``doctor`` and the menu bar need, gathered once.

    One ``ps``, one directory walk, one ``stat``. Cheap enough for a status
    command and for the menu bar's slow poll; far too expensive for a render
    tick, which is why the bridge uses :class:`CodeWatcher` instead.
    """
    listing = process_rows() if rows is None else rows
    processes = find_freemicro_processes(rows=listing, clock=clock)
    newest = package_mtime() if mtime is None else mtime
    path = active_config_path() if config_path is None else Path(config_path)
    stale: List[Staleness] = []
    for process in processes:
        code_stale = bool(newest) and process.started + GRACE_SECONDS < newest
        config_stale = process.role not in RELOADING_ROLES and config_changed_since(
            process.pid, config_path=path, started=process.started
        )
        if code_stale or config_stale:
            stale.append(
                Staleness(
                    process=process,
                    code_stale=code_stale,
                    config_stale=config_stale,
                    config_path=str(path) if path is not None else "",
                )
            )
    return Report(
        listener=pad_listener(processes=processes, holder=holder),
        processes=processes,
        stale=tuple(stale),
    )


# ---------------------------------------------------------------------------
# Stale locks
# ---------------------------------------------------------------------------

def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def reclaim_stale_lock(
    path: Optional[Path] = None,
    holder: Optional[Callable[[], Optional[Dict[str, Any]]]] = None,
    alive: Callable[[int], bool] = _pid_alive,
) -> str:
    """Note that we are taking over a lock file nobody holds. One line, or "".

    Reclaiming itself needs no code: :class:`freemicro.daemon.PadLock` takes an
    ``flock``, and the kernel drops that the instant the holder dies however it
    dies, so the next ``acquire()`` simply succeeds and overwrites the file.
    The web UI's ``padlink.reclaim_stale_lock`` deletes the leftover file on the
    same reasoning. This function exists to *say so* - because the one thing a
    user must never be told is to go and delete a file.
    """
    from freemicro import daemon

    target = Path(path) if path is not None else daemon.lock_path()
    if not target.exists():
        return ""
    probe = holder or _default_lock_holder
    try:
        if probe():
            return ""  # genuinely held; not ours to take
    except Exception:  # noqa: BLE001
        return ""
    try:
        import json

        data = json.loads(target.read_text(encoding="utf-8") or "{}")
    except (OSError, ValueError):
        data = {}
    pid = data.get("pid") if isinstance(data, dict) else None
    if not isinstance(pid, int):
        # An empty file is a lock that was *released* cleanly - the release
        # truncates it. Announcing that as a reclaim would put a scary word in
        # front of the most ordinary thing that happens here.
        return ""
    if alive(pid):
        # The lock is free but the process is somehow still around - say
        # nothing rather than claim something we cannot support.
        return ""
    role = str(data.get("role") or "") if isinstance(data, dict) else ""
    who = f" left by {role_label(role)}" if role else ""
    return f"  reclaimed a stale lock{who} - nothing for you to do."


# ---------------------------------------------------------------------------
# Never being stale: the config
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ConfigChange:
    """A config file that has changed under a running process."""

    #: The freshly loaded config, or ``None`` when it would not load.
    config: Any = None
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.config is not None


class ConfigWatcher:
    """Notices the config file changing, and hands back a reloaded one.

    :class:`freemicro.lighting_owner.LightingOwner` already does this for
    *lighting*, but only while it has a renderer to defend - which means only
    while a pad is attached - and it applies the result to the LEDs alone. The
    config governs bindings, agent-key policy and joystick tuning too, and the
    incident that made this necessary was a bridge whose lights followed an
    edit while its keys kept doing the old thing.

    So: one stat per interval, no subprocess, and a config that fails to parse
    leaves the running one exactly as it was. Never half-apply - a bridge
    running half of one config and half of another is worse than a bridge
    running an old one, because nothing on screen could ever explain it.
    """

    def __init__(
        self,
        path: Optional[Path] = None,
        loader: Optional[Callable[[], Any]] = None,
        clock: Callable[[], float] = time.time,
        interval: float = 1.0,
    ) -> None:
        self.path = Path(path) if path is not None else None
        self._loader = loader
        self._clock = clock
        self._interval = interval
        self._next_check = clock()
        self._stamp = self._read_stamp()

    def _read_stamp(self) -> Optional[Tuple[int, int]]:
        if self.path is None:
            return None
        try:
            info = os.stat(str(self.path))
        except OSError:
            return None
        return (info.st_mtime_ns, info.st_size)

    def poll(self) -> Optional[ConfigChange]:
        """``None`` when nothing changed. Cheap enough for a render tick."""
        if self.path is None:
            return None
        now = self._clock()
        if now < self._next_check:
            return None
        self._next_check = now + self._interval
        stamp = self._read_stamp()
        if stamp == self._stamp:
            return None
        # Record it either way: a config that will not parse must be reported
        # once, not on every tick until it is fixed.
        self._stamp = stamp
        if stamp is None:
            return None  # deleted; keep running what we have, silently
        try:
            return ConfigChange(config=self._load())
        except Exception as exc:  # noqa: BLE001 - a bad edit must not stop us
            return ConfigChange(error=str(exc))

    def _load(self) -> Any:
        if self._loader is not None:
            return self._loader()
        from freemicro import padconfig

        return padconfig.load(self.path)


def config_watch_path(pad: Any = None, override: Optional[Path] = None) -> Optional[Path]:
    """Which file a running bridge should watch for config edits.

    An explicit ``--config`` wins. Otherwise: whatever is in effect, and
    failing that where a new config *would* go - so creating one while the
    bridge is running is picked up rather than ignored until the next restart.
    """
    if override is not None:
        return Path(override)
    source = getattr(pad, "source", None) if pad is not None else None
    active = active_config_path()
    if active is not None:
        return active
    if source is not None:
        try:
            from freemicro import padconfig

            if Path(source) != Path(padconfig.DEFAULT_CONFIG_PATH):
                return Path(source)
        except Exception:  # noqa: BLE001
            return Path(source)
    try:
        from freemicro import padconfig

        return padconfig.user_path()
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# Never being stale: the self-restart
# ---------------------------------------------------------------------------

class RestartRequested(Exception):
    """Raised out of a render tick to unwind the run loop before re-execing.

    Deliberately an exception rather than a flag: it unwinds ``device.stream``
    through its ``finally``, which unschedules the IOKit callback, and then
    ``run_with_reconnect``, so by the time anyone calls ``execv`` the pad has
    been let go in the right order. A re-exec that leaves an input-report
    callback scheduled is the exact shape that has segfaulted this project
    before.
    """


#: Carried across ``execv`` so a restart loop cannot hide from itself.
RESTART_ENV = "FREEMICRO_RESTARTS"

#: How many self-restarts we allow inside :data:`RESTART_WINDOW` before giving
#: up and staying on the running copy. Being stale is bad; a bridge that
#: re-execs forever under launchd's ``KeepAlive`` is far worse.
MAX_RESTARTS = 3
RESTART_WINDOW = 600.0

#: How long the package mtime must hold still before we believe the update has
#: finished. A ``pip install`` writes many files; restarting into a half-written
#: tree is how you turn an update into an outage.
SETTLE_SECONDS = 2.0

#: How often the watcher may walk the package directory. Not per tick - but
#: often enough that "I just updated it" and "it picked that up" feel like the
#: same moment, which is the whole point.
CHECK_SECONDS = 2.0

REASON_STALE_CODE = "stale-code"
REASON_LOOP_GUARD = "loop-guard"
REASON_VERIFY_FAILED = "verify-failed"


@dataclass(frozen=True)
class RestartDecision:
    """What the watcher concluded, and the line to print about it."""

    restart: bool
    reason: str
    message: str


def verify_new_code(
    runner: Optional[Callable[[List[str]], Tuple[int, str]]] = None,
    timeout: float = 30.0,
) -> Tuple[bool, str]:
    """Does the code on disk actually import? Answered in a fresh interpreter.

    The running process has every module cached, so it is structurally unable
    to answer this about itself. A subprocess is the only honest test, and it
    is the difference between a restart and a crash-loop.

    The child is handed this process's ``sys.path`` as ``PYTHONPATH``, because
    a bare interpreter does not necessarily import the same ``freemicro`` we
    are running - or any at all. ``pyproject.toml``'s ``pythonpath = ["src"]``
    is the case that proves it: pytest edits ``sys.path`` **in process**, so
    nothing reaches the child through the environment and the import fails.
    Measured, not assumed::

        PYTHONPATH inherited       -> (True, '')
        sys.path edited in-process -> (False, "No module named 'freemicro'")

    That false negative is the dangerous direction. A failed verify means the
    watcher declines to re-exec, so the process **stays on stale code** and
    reports that the new code would not import - a confident, wrong diagnosis
    of the very condition this module exists to prevent.
    """
    command = [sys.executable, "-c", "import freemicro.cli"]
    run = runner or _run_verify
    try:
        code, output = run(command)
    except Exception as exc:  # noqa: BLE001 - never fail *into* a restart
        return False, str(exc)
    if code == 0:
        return True, ""
    lines = [line for line in (output or "").splitlines() if line.strip()]
    return False, lines[-1].strip() if lines else "it would not import"


def _run_verify(command: List[str]) -> Tuple[int, str]:
    proc = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=30,
        env=_child_env(),
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def _child_env() -> Dict[str, str]:
    """This process's environment, with ``sys.path`` handed down explicitly.

    See :func:`verify_new_code` for why. Entries already on ``PYTHONPATH`` are
    kept ahead of the rest so an operator's deliberate ordering survives, and
    the empty string (``sys.path``'s "current directory" marker) is dropped
    rather than exported, since in a child it would mean a *different*
    directory.
    """
    env = dict(os.environ)
    seen = set()
    parts: List[str] = []
    existing = env.get("PYTHONPATH", "")
    for entry in existing.split(os.pathsep) + sys.path:
        if not entry or entry in seen:
            continue
        seen.add(entry)
        parts.append(entry)
    if parts:
        env["PYTHONPATH"] = os.pathsep.join(parts)
    return env


class CodeWatcher:
    """Makes a long-lived bridge structurally unable to run stale code.

    Polled from the render tick. It walks the installed package at most every
    :data:`CHECK_SECONDS`, and only when the newest mtime is *both* newer than
    this process and unchanged for :data:`SETTLE_SECONDS` does it consider
    acting. Then, in order:

    1. **Loop guard.** More than :data:`MAX_RESTARTS` restarts inside
       :data:`RESTART_WINDOW` and it stops trying, says so loudly, and keeps
       running the old code.
    2. **Verify.** A subprocess imports the new tree. If that fails we stay put
       and say why - never re-exec into code that will not start.
    3. **Restart.** The caller unwinds its loop, closes the pad, releases the
       lock, and calls :meth:`restart`, which re-execs with the same argv so
       every flag survives.

    Nothing is carried across the exec except intent: the state store is on
    disk and the pad reconnects on its own.
    """

    def __init__(
        self,
        started_at: Optional[float] = None,
        mtime: Callable[[], float] = package_mtime,
        clock: Callable[[], float] = time.time,
        verify: Callable[[], Tuple[bool, str]] = verify_new_code,
        exec_: Optional[Callable[[str, List[str]], Any]] = None,
        argv: Optional[List[str]] = None,
        environ: Optional[Dict[str, str]] = None,
        executable: Optional[str] = None,
        settle: float = SETTLE_SECONDS,
        check_interval: float = CHECK_SECONDS,
        max_restarts: int = MAX_RESTARTS,
        window: float = RESTART_WINDOW,
    ) -> None:
        self._clock = clock
        self._started = PROCESS_STARTED if started_at is None else started_at
        self._mtime = mtime
        self._verify = verify
        self._exec = exec_ if exec_ is not None else os.execv
        self._argv = list(argv) if argv is not None else list(sys.argv)
        self._environ = os.environ if environ is None else environ
        self._executable = executable or sys.executable
        self._settle = settle
        self._check_interval = check_interval
        self._max_restarts = max_restarts
        self._window = window

        self._next_check = self._clock()
        self._seen_mtime: Optional[float] = None
        self._seen_at = 0.0
        self._rejected: Optional[float] = None
        self._decision: Optional[RestartDecision] = None

    # -- the tick ---------------------------------------------------------

    @property
    def pending(self) -> bool:
        """Has a restart been decided and not yet carried out?"""
        return self._decision is not None and self._decision.restart

    def poll(self) -> Optional[RestartDecision]:
        """Called once per render tick. ``None`` means nothing to say."""
        if self._decision is not None:
            return None  # decided once; never twice, never every tick
        now = self._clock()
        if now < self._next_check:
            return None
        self._next_check = now + self._check_interval

        newest = self._mtime()
        if not newest or newest <= self._started + GRACE_SECONDS:
            self._seen_mtime = None
            return None

        if newest != self._seen_mtime:
            # Still being written. Wait for it to hold still.
            self._seen_mtime, self._seen_at = newest, now
            return None
        if now - self._seen_at < self._settle:
            return None
        if newest == self._rejected:
            return None  # already tried this tree and it did not import

        restarts, first_at = self.history()
        if restarts >= self._max_restarts and now - first_at < self._window:
            self._decision = RestartDecision(
                False,
                REASON_LOOP_GUARD,
                "[update] freemicro changed on disk again, but this process has "
                f"already restarted itself {restarts} times - staying on the "
                "code it has. Restart it by hand once the update settles.",
            )
            return self._decision

        ok, detail = self._verify()
        if not ok:
            self._rejected = newest
            return RestartDecision(
                False,
                REASON_VERIFY_FAILED,
                "[update] freemicro changed on disk but the new code does not "
                f"import - staying on the running copy ({detail}). Fix it, then "
                "restart.",
            )

        self._decision = RestartDecision(
            True,
            REASON_STALE_CODE,
            "[update] freemicro changed on disk - restarting…",
        )
        return self._decision

    # -- carrying it out ---------------------------------------------------

    def restart(self, release: Optional[Callable[[], None]] = None) -> None:
        """Re-exec with the same argv. Does not return.

        ``release`` is the caller's last chance to hand back the device and the
        pad lock. It runs first, and a failure in it is never allowed to stop
        the restart - a half-released pad that reconnects is recoverable, a
        process wedged mid-update is not.
        """
        if release is not None:
            try:
                release()
            except Exception:  # noqa: BLE001 - teardown must not block a restart
                pass
        self._remember_restart()
        self._exec(self._executable, [self._executable, *self._argv])

    # -- the loop guard ----------------------------------------------------

    def history(self) -> Tuple[int, float]:
        """``(restarts so far, when the first one was)``, from the environment.

        Kept in an env var precisely because it has to survive ``execv``: the
        whole point of a loop guard is that it counts across the restarts.
        """
        raw = str(self._environ.get(RESTART_ENV, "") or "")
        count, _, stamp = raw.partition(":")
        try:
            restarts = int(count)
            first_at = float(stamp)
        except ValueError:
            return 0, self._clock()
        if self._clock() - first_at >= self._window:
            return 0, self._clock()
        return max(0, restarts), first_at

    def _remember_restart(self) -> None:
        restarts, first_at = self.history()
        if restarts == 0:
            first_at = self._clock()
        self._environ[RESTART_ENV] = f"{restarts + 1}:{first_at}"


__all__ = [
    "CHECK_SECONDS",
    "GRACE_SECONDS",
    "MAX_RESTARTS",
    "PAD_ROLES",
    "PROCESS_STARTED",
    "RELOADING_ROLES",
    "REASON_LOOP_GUARD",
    "REASON_STALE_CODE",
    "REASON_VERIFY_FAILED",
    "RESTART_ENV",
    "RESTART_WINDOW",
    "ROLE_DAEMON",
    "ROLE_KEYS",
    "ROLE_MENUBAR",
    "ROLE_RUN",
    "ROLE_WEBUI",
    "SETTLE_SECONDS",
    "CodeWatcher",
    "ConfigChange",
    "ConfigWatcher",
    "PadListener",
    "ProcessInfo",
    "Report",
    "RestartDecision",
    "RestartRequested",
    "Staleness",
    "active_config_path",
    "config_changed_since",
    "config_watch_path",
    "find_freemicro_processes",
    "package_mtime",
    "package_root",
    "pad_listener",
    "process_rows",
    "process_start_time",
    "process_started_before_code",
    "reclaim_stale_lock",
    "report",
    "restart_hint",
    "role_label",
    "self_staleness",
    "verify_new_code",
]
