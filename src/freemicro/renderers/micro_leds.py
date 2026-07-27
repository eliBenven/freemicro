"""Drive the Codex Micro's own LEDs from Claude Code's state.

This is the renderer the whole project was aimed at: the pad's backlight,
underglow and Agent Keys lit by *your* agent, with no vendor desktop app in the
loop. It speaks the pad's documented lighting methods over the same vendor HID
channel that carries key events (``docs/PROTOCOL.md``).

Which method matters, and why
-----------------------------
* ``v.oai.rgbcfg`` drives the **underglow and key backlight**. Verified on
  hardware, and the method the vendor app uses for those surfaces. This is the
  default (``lighting.method``).
* ``v.oai.thstatus`` sets the six Agent Keys individually, one array entry each.
* ``lights.preview`` is in the firmware's method table and replies
  ``{"result": null}``, but produces **no visible change** on firmware v0.4.1
  over either transport. It stays selectable purely for debugging; an earlier
  claim that it was the working path predated the Bluetooth framing fix and was
  wrong.

Six keys, six projects
----------------------
The Agent Keys are addressed *individually*, and this renderer treats them that
way: each key is coloured by the state of the project it stands for (see
:mod:`freemicro.agentkeys`), not by the one winning state. Six copies of one
light is what the pad did before, and it wasted five sixths of the hardware.

An empty slot is sent ``{c: 0, b: 0, e: off}`` - dark, not dim. That is the
factory's "no agent assigned" treatment (``docs/FACTORY-DEFAULTS.md`` §1a), and
it is what makes the lit keys mean something: if three projects are running,
three keys glow and three are off, so the count is readable at a glance.

Anyone who preferred the old behaviour sets ``agent_keys.policy`` to ``mirror``.

Three design choices worth keeping:

* **Colours come from the user's config**, not constants here. A state the
  config omits falls back to :data:`freemicro.padconfig.FACTORY_PALETTE`, which
  is the one definition of the factory colours in the project and the one
  ``docs/CUSTOMIZING.md`` promises.
* **:meth:`available` is honest.** No pad, no macOS, no Input Monitoring grant →
  ``False``, and the registry falls through to the screen renderer. The alert
  never depends on the pad; that guarantee predates the LEDs working and
  outlives any firmware change.
* **The device handle is shared.** ``freemicro run`` reads keys and writes
  lights through one open handle, so this renderer borrows
  :func:`freemicro.device.shared_device` rather than opening its own.

Sharing the LEDs with the vendor app
------------------------------------
We are not the only writer. The ChatGPT desktop app drives the same zones over
the same channel and the last write wins, so this renderer exposes three small
hooks for :class:`freemicro.lighting_owner.LightingOwner`, which decides *when*
a frame we already sent should be sent again:

* :meth:`~MicroLedsRenderer.invalidate` drops the dedupe cache - the deliberate,
  named exception to it, never its removal;
* :attr:`~MicroLedsRenderer.sends` counts frames that actually went out, so the
  heartbeat can tell a reassert from real work;
* :meth:`~MicroLedsRenderer.apply_config` swaps the config in place, so editing
  colours does not require restarting ``freemicro run``.

A write that fails is not a lighting that is over
-------------------------------------------------
The pad drops. Bluetooth blips, the vendor app grabs the device for a moment,
the cable gets nudged. A single failed ``device.send`` used to latch lighting
off for the life of the process, silently, which is almost certainly the story
behind more than one "the lights just stopped" report. So a failure now:

* **retries**, on the vendor's own backoff ladder
  (:data:`WRITE_RETRY_BACKOFF`, ``docs/FACTORY-DEFAULTS.md`` §9);
* **says so once** when it starts and once when it recovers, and never once per
  tick - a pad that is genuinely gone must not fill the log;
* claims nothing it cannot back up. ``docs/PROTOCOL.md`` records that a wrongly
  framed write returns ``kIOReturnSuccess`` and is discarded, so "the write went
  out" is the strongest thing any message here is allowed to say.

A layer for "this key is doing its thing right now"
---------------------------------------------------
A binding can carry a ``light`` (:class:`freemicro.padconfig.ActivityLight`) -
the mic key is the shipped case, and push-to-talk is the reason it exists. The
rule that makes it safe is that it is **composed, never written**:
:meth:`~MicroLedsRenderer.set_overlay` only records the layer, and the next
ordinary frame is built from the live state *and* the layer together. Three
things follow from that, and all three were the point:

* the zones the layer does not name keep showing agent state, so the six Agent
  Keys go on carrying six projects while you dictate;
* letting go restores the truth **as it is then**, not as it was when the key
  went down - there is no saved frame to put back, so a project that finished
  mid-sentence is already green when you release;
* nothing has to be undone, so no failure path can strand a colour on the pad.

The zones a layer *can* claim are a property of the config, not of the moment
(:attr:`freemicro.padconfig.PadConfig.activity_zones`), so a zone the state
lighting never drives is painted dark in every ordinary frame and lit only
while the layer is up. That is what keeps "who owns this zone?" answerable
without the renderer remembering anything.

Auto-dim, and the one place we do not copy the factory
------------------------------------------------------
The factory blanks the whole pad after three minutes of inactivity, and dim
means *dark*, not dimmer (``docs/FACTORY-DEFAULTS.md`` §4). That is copied here,
timing and payload, because our ``idle`` is white at full brightness and idle is
what a live project shows most of the time: without it, FreeMicro is a
full-brightness white light on a desk at 1 a.m.

The exception is :attr:`~freemicro.padconfig.LightingConfig.auto_dim_alerts`,
which defaults to *not* dimming ``waiting`` and ``error``. The whole value of an
amber key is that it reaches you when you are not at your desk - which is
exactly when nothing is resetting the inactivity timer. Set it to ``true`` for
exact factory behaviour.

Getting the pad back when we are killed
---------------------------------------
``lighting.on_exit`` used to be honoured only from a ``finally``, and Python
does not unwind ``finally`` on a default ``SIGTERM``. ``launchctl bootout``,
``pkill``, logout and shutdown all send exactly that, so stopping FreeMicro
could leave the pad glowing with nothing installed that could ever turn it off.
:func:`release_lighting` closes that hole from ``atexit`` *and* from chained
``SIGINT``/``SIGTERM``/``SIGHUP`` handlers, armed the first time we actually
light the pad. It is deliberately the same shape as
:func:`freemicro.input.quartz.install_release_guard`, which solves the same
problem for held modifier keys: two incompatible signal schemes in one process
would be worse than either bug.
"""

from __future__ import annotations

import atexit
import os
import signal
import threading
import time
import weakref
from typing import TYPE_CHECKING, Any, Callable, List, Optional, Sequence, Tuple

from freemicro.agentkeys import AgentKeyOwnership, AgentSlot, SlotResolver
from freemicro.device import Device, close_shared, shared_device
from freemicro.device.lighting import (
    ZONE_AGENT_KEYS,
    ZONE_BACKLIGHT,
    ZONE_UNDERGLOW,
    parse_effect,
    preview_message,
    rgbcfg_message,
    rgbcfg_side,
    thread_entry,
    thstatus_message,
)
from freemicro.padconfig import (
    ActivityLight,
    LightingConfig,
    PadConfig,
    PadConfigError,
    StateLight,
)
from freemicro.padconfig import load as load_padconfig
from freemicro.renderers.base import Renderer, register
from freemicro.state.engine import AgentState

if TYPE_CHECKING:  # a type-only import: only used in annotations
    from freemicro.state.engine import SessionState

#: How long to wait after a failed write before trying the next one, in
#: seconds, clamped at the last step. This is the vendor's own transport
#: backoff ladder (``docs/FACTORY-DEFAULTS.md`` §9, ``[1000, 2000, 5000,
#: 10000]`` ms) rather than a number someone liked the look of: it is the only
#: measured evidence we have about how long this device takes to come back.
WRITE_RETRY_BACKOFF: Tuple[float, ...] = (1.0, 2.0, 5.0, 10.0)

#: States that exist to fetch you. They do not dim by default - see the module
#: docstring and ``LightingConfig.auto_dim_alerts``.
ALERT_STATES: Tuple[AgentState, ...] = (AgentState.WAITING, AgentState.ERROR)

#: The all-off look, shared by auto-dim and by ``on_exit: off``. One object
#: because they are the same payload for the same reason: the factory's
#: "nothing to show" state is dark, not dimmer (``docs/FACTORY-DEFAULTS.md``
#: §1d and §4).
_DARK = StateLight(color=0, effect=parse_effect("off"), brightness=0.0, speed=0.0)

#: How long an entry flash lasts. Short on purpose - one visible blip, not a
#: strobe. The flash is a single pulse in the element's *own* colour: on for the
#: first half, dark for the second, then the steady look. Keeping the flash in
#: the state's colour (rather than a white overlay) means the new state is
#: *shown* the instant it is entered - the flash draws the eye to it, it never
#: stands in front of it - and it needs no sub-tick timing, so it survives a
#: slow render interval.
FLASH_SECONDS = 0.4

#: The blink half-period at ``speed`` 0 and 1, in seconds. A blink is a software
#: effect: the render loop (which already owns the clock and ticks every poll)
#: recomputes the frame each tick and toggles the affected LEDs between their
#: colour and off. The floor, 0.30 s, is deliberately above the default render
#: interval (0.25 s, ``freemicro run --interval``) so even the fastest blink
#: shows a real off phase; a larger ``--interval`` slows the blink to the tick
#: rate rather than aliasing it away. No second thread writes the channel - that
#: is the one rule the lighting code holds, and blink keeps it.
BLINK_HALF_MAX = 0.75
BLINK_HALF_MIN = 0.30


def _clamp01(value: float) -> float:
    return 0.0 if value < 0.0 else 1.0 if value > 1.0 else value


def _blink_on(speed: float, now: float) -> bool:
    """Whether a blinking light is in its *on* phase at ``now``.

    The rate comes from ``speed`` alone, so two blinking lights with different
    speeds keep their own rhythms; the phase comes from the shared clock, so
    nothing has to be remembered between ticks.
    """
    half = BLINK_HALF_MAX - (BLINK_HALF_MAX - BLINK_HALF_MIN) * _clamp01(speed)
    return int(now // half) % 2 == 0


def _default_battery_reader() -> Optional[dict]:
    """The cached ``device.status`` reading, read as a plain file.

    This is the whole battery seam: a file the menu bar poller (and, when it
    owns the pad, the daemon) refreshes on its own slow clock. Reading it puts
    nothing on the vendor channel, so it cannot fight a lighting write or stall
    the render loop - which matters because the loop is already inside one long
    ``device.stream`` pump and cannot safely nest the round trip itself.
    """
    try:
        from freemicro.menubar.status import read_status

        return read_status()
    except Exception:  # noqa: BLE001 - no reading is just "battery unknown"
        return None


_NOTICE_PREFIX = "  [lighting] "


def _print_notice(message: str) -> None:
    """Where lighting news goes when nobody supplies somewhere better.

    The prefix matches the one ``freemicro run`` puts on lighting-owner events,
    because from the reader's side they are the same subject. Continuation
    lines are indented under it, so a two-line notice still reads as one
    paragraph in a log that has key dispatches interleaved with it.
    """
    lines = message.splitlines() or [""]
    print(_NOTICE_PREFIX + lines[0], flush=True)
    for line in lines[1:]:
        print(" " * len(_NOTICE_PREFIX) + line, flush=True)


# ---------------------------------------------------------------------------
# Handing the pad back, on every exit path there is
# ---------------------------------------------------------------------------
#
# `lighting.on_exit` is a promise about what the pad looks like once FreeMicro
# is not driving it any more. A `finally` keeps that promise for Ctrl-C and for
# a clean return, and for nothing else: Python's default SIGTERM disposition
# terminates the process without unwinding, and `os.execv` runs neither
# `finally` nor `atexit`. Since `freemicro daemon uninstall` stops the agent
# with SIGTERM, the promise was being broken by our own uninstall command, and
# the user's only recourse was to unplug the pad.
#
# So the process that lit the LEDs has to guarantee the hand-back itself, the
# same way `input.quartz` guarantees key-ups for a chord it is holding.

_guard_lock = threading.RLock()
#: Weak references to the renderers currently driving a pad. Weak on purpose:
#: `freemicro run` builds a fresh renderer on every reconnect and drops the old
#: one without closing it, and an orphan holding a closed handle must not keep
#: itself alive to send a blank frame down it.
_driving: List[Any] = []
_guard_installed = False


def _register_driving(renderer: "MicroLedsRenderer") -> None:
    """Note that this renderer has painted the pad, and arm the guard once."""
    with _guard_lock:
        _driving[:] = [ref for ref in _driving if ref() is not None]
        if not any(ref() is renderer for ref in _driving):
            _driving.append(weakref.ref(renderer))
    _install_exit_guard()


def _unregister_driving(renderer: "MicroLedsRenderer") -> None:
    with _guard_lock:
        _driving[:] = [
            ref for ref in _driving
            if ref() is not None and ref() is not renderer
        ]


def release_lighting() -> int:
    """Apply ``lighting.on_exit`` for every renderer still driving a pad.

    Returns how many pads were handed back. Idempotent, safe to call from
    anywhere, and **it must never raise**: it runs from ``atexit`` and from a
    signal handler, where an exception would leave the pad lit *and* mangle the
    exit. A renderer that already handed back is skipped, so calling this and
    then :meth:`MicroLedsRenderer.close` does not write twice.

    Also the hook for ``os.execv``: a re-exec runs neither ``finally`` nor
    ``atexit``, so whatever is about to call it should call this first.
    """
    with _guard_lock:
        refs = list(_driving)
        del _driving[:]
    released = 0
    for ref in refs:
        renderer = ref()
        if renderer is None:
            continue
        try:
            if renderer.hand_back():
                released += 1
        except Exception:  # noqa: BLE001 - a lit pad is the worse outcome
            pass
    return released


def _release_and_chain(signum: int, previous: Any) -> None:
    """Hand the pad back, then let the signal do what it was going to do."""
    release_lighting()
    if callable(previous):
        previous(signum, None)
        return
    if previous == signal.SIG_IGN:
        return
    # SIG_DFL: restore it and re-raise, so the process still dies the way the
    # sender asked. Swallowing a SIGTERM to blank some LEDs would be worse, and
    # it would also strand the *other* guard in this process.
    try:
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)
    except (OSError, ValueError):  # pragma: no cover - defensive
        pass


def _install_exit_guard() -> None:
    """Arm the safety net. Called the first time a frame reaches the pad.

    Lazy on purpose: importing this module must not touch the process's signal
    handlers, and a run with lighting off has nothing to hand back.

    The previous handler is captured and chained, which is what lets this
    coexist with :func:`freemicro.input.quartz.install_release_guard` in either
    order - each guard runs, and whichever was installed first still gets to
    re-raise so the signal ends up doing what the sender asked.
    """
    global _guard_installed
    with _guard_lock:
        if _guard_installed:
            return
        _guard_installed = True
    atexit.register(release_lighting)
    for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        try:
            previous = signal.getsignal(signum)
            signal.signal(
                signum,
                lambda num, frame, _prev=previous: _release_and_chain(num, _prev),
            )
        except (ValueError, OSError, AttributeError):
            # signal.signal only works on the main thread and SIGHUP does not
            # exist everywhere. Losing the handler is survivable: atexit and
            # the `finally` in the run loop still cover the ordinary paths.
            pass


@register
class MicroLedsRenderer(Renderer):
    """Light the pad from agent state."""

    name = "micro-leds"
    #: Above every other target: when the pad is present it *is* the display.
    priority = 100
    experimental = False

    def __init__(
        self,
        device: Optional[Device] = None,
        config: Optional[PadConfig] = None,
        store: Any = None,
        clock: Callable[[], float] = time.monotonic,
        notify: Optional[Callable[[str], None]] = None,
        battery_reader: Optional[Callable[[], Optional[dict]]] = None,
        ownership: Optional[AgentKeyOwnership] = None,
    ) -> None:
        # A caller-supplied device is borrowed; otherwise we use (and release)
        # the process-wide shared handle. Either way we never open a second one.
        self._device = device
        self._borrowed = device is not None
        self._config = config
        #: The effective Agent-Key ownership - which of the six keys FreeMicro
        #: drives *right now*, the configured subset only while Codex is running.
        #: Injected by ``freemicro run`` so the LEDs and the key bridge share one
        #: (they must never disagree about which keys are ours); ``None`` for a
        #: standalone renderer (the preview path, the registry), where it is built
        #: lazily from the config and simply honours the configured subset - it is
        #: never refreshed, so it stays the config's static answer, exactly the
        #: pre-dynamic behaviour. See :meth:`owned_keys` and :class:`AgentKeyOwnership`.
        self._ownership = ownership
        self._ownership_injected = ownership is not None
        # ``store`` is a StateStore; left untyped so this module does not have
        # to import it for a construction it may never do.
        self._store = store
        self._store_probed = store is not None
        self._resolver: Optional[SlotResolver] = None
        self._published: Tuple[str, ...] = ()
        self._request_id = 0
        #: Monotonic by default: every interval here is a duration, and a
        #: duration measured on a wall clock is a duration an NTP step can make
        #: negative. Injectable so the tests can drive minutes in microseconds.
        self._clock = clock
        self._notify = notify if notify is not None else _print_notice
        #: The frame we believe is on the pad. ``None`` means "unknown, send".
        self._frame: Optional[Tuple[Any, ...]] = None
        #: The frame the agent state *asks* for, dimmed or not. Changing it is
        #: activity in the factory's sense (§4) and wakes a dimmed pad.
        self._model: Optional[Tuple[Any, ...]] = None
        self._dimmed = False
        self._dim_asserted = False
        self._activity_at = clock()
        #: The layer a live binding has put over the state lighting, if any.
        #: Only ever *read* when a frame is built - see :meth:`set_overlay`.
        self._overlay: Optional[ActivityLight] = None
        #: Consecutive failed writes, and when the next attempt may go out.
        self._failures = 0
        self._failed_at = 0.0
        self._retry_at = 0.0
        #: True once we have applied ``on_exit`` and are no longer driving.
        self._released = False
        #: Frames that actually reached the device. Read by the lighting owner
        #: to tell "we just sent something real" from "nothing happened".
        self._sends = 0
        #: The state each slot (and the resolved state) was in last tick, so an
        #: *entry* into a flash state can be told from merely *being* in one.
        self._prev_state: Optional[AgentState] = None
        self._prev_slot_states: dict = {}
        #: flash key -> the clock time its flash ends. "state" is the resolved
        #: state's zone flash; a slot path is that Agent Key's flash.
        self._flash_until: dict = {}
        #: The battery cue, read from the cache on a slow clock (never a device
        #: round trip - see :func:`_default_battery_reader`).
        self._battery_reader = battery_reader or _default_battery_reader
        self._battery_low = False
        self._battery_checked_at: Optional[float] = None

    # -- config -----------------------------------------------------------

    @property
    def config(self) -> PadConfig:
        if self._config is None:
            try:
                self._config = load_padconfig()
            except PadConfigError:
                # A broken keymap must not take the light down with it - the
                # `keys` command is where that error gets reported loudly.
                from freemicro.padconfig import load_default

                self._config = load_default()
        return self._config

    @property
    def lighting(self) -> LightingConfig:
        return self.config.lighting

    @property
    def key_ownership(self) -> AgentKeyOwnership:
        """The effective Agent-Key ownership this renderer paints from.

        The shared one injected by ``freemicro run`` (so the LEDs and the key
        bridge agree), or one built here from the config for a standalone
        renderer. Built lazily so constructing a renderer never probes.
        """
        if self._ownership is None:
            self._ownership = AgentKeyOwnership(self.config.agent_keys)
        return self._ownership

    @property
    def owned_keys(self) -> Tuple[int, ...]:
        """The Agent Key indices FreeMicro drives *right now*, in key order.

        All six unless ``agent_keys.keys`` names a strict subset to coexist with
        the ChatGPT desktop app (Codex) *and* that app is actually running - when
        it is not, there is no one to share with, so FreeMicro owns all six even
        though a subset is configured (see :class:`AgentKeyOwnership`). Every
        ``thstatus`` this renderer builds - per-slot state, the mirrored look, an
        overlay or battery layer that seizes the whole zone, and the blank/exit
        frame - is restricted to these keys, so the keys we do not own are never
        in a message we send and the vendor app's writes to them persist. With
        the default all-six set this is ``(0, 1, 2, 3, 4, 5)`` and every builder
        is byte-identical to before.
        """
        return self.key_ownership.keys

    def apply_config(self, config: PadConfig) -> None:
        """Swap in a freshly loaded config while we are running.

        Everything derived from the old one has to go: the slot resolver caches
        the agent-keys policy, and the dedupe key describes a frame built from
        colours that may no longer be the configured ones.

        If lighting was just switched *off*, we hand the pad back on the way out
        exactly as :meth:`close` would - leaving our colours burned in after the
        user disabled us would be the rudest possible reading of "disabled".
        """
        previous = self._config.lighting if self._config is not None else None
        self._config = config
        self._resolver = None
        # A self-built ownership caches the *old* config's agent_keys, so rebuild
        # it from the new one lazily. An injected (shared) ownership is the run
        # loop's to update - it calls set_config on the one object the bridge
        # shares - so we must not swap it out from under the bridge here.
        if not self._ownership_injected:
            self._ownership = None
        self._published = ()
        self._frame = None
        self._model = None
        # The layer itself survives a reload - a key is still physically down -
        # but which zones it may claim came from the old file, so the frame it
        # describes has to be rebuilt. Clearing _frame above is that rebuild.
        # A settings change is activity in the factory's own model (§4), which
        # is what makes editing colours in the web UI wake a dimmed pad.
        self._wake(self._clock())
        if (
            previous is not None
            and previous.enabled
            and not config.lighting.enabled
            and self._device is not None
        ):
            # Hand the pad back the way the *old* config asked us to - those are
            # the zones we were driving and the exit look the user chose.
            self.hand_back(lighting=previous)

    def light_for(self, state: AgentState) -> StateLight:
        """The look for one state: the config's, or the factory's if it has none."""
        return self.lighting.light_for(state)

    # -- slots -------------------------------------------------------------

    @property
    def store(self) -> Any:
        """The session store, opened lazily and at most once.

        Probed rather than required: a renderer with no readable store still
        lights the pad, it just mirrors the one resolved state instead of
        showing six projects.
        """
        if not self._store_probed:
            self._store_probed = True
            try:
                from freemicro.state.engine import default_store

                self._store = default_store()
            except Exception:  # noqa: BLE001 - no store, no per-slot colours
                self._store = None
        return self._store

    def slots(
        self, sessions: "Optional[List[SessionState]]" = None
    ) -> Optional[List[AgentSlot]]:
        """The six slots as they should be lit, or ``None`` to mirror.

        ``None`` means "fall back to the single-colour behaviour", which is
        what ``policy: mirror`` asks for and also what happens when there is no
        session store to read.

        ``sessions`` is the already-read session list from this tick, if the
        caller has one; passing it avoids a second directory scan. When it is
        ``None`` the store is read here as before.
        """
        config = self.config.agent_keys
        if config.mirrors or not self.lighting.drives_agent_keys:
            return None
        store = self.store
        if store is None:
            return None
        if self._resolver is None:
            from freemicro.state import slots as slot_cache

            # The store's own decay policy, whole: the records arrive already
            # decayed and re-deciding under any other timers would let the pad
            # contradict `freemicro status` about the same session.
            self._resolver = SlotResolver.for_store(store, config=config)
            # Seed from the shared cache so the key you press is the key you
            # saw lit, even across a restart or a second process.
            self._resolver.seed(slot_cache.load())
        try:
            if sessions is None:
                sessions = store.sessions()
            # The effective owned set, not the static ``config.keys``: when Codex
            # is not running the subset gives way to all six, so the keys it
            # names are re-driven (their projects / dark-if-empty). ``owned``
            # threads that into the pure resolver, keeping the config immutable.
            resolved = self._resolver.resolve(sessions, owned=self.owned_keys)
        except Exception:  # noqa: BLE001 - an unreadable store must not go dark
            return None
        self._publish(self._resolver.previous)
        return resolved

    def _publish(self, assignment: Sequence[str]) -> None:
        """Share the assignment with the process that handles key presses."""
        if tuple(assignment) == self._published:
            return
        from freemicro.state import slots as slot_cache

        if slot_cache.save(assignment):
            self._published = tuple(assignment)

    # -- renderer interface ----------------------------------------------

    def available(self) -> bool:
        """Whether this renderer can drive the pad *at all*.

        Deliberately not "is every write succeeding": a failed write is a
        transient state this renderer recovers from on its own, and answering
        ``False`` to it used to hand the pad to the screen fallback forever
        because of one Bluetooth blip.
        """
        if not self.lighting.enabled:
            return False
        if self._device is None:
            self._device = shared_device()
        return self._device is not None

    def render(
        self, state: AgentState, sessions: "Optional[List[SessionState]]" = None
    ) -> None:
        """Send this state's lighting. A no-op if nothing changed.

        Skipping unchanged frames is not just an optimisation: each lighting
        call *replaces* the previous one, so re-sending on every poll tick would
        restart animated effects several times a second.

        The comparison is over the whole frame - the resolved state *and* the
        six slots - because a slot can change state while the winning state
        does not (project A finishes while project B is still working).

        ``sessions`` lets the run loop hand in the session list it already read
        this tick (via ``StateStore.resolved_and_sessions``), so the pad is not
        painted from a second, independent scan of the state directory that
        could disagree with the state just computed. Omitted, the renderer reads
        the store itself, so it still works when driven standalone.

        This is also the auto-dim clock. Nothing else ticks often enough to be
        one, and the alternative - a timer thread writing to the pad behind the
        run loop's back - would put a second writer on the channel that carries
        key events.
        """
        if self._device is None or not self.lighting.enabled:
            return
        now = self._clock()
        slots = self.slots(sessions)
        overlay = self._overlay
        frame = self._frame_key(state, slots, overlay)
        if frame != self._model:
            # A change in the lighting model counts as activity and wakes the
            # pad, exactly as it does for the vendor app (§4). A state entry is
            # exactly such a change, so this is also where the entry flash is
            # armed - a blink phase flipping is *not* a model change and rightly
            # does neither.
            self._model = frame
            self._wake(now)
            self._note_flash_entries(state, slots, now)

        # A cheap, throttled cache read - never a device round trip. See
        # _default_battery_reader for why this must not touch the channel.
        self._refresh_battery(now)

        if self._dimmed:
            # Dark is the frame now. Re-send it only if something suggests the
            # pad is no longer showing it (a failed write, or a reassert).
            if not self._dim_asserted:
                self._dim_asserted = self._send(self._blank_messages())
            return

        # The dedupe compares the *bytes we would send*, not a summary of the
        # model, because blink, the entry flash and the battery cue all change
        # the frame while the model is unchanged. Building the frame every tick
        # is a handful of dicts at 4 Hz; the "skip unchanged frames" guarantee
        # that keeps animated effects from restarting is preserved exactly -
        # every half-period a blinking frame genuinely differs, and only then.
        messages = self.messages_for(state, slots=slots, overlay=overlay, now=now)
        # The ``lights.preview`` method carries a per-call ``id`` that increments
        # every build; it is correlation metadata, not part of what the pad
        # shows, so it is excluded from the dedupe or an unchanged preview frame
        # would re-send on every tick.
        key = repr([
            {k: v for k, v in message.items() if k != "id"}
            for message in messages
        ])
        if key != self._frame:
            if self._send(messages):
                self._frame = key
            return

        if self._should_dim(state, slots, now):
            self._dimmed = True
            self._frame = None
            self._dim_asserted = self._send(self._blank_messages())

    @property
    def sends(self) -> int:
        """How many frames have actually reached the pad. Monotonic."""
        return self._sends

    @property
    def write_failures(self) -> int:
        """Consecutive failed writes right now. 0 when the pad is answering."""
        return self._failures

    @property
    def dimmed(self) -> bool:
        """Whether auto-dim has blanked the pad."""
        return self._dimmed

    @property
    def overlay(self) -> Optional[ActivityLight]:
        """The layer currently over the state lighting, if any."""
        return self._overlay

    def set_overlay(self, overlay: Optional[ActivityLight]) -> bool:
        """Put a binding's light over the state lighting, or take it away.

        Records and returns; it deliberately **does not write**. The pad is
        painted from one place - :meth:`render` - and that is what guarantees
        the layer and the state underneath it are always one consistent frame,
        that letting go shows the state as it is *now*, and that no code path
        exists which could leave a colour behind because it failed halfway.

        The lag that buys is one render tick (``freemicro run --interval``,
        default 0.25 s), and it is the right trade twice over: this is called
        from the thread reading key events on the same channel lighting goes
        down, and the vendor app defers its own lighting writes for 100 ms after
        the last input for exactly that reason (``docs/FACTORY-DEFAULTS.md``
        §3).

        Idempotent. Returns whether anything changed, so a caller can log a
        transition without tracking one itself.
        """
        if overlay == self._overlay:
            return False
        self._overlay = overlay
        return True

    def note_activity(self) -> None:
        """The user did something. Wake the pad if auto-dim put it out.

        Called for every key, dial detent and joystick sample by
        :meth:`freemicro.lighting_owner.LightingOwner.note_input`, so it is one
        clock read and nothing else. In particular it does **not** write: the
        channel carrying this event is the channel lighting goes down, and the
        repaint waits for the next render tick like every other frame.
        """
        self._wake(self._clock())

    def invalidate(self) -> None:
        """Forget what we last sent, so the next render sends it again.

        The dedupe in :meth:`render` exists because every lighting call replaces
        the previous one - re-sending on each poll tick would restart animated
        effects several times a second. This is the one sanctioned way around
        it, for the moments when something *else* has painted the pad and our
        cached frame no longer describes what the user is looking at.

        A reassert is not activity: if we are dimmed, we stay dimmed and it is
        the *dark* frame that gets sent again. Something else painting the pad
        is not the user coming back to their desk.
        """
        self._frame = None
        self._dim_asserted = False

    # -- auto-dim ---------------------------------------------------------

    def _wake(self, now: float) -> None:
        """Restart the inactivity timer, and undim if we are dim."""
        self._activity_at = now
        if self._dimmed:
            self._dimmed = False
            self._dim_asserted = False
            # The pad is dark, so whatever we think we last sent is not on it.
            self._frame = None

    def _should_dim(
        self, state: AgentState, slots: Optional[Sequence[AgentSlot]], now: float
    ) -> bool:
        lighting = self.lighting
        if not lighting.auto_dim_enabled:
            return False
        if self._overlay is not None:
            # A key is being held *right now*. Blanking the pad mid-hold would
            # be the pad going dark while you are still talking into it, and
            # holding a key is the least ambiguous activity there is - the
            # factory's own wake rule is "any HID event" (§4) and this is one
            # that has not finished yet.
            return False
        if now - self._activity_at < lighting.auto_dim_seconds:
            return False
        if not lighting.auto_dim_alerts and self._asks_for_you(state, slots):
            return False
        return True

    @staticmethod
    def _asks_for_you(
        state: AgentState, slots: Optional[Sequence[AgentSlot]]
    ) -> bool:
        """Is anything on the pad waiting for the user right now?

        Checked per slot, not just on the resolved state: five idle projects and
        one blocked one is precisely the case the pad exists for.
        """
        if state in ALERT_STATES:
            return True
        if slots is None:
            return False
        return any(
            not slot.empty and slot.state in ALERT_STATES for slot in slots
        )

    @staticmethod
    def _frame_key(
        state: AgentState,
        slots: Optional[Sequence[AgentSlot]],
        overlay: Optional[ActivityLight] = None,
    ) -> Tuple[Any, ...]:
        """Everything that affects what the pad shows, and nothing else.

        The overlay is part of it, which is what makes the layer go up and come
        down through the ordinary dedupe rather than around it - and what makes
        a layer appearing count as activity and wake a dimmed pad.
        """
        slot_key = None if slots is None else tuple(
            (s.path, s.state) for s in slots
        )
        return (state, slot_key, overlay)

    def close(self) -> None:
        device, self._device = self._device, None
        _unregister_driving(self)
        if device is None:
            return
        # Only clean up after ourselves if we were ever driving. A renderer
        # constructed with lighting off has not touched the LEDs, and blanking
        # them on the way out would be taking over the pad to *stop* using it.
        self.hand_back(device=device)
        if not self._borrowed:
            close_shared()

    def hand_back(
        self,
        device: Optional[Device] = None,
        lighting: Optional[LightingConfig] = None,
    ) -> bool:
        """Apply ``lighting.on_exit`` now, at most once. Never raises.

        The one entry point for "we are done driving this pad", called by
        :meth:`close`, by :meth:`apply_config` when lighting is switched off,
        and by :func:`release_lighting` from ``atexit`` and from the signal
        handlers. Returns whether it did anything, and remembers that it did, so
        a SIGINT that both trips the guard *and* unwinds the run loop does not
        write the same frame twice.
        """
        device = device if device is not None else self._device
        lighting = lighting if lighting is not None else self.lighting
        if device is None or self._released or not lighting.enabled:
            return False
        self._released = True
        _unregister_driving(self)
        # If the process turns out to survive this - a SIGHUP the shell ignores,
        # lighting switched off and then on again - the pad is now showing our
        # exit look, and nothing we believe about it is true any more.
        self._frame = None
        self._dim_asserted = False
        try:
            self._apply_exit_state(device, lighting)
        except Exception:  # noqa: BLE001 - never raise on the way out
            return False
        return True

    # -- message building (pure - asserted directly in the tests) ---------

    def messages_for(
        self,
        state: AgentState,
        light: Optional[StateLight] = None,
        slots: Optional[Sequence[AgentSlot]] = None,
        overlay: Optional[ActivityLight] = None,
        now: Optional[float] = None,
    ) -> List[dict]:
        """Every protocol message this state should produce, in send order.

        ``light`` overrides the configured look for this call only, which is how
        ``freemicro lights --color`` experiments without touching the config. It
        also means "one look, on the zones I drive", so an explicit ``light``
        never drags in the zones only an activity layer would claim - a preview
        must show what it was asked to show and nothing else.

        ``slots`` colours the six Agent Keys individually. ``None`` - the
        default, and what an explicit ``light`` implies - mirrors ``state``
        across all six, which is the ``mirror`` policy and the behaviour of
        every caller that has one look to show rather than six.

        ``overlay`` is the layer a live binding has put over all of it. It wins
        on the zones it names and changes nothing anywhere else.
        """
        base = light or self.light_for(state)
        lighting = self.lighting
        extra = () if light is not None else self._extra_paint_zones()
        battery = lighting.battery
        # The battery cue is a state-driven layer, so it only applies to a real
        # render (a live clock, no explicit ``light`` preview overriding it).
        show_battery = (
            light is None and now is not None and self._battery_low
            and battery.enabled
        )
        battery_zone = battery.zone
        battery_light = battery.light() if show_battery else None

        def eff(base_light: StateLight, flash_key: Optional[str]) -> StateLight:
            """Resolve the software effects (flash, then blink) for one element.

            ``now is None`` is the preview path: no clock, so nothing animates
            and the on-phase colour is shown.
            """
            if now is None:
                return base_light
            return self._effective_look(base_light, flash_key, now)

        def look(zone: str) -> Optional[StateLight]:
            """What backlight/underglow shows, or ``None`` if we do not drive it."""
            if overlay is not None and zone in overlay.zones:
                return eff(overlay, None)
            if battery_light is not None and zone == battery_zone:
                return eff(battery_light, None)
            if zone in lighting.zones:
                return eff(base, "state")
            if zone in extra:
                # A zone only a layer (an activity light, or the battery cue)
                # ever claims. Dark is not a decision about this frame, it is
                # what "we drive this zone and nothing is on it" looks like - the
                # same thing the factory sends the underglow when idle (§1b).
                return _DARK
            return None

        messages: List[dict] = []
        keys, ambient = look(ZONE_BACKLIGHT), look(ZONE_UNDERGLOW)
        if keys is not None or ambient is not None:
            messages.append(self._zone_message(keys, ambient))

        # Agent keys, resolved with the same precedence but able to go per-slot:
        # a held-key layer or the battery cue can seize the whole zone, otherwise
        # it is agent state - one colour per project when we have slots, mirrored
        # across six when we do not.
        agent_msg: Optional[dict] = None
        if overlay is not None and ZONE_AGENT_KEYS in overlay.zones:
            agent_msg = self._agent_keys_message(eff(overlay, None))
        elif battery_light is not None and battery_zone == ZONE_AGENT_KEYS:
            agent_msg = self._agent_keys_message(eff(battery_light, None))
        elif ZONE_AGENT_KEYS in lighting.zones:
            if slots is not None and light is None:
                # Only the owned keys: an un-owned slot is never allocated a
                # project (it comes back ``owned=False``) and is left out of the
                # message entirely, so Codex's colour on that key persists. With
                # the default all-six set every slot is owned and this is the
                # full six-entry array it always was.
                agent_msg = thstatus_message(
                    self._slot_entry(slot, now)
                    for slot in slots
                    if slot.owned
                )
            else:
                agent_msg = self._agent_keys_message(eff(base, "state"))
        elif ZONE_AGENT_KEYS in extra:
            agent_msg = self._agent_keys_message(_DARK)
        if agent_msg is not None:
            messages.append(agent_msg)
        return messages

    def _agent_keys_message(self, light: StateLight) -> dict:
        """One look across every Agent Key we own (all six by default).

        Built here from :func:`thread_entry` rather than :func:`all_agent_keys`
        so the owned-key restriction lives entirely in this renderer: a subset
        config (coexisting with Codex) yields a *partial* ``thstatus`` - an array
        with an entry for each owned key only, which the firmware treats as an
        update of just those keys and leaves the rest as Codex set them. With the
        default all-six set this is byte-identical to :func:`all_agent_keys`.
        This relies on the firmware doing partial ``thstatus`` updates
        (``docs/PROTOCOL.md``); it wants a quick real-hardware confirmation.

        The mirror policy, a held-key overlay and the battery cue all go through
        here, so none of them ever touch a key we do not own.
        """
        return thstatus_message(
            thread_entry(
                index, light.color, light.effect, light.brightness, light.speed
            )
            for index in self.owned_keys
        )

    def _effective_look(
        self, base: StateLight, flash_key: Optional[str], now: float
    ) -> StateLight:
        """The look one element actually wears now, after the software effects.

        Flash wins while its brief window is open (a state just entered a
        flash state): the element shows its colour for the first half of the
        window and goes dark for the second, one clean pulse that draws the eye
        to the state it has just entered without hiding it. Otherwise a blinking
        light is dark on its off phase and its own colour on its on phase.
        Everything else is unchanged.
        """
        until = self._flash_until.get(flash_key) if flash_key is not None else None
        if until is not None and now < until:
            elapsed = now - (until - FLASH_SECONDS)
            return base if elapsed < FLASH_SECONDS / 2.0 else _DARK
        if base.blink and not _blink_on(base.speed, now):
            return _DARK
        return base

    def _extra_paint_zones(self) -> Tuple[str, ...]:
        """Zones we paint (dark, or lit by a layer) beyond the state zones.

        The activity-light zones, plus the battery cue's zone when it is enabled
        and lands somewhere the state lighting does not already drive. Being in
        this set is what makes a zone painted *dark* in an ordinary frame and by
        auto-dim and on exit, so the battery pulse never gets stranded lit when
        the battery recovers or the pad blanks.
        """
        zones = list(self._activity_zones())
        battery = self.lighting.battery
        if battery.enabled:
            zone = battery.zone
            if zone not in self.lighting.zones and zone not in zones:
                zones.append(zone)
        return tuple(zones)

    # -- software effects: flash on entry, and the battery cue -------------

    def _note_flash_entries(
        self,
        state: AgentState,
        slots: "Optional[Sequence[AgentSlot]]",
        now: float,
    ) -> None:
        """Arm a brief flash for anything that just *entered* a flash state.

        Called only when the lighting model changed, and only ever flashes on an
        actual transition into a configured state - never on the first sight of
        a state (there is no previous to compare) and never every render. An
        empty ``flash_on`` disables it while still tracking the previous states,
        so turning it on later does not flash retroactively.
        """
        flash_on = self.lighting.flash_on
        if flash_on:
            if (
                self._prev_state is not None
                and state != self._prev_state
                and state in flash_on
            ):
                self._flash_until["state"] = now + FLASH_SECONDS
            if slots is not None:
                for slot in slots:
                    if slot.empty:
                        continue
                    was = self._prev_slot_states.get(slot.path)
                    if (
                        was is not None
                        and slot.state != was
                        and slot.state in flash_on
                    ):
                        self._flash_until[slot.path] = now + FLASH_SECONDS
        self._prev_state = state
        self._prev_slot_states = (
            {} if slots is None
            else {s.path: s.state for s in slots if not s.empty}
        )

    def _refresh_battery(self, now: float) -> None:
        """Re-read the cached battery reading, at most every ``poll_seconds``."""
        battery = self.lighting.battery
        if not battery.enabled:
            self._battery_low = False
            return
        last = self._battery_checked_at
        if last is not None and now - last < battery.poll_seconds:
            return
        self._battery_checked_at = now
        self._battery_low = self._read_battery_low(battery)

    def _read_battery_low(self, battery: Any) -> bool:
        """Whether the cached reading says the battery is low and not charging.

        A charging pad is never "low" for this purpose - the cue is a call to go
        plug it in, and it is already plugged in. An unreadable or absent
        reading is not low either: the pad's job is to under-claim, never to cry
        wolf about a battery it cannot see.
        """
        try:
            data = self._battery_reader()
        except Exception:  # noqa: BLE001 - a read hiccup keeps the last answer
            return self._battery_low
        if not isinstance(data, dict):
            return False
        level = data.get("battery")
        if level is None or bool(data.get("is_charging")):
            return False
        try:
            return int(level) <= battery.threshold
        except (TypeError, ValueError):
            return False

    def _activity_zones(self) -> Tuple[str, ...]:
        """Zones some binding's light can claim but the state lighting cannot.

        Read from the config rather than remembered from what we last sent: a
        fixed answer is what lets every frame paint every zone we drive, so
        taking a layer down is an ordinary frame and not an undo.
        """
        try:
            claimed = self.config.activity_zones
        except Exception:  # noqa: BLE001 - a config oddity must not go dark
            return ()
        zones = self.lighting.zones
        return tuple(zone for zone in claimed if zone not in zones)

    def _slot_entry(self, slot: AgentSlot, now: Optional[float] = None) -> dict:
        """One ``v.oai.thstatus`` entry for one Agent Key.

        An empty slot is **off**, not dim: colour 0, brightness 0, effect 0.
        That is the factory's "no agent assigned" payload
        (``docs/FACTORY-DEFAULTS.md`` §1a), and it is what makes a lit key
        countable - three glowing keys means three live projects.

        ``now`` lets one key blink, or flash on entry, while its five neighbours
        stay solid: the software effects are resolved per slot here, keyed on the
        slot's own project path, so they compose with the per-key model instead
        of fighting it.
        """
        if slot.empty:
            return thread_entry(
                slot.index, 0, effect="off", brightness=0.0, speed=0.0
            )
        base = self.light_for(slot.state)
        light = base if now is None else self._effective_look(base, slot.path, now)
        return thread_entry(
            slot.index, light.color, light.effect, light.brightness, light.speed
        )

    def _zone_message(
        self,
        keys: Optional[StateLight],
        ambient: Optional[StateLight],
        lighting: Optional[LightingConfig] = None,
    ) -> dict:
        """Build the backlight/underglow message for the configured method.

        The two sides are passed separately because they can genuinely differ:
        an activity layer may own the underglow while agent state owns the
        backlight. ``None`` means "we are not driving that side", which is not
        the same as "that side is off" and must not be sent as one.

        ``v.oai.rgbcfg`` is the default because it is the one that works:
        verified by eye on firmware v0.4.1 driving the underglow, and the same
        method the vendor app uses for every surface. ``lights.preview`` is
        accepted by the firmware and changes nothing visible - it stays
        selectable for debugging only. See ``docs/PROTOCOL.md``.
        """
        lighting = lighting if lighting is not None else self.lighting
        if lighting.method == "rgbcfg":
            return rgbcfg_message(
                keys=None if keys is None else rgbcfg_side(
                    keys.color, keys.effect, keys.brightness, keys.speed
                ),
                ambient=None if ambient is None else rgbcfg_side(
                    ambient.color, ambient.effect, ambient.brightness,
                    ambient.speed,
                ),
            )
        self._request_id += 1
        return preview_message(
            backlight=None if keys is None else keys.to_zone(),
            underglow=None if ambient is None else ambient.to_zone(),
            request_id=self._request_id,
        )

    def _look_messages(
        self, light: StateLight, lighting: Optional[LightingConfig] = None
    ) -> List[dict]:
        """One look across every zone we drive. The whole pad, one colour.

        "Every zone we drive" includes the ones only an activity layer ever
        claims. This builds the blank frame and the exit frame, and a zone that
        can be lit by a held key but never blanked by auto-dim or on the way out
        is exactly the stranded colour this feature must not create.
        """
        lighting = lighting if lighting is not None else self.lighting
        extra = self._extra_paint_zones()
        drives = {
            ZONE_BACKLIGHT: lighting.drives_backlight or ZONE_BACKLIGHT in extra,
            ZONE_UNDERGLOW: lighting.drives_underglow or ZONE_UNDERGLOW in extra,
            ZONE_AGENT_KEYS: (
                lighting.drives_agent_keys or ZONE_AGENT_KEYS in extra
            ),
        }
        messages: List[dict] = []
        if drives[ZONE_BACKLIGHT] or drives[ZONE_UNDERGLOW]:
            messages.append(
                self._zone_message(
                    light if drives[ZONE_BACKLIGHT] else None,
                    light if drives[ZONE_UNDERGLOW] else None,
                    lighting,
                )
            )
        if drives[ZONE_AGENT_KEYS]:
            # Blank and exit frames only ever touch the keys we own, so
            # auto-dim and blank-on-exit never darken a key Codex is driving.
            messages.append(self._agent_keys_message(light))
        return messages

    def _blank_messages(self, lighting: Optional[LightingConfig] = None) -> List[dict]:
        """The factory's all-off payload, for the zones we drive.

        ``{"e": 0, "b": 0, "s": 0, "c": 0}`` on every surface: the same thing
        the vendor app sends on auto-dim and on quit
        (``docs/FACTORY-DEFAULTS.md`` §1d and §4). Dim means dark. Reducing
        brightness instead is a different behaviour and users notice.
        """
        return self._look_messages(_DARK, lighting)

    def paint(self, messages: List[dict]) -> bool:
        """Put one caller-built frame on the wire, through the guarded path.

        For callers that need a look this renderer would not choose on its own.
        The web UI's live preview is the case that motivated it: the user picks
        one colour and expects all six Agent Keys to show it, whereas
        :meth:`render` deliberately paints each key its own project's state.
        So preview builds its frame with :meth:`messages_for` and hands it here.

        The reason this exists at all, rather than callers sending to the device
        themselves: :meth:`_send` is the **only** place that registers a
        renderer as driving the pad, and that registration is what arms the
        ``atexit``/SIGTERM guard. Writing straight to the device lit the pad
        with nothing tracking it, so killing the process stranded it glowing.
        Retries, failure reporting and the write backoff come along too.
        """
        return self._send(messages)

    # -- internals --------------------------------------------------------

    def _send(self, messages: List[dict]) -> bool:
        """Put one frame on the wire, or note why it did not go.

        Returns whether every message went out. A ``False`` here is never
        terminal: the caller leaves its cached frame unset, and the next render
        tick tries again once the backoff allows it.
        """
        device = self._device
        if device is None:
            return False
        now = self._clock()
        if self._failures and now < self._retry_at:
            # Inside the backoff window. Silent by construction: this is the
            # branch a genuinely absent pad takes on every single tick.
            return False
        for message in messages:
            try:
                device.send(message)
            except Exception as exc:  # noqa: BLE001 - a dropped pad must not kill the loop
                self._note_failure(exc, now)
                return False
        if self._failures:
            self._note_recovery(now)
        self._sends += 1
        self._released = False
        _register_driving(self)
        return True

    def _note_failure(self, exc: BaseException, now: float) -> None:
        """Schedule the retry, and say it once - not once per tick."""
        self._failures += 1
        step = WRITE_RETRY_BACKOFF[
            min(self._failures - 1, len(WRITE_RETRY_BACKOFF) - 1)
        ]
        self._retry_at = now + step
        # A burst can fail halfway, leaving the underglow updated and the keys
        # stale, so nothing we believe about the pad survives a failed write.
        self._frame = None
        self._dim_asserted = False
        if self._failures == 1:
            self._failed_at = now
            ladder = ", ".join(f"{s:g}s" for s in WRITE_RETRY_BACKOFF[:-1])
            self._notify(
                f"write failed: {exc}\n"
                f"The pad still shows its last colour. Retrying in {ladder},"
                f"\nthen every {WRITE_RETRY_BACKOFF[-1]:g}s until one lands."
            )

    def _note_recovery(self, now: float) -> None:
        """Say it came back, and be careful what that claims.

        ``docs/PROTOCOL.md``: a wrongly framed write returns
        ``kIOReturnSuccess`` and is silently discarded. So the strongest honest
        statement is that the write left the machine - not that the LEDs
        changed - and this message says exactly that much.
        """
        failures, self._failures = self._failures, 0
        self._retry_at = 0.0
        elapsed = max(0.0, now - self._failed_at)
        plural = "" if failures == 1 else "s"
        self._notify(
            f"writes are going through again after {failures} failure{plural} "
            f"({elapsed:.0f}s);\nthe current colours were just sent again. This "
            "pad acks writes it\nthrows away, so this is not proof the LEDs "
            "changed. Glance at them."
        )

    def _apply_exit_state(
        self, device: Device, lighting: Optional[LightingConfig] = None
    ) -> None:
        """Leave the pad in a sane state when we stop driving it.

        ``lighting`` names the config to obey, which is not always the current
        one: when lighting is switched *off* mid-run we hand the pad back the
        way the config that was driving it asked us to.

        Deliberately not routed through :meth:`_send`: this runs on the way out,
        possibly from a signal handler, where a backoff window we happen to be
        inside must not be the reason the pad stays lit, and where there is
        nobody left to read a message about a failure.
        """
        lighting = lighting if lighting is not None else self.lighting
        mode = lighting.on_exit
        if mode == "leave":
            return
        if mode == "off":
            # Blank, not dimmed. Handing the device back dark is what the vendor
            # app does on quit, and it is good manners when we share the LEDs.
            exit_light = _DARK
        else:  # "breath" - a calm idle pulse in the idle colour
            idle = self.light_for(AgentState.IDLE)
            exit_light = StateLight(
                color=idle.color, effect=parse_effect("breath"),
                brightness=idle.brightness, speed=0.15,
            )
        for message in self._look_messages(exit_light, lighting):
            try:
                device.send(message)
            except Exception:  # noqa: BLE001 - best effort on the way out
                return


__all__ = [
    "ALERT_STATES",
    "BLINK_HALF_MAX",
    "BLINK_HALF_MIN",
    "FLASH_SECONDS",
    "WRITE_RETRY_BACKOFF",
    "MicroLedsRenderer",
    "release_lighting",
]
