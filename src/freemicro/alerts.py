"""Off-pad alerts: a sound and a macOS notification on agent state changes.

FreeMicro's whole design is that the pad is the display. That is exactly its
blind spot: the moment you look away from the pad you miss everything it is
telling you, and the states that matter most - ``waiting`` on your approval,
``error`` - are the ones you are least likely to be staring at the desk for.
This module adds two channels that reach you when the pad cannot: a short sound
and a Notification Center banner, fired on the same state transitions the LEDs
already light on.

Three properties this module is built around, none of them negotiable:

* **Off until asked.** No ``alerts`` block in ``config.json`` means no sound and
  no notification, ever. FreeMicro does not start making noise on a desk
  uninvited, the same posture the LEDs take (off until ``lights --enable``). A
  malformed or unknown block is silently "no alerts", never an error - this code
  can run inside the daemon and must not turn a typo into a broken product.

* **Never blocks the render loop.** Both channels are fire-and-forget: a
  subprocess is spawned and *not waited on*. A wedged ``osascript`` or a slow
  ``afplay`` must not be able to freeze the pad, so nothing here ever calls
  ``wait()`` or reads a pipe. The default runner is :func:`spawn`; tests inject
  their own and assert what *would* have been spawned without making a sound.

* **Debounced.** A state that flaps - ``waiting`` to ``working`` and back inside
  a second while an agent churns through permission prompts - must not
  machine-gun banners or sounds. Each channel remembers when it last fired for a
  given state and stays quiet until :data:`DEFAULT_DEBOUNCE_SECONDS` have passed.

The sound is a named macOS system sound under ``/System/Library/Sounds`` played
with ``afplay``; the notification is ``osascript -e 'display notification ...'``.
Both are already on every Mac, so this adds no dependency.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from freemicro.state.engine import AgentState

#: Where macOS keeps its built-in system sounds. A configured sound is a bare
#: name (``"Glass"``) resolved to ``<this>/<name>.aiff``.
SYSTEM_SOUNDS_DIR = Path("/System/Library/Sounds")

#: The recommended sound per state, used by ``freemicro alerts --test`` and by
#: the documentation. ``done`` gets a gentle chime; ``waiting`` and ``error``
#: get more insistent tones, because those are the ones that need you. These are
#: only *defaults for a user to copy* - nothing plays unless the config opts in.
DEFAULT_SOUNDS: Dict[str, str] = {
    AgentState.DONE.value: "Glass",
    AgentState.WAITING.value: "Ping",
    AgentState.ERROR.value: "Basso",
}

#: The states that post a banner by default. ``waiting`` and ``error`` are the
#: two where the human is the blocker, so they are the two worth interrupting
#: you for. Documented as the recommended ``notify`` list.
DEFAULT_NOTIFY: Tuple[str, ...] = (AgentState.WAITING.value, AgentState.ERROR.value)

#: How long a channel stays quiet after firing for a given state, unless the
#: config overrides it with ``alerts.debounce_seconds``. One poll of the render
#: loop is a second, so this rides out the several-tick flap of an agent moving
#: through a couple of permission prompts without re-alerting each time.
DEFAULT_DEBOUNCE_SECONDS = 8.0

#: The set of state names an alert can be configured for, so a typo in the
#: config ("wating") is dropped rather than silently arming nothing.
_VALID_STATES = frozenset(s.value for s in AgentState)


def escape_applescript(text: str) -> str:
    """Escape a string for embedding inside an AppleScript double-quoted literal.

    A deliberate local copy of the one in :mod:`freemicro.input.keys` rather than
    an import: the alert path must not reach into the input layer, and this is
    two replacements. Backslashes first (so the quote-escape's own backslash is
    not doubled), then double quotes. Anything a user might put in a project name
    or title is text here, never AppleScript.
    """
    return text.replace("\\", "\\\\").replace('"', '\\"')


def spawn(argv: Sequence[str]) -> None:
    """Start ``argv`` and return at once, without ever waiting on it.

    The default runner for a real machine, and the whole reason alerts cannot
    stall the pad: :class:`subprocess.Popen` forks and returns, so a hung
    ``osascript`` or a ten-second sound file is the child's problem, not the
    render loop's. Output is discarded - an alert has nothing to say to a
    terminal - and every error is swallowed, because a missing binary or a
    sandbox that forbids the spawn must never surface as a traceback in a loop
    that is meant to be driving lights.

    We do not keep the returned handle. These are short-lived leaf processes
    that reparent to launchd when we exit; collecting them would mean waiting,
    which is the one thing this must not do.
    """
    try:
        subprocess.Popen(  # noqa: S603 - argv is built from a fixed vocabulary
            list(argv),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
        )
    except Exception:  # noqa: BLE001 - an alert must never break the render loop
        pass


#: The runner signature: given an argv, start it fire-and-forget. Injected so a
#: test can capture the argv instead of making a sound.
Runner = Callable[[Sequence[str]], None]


def sound_path(name: str) -> Path:
    """The ``.aiff`` file a system-sound name resolves to. Not checked here.

    A bare name maps to ``/System/Library/Sounds/<name>.aiff``; a name that
    already carries a path or an extension is taken as given, so an advanced user
    can point at their own file. Existence is the caller's business - ``afplay``
    on a missing file simply plays nothing, which is the correct fire-and-forget
    failure.
    """
    candidate = Path(name)
    if candidate.suffix or candidate.parent != Path("."):
        return candidate
    return SYSTEM_SOUNDS_DIR / f"{name}.aiff"


@dataclass(frozen=True)
class AlertConfig:
    """The parsed ``alerts`` block: which states play, which notify, how often.

    ``enabled`` is the master switch and it is what "off by default" means: it is
    ``True`` only when the config actually carried an ``alerts`` object. A
    disabled config has empty maps and produces no side effects at all, so the
    render loop can always hold an :class:`Alerter` and simply have it do nothing
    when the user has not opted in.
    """

    enabled: bool = False
    #: ``state name -> system-sound name``. A state absent here plays no sound.
    sounds: Dict[str, str] = field(default_factory=dict)
    #: State names that post a Notification Center banner.
    notify: Tuple[str, ...] = ()
    debounce_seconds: float = DEFAULT_DEBOUNCE_SECONDS

    @classmethod
    def from_raw(cls, raw: object) -> "AlertConfig":
        """Read an :class:`AlertConfig` from an already-loaded config dict.

        ``raw`` is :attr:`freemicro.config.Config.raw` - the whole merged
        ``config.json`` - and the only key touched is ``alerts``. Parsing lives
        here rather than in the shared config loader on purpose: this feature
        owns its own block, an unknown block must never be an error, and keeping
        it self-contained means no other module has to grow a field for it.

        Every field is validated leniently. A non-object ``alerts``, a
        ``sound`` that is not a mapping, a state name that is not real, a
        notify entry that is not a string - each is dropped, and what is left is
        armed. The worst a broken block can do is arm nothing.
        """
        if not isinstance(raw, dict):
            return cls()
        block = raw.get("alerts")
        if not isinstance(block, dict):
            return cls()

        sounds: Dict[str, str] = {}
        raw_sounds = block.get("sound")
        if isinstance(raw_sounds, dict):
            for state, name in raw_sounds.items():
                if state in _VALID_STATES and isinstance(name, str) and name:
                    sounds[state] = name

        notify: List[str] = []
        raw_notify = block.get("notify")
        if isinstance(raw_notify, (list, tuple)):
            for state in raw_notify:
                if state in _VALID_STATES and state not in notify:
                    notify.append(state)

        debounce = _as_seconds(
            block.get("debounce_seconds"), DEFAULT_DEBOUNCE_SECONDS
        )
        return cls(
            enabled=True,
            sounds=sounds,
            notify=tuple(notify),
            debounce_seconds=debounce,
        )


def _as_seconds(value: object, default: float) -> float:
    """A sane, non-negative number of seconds, or ``default``. Never raises."""
    try:
        seconds = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return float(default)
    if seconds != seconds or seconds in (float("inf"), float("-inf")):
        return float(default)
    return max(0.0, seconds)


class Alerter:
    """Fires sound and notification on configured state transitions.

    Built once and handed the transition each time the render loop sees one
    (``state != last``). It owns three things the loop should not: which
    transitions are worth an alert, the debounce that stops a flap from spamming,
    and the exact subprocess argv - all injectable for tests via ``runner`` and
    ``clock`` so the suite asserts what *would* be played and posted without a
    sound or a banner ever happening.
    """

    def __init__(
        self,
        config: AlertConfig,
        *,
        runner: Runner = spawn,
        clock: Callable[[], float] = time.monotonic,
        afplay: str = "afplay",
        osascript: str = "osascript",
    ) -> None:
        self._config = config
        self._runner = runner
        self._clock = clock
        self._afplay = afplay
        self._osascript = osascript
        #: ``(channel, state) -> last fire time``, the debounce ledger.
        self._last: Dict[Tuple[str, str], float] = {}

    @classmethod
    def from_config(cls, raw: object, **kwargs: object) -> "Alerter":
        """Build straight from a raw config dict. The loop's one call site."""
        return cls(AlertConfig.from_raw(raw), **kwargs)  # type: ignore[arg-type]

    @property
    def enabled(self) -> bool:
        return self._config.enabled

    def alert(
        self,
        state: AgentState,
        previous: "Optional[AgentState]" = None,
        *,
        project: str = "",
    ) -> None:
        """Fire whatever this transition into ``state`` calls for. Never raises.

        ``previous`` is only for context in the log line the caller may print; it
        does not gate anything, because the caller has already decided this is a
        transition. ``project`` is a human name for the banner body (the winning
        session's folder, usually) and is optional - an empty one just yields a
        generic message.

        Off-by-default and debounce both live here so the render loop's hook is a
        single unconditional call: a disabled alerter returns immediately, and a
        state that fired moments ago is skipped per channel.
        """
        if not self._config.enabled:
            return
        value = getattr(state, "value", state)
        self._maybe_sound(value)
        self._maybe_notify(value, project)

    def _maybe_sound(self, state: str) -> None:
        name = self._config.sounds.get(state)
        if not name or not self._passes_debounce("sound", state):
            return
        self._runner([self._afplay, str(sound_path(name))])

    def _maybe_notify(self, state: str, project: str) -> None:
        if state not in self._config.notify:
            return
        if not self._passes_debounce("notify", state):
            return
        title, body = notification_text(state, project)
        self._runner(
            [self._osascript, "-e", _display_notification(title, body)]
        )

    def _passes_debounce(self, channel: str, state: str) -> bool:
        """True if this ``(channel, state)`` may fire now, recording it if so."""
        now = self._clock()
        window = self._config.debounce_seconds
        key = (channel, state)
        last = self._last.get(key)
        if last is not None and window > 0 and (now - last) < window:
            return False
        self._last[key] = now
        return True


#: Per-state banner copy. The title is what the user reads at a glance from
#: across the room; the body carries the project when we know it. ``waiting`` and
#: ``error`` are phrased as the thing the human has to go do.
_NOTIFY_TITLES: Dict[str, str] = {
    AgentState.WAITING.value: "Claude Code needs you",
    AgentState.ERROR.value: "Claude Code hit an error",
    AgentState.DONE.value: "Claude Code is done",
    AgentState.WORKING.value: "Claude Code is working",
    AgentState.IDLE.value: "Claude Code is idle",
}

_NOTIFY_BODIES: Dict[str, str] = {
    AgentState.WAITING.value: "Waiting for your approval",
    AgentState.ERROR.value: "A turn ended with an error",
    AgentState.DONE.value: "A turn just finished",
    AgentState.WORKING.value: "A turn just started",
    AgentState.IDLE.value: "Went idle",
}


def notification_text(state: str, project: str = "") -> Tuple[str, str]:
    """``(title, body)`` for a banner about ``state``, naming ``project`` if given.

    Kept out of :class:`Alerter` so the copy is a pure function the tests pin
    directly. A known ``project`` is appended to the body ("Waiting for your
    approval - freemicro"); an unknown one is simply omitted.
    """
    title = _NOTIFY_TITLES.get(state, "Claude Code")
    body = _NOTIFY_BODIES.get(state, state)
    project = (project or "").strip()
    if project:
        body = f"{body} - {project}"
    return title, body


def _display_notification(title: str, body: str) -> str:
    """The AppleScript one-liner that posts a banner, with both fields escaped."""
    return (
        f'display notification "{escape_applescript(body)}" '
        f'with title "{escape_applescript(title)}"'
    )


def diagnostics() -> List[str]:
    """Lines for ``freemicro alerts --test`` about whether the tools are present.

    ``afplay`` and ``osascript`` ship with macOS, so their absence means this is
    not a Mac (or a very unusual one) and alerts cannot work. Reported rather
    than raised, because the point of ``--test`` is to *say* what is wrong.
    """
    notes: List[str] = []
    for tool in ("afplay", "osascript"):
        if shutil.which(tool) is None:
            notes.append(
                f"{tool} not found on PATH - alerts need macOS built-ins; "
                "sound/notifications will be silently skipped."
            )
    return notes


__all__ = [
    "DEFAULT_SOUNDS",
    "DEFAULT_NOTIFY",
    "DEFAULT_DEBOUNCE_SECONDS",
    "SYSTEM_SOUNDS_DIR",
    "AlertConfig",
    "Alerter",
    "Runner",
    "diagnostics",
    "escape_applescript",
    "notification_text",
    "sound_path",
    "spawn",
]
