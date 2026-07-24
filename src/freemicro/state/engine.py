"""The state engine: a tiny, dependency-free store of per-session agent state.

Design notes
------------
* **One file per session.** Each Claude Code session owns exactly one JSON
  file under the state directory. Hooks fire concurrently from independent
  processes, so we never hold a lock across sessions; the filesystem is the
  concurrency primitive. A single-file write is atomic enough for our needs
  (we write to a temp file and ``os.replace``).
* **Priority resolution.** With several agents running at once you want the
  LED to show the one that needs *you*. Priority is
  ``waiting > error > done > working > idle`` - a session waiting on your
  approval always wins.
* **Liveness, then the clock.** A session lives until its process dies or it
  says goodbye. Every record carries the ``pid`` of the process that reported
  it - the ``claude`` process itself - so "is this session still open?" is a
  question with a definitive answer rather than a guess made from silence. A
  hook only fires on activity, so a clock cannot tell *quiet* from *gone*; a
  pid can. The TTL survives only as the backstop for records we cannot vouch
  for (no pid, a pid we cannot verify, a machine that was force-powered-off),
  and it can only ever *end* a record's life, never extend it.
* **Terminal identity is captured, not asked for.** A hook runs as a child of
  the Claude Code process, so the hook process *is* standing in the session's
  terminal - even though, as it turns out, it cannot simply read the tty off
  itself (see :func:`current_terminal`). Recording it at ``update()`` time is
  the only moment it is available - by the time a key is pressed, the pressing
  process knows nothing about any terminal.

The engine has no idea what a "renderer" or a "hook" is. It is pure state.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass, field, fields, replace
from enum import Enum
from pathlib import Path
from typing import Callable, Dict, Optional, Tuple


class AgentState(str, Enum):
    """The normalized states every agent is mapped onto.

    Inherits from :class:`str` so values serialize straight to JSON and
    compare equal to their names (``AgentState.WORKING == "working"``).
    """

    IDLE = "idle"
    WORKING = "working"
    WAITING = "waiting"
    DONE = "done"
    ERROR = "error"

    @property
    def priority(self) -> int:
        """Higher wins when several sessions are active at once."""
        return _PRIORITY[self]

    @property
    def needs_you(self) -> bool:
        """True for states where the human is the blocker."""
        return self in (AgentState.WAITING, AgentState.ERROR, AgentState.DONE)


# waiting > error > done > working > idle  (see module docstring)
_PRIORITY: dict[AgentState, int] = {
    AgentState.IDLE: 0,
    AgentState.WORKING: 1,
    AgentState.DONE: 2,
    AgentState.ERROR: 3,
    AgentState.WAITING: 4,
}

#: The backstop that removes a record we cannot vouch for another way.
#:
#: This used to be the *only* rule, and it was wrong: a hook fires on activity,
#: so half an hour of silence looked exactly like a closed terminal, and four
#: projects worked on across a day would lose their Agent Keys one by one while
#: their terminals sat open. Liveness by pid answers that question properly
#: (:class:`ProcessLiveness`), and this timer now applies only where liveness
#: has nothing to say: a record with no usable pid (written by ``freemicro
#: emit``, or by a build old enough not to have captured one), a pid whose
#: process is gone, or a pid we cannot prove is still the original.
#:
#: It is deliberately *not* longer than it was. Its whole remaining job is to
#: stop unvouchable records piling up on disk after a crash or a hard power
#: cut, and lengthening it would only make that pile last longer.
DEFAULT_TTL_SECONDS = 30 * 60

#: How long a finished session keeps showing DONE before decaying to IDLE.
#:
#: The factory pad's green means *unread*, not *completed* - it clears when you
#: look at the session. Without a decay the pad simply sits green forever after
#: the first task, which stops matching the hardware's own behaviour within
#: minutes. 180s mirrors the vendor's auto-dim timeout.
DEFAULT_DONE_TTL_SECONDS = 180.0

#: How long a session may claim ``working`` in silence before we stop believing
#: it.
#:
#: Claude Code fires **no hook at all** when you press Escape to interrupt it
#: (confirmed against 152 captured payloads, not inferred). The last thing the
#: session said was ``working``, so without this the pad keeps the key blue -
#: "still thinking" - until the session TTL expires half an hour later. A
#: status light nobody believes is worse than no status light.
#:
#: ``prompt_id`` (see :class:`SessionSignals`) settles the same question
#: *exactly* - but only once another event arrives, and after an interrupt no
#: event ever does. Elapsed time is therefore the only thing that can retire an
#: abandoned turn, and this is that timer.
#:
#: The threshold comes from what working actually looks like. A measured
#: session emitted ``UserPromptSubmit``/``PreToolUse``/``PostToolUse`` at 0.3,
#: 9.7, 14.3, 20.7, 28.6, 29.8, 35.9, 36.2, 37.1, 38.0 and 42.3 seconds - a
#: heartbeat every few seconds. The legitimate long silences are a running tool
#: call and a running background task, and both are detected outright below, so
#: this timer only has to cover "quiet while nothing is running". 120s is over
#: ten times the observed gap and matches Claude Code's own default Bash
#: timeout, which is the longest silence an *unannounced* tool call can cause.
DEFAULT_WORKING_TTL_SECONDS = 120.0

#: The grace given instead when the last thing we heard was a tool *starting*.
#:
#: Between ``PreToolUse`` and ``PostToolUse`` there is nothing to emit: a five
#: minute build is silent and genuinely working, and blanking its key would be
#: the opposite lie. 600s is Claude Code's maximum Bash timeout, i.e. the
#: longest a single tool call can legitimately keep us in the dark.
DEFAULT_TOOL_TTL_SECONDS = 600.0

#: Hook events after which silence is *expected* rather than suspicious.
#:
#: The sharp distinction a timeout alone cannot make: silence after
#: ``PostToolUse`` means the agent stopped between tools, while the same
#: silence after ``PreToolUse`` means a tool is still running.
TOOL_START_EVENTS = frozenset({"PreToolUse", "PreCompact"})

#: Hook events that close a turn. A turn that never sees one of these, and is
#: then replaced by a new ``prompt_id``, was abandoned - i.e. interrupted.
TURN_CLOSING_EVENTS = frozenset({"Stop", "StopFailure", "SessionEnd"})


def _as_seconds(value: object, default: float) -> float:
    """``value`` as a sane number of seconds, or ``default``.

    A duration read out of a JSON file is whatever the user typed, and a
    ``null``, a stray string or an infinity must not decide how long a light
    stays on; those get the documented default. A *negative* number is
    different - it is a number, and clamping it to zero is what the timers did
    before this class existed (``max(0.0, ...)``), which reads as "switch the
    check off" rather than "expired before it started".
    """
    try:
        seconds = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return float(default)
    if seconds != seconds or seconds in (float("inf"), float("-inf")):
        return float(default)
    return max(0.0, seconds)


@dataclass(frozen=True)
class DecayPolicy:
    """Every clock that decides how long a session's claim is believed.

    One value rather than four loose floats, and that is the entire point.
    These timers only mean anything *together*: a caller holding three of them
    is a caller that will retire a ``working`` claim the store still believes,
    which is how the pad and ``freemicro status`` ended up contradicting each
    other inside a single process. Four floats can be passed on in fifteen
    wrong combinations; a policy has one.

    Adding a fifth timer next year is the case this shape is designed for. Add
    the field here and to :class:`freemicro.config.Config`, and every reader
    already has it: :meth:`from_config` reads whatever fields this class
    declares rather than a hand-written list, and everything downstream
    (:func:`claim_ttl`, :func:`effective_state`, :class:`StateStore`,
    :class:`freemicro.agentkeys.SlotResolver`) passes the policy on whole. The
    only way to forget the new timer is to delete the field again.

    Instances are frozen and shared: :data:`DEFAULT_DECAY` is the documented
    factory policy, and is safe as a default argument for exactly that reason.
    """

    #: The backstop for records nothing can vouch for. See
    #: :data:`DEFAULT_TTL_SECONDS` - liveness, not this, decides the ordinary
    #: case.
    ttl_seconds: float = DEFAULT_TTL_SECONDS
    #: How long ``done`` shows before decaying to ``idle``. ``0`` = forever.
    done_ttl_seconds: float = DEFAULT_DONE_TTL_SECONDS
    #: How long a silent ``working`` claim is believed. ``0`` = until the
    #: session TTL, i.e. the check is off.
    working_ttl_seconds: float = DEFAULT_WORKING_TTL_SECONDS
    #: The same, while a tool call is known to be running. ``0`` = off.
    tool_ttl_seconds: float = DEFAULT_TOOL_TTL_SECONDS

    def __post_init__(self) -> None:
        for spec in fields(self):
            object.__setattr__(
                self,
                spec.name,
                _as_seconds(getattr(self, spec.name), spec.default),
            )

    @classmethod
    def names(cls) -> Tuple[str, ...]:
        """The timers this policy holds, in declaration order."""
        return tuple(spec.name for spec in fields(cls))

    @classmethod
    def from_config(cls, config: object) -> "DecayPolicy":
        """Read *every* timer this policy has off ``config``, by name.

        Reflective on purpose. The bug this class exists to end was written by
        hand, four times, as a list of ``x=config.x`` lines that each stopped
        one or two short. Nothing here lists the fields, so nothing here can
        list them incompletely: a timer added to both classes is picked up with
        no further edits, and a config that has never heard of a timer (an old
        one, a stub in a test) gets its documented default rather than a crash.
        """
        return cls(
            **{
                name: getattr(config, name, getattr(DEFAULT_DECAY, name))
                for name in cls.names()
            }
        )


#: The factory policy: what every timer means when nobody has said otherwise.
DEFAULT_DECAY = DecayPolicy()


@dataclass(frozen=True)
class TerminalInfo:
    """Where a session is *physically* running, as best we can tell.

    Every field is optional and every field is a guess that may be wrong on
    some setup, so nothing here is ever required to be present. What it buys is
    the ability to bring a session's terminal to the front (see
    :mod:`freemicro.focus`) instead of typing into whatever happens to be
    focused.

    ``tty`` is by far the strongest signal: Terminal.app exposes ``tty`` on
    every tab and iTerm2 on every session, so a tty match identifies an exact
    tab rather than "one of the windows of some terminal app".
    """

    #: Controlling terminal device, e.g. ``/dev/ttys004``. Empty when the
    #: reporting process had none (a launchd agent, a CI runner, a GUI client).
    tty: str = ""
    #: PID of the process that reported - the hook's parent, i.e. Claude Code.
    pid: int = 0
    #: ``TERM_PROGRAM``: ``Apple_Terminal``, ``iTerm.app``, ``vscode``, ``ghostty``…
    program: str = ""
    #: ``ITERM_SESSION_ID`` / ``TERM_SESSION_ID`` - a per-tab identifier some
    #: emulators export. Kept for future use; the tty is what we match on.
    session: str = ""

    @property
    def empty(self) -> bool:
        return not (self.tty or self.program or self.session)


@dataclass(frozen=True)
class SessionSignals:
    """What a hook event said about the session, beyond its state.

    The engine deliberately knows nothing about hook payloads - the field names
    live in :mod:`freemicro.state.hooks`, which parses one of these out of an
    event. What the engine knows is what each signal *means* for how long a
    claim is believed, which is the question :func:`claim_ttl` answers.

    Every field is optional. A caller that passes nothing gets exactly the
    behaviour that existed before any of this: a plain timer.
    """

    #: ``hook_event_name`` - ``UserPromptSubmit``, ``PreToolUse``, ``Stop``…
    event: str = ""
    #: The turn this event belongs to. Stable across a whole turn, so a *new*
    #: one arriving before the old one was closed is proof of an interrupt.
    prompt_id: str = ""
    #: ``permission_mode``: ``default``, ``bypassPermissions``, ``plan``…
    #: A session in ``bypassPermissions`` never prompts, so it is never
    #: expected to go amber.
    permission_mode: str = ""
    #: ``effort.level`` - the session's reasoning effort, for the dial.
    effort: str = ""
    #: How many ``background_tasks`` are ``running``. A session with one is
    #: still working even when its own turn has stopped.
    background_tasks: int = 0
    #: ``reason`` on ``SessionEnd``: ``exit``, ``clear``, ``other``. ``clear``
    #: is a ``/clear``, which is *not* the session going away.
    end_reason: str = ""

    @property
    def closes_turn(self) -> bool:
        return self.event in TURN_CLOSING_EVENTS

    @property
    def empty(self) -> bool:
        return not (
            self.event
            or self.prompt_id
            or self.permission_mode
            or self.effort
            or self.background_tasks
            or self.end_reason
        )


#: A device name has to look like one before it is allowed to travel any
#: further. This is the value that eventually reaches an AppleScript, so it is
#: pattern-checked at the source as well as at the point of use. Path segments
#: may not begin with a dot, which is what keeps ``..`` out: ``/dev`` has real
#: subdirectories (``/dev/pts/3``) but nothing above it is a terminal.
_TTY_NAME_RE = re.compile(
    r"^[A-Za-z0-9_-][A-Za-z0-9._-]*(?:/[A-Za-z0-9_-][A-Za-z0-9._-]*)*$"
)

#: ``ps`` answers about a local process instantly; the timeout exists only so a
#: wedged system cannot hang a hook or a key press.
PS_TIMEOUT_SECONDS = 2.0

#: How far up the process tree to look for a controlling terminal. A hook is a
#: grandchild of the shell at worst, so this is generous; it exists to bound the
#: walk rather than to be reached.
MAX_TTY_HOPS = 8


def normalise_tty(value: object) -> str:
    """Return ``value`` as ``/dev/ttysNNN``, or ``""`` if it is not a device.

    The two sources disagree about spelling: ``os.ttyname`` gives the full path
    while ``ps`` prints a bare ``ttys003`` and ``??`` for a process with no
    controlling terminal. Terminal.app and iTerm2 both report the full path, so
    that is the form everything downstream is normalised to.
    """
    name = str(value or "").strip()
    if not name or name.startswith("?"):
        return ""
    if name.startswith("/dev/"):
        name = name[len("/dev/"):]
    if not name or len(name) > 59 or not _TTY_NAME_RE.match(name):
        return ""
    return "/dev/" + name


def ps_tty_and_parent(pid: int) -> Tuple[str, int]:
    """``(tty, ppid)`` for ``pid`` according to ``ps``. Never raises.

    One ``ps`` call answers both questions, which is what makes walking up the
    process tree cheap. A pid that has already exited makes ``ps`` exit
    non-zero with no output; that is a normal answer here - ``("", 0)`` - not
    an error, and callers treat it as "this session has no identifiable tab".
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return ("", 0)
    if pid <= 0:
        return ("", 0)
    try:
        completed = subprocess.run(
            ["ps", "-o", "tty=,ppid=", "-p", str(pid)],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=PS_TIMEOUT_SECONDS,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return ("", 0)
    if completed.returncode != 0:
        return ("", 0)
    fields = completed.stdout.decode("utf-8", "replace").split()
    if not fields:
        return ("", 0)
    if len(fields) == 1:
        # Only one column came back. A number can only be the ppid.
        return ("", _as_int(fields[0])) if fields[0].isdigit() else (fields[0], 0)
    return (fields[0], _as_int(fields[1]))


def _as_int(value: object) -> int:
    """``value`` as a whole number, or ``0`` for anything that is not one."""
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def tty_for_pid(
    pid: int,
    *,
    probe: "Callable[[int], Tuple[str, int]]" = ps_tty_and_parent,
    max_hops: int = MAX_TTY_HOPS,
) -> str:
    """Controlling tty of ``pid``, or of its nearest ancestor that has one.

    The walk upward is the whole point. Claude Code spawns a hook with pipes
    for its standard streams *and* outside the terminal's session, so the hook
    process genuinely has no controlling terminal - ``ps`` says ``??`` for it.
    Its parent, the ``claude`` process, is the one sitting in the tab, and that
    is the process whose tty names the tab we want.

    Stops at the first ancestor that has one, so a session started from a real
    tab resolves to *that* tab and never to some unrelated login shell. Returns
    ``""`` when nothing in the chain has a terminal (launchd, cron, a
    container) or when the pid is already gone - never a guess.
    """
    seen = set()
    current = _as_int(pid)
    for _ in range(max(0, _as_int(max_hops))):
        # pid 1 is launchd, which never has a terminal and is its own ceiling.
        if current <= 1 or current in seen:
            break
        seen.add(current)
        try:
            raw_tty, parent = probe(current)
        except Exception:  # noqa: BLE001 - a lookup must never break a hook
            return ""
        tty = normalise_tty(raw_tty)
        if tty:
            return tty
        current = _as_int(parent)
    return ""


def _controlling_tty() -> str:
    """The tty of ``/dev/tty``, if this process has a controlling terminal.

    Opening ``/dev/tty`` rather than looking at ``stdin``/``stdout`` is the
    strongest form of the question: Claude Code hands a hook its JSON on a pipe
    and captures both output streams, so none of the three standard descriptors
    is a terminal. It still comes back empty inside a hook - the hook is not in
    the terminal's session at all - which is why :func:`current_terminal` has a
    second answer. Kept because it is exact when it does work (a plain
    ``freemicro`` command run by hand).
    """
    try:
        fd = os.open("/dev/tty", os.O_RDONLY | getattr(os, "O_NOCTTY", 0))
    except (OSError, AttributeError):
        return ""
    try:
        return normalise_tty(os.ttyname(fd))
    except (OSError, AttributeError):
        return ""
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def current_terminal(
    *,
    direct: "Callable[[], str]" = _controlling_tty,
    from_pid: "Callable[[int], str]" = tty_for_pid,
) -> TerminalInfo:
    """Describe the terminal *this* process is running in. Never raises.

    Two ways to answer, tried in that order:

    1. ``/dev/tty`` - exact, and empty inside a Claude Code hook.
    2. The process tree - ``ps`` on this process, then on its ancestors, until
       one of them reports a tty. Inside a hook that lands on the ``claude``
       process, which is the one running in the tab.

    Only the second one fires in practice, and it is the reason an Agent Key
    can select a tab at all: without it every record on disk had ``tty: ""``
    and every key press degraded to "activate the app, tab unknown".

    Everything is best effort. Nothing in the chain having a terminal (launchd,
    cron, a container) simply yields a :class:`TerminalInfo` with no tty.

    The injection points are for tests: a suite must not depend on whether the
    developer ran it from a terminal, and must never shell out.
    """
    try:
        tty = direct()
    except Exception:  # noqa: BLE001 - never fail a hook over metadata
        tty = ""
    if not tty:
        try:
            tty = from_pid(os.getpid())
        except Exception:  # noqa: BLE001
            tty = ""
    environ = os.environ
    return TerminalInfo(
        tty=normalise_tty(tty),
        pid=os.getppid(),
        program=environ.get("TERM_PROGRAM", ""),
        session=(
            environ.get("ITERM_SESSION_ID") or environ.get("TERM_SESSION_ID") or ""
        ),
    )


# ---------------------------------------------------------------------------
# Is the process that reported this session still running?
#
# The one question that separates "quiet" from "gone", and the reason a session
# no longer evaporates because you spent an hour in another repo.
# ---------------------------------------------------------------------------

#: How long a cheap liveness answer is reused.
#:
#: The renderers poll at 4 Hz and read every slot on every tick. One syscall per
#: pid per second is a probe; six per frame is a habit worth not forming.
DEFAULT_LIVENESS_CACHE_SECONDS = 1.0

#: How long a process's start time is reused once we have paid for it.
#:
#: Escalating to ``ps`` is the only expensive thing in this file, and the answer
#: it gives - when a process started - cannot change while that process is
#: alive. Re-asking is only ever about noticing that the pid changed hands.
DEFAULT_IDENTITY_CACHE_SECONDS = 60.0

#: Slack allowed when comparing a process's start time against the record it
#: wrote. ``ps`` reports elapsed time truncated to the second, so a start time
#: derived from it can land a shade *after* the real one; without a tolerance a
#: session whose very first hook fired within a second of launch could fail its
#: own identity check.
PID_START_TOLERANCE_SECONDS = 5.0


def pid_alive(pid: int) -> bool:
    """Is ``pid`` a live process? One signal-free ``kill``, no subprocess.

    ``PermissionError`` means the process exists and simply is not ours to
    signal, which is still *alive* - answering "no" there would quietly declare
    every session started under another account dead.

    Pids at or below 1 are refused outright: ``0`` means "my whole process
    group" and a negative pid means "some other group", so asking about either
    would answer a completely different question and answer it "yes". ``1`` is
    launchd, which is always alive and is never a Claude Code session.
    """
    pid = _as_int(pid)
    if pid <= 1:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def parse_elapsed(text: str) -> "Optional[float]":
    """``[[dd-]hh:]mm:ss`` - BSD ``ps``'s elapsed time - as seconds, or ``None``.

    ``etime`` rather than ``lstart`` on purpose: ``lstart`` prints month and day
    names, which are locale-dependent, and a status light that works only under
    ``LC_TIME=C`` is not a status light. ``etimes`` would be simpler still and
    is Linux-only - macOS ``ps`` refuses the whole command when it sees it.
    """
    text = str(text or "").strip()
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
        values = [float(part) for part in parts]
    except ValueError:
        return None
    seconds = 0.0
    for value in values:
        seconds = seconds * 60 + value
    return seconds + days * 86400


def pid_started(pid: int, *, now: "Optional[float]" = None) -> "Optional[float]":
    """When ``pid`` started, in epoch seconds, or ``None``. Never raises.

    Derived from elapsed time rather than read directly, because that is the
    form ``ps`` will give us in every locale. ``None`` means the question could
    not be answered - the process is gone, or ``ps`` is unavailable - and every
    caller treats that as "cannot vouch", never as an error.
    """
    pid = _as_int(pid)
    if pid <= 1:
        return None
    when = time.time() if now is None else now
    try:
        completed = subprocess.run(
            ["ps", "-o", "etime=", "-p", str(pid)],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=PS_TIMEOUT_SECONDS,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    elapsed = parse_elapsed(completed.stdout.decode("utf-8", "replace"))
    if elapsed is None:
        return None
    return when - elapsed


@dataclass
class ProcessLiveness:
    """Answers "is the process that wrote this record still running?".

    Two questions at two prices, asked in that order:

    1. **Is the pid alive?** ``os.kill(pid, 0)`` - a syscall, no fork, cached
       for a second so a 4 Hz render loop reading six slots does not turn into
       twenty-four syscalls a second.
    2. **Is it still the same process?** Only asked when a pid looks alive but
       the record it belongs to has been silent long enough to be suspicious.
       Pids are recycled, and a recycled pid must not resurrect a dead session.

    The reuse test needs nothing stored alongside the pid, because the record
    already carries the proof: whichever process wrote it was holding that pid
    at ``updated_at``. So a process that *started after* the record was written
    cannot be the one that wrote it - it can only be a later tenant of the same
    number. ``started <= updated_at`` is therefore exact, works for records
    written by older builds, and survives a reboot (everything post-boot starts
    after every pre-boot record).

    Every probe is injectable so the tests never touch a real process, and a
    test that skips the injection does not merely lose coverage - it inverts
    the answer. A fake clock starting at ``1000.0`` dates its records to 1970,
    which is before every process on the machine, so the reuse test judges a
    perfectly live pid *recycled* and the record is pruned. That looks like the
    old TTL working and is not. Inject the probe, or use a plausible epoch.
    """

    #: Cheap liveness, ``pid -> bool``.
    alive_probe: "Callable[[int], bool]" = pid_alive
    #: Expensive identity, ``pid -> epoch seconds or None``.
    started_probe: "Callable[[int], Optional[float]]" = pid_started
    clock: "Callable[[], float]" = time.time
    cache_seconds: float = DEFAULT_LIVENESS_CACHE_SECONDS
    identity_cache_seconds: float = DEFAULT_IDENTITY_CACHE_SECONDS
    tolerance: float = PID_START_TOLERANCE_SECONDS
    _alive: "Dict[int, Tuple[float, bool]]" = field(
        default_factory=dict, init=False, repr=False
    )
    _started: "Dict[int, Tuple[float, Optional[float]]]" = field(
        default_factory=dict, init=False, repr=False
    )

    def alive(self, pid: int) -> bool:
        """Is ``pid`` alive? Cached, so repeat callers in a tick are free."""
        pid = _as_int(pid)
        if pid <= 1:
            return False
        now = self.clock()
        cached = self._alive.get(pid)
        if cached is not None and (now - cached[0]) < self.cache_seconds:
            return cached[1]
        try:
            answer = bool(self.alive_probe(pid))
        except Exception:  # noqa: BLE001 - a probe must never break a render
            answer = False
        self._alive[pid] = (now, answer)
        if not answer:
            # A pid that has gone away invalidates anything we knew about it;
            # whoever holds that number next is a different process.
            self._started.pop(pid, None)
        return answer

    def started(self, pid: int) -> "Optional[float]":
        """When ``pid`` started, cached for a minute. ``None`` if unknowable."""
        pid = _as_int(pid)
        if pid <= 1:
            return None
        now = self.clock()
        cached = self._started.get(pid)
        if cached is not None and (now - cached[0]) < self.identity_cache_seconds:
            return cached[1]
        try:
            answer = self.started_probe(pid)
        except Exception:  # noqa: BLE001 - a probe must never break a render
            answer = None
        self._started[pid] = (now, answer)
        return answer

    def verdict(self, session: "SessionState", *, verify: bool) -> "Optional[bool]":
        """Is ``session``'s process still running? Tri-state, never raises.

        ``True``
            Yes - and, when ``verify`` is set, provably the same process that
            wrote the record rather than a later tenant of its pid.
        ``False``
            No: the process is gone, or the pid has changed hands.
        ``None``
            The record names no pid we can ask about, or we could not prove
            what we needed to. Callers fall back to the TTL, which is exactly
            the behaviour that existed before any of this.

        ``verify`` is the escalation switch: paying for a ``ps`` on every
        record on every tick would be absurd, and a pid that was heard from
        moments ago has not had time to be recycled. It is set only once a
        record has been quiet long enough for the question to be live.
        """
        pid = _as_int(session.pid)
        if pid <= 1:
            return None
        if not self.alive(pid):
            return False
        if not verify:
            return True
        started = self.started(pid)
        if started is None:
            return None
        return started <= session.updated_at + self.tolerance

    def forget(self) -> None:
        """Drop every cached answer - the next question is asked afresh."""
        self._alive.clear()
        self._started.clear()


#: The process-wide liveness cache. Shared on purpose: "is pid 4242 alive?" is
#: a question about the machine, not about a store, and several callers build a
#: throwaway :class:`StateStore` on every poll (the menu bar does, twice). A
#: cache that died with the store would be no cache at all, and the ``ps`` half
#: of the check would run several times a second for every quiet project.
_SHARED_LIVENESS = ProcessLiveness()


def default_liveness() -> ProcessLiveness:
    """The liveness probe a :class:`StateStore` uses unless given another."""
    return _SHARED_LIVENESS


@dataclass(frozen=True)
class SessionState:
    """An immutable snapshot of one session's state."""

    session_id: str
    state: AgentState
    updated_at: float
    title: str = ""
    cwd: str = ""
    #: Terminal identity, captured by whichever process called ``update()``.
    tty: str = ""
    pid: int = 0
    term_program: str = ""
    term_session: str = ""
    #: The hook event this record was written for (``PreToolUse``…), when the
    #: caller knew it. Empty is fine and common; it only sharpens how long a
    #: ``working`` claim is believed. See :func:`effective_state`.
    last_event: str = ""
    #: The turn (``prompt_id``) this record belongs to.
    prompt_id: str = ""
    #: True while that turn is still open - nothing has closed it yet.
    turn_open: bool = False
    #: True when the *previous* turn was abandoned: a new ``prompt_id`` arrived
    #: while the old one was still open, which only an interrupt can cause.
    interrupted: bool = False
    #: ``permission_mode`` of the session, e.g. ``bypassPermissions``.
    permission_mode: str = ""
    #: Reasoning effort level, for the dial.
    effort: str = ""
    #: Background tasks (subagents) still running at the last event.
    background_tasks: int = 0
    #: Set by :meth:`StateStore.sessions` when ``state`` is *not* what the
    #: session last claimed - the claim expired and was retired. Never
    #: persisted: it is a reading of the record, not part of it.
    claimed_state: "Optional[AgentState]" = None
    #: Set by :meth:`StateStore.sessions`: is the process that reported this
    #: session still running? ``None`` when the record names no pid we can ask
    #: about - an older record, or one written outside a terminal. Never
    #: persisted: it is a reading of the *world*, not of the record.
    process_alive: "Optional[bool]" = None
    #: True when this record has been silent past the session TTL and is still
    #: here only because its process is provably alive. The difference between
    #: "idle, still open" and "gone", in one flag. Never persisted.
    kept_by_process: bool = False

    @property
    def claim(self) -> AgentState:
        """What the session last said about itself, expiry ignored."""
        return self.claimed_state if self.claimed_state is not None else self.state

    @property
    def stale(self) -> bool:
        """True when this session's own claim has been retired as too old."""
        return self.claimed_state is not None and self.claimed_state != self.state

    @property
    def tool_running(self) -> bool:
        """True when the last thing we heard was a tool call *starting*."""
        return self.last_event in TOOL_START_EVENTS

    @property
    def busy_elsewhere(self) -> bool:
        """True while a background task (a subagent) is still running.

        Such a session is not idle even when its own turn has stopped, so its
        claim is not allowed to expire on a timer.
        """
        return self.background_tasks > 0

    @property
    def prompts_for_permission(self) -> bool:
        """False for a session that can never go amber - it never asks."""
        return self.permission_mode != "bypassPermissions"

    @property
    def process_gone(self) -> bool:
        """True when the process that reported this session is known to be gone.

        Distinct from "we did not ask" and from "we have no pid to ask about",
        both of which read as ``None`` on :attr:`process_alive`.
        """
        return self.process_alive is False

    def describe_claim(self) -> str:
        """One honest phrase for ``freemicro status`` and the web UI."""
        text = self._describe_state()
        if self.kept_by_process:
            # The distinction the owner needs at a glance: this project has not
            # been touched in a long time, and its terminal is still sitting
            # there. Quiet is not gone.
            return f"{text} - terminal still open"
        return text

    def _describe_state(self) -> str:
        if self.stale:
            claimed = self.claim.value
            if self.interrupted:
                return f"idle (was {claimed}; last turn was interrupted)"
            return f"idle (was {claimed}, and went quiet)"
        if self.state == AgentState.WORKING and self.busy_elsewhere:
            count = self.background_tasks
            return f"working ({count} background task{'' if count == 1 else 's'})"
        return self.state.value

    @property
    def terminal(self) -> TerminalInfo:
        """The terminal fields as one value object."""
        return TerminalInfo(
            tty=self.tty,
            pid=self.pid,
            program=self.term_program,
            session=self.term_session,
        )

    def to_dict(self) -> dict:
        """The record as JSON. Also what ``freemicro status --json`` prints.

        The liveness reading is included *only when something asked for it*, so
        the form written to disk by :meth:`StateStore.update` is byte-for-byte
        what it has always been - a reading of the world has no business being
        persisted, and a record loaded tomorrow must not claim yesterday's
        answer.
        """
        data = {
            "session_id": self.session_id,
            "state": self.state.value,
            "updated_at": self.updated_at,
            "title": self.title,
            "cwd": self.cwd,
            "tty": self.tty,
            "pid": self.pid,
            "term_program": self.term_program,
            "term_session": self.term_session,
            "last_event": self.last_event,
            "prompt_id": self.prompt_id,
            "turn_open": self.turn_open,
            "interrupted": self.interrupted,
            "permission_mode": self.permission_mode,
            "effort": self.effort,
            "background_tasks": self.background_tasks,
        }
        if self.process_alive is not None:
            data["process_alive"] = self.process_alive
        if self.kept_by_process:
            data["kept_by_process"] = True
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "SessionState":
        # Terminal fields are read defensively: records written by an older
        # build simply do not have them, and must still load.
        try:
            pid = int(data.get("pid") or 0)
        except (TypeError, ValueError):
            pid = 0
        return cls(
            session_id=str(data["session_id"]),
            state=AgentState(data["state"]),
            updated_at=float(data["updated_at"]),
            title=str(data.get("title", "")),
            cwd=str(data.get("cwd", "")),
            tty=str(data.get("tty", "")),
            pid=pid,
            term_program=str(data.get("term_program", "")),
            term_session=str(data.get("term_session", "")),
            last_event=str(data.get("last_event", "")),
            prompt_id=str(data.get("prompt_id", "")),
            turn_open=bool(data.get("turn_open", False)),
            interrupted=bool(data.get("interrupted", False)),
            permission_mode=str(data.get("permission_mode", "")),
            effort=str(data.get("effort", "")),
            background_tasks=_as_int(data.get("background_tasks", 0)),
        )

    def age(self, now: float | None = None) -> float:
        return (now if now is not None else time.time()) - self.updated_at


# ---------------------------------------------------------------------------
# Do we still believe what this record says?
# ---------------------------------------------------------------------------

def claim_ttl(
    session: SessionState,
    *,
    decay: DecayPolicy = DEFAULT_DECAY,
) -> float:
    """How long ``session``'s claim is believed, in seconds. ``0`` = forever.

    Only two states expire, for quite different reasons: ``done`` is an
    *unread* marker that clears itself, and ``working`` is a claim that has to
    keep being renewed because the event that would withdraw it does not exist.
    ``waiting`` and ``error`` need you and stay put until the session TTL
    removes them entirely.

    A session with a background task still running never expires on a timer:
    its own turn may have stopped, but a subagent is still working, and going
    quiet is exactly what that looks like from the hook stream.

    ``0`` from any of the timers means the same thing here as it does in the
    config file: this claim never expires on the clock. What is left is the
    session TTL, which only ever fires for a record no live process vouches
    for - so ``working_ttl_seconds: 0`` really is "switch the check off".
    """
    if session.busy_elsewhere:
        return 0.0
    if session.state == AgentState.DONE:
        return decay.done_ttl_seconds
    if session.state == AgentState.WORKING:
        if session.tool_running:
            return decay.tool_ttl_seconds
        return decay.working_ttl_seconds
    return 0.0


def effective_state(
    session: SessionState,
    *,
    now: float,
    decay: DecayPolicy = DEFAULT_DECAY,
) -> AgentState:
    """``session``'s state once its claim has been allowed to expire.

    The single implementation of "what is this session really doing", used by
    :meth:`StateStore.sessions` - and therefore by the resolved state, the
    Agent Keys, the renderers and ``freemicro status`` alike. Somewhere in the
    codebase disagreeing about this is how the pad ends up showing one thing
    and the status command another.

    Both expiries land on ``idle``: nothing is known to be happening.
    """
    ttl = claim_ttl(session, decay=decay)
    if ttl > 0 and (now - session.updated_at) > ttl:
        return AgentState.IDLE
    return session.state


def as_read(
    session: SessionState,
    *,
    now: float,
    decay: DecayPolicy = DEFAULT_DECAY,
) -> SessionState:
    """``session`` with its expired claim retired, and the claim remembered.

    The record keeps ``claimed_state`` so a reader can say *why* a session went
    quiet ("idle, was working") instead of silently reporting either half of
    that as the whole truth.
    """
    state = effective_state(session, now=now, decay=decay)
    if state == session.state:
        return session
    return replace(session, state=state, claimed_state=session.state)


@dataclass(frozen=True)
class _Turn:
    """Where a session's turn stands: which one, open, and how the last ended."""

    prompt_id: str = ""
    turn_open: bool = False
    interrupted: bool = False


@dataclass
class StateStore:
    """Reads and writes per-session state files and resolves the winner.

    Parameters
    ----------
    directory:
        Where session JSON files live. Defaults to ``~/.freemicro/state``.
    decay:
        Every timer that governs how a claim expires, as one
        :class:`DecayPolicy`. There is deliberately no way to pass the timers
        individually: a store built from the user's config with two of them
        filled in is precisely the bug this shape exists to prevent, and it
        stayed invisible for as long as it did because each call site looked
        reasonable on its own. Build one from a config with
        :meth:`DecayPolicy.from_config` - or, better, do not build a store at
        all and call :func:`default_store`.
    clock:
        Injectable time source (``callable() -> float``) for tests.
    terminal_probe:
        How ``update()`` learns which terminal it is running in. Defaults to
        :func:`current_terminal`; tests inject a stub so a developer's real tty
        never ends up in a fixture.
    liveness:
        How ``sessions()`` learns whether a session's process is still running.
        Defaults to the process-wide :func:`default_liveness` cache, so a
        caller that rebuilds its store on every poll still pays for a probe
        only once per cache window; tests inject a fake so nothing here depends
        on a real process.
    """

    directory: Path = field(
        default_factory=lambda: Path.home() / ".freemicro" / "state"
    )
    decay: DecayPolicy = DEFAULT_DECAY
    clock: "callable" = time.time
    terminal_probe: "Callable[[], TerminalInfo]" = current_terminal
    liveness: ProcessLiveness = field(default_factory=default_liveness)

    def __post_init__(self) -> None:
        if not isinstance(self.decay, DecayPolicy):
            raise TypeError(
                "StateStore(decay=...) takes a DecayPolicy, not "
                f"{type(self.decay).__name__}. The individual TTLs are no "
                "longer separate arguments - build the policy with "
                "DecayPolicy.from_config(config), or call default_store()."
            )
        self.directory = Path(self.directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    # -- writing ---------------------------------------------------------

    def _path_for(self, session_id: str) -> Path:
        # Keep filenames filesystem-safe regardless of what a session id is.
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in session_id)
        return self.directory / f"{safe or 'default'}.json"

    def update(
        self,
        session_id: str,
        state: AgentState,
        *,
        title: str = "",
        cwd: str = "",
        terminal: "Optional[TerminalInfo]" = None,
        signals: "Optional[SessionSignals]" = None,
    ) -> SessionState:
        """Record ``state`` for ``session_id`` and return the snapshot.

        ``terminal`` defaults to probing the *calling* process, which is the
        point: this runs inside a Claude Code hook, so the caller is sitting in
        exactly the terminal we later want to raise. Pass an explicit (possibly
        empty) :class:`TerminalInfo` to suppress the probe.

        ``signals`` carries what else the hook event said (see
        :class:`SessionSignals`). It is optional so an ordinary
        ``freemicro emit`` still works, and it is what turns the record from
        "state plus a timestamp" into something that can say *why* it looks the
        way it does.
        """
        if terminal is None:
            try:
                terminal = self.terminal_probe()
            except Exception:  # noqa: BLE001 - never fail a hook over metadata
                terminal = TerminalInfo()
        signals = signals or SessionSignals()
        turn = self._turn_after(session_id, signals)
        record = SessionState(
            session_id=session_id,
            state=state,
            updated_at=self.clock(),
            title=title,
            cwd=cwd,
            tty=terminal.tty,
            pid=terminal.pid,
            term_program=terminal.program,
            term_session=terminal.session,
            last_event=signals.event,
            prompt_id=turn.prompt_id,
            turn_open=turn.turn_open,
            interrupted=turn.interrupted,
            permission_mode=signals.permission_mode,
            effort=signals.effort,
            background_tasks=max(0, int(signals.background_tasks)),
        )
        path = self._path_for(session_id)
        # Atomic write: temp file in the same dir, then replace.
        fd, tmp = tempfile.mkstemp(dir=self.directory, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(record.to_dict(), fh)
            os.replace(tmp, path)
        except BaseException:
            # Best effort cleanup; never leak a temp file on error.
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return record

    def clear(self, session_id: str) -> None:
        """Remove a session's record (used by ``SessionEnd``).

        Idempotent on purpose: ``SessionEnd`` was observed firing twice for one
        session, and the second one must be a no-op rather than an error.
        """
        try:
            self._path_for(session_id).unlink()
        except FileNotFoundError:
            pass

    def _turn_after(self, session_id: str, signals: SessionSignals) -> "_Turn":
        """Where the session's *turn* stands once ``signals`` is applied.

        This is the exact half of interrupt detection. Every event carries the
        ``prompt_id`` of the turn it belongs to, and a turn ends with ``Stop``.
        So a **new** ``prompt_id`` arriving while the previous one was still
        open can only mean the previous turn was abandoned - the user pressed
        Escape and typed something else. No timer, no guessing.

        What it cannot do is fire on its own: after an interrupt Claude Code
        emits *nothing*, so this only learns the truth when the next turn
        starts. Retiring a light while the user is away is still the timer's
        job (:func:`claim_ttl`).
        """
        previous = self.read(session_id)
        prompt_id = signals.prompt_id or (previous.prompt_id if previous else "")
        if signals.closes_turn:
            return _Turn(prompt_id=prompt_id, turn_open=False, interrupted=False)
        if previous is None:
            return _Turn(prompt_id=prompt_id, turn_open=bool(prompt_id))
        same_turn = bool(prompt_id) and prompt_id == previous.prompt_id
        abandoned = (
            previous.turn_open
            and bool(signals.prompt_id)
            and bool(previous.prompt_id)
            and signals.prompt_id != previous.prompt_id
        )
        return _Turn(
            prompt_id=prompt_id,
            turn_open=bool(prompt_id) or previous.turn_open,
            # Carried through the turn it happened in, so ``status`` can still
            # say "the last turn was interrupted"; a clean ``Stop`` clears it.
            interrupted=bool(abandoned or (same_turn and previous.interrupted)),
        )

    def read(self, session_id: str) -> "Optional[SessionState]":
        """The record on disk for ``session_id``, or ``None``. Never raises."""
        try:
            data = json.loads(
                self._path_for(session_id).read_text(encoding="utf-8")
            )
            return SessionState.from_dict(data)
        except (OSError, ValueError, KeyError):
            return None

    # -- reading ---------------------------------------------------------

    def sessions(
        self, *, include_stale: bool = False, decay: bool = True
    ) -> list[SessionState]:
        """Return all session snapshots, freshest first.

        **A session lives until its process dies or it says goodbye.** Silence
        alone removes nothing: a hook fires on activity, so a project you have
        not touched since lunch looks exactly like one whose terminal you
        closed - to a clock. To a pid it does not, and every record carries the
        pid of the ``claude`` process that wrote it. A record that has been
        quiet past the TTL is kept when that process is provably still running,
        and removed when it is gone, was recycled, or cannot be identified at
        all. That last case is the old behaviour, unchanged, for the records
        that cannot do better: no pid, or a machine that will not tell us about
        its processes.

        Liveness only ever *extends* a record's life. A pid that looks dead
        inside the TTL removes nothing, because not every writer of a record is
        a long-lived session (``freemicro emit`` runs from a shell that exits
        immediately), and being wrong in that direction would empty the pad.

        Expired *claims* are retired here - one place, so the resolved state,
        the Agent Keys, the renderers and ``freemicro status`` cannot disagree
        about whether a session is still working. Each record keeps what it
        claimed (``claimed_state``/``stale``) and what we found out about its
        process (``process_alive``/``kept_by_process``), so a reader can be
        honest about the difference between "idle, still open" and "gone".
        Pass ``decay=False`` for the raw records, e.g. to debug the store.
        """
        now = self.clock()
        live: list[SessionState] = []
        for path in self.directory.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                record = SessionState.from_dict(data)
            except (OSError, ValueError, KeyError):
                # A partially written or corrupt file - skip it. It will be
                # overwritten on the next update for that session.
                continue
            quiet = record.age(now) > self.decay.ttl_seconds
            # The expensive half of the liveness check is only worth paying for
            # once a record has been quiet long enough for its pid to have had
            # time to change hands.
            alive = self.liveness.verdict(record, verify=quiet)
            if not include_stale and quiet and not alive:
                # Nothing vouches for this one: opportunistic cleanup.
                try:
                    path.unlink()
                except OSError:
                    pass
                continue
            record = replace(
                record,
                process_alive=alive,
                kept_by_process=bool(quiet and alive),
            )
            live.append(self.as_read(record, now=now) if decay else record)
        live.sort(key=lambda s: s.updated_at, reverse=True)
        return live

    def as_read(
        self, record: SessionState, *, now: "Optional[float]" = None
    ) -> SessionState:
        """``record`` with this store's expiry rules applied to it."""
        return as_read(
            record,
            now=self.clock() if now is None else now,
            decay=self.decay,
        )

    def resolve(self) -> SessionState | None:
        """Return the highest-priority live session, or ``None`` if idle.

        Ties in priority are broken by recency (most recently updated wins),
        which keeps the light responsive when two agents are both working.
        Sessions arrive with expired claims already retired, so a decayed
        ``done`` or an abandoned ``working`` counts as ``idle`` here.
        """
        sessions = self.sessions()
        if not sessions:
            return None
        return max(sessions, key=lambda s: (s.state.priority, s.updated_at))

    def resolved_state(self) -> AgentState:
        """Convenience: the resolved state value, defaulting to ``IDLE``."""
        winner = self.resolve()
        return winner.state if winner else AgentState.IDLE


def default_store(config: object = None) -> StateStore:
    """**The** way to build a store from the user's configuration.

    The CLI, the hooks, the menu bar, the web UI and the renderers all read the
    same directory, and they have to read it under the same rules or they
    contradict each other in front of the user - the console line saying
    ``idle`` while the pad still shows blue, in one process, at one instant.
    That is not a hypothetical: it is what four hand-written copies of this
    construction actually did, each of them missing a different subset of the
    timers.

    So there is one copy, here, and it is two lines long because
    :class:`DecayPolicy` carries the timers as a unit. A caller that wants a
    store gets this one; a caller that wants a *different* one has to say so
    explicitly, in an obvious way, at a call site a reviewer can see.

    ``config`` is accepted for the callers that have already loaded one (the
    CLI loads it once per command and threads it through); passing nothing
    loads it. Imports are deferred because :mod:`freemicro.config` imports the
    TTL defaults from *this* module.
    """
    from freemicro.config import Config, config_home

    cfg = Config.load() if config is None else config
    return StateStore(
        directory=config_home() / "state",
        decay=DecayPolicy.from_config(cfg),
    )


def decay_of(store: object) -> DecayPolicy:
    """The policy ``store`` reads by, for code handed a store to follow.

    A reader must never pick timers off a store one at a time: that is the
    same drift as building a store with half of them, arrived at from the other
    end - the store decays a record and the reader then re-decays it under a
    different rule, and the stricter of the two wins whatever the user
    configured. Take the whole policy or take none of it.

    Anything without one - a stub in a test, a store from an older build -
    reads by the documented defaults rather than by nothing at all.
    """
    policy = getattr(store, "decay", None)
    return policy if isinstance(policy, DecayPolicy) else DEFAULT_DECAY
