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
from typing import Any, Callable, List, Optional, Sequence, Tuple

from freemicro.agentkeys import AgentSlot, SlotResolver
from freemicro.device import Device, close_shared, shared_device
from freemicro.device.lighting import (
    all_agent_keys,
    parse_effect,
    preview_message,
    rgbcfg_message,
    rgbcfg_side,
    thread_entry,
    thstatus_message,
)
from freemicro.padconfig import LightingConfig, PadConfig, PadConfigError, StateLight
from freemicro.padconfig import load as load_padconfig
from freemicro.renderers.base import Renderer, register
from freemicro.state.engine import AgentState

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
    ) -> None:
        # A caller-supplied device is borrowed; otherwise we use (and release)
        # the process-wide shared handle. Either way we never open a second one.
        self._device = device
        self._borrowed = device is not None
        self._config = config
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
        #: Consecutive failed writes, and when the next attempt may go out.
        self._failures = 0
        self._failed_at = 0.0
        self._retry_at = 0.0
        #: True once we have applied ``on_exit`` and are no longer driving.
        self._released = False
        #: Frames that actually reached the device. Read by the lighting owner
        #: to tell "we just sent something real" from "nothing happened".
        self._sends = 0

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
        self._published = ()
        self._frame = None
        self._model = None
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

    def slots(self) -> Optional[List[AgentSlot]]:
        """The six slots as they should be lit, or ``None`` to mirror.

        ``None`` means "fall back to the single-colour behaviour", which is
        what ``policy: mirror`` asks for and also what happens when there is no
        session store to read.
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
            resolved = self._resolver.resolve(store.sessions())
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

    def render(self, state: AgentState) -> None:
        """Send this state's lighting. A no-op if nothing changed.

        Skipping unchanged frames is not just an optimisation: each lighting
        call *replaces* the previous one, so re-sending on every poll tick would
        restart animated effects several times a second.

        The comparison is over the whole frame - the resolved state *and* the
        six slots - because a slot can change state while the winning state
        does not (project A finishes while project B is still working).

        This is also the auto-dim clock. Nothing else ticks often enough to be
        one, and the alternative - a timer thread writing to the pad behind the
        run loop's back - would put a second writer on the channel that carries
        key events.
        """
        if self._device is None or not self.lighting.enabled:
            return
        now = self._clock()
        slots = self.slots()
        frame = self._frame_key(state, slots)
        if frame != self._model:
            # A change in the lighting model counts as activity and wakes the
            # pad, exactly as it does for the vendor app (§4).
            self._model = frame
            self._wake(now)

        if self._dimmed:
            # Dark is the frame now. Re-send it only if something suggests the
            # pad is no longer showing it (a failed write, or a reassert).
            if not self._dim_asserted:
                self._dim_asserted = self._send(self._blank_messages())
            return

        if frame != self._frame:
            if self._send(self.messages_for(state, slots=slots)):
                self._frame = frame
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
        state: AgentState, slots: Optional[Sequence[AgentSlot]]
    ) -> Tuple[Any, ...]:
        """Everything that affects what the pad shows, and nothing else."""
        if slots is None:
            return (state, None)
        return (state, tuple((s.path, s.state) for s in slots))

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
    ) -> List[dict]:
        """Every protocol message this state should produce, in send order.

        ``light`` overrides the configured look for this call only, which is how
        ``freemicro lights --color`` experiments without touching the config.

        ``slots`` colours the six Agent Keys individually. ``None`` - the
        default, and what an explicit ``light`` implies - mirrors ``state``
        across all six, which is the ``mirror`` policy and the behaviour of
        every caller that has one look to show rather than six.
        """
        light = light or self.light_for(state)
        lighting = self.lighting
        messages: List[dict] = []

        if lighting.drives_backlight or lighting.drives_underglow:
            messages.append(self._zone_message(light))
        if lighting.drives_agent_keys:
            if slots is None:
                messages.append(
                    all_agent_keys(
                        light.color, light.effect, light.brightness, light.speed
                    )
                )
            else:
                messages.append(
                    thstatus_message(self._slot_entry(slot) for slot in slots)
                )
        return messages

    def _slot_entry(self, slot: AgentSlot) -> dict:
        """One ``v.oai.thstatus`` entry for one Agent Key.

        An empty slot is **off**, not dim: colour 0, brightness 0, effect 0.
        That is the factory's "no agent assigned" payload
        (``docs/FACTORY-DEFAULTS.md`` §1a), and it is what makes a lit key
        countable - three glowing keys means three live projects.
        """
        if slot.empty:
            return thread_entry(
                slot.index, 0, effect="off", brightness=0.0, speed=0.0
            )
        light = self.light_for(slot.state)
        return thread_entry(
            slot.index, light.color, light.effect, light.brightness, light.speed
        )

    def _zone_message(
        self, light: StateLight, lighting: Optional[LightingConfig] = None
    ) -> dict:
        """Build the backlight/underglow message for the configured method.

        ``v.oai.rgbcfg`` is the default because it is the one that works:
        verified by eye on firmware v0.4.1 driving the underglow, and the same
        method the vendor app uses for every surface. ``lights.preview`` is
        accepted by the firmware and changes nothing visible - it stays
        selectable for debugging only. See ``docs/PROTOCOL.md``.
        """
        lighting = lighting if lighting is not None else self.lighting
        if lighting.method == "rgbcfg":
            side = rgbcfg_side(
                light.color, light.effect, light.brightness, light.speed
            )
            return rgbcfg_message(
                keys=side if lighting.drives_backlight else None,
                ambient=side if lighting.drives_underglow else None,
            )
        zone = light.to_zone()
        self._request_id += 1
        return preview_message(
            backlight=zone if lighting.drives_backlight else None,
            underglow=zone if lighting.drives_underglow else None,
            request_id=self._request_id,
        )

    def _look_messages(
        self, light: StateLight, lighting: Optional[LightingConfig] = None
    ) -> List[dict]:
        """One look across every zone we drive. The whole pad, one colour."""
        lighting = lighting if lighting is not None else self.lighting
        messages: List[dict] = []
        if lighting.drives_backlight or lighting.drives_underglow:
            messages.append(self._zone_message(light, lighting))
        if lighting.drives_agent_keys:
            messages.append(
                all_agent_keys(
                    light.color, light.effect, light.brightness, light.speed
                )
            )
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
    "WRITE_RETRY_BACKOFF",
    "MicroLedsRenderer",
    "release_lighting",
]
