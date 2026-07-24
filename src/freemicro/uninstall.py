"""Remove FreeMicro from a machine, completely and honestly.

Anything that installs itself has to be able to remove itself. FreeMicro writes
hooks into Claude Code's settings, registers a LaunchAgent, creates
``~/.freemicro``, and - if you let it - takes the pad's LEDs over. Before this
module there was no single command that even *named* that footprint, let alone
undid it, and two partial removals in two different places (``freemicro install
--uninstall``, ``freemicro daemon uninstall``) left the rest behind.

Four rules, in the order they matter:

1. **Stop first, then remove.** A daemon still holding the pad while its config
   is deleted is the stale-process failure this project keeps hitting. Every
   running FreeMicro is stopped and *verified* stopped before a byte is
   deleted.
2. **Hand the pad back while the code that knows how is still installed.** A
   pad left glowing with nothing on the machine that could ever turn it off is
   the worst outcome in ``docs/FACTORY-DEFAULTS.md`` §12, and it is
   unrecoverable without unplugging the thing.
3. **Show the list before touching it.** :func:`plan` is pure - it reads the
   disk and reports - and the CLI prints it and asks. ``--dry-run`` stops
   there.
4. **Never claim a success that did not happen.** One item that cannot be
   removed does not stop the rest, and it does not get rounded up to "done"
   either. :class:`Result` carries every failure by name.
"""

from __future__ import annotations

import errno
import json
import os
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from freemicro.config import config_home

#: Env var that redirects where the raw hook log is appended. The log only
#: exists when this is set (see ``cli._log_raw_event``), which is why the file
#: has no fixed home and has to be looked for in two places.
ENV_HOOK_LOG = "FREEMICRO_HOOK_LOG"

#: Env var that redirects the pad config. Someone who set it has their keymap
#: somewhere we would otherwise walk straight past.
ENV_KEYMAP = "FREEMICRO_KEYMAP"

#: Where the raw hook log lands when nobody named a path. Not written by this
#: build - kept in the sweep because earlier ones did, and 29 MB of full hook
#: payloads (every ``cwd`` you have worked in) is not something to leave behind.
DEFAULT_HOOK_LOG = "hook-events.jsonl"

#: Files under the config dir that a *person* wrote or would want back on a
#: reinstall. ``--keep-config`` keeps exactly these.
CONFIG_FILES: Tuple[str, ...] = ("keymap.json", "keymap.json.bak", "config.json")
CONFIG_DIRS: Tuple[str, ...] = ("layouts",)

#: Files the program wrote for itself. Always removed - keeping a lock file or
#: a session cache across a reinstall has no upside and several downsides.
STATE_FILES: Tuple[str, ...] = (
    "slots.json",
    "status.json",
    "pad.lock",
    "menubar.lock",
    # Left by the screen renderer, which has since been deleted. It cannot be
    # rewritten, and nothing else will ever clean it up.
    "tk-probe.json",
    DEFAULT_HOOK_LOG,
)
STATE_DIRS: Tuple[str, ...] = ("state", "logs")

#: How long to wait for a process we asked to stop to actually let go.
STOP_TIMEOUT_SECONDS = 6.0

#: Where the two macOS permissions live. We cannot revoke them - TCC grants are
#: the user's to give and the user's to take back - so the least we can do is
#: say exactly where they are rather than "check your privacy settings".
TCC_ENTRIES: Tuple[Tuple[str, str], ...] = (
    ("Input Monitoring", "reading the pad's keys"),
    ("Accessibility", "typing into your terminal"),
)


# ---------------------------------------------------------------------------
# What the footprint looks like
# ---------------------------------------------------------------------------

#: Categories, in the order they are removed.
PROCESS = "process"
LEDS = "leds"
LAUNCHAGENT = "launchagent"
HOOKS = "hooks"
CONFIG = "config"
STATE = "state"
HOME = "home"


@dataclass(frozen=True)
class Item:
    """One thing FreeMicro put on this machine."""

    key: str
    #: One line, in the user's vocabulary, not ours.
    label: str
    category: str
    #: Where it is, when it has a where. ``None`` for processes and LEDs.
    path: Optional[Path] = None
    #: Extra truth for the preview: a size, a pid, a count.
    detail: str = ""
    #: ``False`` means it is not on this machine and there is nothing to do.
    present: bool = True
    #: ``True`` means ``--keep-config`` is protecting it.
    kept: bool = False

    @property
    def acts(self) -> bool:
        """Whether removing this would actually do something."""
        return self.present and not self.kept

    def describe(self) -> str:
        parts = [self.label]
        if self.path is not None:
            parts.append(shorten(self.path))
        line = "  ".join(parts)
        return f"{line}  ({self.detail})" if self.detail else line


def shorten(path: Path) -> str:
    """``~/.freemicro/keymap.json``, not the full path.

    A preview whose lines are mostly ``/Users/<name>/`` is a preview people
    skim, and this is the one list in FreeMicro that has to be read.
    """
    text = str(path)
    try:
        home = str(Path.home())
    except (OSError, RuntimeError):  # pragma: no cover - defensive
        return text
    if home and text.startswith(home + os.sep):
        return "~" + text[len(home):]
    return text


@dataclass(frozen=True)
class Plan:
    """Everything :func:`uninstall` would do, before it does any of it."""

    items: Tuple[Item, ...]
    keep_config: bool
    home: Path
    settings_path: Path
    #: Whether the config we found has FreeMicro driving the LEDs.
    lighting_enabled: bool = False
    lighting_zones: Tuple[str, ...] = ()
    #: Anything that stopped us reading the config, said out loud rather than
    #: silently turning into "lighting is off".
    lighting_note: str = ""

    @property
    def actions(self) -> Tuple[Item, ...]:
        return tuple(i for i in self.items if i.acts)

    @property
    def kept(self) -> Tuple[Item, ...]:
        return tuple(i for i in self.items if i.present and i.kept)

    @property
    def empty(self) -> bool:
        """Nothing to remove. Running twice must land here and say so."""
        return not self.actions


@dataclass(frozen=True)
class Outcome:
    """What happened to one item."""

    key: str
    label: str
    ok: bool
    message: str
    #: True when the item was skipped rather than attempted.
    skipped: bool = False


@dataclass
class Result:
    outcomes: List[Outcome] = field(default_factory=list)
    kept: Tuple[Item, ...] = ()

    def add(self, key: str, label: str, ok: bool, message: str,
            skipped: bool = False) -> None:
        self.outcomes.append(Outcome(key, label, ok, message, skipped))

    @property
    def failures(self) -> List[Outcome]:
        return [o for o in self.outcomes if not o.ok]

    @property
    def done(self) -> List[Outcome]:
        return [o for o in self.outcomes if o.ok and not o.skipped]

    @property
    def ok(self) -> bool:
        return not self.failures


# ---------------------------------------------------------------------------
# Processes
# ---------------------------------------------------------------------------

def _read_pid(path: Path) -> Optional[int]:
    """The pid recorded in a lock file, whichever of our two formats it is."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    if not text:
        return None
    try:
        data = json.loads(text)
    except ValueError:
        data = None
    if isinstance(data, dict):
        raw = data.get("pid")
    else:
        raw = text
    try:
        pid = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return pid if pid > 0 else None


def lock_is_held(path: Path) -> bool:
    """Is somebody actually holding this ``flock`` right now?

    Asked by *trying* the lock rather than by reading the pid inside it: the
    file survives a crash and the lock does not, so a leftover file with a
    recycled pid in it must never be mistaken for a live owner.
    """
    if not path.exists():
        return False
    try:
        import fcntl
    except ImportError:  # pragma: no cover - non-POSIX
        return False
    try:
        fd = os.open(str(path), os.O_RDWR)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return True
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
    return False


def _command_of(pid: int) -> str:
    try:
        proc = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True, text=True, timeout=5.0,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return (proc.stdout or "").strip()


def is_freemicro_process(pid: int) -> bool:
    """Would signalling this pid stop *our* process and nothing else?

    A lock file can outlive its writer and pids get recycled, so the pid alone
    is not enough to justify a SIGTERM. We ask the OS what the process actually
    is and refuse anything that is not visibly FreeMicro. Refusing costs the
    user one manual ``kill``; being wrong costs them whatever else was running.
    """
    if pid <= 0 or pid == os.getpid():
        return False
    return "freemicro" in _command_of(pid).lower()


def stop_pid(
    pid: int,
    held: Callable[[], bool],
    timeout: float = STOP_TIMEOUT_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
) -> Tuple[bool, str]:
    """SIGTERM a FreeMicro process and wait for it to let go. Never SIGKILLs.

    SIGTERM specifically, and never SIGKILL: the renderer's exit guard turns a
    SIGTERM into the hand-back that blanks the pad
    (:func:`freemicro.renderers.micro_leds.release_lighting`), and SIGKILL
    would skip exactly that. A process that will not stop politely is reported,
    not shot.
    """
    if not is_freemicro_process(pid):
        return False, (
            f"pid {pid} is not a FreeMicro process (the lock file is stale, or "
            "the pid has been reused) - left alone"
        )
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True, f"pid {pid} had already exited"
    except OSError as exc:
        if exc.errno == errno.EPERM:
            return False, f"not allowed to stop pid {pid}: {exc}"
        return False, f"could not stop pid {pid}: {exc}"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not held():
            return True, f"stopped (pid {pid})"
        sleep(0.1)
    return False, (
        f"pid {pid} still has the pad {timeout:g}s after being asked to stop; "
        f"run `kill {pid}` and try again"
    )


# ---------------------------------------------------------------------------
# The pad
# ---------------------------------------------------------------------------

def _load_lighting() -> Tuple[Optional[Any], str]:
    """The pad config, or a plain reason we could not read it."""
    from freemicro import padconfig

    try:
        return padconfig.load(), ""
    except padconfig.PadConfigError as exc:
        return None, str(exc)


def restore_pad(pad: Optional[Any] = None, device: Any = None) -> Dict[str, Any]:
    """Put the LEDs back to their factory state: dark, on the zones we drove.

    Called *after* everything else has been stopped and *before* anything is
    deleted, which is the only window in which this can work: while a daemon
    still holds the pad it would repaint over us, and once the package is gone
    nothing on the machine can talk to the device at all.

    Goes through :meth:`MicroLedsRenderer.hand_back` rather than building
    protocol messages here - it is the one entry point that knows what "we are
    done driving this pad" means, and it already promises never to raise. The
    lighting it is handed is the user's own zones with ``on_exit`` forced to
    ``off``: an uninstall is not the moment to honour ``on_exit: breath`` and
    leave the pad pulsing.
    """
    from freemicro.device import close_shared, is_supported, shared_device
    from freemicro.renderers.micro_leds import MicroLedsRenderer

    note = ""
    if pad is None:
        pad, note = _load_lighting()
    if pad is None:
        return {
            "attempted": False, "ok": True, "zones": (),
            "reason": f"could not read your config ({note}), so FreeMicro does "
                      "not know which LEDs it was driving",
        }
    lighting = pad.lighting
    if not lighting.enabled:
        return {
            "attempted": False, "ok": True, "zones": tuple(lighting.zones),
            "reason": "LED control was off, so FreeMicro never drove the lights",
        }

    borrowed = device is not None
    if device is None:
        if not is_supported():
            return {
                "attempted": True, "ok": False, "zones": tuple(lighting.zones),
                "reason": "this platform cannot open the pad",
            }
        device = shared_device()
    if device is None:
        return {
            "attempted": True, "ok": False, "zones": tuple(lighting.zones),
            "reason": "no Codex Micro is reachable right now",
        }

    exit_lighting = replace(lighting, enabled=True, on_exit="off")
    renderer = MicroLedsRenderer(device=device, config=pad)
    try:
        handed = renderer.hand_back(device=device, lighting=exit_lighting)
    finally:
        if not borrowed:
            close_shared()
    return {
        "attempted": True,
        "ok": bool(handed),
        "zones": tuple(lighting.zones),
        "reason": "" if handed else "the pad did not accept the blank frame",
    }


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------

def _size_of(path: Path) -> str:
    try:
        if path.is_dir():
            files = [p for p in path.rglob("*") if p.is_file()]
            total = sum(p.stat().st_size for p in files)
            return f"{len(files)} file{'' if len(files) == 1 else 's'}, " \
                   f"{_bytes(total)}"
        return _bytes(path.stat().st_size)
    except OSError:
        return ""


def _bytes(size: int) -> str:
    if size >= 1024 * 1024:
        return f"{size / (1024 * 1024):.1f} MB"
    if size >= 1024:
        return f"{size / 1024:.0f} KB"
    return f"{size} bytes"


def _extra_env_path(name: str) -> Optional[Path]:
    """A path an env var points at, when it is one we would otherwise miss."""
    raw = os.environ.get(name)
    if not raw:
        return None
    try:
        return Path(raw).expanduser()
    except (OSError, ValueError):  # pragma: no cover - defensive
        return None


def plan(
    home: Optional[Path] = None,
    settings_path: Optional[Path] = None,
    keep_config: bool = False,
) -> Plan:
    """Read the machine and describe exactly what removing FreeMicro would do.

    Pure in the sense that matters: it opens nothing, stops nothing and deletes
    nothing. Every path is resolved from the code that *writes* it, so a new
    file somewhere in ``~/.freemicro`` shows up here as an unrecognised entry
    rather than being silently left behind.
    """
    from freemicro import daemon, hooks_install

    home = Path(home) if home is not None else config_home()
    settings = (
        Path(settings_path) if settings_path is not None
        else hooks_install.default_settings_path()
    )
    items: List[Item] = []

    # -- things that are running ------------------------------------------
    launch_state = daemon.launchctl_state()
    daemon_pid = launch_state.get("pid")
    items.append(Item(
        key="daemon_running",
        label="the background daemon (stop it)",
        category=PROCESS,
        present=bool(launch_state.get("loaded")) or bool(daemon_pid),
        detail=f"pid {daemon_pid}" if daemon_pid else "registered with launchd",
    ))

    pad_lock = home / "pad.lock"
    pad_pid = _read_pid(pad_lock) if lock_is_held(pad_lock) else None
    items.append(Item(
        key="pad_holder",
        label="whatever is holding the pad (stop it)",
        category=PROCESS,
        present=pad_pid is not None,
        detail=f"pid {pad_pid}" if pad_pid else "",
    ))

    menubar_lock = home / "menubar.lock"
    menubar_pid = _read_pid(menubar_lock) if lock_is_held(menubar_lock) else None
    items.append(Item(
        key="menubar_running",
        label="the menu bar item (stop it)",
        category=PROCESS,
        present=menubar_pid is not None,
        detail=f"pid {menubar_pid}" if menubar_pid else "",
    ))

    # -- the LEDs ----------------------------------------------------------
    pad, lighting_note = _load_lighting()
    lighting_on = bool(pad is not None and pad.lighting.enabled)
    zones = tuple(pad.lighting.zones) if pad is not None else ()
    items.append(Item(
        key="leds",
        label="put the pad's LEDs back to their factory state (dark)",
        category=LEDS,
        present=lighting_on,
        detail=", ".join(zones) if lighting_on else "LED control is off",
    ))

    # -- the LaunchAgent ---------------------------------------------------
    plist = daemon.plist_path()
    items.append(Item(
        key="launchagent",
        label="the login-time LaunchAgent",
        category=LAUNCHAGENT,
        path=plist,
        present=plist.exists(),
    ))

    # -- Claude Code's hooks ----------------------------------------------
    hook_status = hooks_install.status(settings)
    events = hook_status.get("events") or []
    items.append(Item(
        key="hooks",
        label="FreeMicro's hook entries in Claude Code's settings",
        category=HOOKS,
        path=settings,
        present=bool(events),
        detail=(
            f"{len(events)} of {len(hooks_install.HOOK_EVENTS)} events; "
            "your other hooks are left alone"
        ) if events else "",
    ))

    # -- the config directory ---------------------------------------------
    seen: set = set()
    for name in CONFIG_FILES:
        path = home / name
        seen.add(name)
        items.append(Item(
            key=f"config:{name}",
            label=_config_label(name),
            category=CONFIG,
            path=path,
            present=path.is_file(),
            detail=_size_of(path) if path.is_file() else "",
            kept=keep_config,
        ))
    for name in CONFIG_DIRS:
        path = home / name
        seen.add(name)
        items.append(Item(
            key=f"config:{name}",
            label="your saved layouts",
            category=CONFIG,
            path=path,
            present=path.is_dir(),
            detail=_size_of(path) if path.is_dir() else "",
            kept=keep_config,
        ))

    # A keymap somewhere else entirely. Two ways that happens, and neither is
    # exotic enough to leave behind: the XDG location is in the search path and
    # documented, and $FREEMICRO_KEYMAP is how people point at a checked-in one.
    from freemicro import padconfig

    for key, path in (
        ("xdg_keymap", padconfig.xdg_path()),
        ("env_keymap", _extra_env_path(ENV_KEYMAP)),
    ):
        if path is None or path == home / padconfig.FILENAME:
            continue
        items.append(Item(
            key=f"config:{key}",
            label=(
                "your pad config in the XDG location" if key == "xdg_keymap"
                else f"your pad config, from ${ENV_KEYMAP}"
            ),
            category=CONFIG,
            path=path,
            present=path.is_file(),
            detail=_size_of(path) if path.is_file() else "",
            kept=keep_config,
        ))

    for name in STATE_FILES:
        path = home / name
        seen.add(name)
        items.append(Item(
            key=f"state:{name}",
            label=_state_label(name),
            category=STATE,
            path=path,
            present=path.is_file(),
            detail=_size_of(path) if path.is_file() else "",
        ))
    for name in STATE_DIRS:
        path = home / name
        seen.add(name)
        items.append(Item(
            key=f"state:{name}",
            label=_state_label(name),
            category=STATE,
            path=path,
            present=path.is_dir(),
            detail=_size_of(path) if path.is_dir() else "",
        ))

    hook_log = _extra_env_path(ENV_HOOK_LOG)
    if hook_log is not None and hook_log.parent != home:
        items.append(Item(
            key="state:hook_log",
            label=f"the raw hook log, from ${ENV_HOOK_LOG}",
            category=STATE,
            path=hook_log,
            present=hook_log.is_file(),
            detail=_size_of(hook_log) if hook_log.is_file() else "",
        ))

    # Anything else in there. Naming it beats leaving it: this directory is
    # ours, and a file we no longer recognise is still a file we created.
    try:
        leftovers = sorted(p.name for p in home.iterdir() if p.name not in seen)
    except OSError:
        leftovers = []
    for name in leftovers:
        path = home / name
        items.append(Item(
            key=f"state:extra:{name}",
            label="left over from an older FreeMicro",
            category=STATE,
            path=path,
            detail=_size_of(path),
        ))

    items.append(Item(
        key="home",
        label="the FreeMicro folder itself",
        category=HOME,
        path=home,
        present=home.is_dir(),
        kept=keep_config,
        detail="kept, because --keep-config keeps what is in it" if keep_config
        else "",
    ))

    return Plan(
        items=tuple(items),
        keep_config=keep_config,
        home=home,
        settings_path=settings,
        lighting_enabled=lighting_on,
        lighting_zones=zones,
        lighting_note=lighting_note,
    )


def _config_label(name: str) -> str:
    return {
        "keymap.json": "your pad config: every binding and colour",
        "keymap.json.bak": "the backup the web editor keeps of it",
        "config.json": "your engine settings (state timeouts, palette)",
    }.get(name, name)


def _state_label(name: str) -> str:
    return {
        "slots.json": "which project is on which Agent Key",
        "status.json": "the menu bar's cached reading",
        "pad.lock": "the pad lock",
        "menubar.lock": "the menu bar lock",
        "tk-probe.json": "a cache left by the deleted screen renderer",
        DEFAULT_HOOK_LOG: "the raw hook log (full payloads, every cwd)",
        "state": "the per-session state directory",
        "logs": "the daemon's logs",
    }.get(name, name)


# ---------------------------------------------------------------------------
# Doing it
# ---------------------------------------------------------------------------

def _remove_path(path: Path) -> Tuple[bool, str]:
    try:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink()
    except FileNotFoundError:
        return True, "already gone"
    except OSError as exc:
        return False, f"could not remove {shorten(path)}: {exc}"
    return True, f"removed {shorten(path)}"


def _stop_lock_holder(
    result: Result, item: Item, lock: Path,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Ask whoever holds ``lock`` to stop, and confirm from the lock itself.

    Re-checks the lock before signalling anything. Stopping the daemon usually
    frees this one on the way past, and a pid read out of a file nobody is
    holding any more is a pid that may since have been handed to something
    else entirely.
    """
    if not lock_is_held(lock):
        result.add(item.key, item.label, True, "already stopped")
        return
    pid = _read_pid(lock)
    if pid is None:
        result.add(
            item.key, item.label, False,
            f"something is holding {shorten(lock)} but it does not say which "
            "process; "
            "close it and re-run",
        )
        return
    ok, message = stop_pid(pid, lambda: lock_is_held(lock), sleep=sleep)
    result.add(item.key, item.label, ok, message)


def uninstall(
    the_plan: Optional[Plan] = None,
    home: Optional[Path] = None,
    settings_path: Optional[Path] = None,
    keep_config: bool = False,
    sleep: Callable[[float], None] = time.sleep,
) -> Result:
    """Carry out a plan. Every step is independent and every failure is named.

    The order is the whole design: stop, verify, blank the pad, then delete.
    Nothing later in the list is skipped because something earlier failed - a
    LaunchAgent that will not unload is no reason to leave someone's hook
    entries in their settings file.
    """
    the_plan = the_plan if the_plan is not None else plan(
        home=home, settings_path=settings_path, keep_config=keep_config
    )
    result = Result(kept=the_plan.kept)
    by_key = {item.key: item for item in the_plan.items}

    # 1. Stop the daemon and take its plist with it. One call: `daemon.uninstall`
    #    boots the job out, deletes the plist, and *verifies* launchd has let go
    #    rather than assuming, which is exactly the check this step exists for.
    daemon_item = by_key["daemon_running"]
    plist_item = by_key["launchagent"]
    if daemon_item.acts or plist_item.acts:
        from freemicro import daemon

        outcome = daemon.uninstall()
        ok = bool(outcome["ok"])
        error = str(outcome.get("error") or "launchd would not let go")
        if daemon_item.acts:
            result.add(
                daemon_item.key, daemon_item.label, ok,
                "stopped, and launchd confirms it is gone" if ok else error,
            )
        if plist_item.acts:
            result.add(
                plist_item.key, plist_item.label, ok,
                f"removed {shorten(plist_item.path)}" if ok else error,
            )

    # 2. Anything else still holding the pad, and the menu bar. SIGTERM, then
    #    watch the lock rather than trusting the signal.
    item = by_key["pad_holder"]
    if item.acts:
        _stop_lock_holder(result, item, the_plan.home / "pad.lock", sleep=sleep)
    item = by_key["menubar_running"]
    if item.acts:
        _stop_lock_holder(
            result, item, the_plan.home / "menubar.lock", sleep=sleep
        )

    # 3. Hand the pad back, while the code that knows how is still here. After
    #    the stops on purpose: a daemon that is still running would simply
    #    repaint over us on its next tick.
    item = by_key["leds"]
    if item.acts:
        if lock_is_held(the_plan.home / "pad.lock"):
            result.add(
                item.key, item.label, False,
                "something still has the pad, so the LEDs were left alone. "
                "Stop it and run\n          `freemicro lights --disable`.",
            )
        else:
            outcome = restore_pad()
            if outcome["ok"]:
                zones = ", ".join(outcome["zones"]) or "every zone"
                result.add(item.key, item.label, True, f"blanked {zones}")
            else:
                result.add(
                    item.key, item.label, False,
                    f"{outcome['reason']}. The pad may still be showing "
                    "FreeMicro's colours;\n          plug it in and run "
                    "`freemicro lights --disable` before removing the package.",
                )

    # 4. Our hook entries, and only ours.
    item = by_key["hooks"]
    if item.acts:
        from freemicro import hooks_install

        try:
            path, removed = hooks_install.uninstall_hooks(
                settings_path=the_plan.settings_path
            )
            result.add(
                item.key, item.label, True,
                f"removed {removed} entr{'y' if removed == 1 else 'ies'} from "
                f"{shorten(Path(path))}",
            )
        except OSError as exc:
            result.add(
                item.key, item.label, False,
                f"could not rewrite {shorten(the_plan.settings_path)}: {exc}",
            )

    # 5. Files. Config first so `--keep-config` reads clearly in the log.
    for item in the_plan.items:
        if item.category not in (CONFIG, STATE) or not item.acts:
            continue
        if item.path is None:
            continue
        ok, message = _remove_path(item.path)
        result.add(item.key, item.label, ok, message)

    # 6. The directory itself - and its XDG parent, if we emptied that too.
    item = by_key["home"]
    if item.acts and item.path is not None:
        ok, message = _remove_path(item.path)
        result.add(item.key, item.label, ok, message)
    _prune_empty_parents(the_plan, result)

    return result


def _prune_empty_parents(the_plan: Plan, result: Result) -> None:
    """Remove ``~/.config/freemicro`` once its only file is gone.

    Leaving an empty directory behind is not a functional problem, but "removed
    everything" has to be true or it is not worth saying.
    """
    if the_plan.keep_config:
        return
    from freemicro import padconfig

    parent = padconfig.xdg_path().parent
    if parent == the_plan.home or not parent.is_dir():
        return
    try:
        if any(parent.iterdir()):
            return
        parent.rmdir()
    except OSError:
        return
    result.add(
        "config:xdg_dir", "the XDG config folder", True,
        f"removed {shorten(parent)}",
    )


# ---------------------------------------------------------------------------
# What we cannot do
# ---------------------------------------------------------------------------

def cannot_remove() -> List[str]:
    """The honest list. Printed on every run, including the empty one.

    Both entries are things only the user can do, and both are things a user
    who has just uninstalled something reasonably assumes are gone.
    """
    lines = [
        "Two macOS permission grants are yours to remove, not ours - nothing "
        "on this machine\ncan revoke a TCC entry on your behalf:",
    ]
    for name, why in TCC_ENTRIES:
        lines.append(
            f"  System Settings -> Privacy & Security -> {name}\n"
            f"    switch off (or select and '-') every FreeMicro entry - "
            f"it was for {why}"
        )
    lines.append(
        "The package itself is still installed. This command removed FreeMicro's "
        "state,\nnot FreeMicro:\n"
        "  pipx uninstall freemicro          if you installed it with pipx\n"
        "  pip uninstall freemicro           if you installed it with pip\n"
        "  rm -rf <clone>/.venv              if you ran it from a clone"
    )
    if os.environ.get(ENV_HOOK_LOG) or os.environ.get(ENV_KEYMAP):
        names = ", ".join(
            f"${n}" for n in (ENV_HOOK_LOG, ENV_KEYMAP) if os.environ.get(n)
        )
        lines.append(
            f"{names} is set in this shell. Take it out of your shell profile "
            "too,\nor the next thing that reads it will point at a path that is "
            "no longer there."
        )
    return lines


__all__ = [
    "CONFIG",
    "CONFIG_DIRS",
    "CONFIG_FILES",
    "ENV_HOOK_LOG",
    "ENV_KEYMAP",
    "HOME",
    "HOOKS",
    "Item",
    "LAUNCHAGENT",
    "LEDS",
    "Outcome",
    "PROCESS",
    "Plan",
    "Result",
    "STATE",
    "STATE_DIRS",
    "STATE_FILES",
    "STOP_TIMEOUT_SECONDS",
    "TCC_ENTRIES",
    "cannot_remove",
    "is_freemicro_process",
    "lock_is_held",
    "plan",
    "restore_pad",
    "shorten",
    "stop_pid",
    "uninstall",
]
