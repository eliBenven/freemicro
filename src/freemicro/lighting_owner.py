"""Sharing the pad's LEDs with the ChatGPT desktop app, instead of losing to it.

Both programs drive the same three lighting surfaces over the same vendor HID
channel, macOS opens that device **non-exclusively**, and the last write wins.
Until now FreeMicro's answer was "quit the other app". That is a bad answer: the
vendor app is what most owners bought the pad for, and refusing to coexist is
what made the web UI disable *key capture* - a capability that never conflicted
at all.

This module holds the two honest answers, plus the vocabulary to describe the
situation truthfully.

What actually conflicts
-----------------------
* **Reading input does not conflict.** Two processes read every key, detent and
  joystick sample simultaneously; verified on hardware. Nothing here may ever
  block input.
* **Writing lighting conflicts, but only in bursts.** The vendor app is
  *event-driven*: it writes when its own model changes, on a 100 ms input-quiet
  debounce, and then stops (``docs/FACTORY-DEFAULTS.md`` §3, §9). Between those
  writes our colours simply persist.

Two mitigations follow from that, and both are implemented here.

1. **Reassert** (:class:`LightingOwner`). Because every lighting call *replaces*
   the previous state, re-sending what we already sent is idempotent and safe.
   So when something plausibly clobbered us - most importantly when ChatGPT
   quits and the field is ours again - we send the current frame once more. This
   is the owner's "reinit after conflict".

2. **Zone ownership** (:data:`VENDOR_QUIET_ZONES`). The vendor keeps the key
   backlight dark essentially always: it lights it for a ~4 s flash when the
   selected thread changes and otherwise sends the all-off payload
   (``docs/FACTORY-DEFAULTS.md`` §1c). A user running both apps can therefore
   set ``lighting.zones`` to ``["backlight"]`` and never collide at all. The
   trade-off is real - the backlight sits *under* the keycaps, so it reads as
   one glow rather than six per-project status lights - which is why it is
   offered rather than imposed.

Why the heartbeat is off by default
-----------------------------------
A slow periodic re-send is supported (``lighting.reassert.heartbeat_seconds``)
and defaults to **0, meaning off**. Two reasons, in order of weight:

* Every lighting call replaces the last, so a heartbeat *restarts* animated
  effects on every beat. A ``breath`` idle colour would visibly hitch every five
  seconds - worse than the problem it defends against.
* The channel that carries lighting also carries key events. Permanent
  background traffic to defend against a clobberer we cannot name is a cost paid
  forever for a benefit that is hypothetical; the event triggers cover every
  clobberer we *can* name.

Turn it on if you run both apps constantly, use only ``solid`` effects, and
prefer self-healing to precision.

Nothing in here opens the device or writes to it. It decides *when* the renderer
should send again, and the renderer does the sending.
"""

from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from freemicro.device.lighting import ZONE_BACKLIGHT
from freemicro.padconfig import LightingConfig, PadConfig, ReassertConfig

# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

#: The two things the pad can do for us, and the reason a single "is the pad
#: busy?" boolean was wrong: they contend for completely different reasons.
CAPABILITY_INPUT = "input"
CAPABILITY_LIGHTING = "lighting"
CAPABILITIES: Tuple[str, ...] = (CAPABILITY_INPUT, CAPABILITY_LIGHTING)

#: ``fatal`` - the capability genuinely cannot be used. ``advisory`` - it works,
#: with a caveat the user is entitled to decide about.
SEVERITY_FATAL = "fatal"
SEVERITY_ADVISORY = "advisory"

SOURCE_FREEMICRO = "freemicro"
SOURCE_VENDOR_APP = "chatgpt"

#: The vendor app, by the process name ``pgrep -x`` matches.
VENDOR_APP = "ChatGPT"

#: Zones the vendor app leaves alone in practice, so we can own them outright.
#: Only the key backlight qualifies: ``docs/FACTORY-DEFAULTS.md`` §1c records it
#: as off "by default and essentially always", lit only as a ~4 s confirmation
#: flash when the selected thread changes.
VENDOR_QUIET_ZONES: Tuple[str, ...] = (ZONE_BACKLIGHT,)


@dataclass(frozen=True)
class Conflict:
    """One reason one capability is compromised, and what to do about it.

    Deliberately one conflict *per capability*: the whole point of this type is
    that "another FreeMicro process has the pad" and "the ChatGPT app is open"
    are not the same kind of problem and must not collapse into one flag.
    """

    capability: str
    severity: str
    source: str
    #: One line, safe to put in a menu item or a status row.
    summary: str
    #: The full explanation, already wrapped for an 80-column terminal.
    detail: str
    #: What the user can do, if anything. May be empty.
    mitigation: str = ""
    pids: Tuple[int, ...] = ()

    @property
    def fatal(self) -> bool:
        return self.severity == SEVERITY_FATAL

    def to_dict(self) -> Dict[str, Any]:
        return {
            "capability": self.capability,
            "severity": self.severity,
            "source": self.source,
            "summary": self.summary,
            "detail": self.detail,
            "mitigation": self.mitigation,
            "fatal": self.fatal,
            "pids": list(self.pids),
        }


@dataclass(frozen=True)
class Contention:
    """Everything contending for the pad right now, capability by capability."""

    conflicts: Tuple[Conflict, ...] = ()
    #: Something we quietly fixed on the user's behalf, worth one reassuring
    #: line rather than an error they must act on.
    notice: str = ""

    def for_capability(self, capability: str) -> Tuple[Conflict, ...]:
        return tuple(c for c in self.conflicts if c.capability == capability)

    @property
    def fatal(self) -> bool:
        return any(c.fatal for c in self.conflicts)

    @property
    def input_blocked(self) -> bool:
        """Input is blocked **only** by a peer holding the device.

        The vendor app never appears here, and that is the whole fix: it used to
        disable key capture entirely, so a first-time user with ChatGPT open -
        the normal case - pressed keys, saw nothing, and had no way to know why.
        """
        return any(c.fatal for c in self.for_capability(CAPABILITY_INPUT))

    @property
    def lighting_contended(self) -> bool:
        return bool(self.for_capability(CAPABILITY_LIGHTING))

    @property
    def vendor_app_running(self) -> bool:
        return any(c.source == SOURCE_VENDOR_APP for c in self.conflicts)

    @property
    def input_reason(self) -> str:
        for conflict in self.for_capability(CAPABILITY_INPUT):
            if conflict.fatal:
                return conflict.detail
        return ""

    @property
    def lighting_reason(self) -> str:
        for conflict in self.for_capability(CAPABILITY_LIGHTING):
            return conflict.detail
        return ""

    def to_dict(self) -> Dict[str, Any]:
        """A shape every surface can render: doctor, menubar and the web UI.

        The four flat keys are the ones the web UI already consumes; the
        ``conflicts`` list is the structured truth behind them.
        """
        return {
            "input": self.input_reason,
            "lighting": self.lighting_reason,
            "fatal": self.fatal,
            "notice": self.notice,
            "conflicts": [c.to_dict() for c in self.conflicts],
        }


# ---------------------------------------------------------------------------
# Detecting contention
# ---------------------------------------------------------------------------

_PEER_PATTERN = r"freemicro (run|keys|watch|daemon)"


def _default_lock_holder() -> Optional[Dict[str, Any]]:
    from freemicro import daemon

    return daemon.lock_holder()


def _default_peer_pids() -> Sequence[int]:
    """Other FreeMicro processes that drive the pad without taking the lock."""
    try:
        proc = subprocess.run(
            ["pgrep", "-f", _PEER_PATTERN],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return ()
    if proc.returncode != 0:
        return ()
    pids = []
    for token in (proc.stdout or "").split():
        try:
            pid = int(token)
        except ValueError:
            continue
        if pid != os.getpid():
            pids.append(pid)
    return tuple(pids)


def vendor_app_running() -> bool:
    """Is the ChatGPT desktop app up? One ``pgrep``, shared by every caller.

    Cheap, but not free: it forks. Never call it from a per-key or per-tick
    path - :class:`LightingOwner` rate-limits it deliberately.
    """
    from freemicro import permissions

    return permissions.chatgpt_running()


_VENDOR_SUMMARY = "The ChatGPT desktop app is running"

_VENDOR_DETAIL = (
    "It drives these same LEDs over the same channel, so a colour\n"
    "FreeMicro sets can be overwritten next time ChatGPT changes its\n"
    "own lighting. Your keys, dial and joystick still work exactly as\n"
    "they do without it: macOS shares this device for reading, and\n"
    "both apps see every press."
)

_VENDOR_MITIGATION = (
    "Nothing, if you can live with the odd repaint: FreeMicro re-sends\n"
    "its colours as soon as ChatGPT quits, so the pad heals itself.\n"
    "Or avoid the fight entirely:  freemicro lights --coexist\n"
    "which gives FreeMicro only the key backlight - the one zone the\n"
    "ChatGPT app leaves dark."
)


def _peer_conflict(reason: str, pids: Sequence[int] = ()) -> Tuple[Conflict, ...]:
    """A peer FreeMicro holding the device blocks *both* capabilities."""
    return tuple(
        Conflict(
            capability=capability,
            severity=SEVERITY_FATAL,
            source=SOURCE_FREEMICRO,
            summary="Another FreeMicro process is using the pad",
            detail=reason,
            mitigation=(
                "Stop it, or start this one with --take-pad and expect a mess."
            ),
            pids=tuple(pids),
        )
        for capability in CAPABILITIES
    )


def contention(
    lock_holder: Optional[Callable[[], Optional[Dict[str, Any]]]] = None,
    peer_pids: Optional[Callable[[], Sequence[int]]] = None,
    vendor_app: Optional[Callable[[], bool]] = None,
    notice: str = "",
) -> Contention:
    """Who else wants the pad, and what that costs us.

    Every probe is injectable so this is testable with no processes, no lock
    file and no hardware. The defaults are the real ones.

    This forks up to three subprocesses, so it belongs in ``doctor``, the
    menubar's slow refresh and the web UI's status poll - not in a render loop.
    """
    holder_probe = lock_holder or _default_lock_holder
    peers_probe = peer_pids or _default_peer_pids
    vendor_probe = vendor_app or vendor_app_running

    holder = holder_probe()
    if holder:
        from freemicro import daemon

        pid = holder.get("pid")
        reason = (
            f"{daemon.describe_holder(holder)} already has the pad. Only one\n"
            "process can usefully hold this device, so FreeMicro will not "
            "fight it."
        )
        return Contention(
            conflicts=_peer_conflict(
                reason, (pid,) if isinstance(pid, int) else ()
            ),
            notice=notice,
        )

    peers = tuple(peers_probe())
    if peers:
        listed = ", ".join(str(p) for p in peers)
        reason = (
            f"Another freemicro process is driving the pad (pid {listed}).\n"
            "Stop it first - only one process can usefully hold this device."
        )
        return Contention(conflicts=_peer_conflict(reason, peers), notice=notice)

    if vendor_probe():
        return Contention(
            conflicts=(
                Conflict(
                    capability=CAPABILITY_LIGHTING,
                    severity=SEVERITY_ADVISORY,
                    source=SOURCE_VENDOR_APP,
                    summary=_VENDOR_SUMMARY,
                    detail=_VENDOR_DETAIL,
                    mitigation=_VENDOR_MITIGATION,
                ),
            ),
            notice=notice,
        )

    return Contention(notice=notice)


# ---------------------------------------------------------------------------
# Zone ownership
# ---------------------------------------------------------------------------

def owns_only_quiet_zones(lighting: LightingConfig) -> bool:
    """Does this config touch only zones the vendor app leaves dark?"""
    return bool(lighting.zones) and all(
        zone in VENDOR_QUIET_ZONES for zone in lighting.zones
    )


def coexist_advice(lighting: LightingConfig, vendor_running: bool) -> str:
    """One suggestion, or ``""`` - for a user who is running both apps.

    Only offered when it would actually change something: if lighting is off, if
    the vendor app is not running, or if the config already owns nothing but the
    quiet zones, there is nothing useful to say and we stay quiet.
    """
    if not vendor_running or not lighting.enabled:
        return ""
    if owns_only_quiet_zones(lighting):
        return ""
    return (
        "ChatGPT is running and drives these LEDs too. FreeMicro will reassert "
        "its\ncolours when ChatGPT quits. To never collide in the first place:\n"
        "  freemicro lights --coexist    own only the key backlight, which\n"
        "                                ChatGPT leaves dark (one glow under\n"
        "                                the caps, not six per-key lights)"
    )


# ---------------------------------------------------------------------------
# Reasserting
# ---------------------------------------------------------------------------

REASON_RECONNECT = "reconnect"
REASON_VENDOR_QUIT = "vendor-quit"
REASON_VENDOR_STARTED = "vendor-started"
REASON_CONFIG = "config-change"
REASON_CONFIG_BROKEN = "config-broken"
REASON_HEARTBEAT = "heartbeat"

#: How long after the last key event we leave the channel alone. Mirrors the
#: vendor app's own input-quiet debounce (``docs/FACTORY-DEFAULTS.md`` §3): the
#: channel that carries lighting also carries key events, and a laggy keypress
#: is a far worse bug than a late repaint.
QUIET_SECONDS = 0.1


@dataclass(frozen=True)
class LightingEvent:
    """Something worth telling the user about the pad's lighting."""

    reason: str
    message: str
    #: True when this caused the current frame to be sent again.
    reasserted: bool = True
    #: True for events only worth printing at raised verbosity.
    verbose_only: bool = False


class LightingOwner:
    """Keeps FreeMicro's colours on the pad when something else overwrites them.

    Holds no device and sends nothing itself. It watches for the moments when
    our lighting has plausibly been clobbered - or when the field has become
    ours again - and *invalidates* the renderer's dedupe cache so the very next
    render re-sends the current frame.

    That is the important part of the design: the dedupe that stops us
    re-sending identical frames on every poll tick stays exactly as it was.
    Reassert is a deliberate, named exception to it, not its removal.

    Triggers, in order of value:

    ``ChatGPT quits``
        The owner's "reinit after conflict", and the single most valuable case.
        Detected by a rate-limited ``pgrep`` - never on the key path.
    ``The pad reconnects``
        A fresh handle means a pad that may have been repainted while we were
        gone, and a cached frame that no longer describes anything.
    ``The config changes``
        Reloaded and re-applied in place, so ``freemicro lights --coexist`` or a
        colour edit in the web UI lands on a running ``freemicro run``.
    ``A slow heartbeat``
        Off by default. See the module docstring for why.

    Every clock read goes through ``clock`` and every process probe through
    ``vendor_probe``, so the whole thing is drivable from a test with a fake
    clock and a fake device.
    """

    def __init__(
        self,
        renderer: Any = None,
        config: Optional[PadConfig] = None,
        reassert: Optional[ReassertConfig] = None,
        clock: Callable[[], float] = time.time,
        vendor_probe: Optional[Callable[[], bool]] = None,
        config_path: Optional[Path] = None,
        loader: Optional[Callable[[Optional[Path]], PadConfig]] = None,
    ) -> None:
        self._clock = clock
        self._config = config
        self._reassert = reassert or (
            config.lighting.reassert if config is not None else ReassertConfig()
        )
        self._vendor_probe = vendor_probe or vendor_app_running
        self._loader = loader
        path = config_path if config_path is not None else (
            config.source if config is not None else None
        )
        self._config_path: Optional[Path] = Path(path) if path is not None else None
        self._config_stamp = self._stamp()

        now = clock()
        self._renderer = None
        self._note_activity: Optional[Callable[[], None]] = None
        self._attachments = 0
        self._sends = 0
        self._last_input = now - QUIET_SECONDS - 1.0
        self._next_probe = now
        self._beat_at = now
        #: ``None`` until the first probe: we cannot call a transition on our
        #: first look, only from our second onwards.
        self._vendor_running: Optional[bool] = None
        if renderer is not None:
            self.attach(renderer)

    # -- wiring -----------------------------------------------------------

    @property
    def renderer(self) -> Any:
        return self._renderer

    @property
    def config(self) -> Optional[PadConfig]:
        return self._config

    def attach(self, renderer: Any) -> Optional[LightingEvent]:
        """Take (or drop, with ``None``) the renderer we are defending.

        Returns the reconnect event on every attachment *after* the first - the
        first one is just startup, and announcing a reassert there would be
        noise dressed up as information.
        """
        self._renderer = renderer
        # Resolved once here rather than looked up on every key event: this is
        # the one path that runs for every press, release and dial detent.
        activity = getattr(renderer, "note_activity", None)
        self._note_activity = activity if callable(activity) else None
        if renderer is None:
            return None
        now = self._clock()
        self._sends = int(getattr(renderer, "sends", 0) or 0)
        self._beat_at = now
        self._attachments += 1
        if self._attachments == 1:
            return None
        self._force_resend(now)
        return LightingEvent(
            REASON_RECONNECT, "reasserted lighting (pad reconnected)"
        )

    def note_input(self) -> None:
        """A key event just arrived. Kept to a single clock read on purpose.

        This runs for every press, release and dial detent, and it is the reason
        a reassert can never make a keypress feel laggy: while input is flowing
        :meth:`poll` does nothing at all.

        It is also how a dimmed pad learns that its owner is back: any HID event
        wakes the lighting (``docs/FACTORY-DEFAULTS.md`` §4). The renderer only
        records the time here - the repaint happens on the next render tick, so
        this path still writes nothing to the channel the event came in on.
        """
        self._last_input = self._clock()
        if self._note_activity is not None:
            self._note_activity()

    @property
    def busy(self) -> bool:
        """Is a keypress burst in flight right now?"""
        return self._clock() - self._last_input < QUIET_SECONDS

    # -- the tick ---------------------------------------------------------

    def poll(self) -> List[LightingEvent]:
        """Called once per render tick. Returns what to tell the user.

        Cheap by construction: an early return while keys are being pressed, a
        clock comparison, and - at most every ``poll_seconds`` - one ``pgrep``
        and one ``stat``.
        """
        renderer = self._renderer
        if renderer is None or not self._reassert.enabled:
            return []
        now = self._clock()
        if now - self._last_input < QUIET_SECONDS:
            # A burst is in flight. The channel belongs to the keys.
            return []

        self._observe_sends(now)

        events: List[LightingEvent] = []
        if now >= self._next_probe:
            self._next_probe = now + max(0.25, self._reassert.poll_seconds)
            for event in (self._check_vendor(), self._check_config()):
                if event is not None:
                    events.append(event)

        if not any(event.reasserted for event in events):
            beat = self._check_heartbeat(now)
            if beat is not None:
                events.append(beat)

        if any(event.reasserted for event in events):
            self._force_resend(now)
        return events

    # -- individual triggers ----------------------------------------------

    def _check_vendor(self) -> Optional[LightingEvent]:
        running = bool(self._vendor_probe())
        was, self._vendor_running = self._vendor_running, running
        if was is None or was == running:
            return None
        if was and not running:
            return LightingEvent(
                REASON_VENDOR_QUIT, "reasserted lighting (ChatGPT quit)"
            )
        return LightingEvent(
            REASON_VENDOR_STARTED,
            "ChatGPT started - it drives these LEDs too; FreeMicro will "
            "reassert when it quits",
            reasserted=False,
        )

    def _check_config(self) -> Optional[LightingEvent]:
        stamp = self._stamp()
        if stamp == self._config_stamp:
            return None
        self._config_stamp = stamp
        try:
            config = self._load()
        except Exception as exc:  # noqa: BLE001 - a bad edit must not stop us
            return LightingEvent(
                REASON_CONFIG_BROKEN,
                f"config changed but would not load - still running the old "
                f"one ({exc})",
                reasserted=False,
            )
        self._config = config
        self._reassert = config.lighting.reassert
        apply_config = getattr(self._renderer, "apply_config", None)
        if callable(apply_config):
            apply_config(config)
        return LightingEvent(REASON_CONFIG, "reasserted lighting (config changed)")

    def _check_heartbeat(self, now: float) -> Optional[LightingEvent]:
        if not self._reassert.heartbeat_enabled:
            return None
        if now - self._beat_at < self._reassert.heartbeat_seconds:
            return None
        return LightingEvent(
            REASON_HEARTBEAT,
            "reasserted lighting (heartbeat)",
            verbose_only=True,
        )

    # -- internals --------------------------------------------------------

    def _observe_sends(self, now: float) -> None:
        """Notice a real frame going out, so the heartbeat starts over.

        Without this the heartbeat would eventually fire right behind a genuine
        state change and re-send it for no reason - coalescing ahead of the real
        work instead of behind it.
        """
        sends = int(getattr(self._renderer, "sends", 0) or 0)
        if sends != self._sends:
            self._sends = sends
            self._beat_at = now

    def _force_resend(self, now: float) -> None:
        """Drop the renderer's dedupe cache so the next render re-sends."""
        self._beat_at = now
        invalidate = getattr(self._renderer, "invalidate", None)
        if callable(invalidate):
            invalidate()

    def _stamp(self) -> Optional[Tuple[int, int]]:
        path = self._config_path
        if path is None:
            return None
        try:
            info = os.stat(str(path))
        except OSError:
            return None
        return (info.st_mtime_ns, info.st_size)

    def _load(self) -> PadConfig:
        if self._loader is not None:
            return self._loader(self._config_path)
        from freemicro import padconfig

        return padconfig.load(self._config_path)


__all__ = [
    "CAPABILITIES",
    "CAPABILITY_INPUT",
    "CAPABILITY_LIGHTING",
    "Conflict",
    "Contention",
    "LightingEvent",
    "LightingOwner",
    "QUIET_SECONDS",
    "REASON_CONFIG",
    "REASON_CONFIG_BROKEN",
    "REASON_HEARTBEAT",
    "REASON_RECONNECT",
    "REASON_VENDOR_QUIT",
    "REASON_VENDOR_STARTED",
    "SEVERITY_ADVISORY",
    "SEVERITY_FATAL",
    "SOURCE_FREEMICRO",
    "SOURCE_VENDOR_APP",
    "VENDOR_APP",
    "VENDOR_QUIET_ZONES",
    "coexist_advice",
    "contention",
    "owns_only_quiet_zones",
    "vendor_app_running",
]
