"""Turn Codex Micro events into whatever the user bound them to.

The pad's keys emit no scancodes (see :mod:`freemicro.device.codex_micro`), so
pressing one does nothing until something listens. This module *is* that
listener, and it is deliberately the only place where "a device event happened"
meets "a user-configured action should run".

It is written to be testable end to end without hardware: :meth:`Bridge.decode`
is a pure function from a protocol message to a list of input ids, and
:meth:`Bridge.handle` dispatches through an injectable
:class:`~freemicro.input.actions.Backend`. The test suite drives both with
recorded protocol messages and asserts on what *would* have been typed.

The thumbstick is the one input with two personalities, selected by
``joystick.mode``:

* ``directions`` - :class:`JoystickTracker` turns each deflection into a single
  bindable flick. Edge-triggered, uses the large *action* deadzone.
* ``pointer`` (the default) - :mod:`freemicro.input.pointer` takes the raw
  vector and drives the cursor by velocity on its own clock. Produces no inputs
  at all, so nothing here dispatches.

Two keys at once
----------------
Pad keys are not independent, and this module is where that stops being an
assumption. Two rules govern what a second key does:

**A press is suppressed while a ``hold`` binding is down.** ``hold`` presses
*real* modifier keys and keeps them there, which is what makes push-to-talk
work - and which also means every keystroke any other key produces while it is
down is silently modified into something else. See :data:`MODIFIER_SAFE_KINDS`.

**Two keys pressed together can be one binding.** See :meth:`Bridge.press` for
the resolution rule and what it costs.

Saying when a binding is live
-----------------------------
A binding can carry a ``light`` - what the pad should show for as long as that
binding is doing its thing. This module is the only place that knows when that
starts and stops, so it reports it, through ``on_activity``, and knows nothing
else about lighting: it hands over the config's own object and lets whoever is
driving the LEDs decide. See :meth:`Bridge._light` for why "live" here means
*the key is down* and nothing more ambitious.
"""

from __future__ import annotations

import atexit
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from freemicro.device.codex_micro import EVENT_JOYSTICK, EVENT_KEY
from freemicro.input.actions import (
    HOLD_KINDS,
    MODIFIER_HOLDING_KINDS,
    MODIFIER_SAFE_KINDS,
    Action,
    ActionError,
    Backend,
    double_tap_combo,
    is_latching,
    perform,
    release,
)
from freemicro.input import latch as latchmod
from freemicro.input.pointer import Pointer, PointerVector
from freemicro.padconfig import (
    DEFAULT_ACTIVITY_TIMEOUT,
    ENCODER_TICKS,
    JoystickConfig,
    PadConfig,
    chord_key,
    chord_label,
)

# Both classifications are declared on the action kinds themselves
# (``ActionSpec.modifier_safe`` / ``holds_keys``) and derived in
# :mod:`freemicro.input.actions`, so a new kind is classified where it is
# written rather than in a set over here that nobody editing it would think to
# look at. Re-exported under the names this module has always used; see the
# constants there for why one is an allowlist and why it is not ``HOLD_KINDS``.

#: Monotonic, never the wall clock: an NTP step must not expire a settle window.
_monotonic = time.monotonic

#: How long a *physical* ``hold`` may stay down before the bridge lets go of it
#: on its own, as a backstop for a lost key-up.
#:
#: A ``hold`` presses real modifier keys and holds them until the pad reports the
#: release. That release can be lost mid-session - a Bluetooth blip while the key
#: is held - and nothing in the clean-exit paths (:func:`quartz.release_all`,
#: :meth:`Bridge.release_held_keys`, close, reload, disconnect) covers a drop
#: during *normal operation*. Left stuck, macOS believes Ctrl+Cmd are down: every
#: trackpad click becomes a right-click and every other pad key is suppressed.
#:
#: **120 seconds**, deliberately: comfortably longer than the longest real
#: push-to-talk hold (speaking one long sentence is 20-30 s, so this is over
#: four times that and cannot cut off someone genuinely still talking), short
#: enough to bound the damage, and **equal to** :data:`DEFAULT_ACTIVITY_TIMEOUT`
#: so the physical hold and the activity light recover on the same clock rather
#: than at two different times. It is only ever the honest last resort: a
#: repeated key-down reconciles a lost release *instantly* (see
#: :meth:`Bridge._reconcile_stale_hold`), so the cap matters only when a held key
#: is never touched again.
DEFAULT_MAX_HOLD_SECONDS = DEFAULT_ACTIVITY_TIMEOUT


def _one(dispatch: Optional["Dispatch"]) -> List["Dispatch"]:
    """A dispatch as a list, dropping the ``None`` that means "nothing to say"."""
    return [] if dispatch is None else [dispatch]


@dataclass(frozen=True)
class InputEvent:
    """One decoded pad input, and which half of the press it is.

    Release matters because the pad reports it (``v.oai.hid`` ``act`` 0), which
    is what makes true hold-to-talk possible. Most actions ignore it.
    """

    input_id: str
    pressed: bool = True


@dataclass(frozen=True)
class Dispatch:
    """The outcome of one input firing - enough to print a useful log line.

    ``input_id`` is a chord id (``"AG00+AG01"``) when a chord fired, so the log
    and the caller both name the thing the user actually pressed.
    """

    input_id: str
    action: Optional[Action] = None
    ok: bool = True
    error: str = ""
    #: Input id of the ``hold`` binding that blocked this press, if any. A
    #: suppressed press is **not** an error - it is a deliberate refusal - so
    #: ``ok`` stays true and this is what callers test.
    suppressed_by: str = ""
    #: What that binding has physically down, named in the log line.
    holding: str = ""
    #: For a chord-capable key with no binding of its own: the chords it is now
    #: standing by to complete. Without this the press prints as "unmapped",
    #: which is the opposite of the truth.
    chord: str = ""
    #: This dispatch is the bridge recovering a stuck physical ``hold`` whose
    #: key-up was lost, not a key the user pressed. Surfaced in the log because a
    #: silent auto-recovery teaches nothing. Not an error: ``ok`` stays true.
    stuck_release: bool = False

    @property
    def bound(self) -> bool:
        return self.action is not None

    @property
    def suppressed(self) -> bool:
        """Whether this press was refused because a ``hold`` binding is down."""
        return bool(self.suppressed_by)

    def describe(self) -> str:
        if self.stuck_release:
            return f"released a stuck hold on {self.input_id} - its key-up was lost"
        if self.action is None:
            if self.chord:
                return f"chord key - held, ready for {self.chord}"
            return "unmapped"
        summary = f"{self.action.label}: {self.action.describe()}"
        if self.suppressed_by:
            held = f" holding {self.holding}" if self.holding else " held down"
            return f"{summary}  [NOT SENT - {self.suppressed_by} is{held}]"
        return summary


class SettleTimer:
    """Wakes the bridge when a deferred press's settle window runs out.

    Deliberately not a :class:`threading.Timer` per press: a key press is a
    human-rate event but it is also on the hot path, and spawning a thread per
    press to almost always cancel it milliseconds later is work for nothing.
    One daemon thread parks on an event and is woken either by a new deadline
    or by the deadline it is already waiting for.

    Both the clock and the wait are injectable, and ``autostart=False`` plus
    :meth:`step` lets the tests prove the timing from data rather than from
    ``sleep``. Same discipline as :class:`freemicro.input.pointer.PointerLoop`,
    for the same reason.
    """

    def __init__(
        self,
        on_expire: Callable[[], None],
        *,
        clock: Optional[Callable[[], float]] = None,
        sleep: Optional[Callable[[float], None]] = None,
        autostart: bool = True,
        idle_seconds: float = 0.5,
        name: str = "freemicro-chord",
    ) -> None:
        self.on_expire = on_expire
        self.clock = clock or _monotonic
        self.sleep = sleep if sleep is not None else self._wait
        self.autostart = autostart
        self.idle_seconds = idle_seconds
        self.name = name
        self.error: Optional[BaseException] = None
        self._deadline: Optional[float] = None
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    @property
    def running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    @property
    def deadline(self) -> Optional[float]:
        return self._deadline

    def schedule(self, deadline: Optional[float]) -> None:
        """Ask to be woken at ``deadline``; ``None`` cancels the wait."""
        self._deadline = deadline
        if deadline is None:
            return
        if self.autostart and not self.running:
            self.start()
        self._wake.set()

    def step(self) -> bool:
        """Expire the deadline if it is due. No waiting; used by the tests."""
        deadline = self._deadline
        if deadline is None or self.clock() < deadline:
            return False
        self._deadline = None
        self.on_expire()
        return True

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self.error = None
        thread = threading.Thread(
            target=self._run, name=self.name, daemon=True
        )
        self._thread = thread
        atexit.register(self.stop)
        thread.start()

    def stop(self, timeout: float = 1.0) -> None:
        """Stop the thread, with a bounded join. Idempotent."""
        thread = self._thread
        self._deadline = None
        self._stop.set()
        self._wake.set()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)
        self._thread = None
        try:
            atexit.unregister(self.stop)
        except Exception:  # pragma: no cover - unregister never raises on 3.9
            pass

    def _wait(self, seconds: float) -> None:
        """The default wait: bounded, and interruptible by a new deadline."""
        self._wake.wait(seconds)

    def _run(self) -> None:
        while not self._stop.is_set():
            # Cleared before reading the deadline, so a deadline set mid-pass is
            # still waiting for us at the wait below rather than being lost.
            self._wake.clear()
            deadline = self._deadline
            if deadline is None:
                self.sleep(self.idle_seconds)
                continue
            remaining = deadline - self.clock()
            if remaining > 0.0:
                self.sleep(min(remaining, self.idle_seconds))
                continue
            self._deadline = None
            try:
                self.on_expire()
            except Exception as exc:  # noqa: BLE001 - see PointerLoop
                # A backend that cannot deliver at all must not spin forever.
                self.error = exc
                return


@dataclass
class _Latch:
    """One latching MIC key: its state machine and the binding driving it.

    The action is kept alongside the machine so the timer thread can re-send the
    toggle tap and re-assert the light without another config lookup, and so a
    config reload replaces the pair wholesale rather than leaving a machine
    pointed at a binding the user just deleted.
    """

    machine: "latchmod.LatchMachine"
    action: Action


@dataclass
class _Unresolved:
    """A chord-capable key that is down and has not yet decided what it is.

    ``deadline`` is ``None`` for a key with no binding of its own: there is
    nothing to hold back, so it simply stands by as a chord partner for as long
    as it is held, and costs no latency at all.
    """

    input_id: str
    action: Optional[Action] = None
    deadline: Optional[float] = None


class JoystickTracker:
    """Convert the analogue stick into discrete, once-per-flick inputs.

    The pad streams a continuous angle/distance pair, so a single flick produces
    dozens of messages. We fire on the *rising edge* - the first sample past the
    deadzone - and re-arm only once the stick has clearly returned to centre.
    The hysteresis gap stops a stick resting near the threshold from
    machine-gunning keystrokes into your terminal.

    This is ``joystick.mode: "directions"``. It is the right shape for four
    bindable flicks and the wrong shape for pointing, which is why
    :mod:`freemicro.input.pointer` exists rather than this growing a repeat
    rate. Note it uses the *action* deadzone (0.6), not the pointer's.
    """

    #: Fraction of the deadzone the stick must fall back under to re-arm.
    REARM_RATIO = 0.75

    def __init__(self, config: JoystickConfig) -> None:
        self.config = config
        self._armed = True

    def update(self, angle: float, distance: float) -> Optional[str]:
        """Feed one sample; return an input id if a flick just started."""
        if distance < self.config.deadzone * self.REARM_RATIO:
            self._armed = True
            return None
        if distance >= self.config.deadzone and self._armed:
            self._armed = False
            return self.config.direction_for(angle)
        return None


class Bridge:
    """Route pad events to configured actions.

    Chord resolution
    ----------------
    ``"AG00+AG01"`` in ``bindings`` binds the two keys pressed together. The
    hard part is not matching the pair, it is that key-down for the first key
    arrives *before* anything can know a second one is coming - so if ``AG00``
    is bound on its own as well, something has to decide which of the two the
    user meant. The rule, in full:

    1. A key that appears in **no** chord fires the instant it goes down.
       Nothing about this feature may slow down a key that cannot chord, and
       nothing does: the cost is one set lookup.
    2. A key that appears in a chord but has **no binding of its own** also
       fires nothing and waits nothing. It simply stands by as a partner while
       it is held. This is the zero-latency way to build a chord, and the one
       the docs recommend: give one key ``{"action": "none"}``.
    3. A key that appears in a chord **and** has a binding of its own is held
       back for ``chords.settle_ms`` (default 45 ms). If a partner goes down
       inside that window the chord fires and the solo binding never does. If
       the window runs out, or the key is released first, the solo binding
       fires and the key can no longer start a chord - so a chord is never
       completed *after* one of its members has already acted.

    Order does not matter: chords are keyed by their members, sorted.

    Releases follow the same resolution. Both key-ups of a chorded pair are
    swallowed, so nothing fires a stray solo release; if the chord's action was
    a ``hold``, the *first* of the two key-ups releases it, because there is no
    coherent meaning to holding a chord you have half let go of. A press that
    was suppressed (see :data:`MODIFIER_SAFE_KINDS`) has its release swallowed
    too - it was never sent, so its key-up must not be either. Everything a
    ``hold`` actually pressed remains registered in
    :func:`freemicro.input.quartz.release_all`, which is what guarantees the
    key-ups on the paths that skip all of this: Ctrl-C, SIGTERM, re-exec.
    """

    def __init__(
        self,
        config: PadConfig,
        backend: Backend,
        pointer: Optional[Pointer] = None,
        *,
        clock: Optional[Callable[[], float]] = None,
        sleep: Optional[Callable[[float], None]] = None,
        autostart: bool = True,
        on_dispatch: Optional[Callable[[Dispatch], None]] = None,
        on_activity: Optional[Callable[[str, Optional[Any]], None]] = None,
        max_hold_seconds: Optional[float] = None,
    ) -> None:
        self.backend = backend
        self.clock = clock or _monotonic
        #: The physical-hold backstop. See :data:`DEFAULT_MAX_HOLD_SECONDS`.
        self.max_hold_seconds = (
            DEFAULT_MAX_HOLD_SECONDS if max_hold_seconds is None else max_hold_seconds
        )
        #: Called with ``(input_id, light)`` when a binding that carries a
        #: ``light`` goes live, and with ``(input_id, None)`` when it stops.
        #: ``light`` is the config's
        #: :class:`freemicro.padconfig.ActivityLight`, passed through
        #: untouched - this module deliberately knows nothing about colours.
        self.on_activity = on_activity
        #: Called with any dispatch produced off the event path - i.e. a
        #: deferred press whose settle window ran out while nothing else was
        #: happening. Unset, those dispatches are queued and returned by the
        #: next :meth:`handle` instead, which in practice is the matching
        #: key-up a few tens of milliseconds later.
        self.on_dispatch = on_dispatch
        self._lock = threading.RLock()
        #: Held around delivery only, so that at most one action is in flight
        #: whichever thread started it. See :meth:`_run`.
        self._deliver = threading.Lock()
        #: input id -> the ``hold`` action currently physically down.
        self._holding: Dict[str, Action] = {}
        #: input id -> when its physical hold went down, for the max-hold cap.
        #: Moves in lock-step with :attr:`_holding`.
        self._hold_started: Dict[str, float] = {}
        #: input id -> its double-tap detector, for a ``hold`` that also fires a
        #: second shortcut on a double-tap. Empty unless a binding opts in. The
        #: physical hold is untouched; this only watches the timing. See
        #: :class:`freemicro.input.latch.DoubleTapMachine`.
        self._doubletap: Dict[str, "latchmod.DoubleTapMachine"] = {}
        #: input ids whose ``light`` we have declared live and not yet retired.
        self._lit: Dict[str, bool] = {}
        #: Presses we refused; their releases must be refused to match.
        self._suppressed: Dict[str, bool] = {}
        #: Chord-capable keys that are down and undecided.
        self._unresolved: Dict[str, _Unresolved] = {}
        #: member input id -> the chord that consumed its press.
        self._chorded: Dict[str, Tuple[str, ...]] = {}
        #: Chords that have fired and not yet seen a member released.
        self._open_chords: Dict[Tuple[str, ...], Action] = {}
        #: Dispatches produced by the settle timer, awaiting a reader.
        self._deferred: List[Dispatch] = []
        #: input id -> its push-to-talk latch machine and binding. Empty unless
        #: a ``hold`` binding opted into ``latch``. See :mod:`freemicro.input.latch`.
        self._latch: Dict[str, _Latch] = {}
        #: When the latch timer should next re-assert a still-recording light,
        #: or ``None``. Kept apart from the machines' own window deadlines: a
        #: latch records indefinitely, but the activity overlay times a light out
        #: from the clock, so a live latch has to keep saying it is live.
        self._latch_refresh_at: Optional[float] = None
        #: The name of the frontmost app, as last handed to :meth:`set_frontmost`
        #: by the run loop. ``None`` means "unknown / no profile", which resolves
        #: to the base bindings. Read on the key path; the OS lookup that fills
        #: it happens on the run loop's tick, never here.
        self._frontmost: Optional[str] = None
        #: The resolved override map for :attr:`_frontmost`, so a press is an
        #: O(1) dict lookup and not a per-press scan of every profile. Empty
        #: whenever no profile is active - which is always, unless the config has
        #: profiles *and* a matching app is frontmost.
        self._overrides: Mapping[str, Action] = {}
        #: input id -> the exact action delivered on its last press, kept only
        #: for kinds whose *release* matters (holds, the push-to-talk latch, a
        #: long-press answer). The release replays this instead of re-resolving,
        #: so a profile ``hold`` pressed in one app and let go after you have
        #: switched to another still sends *its* key-up - never the new app's
        #: binding, and never a stuck modifier. See :meth:`_run`.
        self._pressed: Dict[str, Action] = {}
        self.settle = SettleTimer(
            self._expire, clock=self.clock, sleep=sleep, autostart=autostart
        )
        #: A second timer, mirroring ``settle``, for the latch machine's 350 ms
        #: waiting and suppressing windows and the light refresh. Separate so the
        #: chord settle path stays exactly as it was.
        self.latch_timer = SettleTimer(
            self._latch_expire, clock=self.clock, sleep=sleep,
            autostart=autostart, name="freemicro-latch",
        )
        #: A third timer, same discipline, for the max-hold cap: it wakes to let
        #: go of a physical ``hold`` whose key-up was lost and never came. Kept
        #: apart from the other two so the chord and latch paths are unchanged.
        self.hold_timer = SettleTimer(
            self._hold_expire, clock=self.clock, sleep=sleep,
            autostart=autostart, name="freemicro-hold",
        )
        self.config = config  # via the setter: builds the chord index
        self._joystick = JoystickTracker(config.joystick)
        self.pointer = pointer if pointer is not None else Pointer(
            config.joystick, move=self._move_pointer
        )
        #: The last stick vector we saw, for ``keys --dry-run``.
        self.last_vector: Optional[PointerVector] = None

    # -- configuration ----------------------------------------------------

    @property
    def config(self) -> PadConfig:
        return self._config

    @config.setter
    def config(self, config: PadConfig) -> None:
        """Adopt a reloaded config, and forget every half-made decision.

        A property for the same reason :attr:`joystick` is one. Anything still
        undecided - a press held back waiting for a partner, a chord whose
        key-ups have not arrived - was decided against the *old* file, and
        resolving it against the new one would run a binding the user has just
        deleted. Dropping them is the only coherent answer, and the keys are
        physically down anyway, so the next press re-establishes the truth.
        """
        # A latch left running under the old file would keep a dictation app
        # recording under a binding that may no longer exist. Stop it first, on
        # the old action, so the app gets its toggle-off tap while the key that
        # sends it is still the one the user configured.
        self.stop_latches("config reloaded")
        self._config = config
        self._chord_keys = frozenset(
            member for members in config.chords for member in members
        )
        with self._lock:
            self._unresolved.clear()
            self._chorded.clear()
            self._open_chords.clear()
            # The new file may define, drop or rewrite the profile for whatever
            # is frontmost, so the resolved overrides are recomputed against it
            # now rather than left describing the old bindings. A press latched
            # under the old file is dropped for the same reason the undecided
            # state above is: it was resolved against bindings that no longer
            # exist.
            self._overrides = config.resolve_profile(self._frontmost)
            self._pressed.clear()
            # A double-tap detector points at the old binding's timing; a reload
            # may have rebound or deleted the key. It holds nothing, so dropping
            # it strands nothing, and the next press rebuilds it.
            self._doubletap.clear()
        self.settle.schedule(None)

    @property
    def chord_keys(self) -> frozenset:
        """Every input id that takes part in a chord. Empty for most configs."""
        return self._chord_keys

    # -- per-app profiles -------------------------------------------------

    def set_frontmost(self, app: Optional[str]) -> bool:
        """Tell the bridge which app is frontmost, so profiles can resolve.

        Called by the run loop's tick from the *cached* value of a
        :class:`freemicro.input.frontmost.FrontmostWatcher` - never from the key
        path, and never from an OS call the key path would wait on. When the app
        actually changes, the matching profile's overrides are resolved once,
        here, so that every following press is a plain dict lookup.

        Returns whether the frontmost changed, so a caller can log a switch.
        Cheap and idempotent: an unchanged name does nothing, and a config with
        no profiles resolves to an empty override map every time.
        """
        with self._lock:
            if app == self._frontmost:
                return False
            self._frontmost = app
            self._overrides = self._config.resolve_profile(app)
            return True

    @property
    def frontmost(self) -> Optional[str]:
        """The app the bridge currently resolves profiles against."""
        return self._frontmost

    def _resolve(self, input_id: str) -> Optional[Action]:
        """The binding for ``input_id`` right now: profile override, else base.

        Both halves are plain dict lookups. When no profile is active
        (:attr:`_overrides` empty - the overwhelmingly common case) this is
        exactly ``self._config.action_for(input_id)`` was before profiles
        existed, so a config without them behaves byte for byte as it did.
        """
        overrides = self._overrides
        if overrides:
            override = overrides.get(input_id)
            if override is not None:
                return override
        return self._config.action_for(input_id)

    @property
    def joystick(self) -> JoystickTracker:
        return self._joystick

    @joystick.setter
    def joystick(self, tracker: JoystickTracker) -> None:
        """Swap in a tracker built from a reloaded config.

        A property rather than a plain attribute because two other things have
        to follow the same edit:

        * the pointer, or a stick still moving at the old speed while the
          flicks obey the new file is a half-applied reload; and
        * any key a ``hold`` binding is physically holding down. Its release
          is produced by matching the *old* binding, and a reload can rebind
          or delete it - after which nothing will ever send the key-up and the
          user is left with a stuck Ctrl. Letting go across a rebind is the
          only safe answer.
        """
        self._joystick = tracker
        self.pointer.configure(tracker.config)
        self.release_held_keys()

    def release_held_keys(self) -> int:
        """Let go of any key a ``hold`` binding left down. Never raises.

        Clears this bridge's idea of what is held as well as the backend's.
        They have to move together: a stale entry here would keep suppressing
        every press for the rest of the run, with nothing actually held down to
        justify it - a silent, permanently dead pad.

        Any activity light goes with them, and for the same reason: the moment
        we can no longer say a key is down is the moment the pad must stop
        claiming it.

        A latching push-to-talk holds nothing physically, but it does leave a
        dictation app recording, which is the same kind of debt: stopped here
        too, so every path that lets go of the keys also stops the recording and
        clears its light. See :meth:`stop_latches`.
        """
        self.stop_latches("released")
        self.release_lights()
        with self._lock:
            self._holding.clear()
            self._hold_started.clear()
            self._suppressed.clear()
            # Nothing is held any more, so no release is owed a replayed
            # binding. Leaving stale entries here would let a key-up that
            # arrives after this replay a hold we have just let go of.
            self._pressed.clear()
        self.hold_timer.schedule(None)
        try:
            return self.backend.release_held_keys()
        except Exception:  # noqa: BLE001 - shutdown must not fail on this
            return 0

    def close(self) -> None:
        """Release everything the bridge is holding. Idempotent.

        Must be called from the run loop's ``finally``: a re-exec never runs
        ``atexit``, so this is the only thing standing between a self-restart
        and a permanently held modifier key.

        A press still inside its settle window is dropped rather than flushed.
        It was held back to find out what the user meant, we are shutting down
        before finding out, and typing into whatever window is frontmost on the
        way out is the one outcome nobody asked for.
        """
        self.settle.stop()
        self.latch_timer.stop()
        self.hold_timer.stop()
        with self._lock:
            self._unresolved.clear()
            self._chorded.clear()
            self._open_chords.clear()
            self._doubletap.clear()
            self._pressed.clear()
            del self._deferred[:]
        self.release_held_keys()
        self.pointer.close()

    def _move_pointer(self, dx: int, dy: int) -> None:
        """The pointer loop's only route to the outside world."""
        self.backend.move_mouse(dx, dy, relative=True)

    # -- decoding ---------------------------------------------------------

    def decode(self, message: Mapping[str, Any]) -> List[InputEvent]:
        """Which inputs (if any) this protocol message fires.

        Every key, the dial press *and* the dial's rotation ticks (``ENC_CW`` /
        ``ENC_CC``) arrive the same way, so there is nothing special to do for
        the encoder. The thumbstick is genuinely analogue and gets its own
        edge detector.

        Pure apart from the joystick's edge state, which is what makes the whole
        event path testable from recorded messages.
        """
        method = message.get("m") or message.get("method")
        params = message.get("p")
        if params is None:
            params = message.get("params")

        if method == EVENT_KEY and isinstance(params, dict):
            key = params.get("k")
            if not isinstance(key, str) or not key:
                return []
            joystick = self.config.joystick
            if joystick.pointing and key and key == joystick.precision_key:
                # Consumed, not dispatched: one key cannot both slow the cursor
                # while held and run a binding on the same press without one of
                # the two being a surprise. The config layer warns if the key
                # also has a binding.
                self.pointer.set_precision(params.get("act") == 1)
                return []
            if key in ENCODER_TICKS:
                # Dial detents are momentary: one tick, no matching release, and
                # the firmware has been observed reporting them with act values
                # other than 1. Filtering on act would silently swallow every
                # dial turn, so we fire on any of them. There is no press/release
                # pair here, so nothing can double-trigger.
                return [InputEvent(key, pressed=True)]
            act = params.get("act")
            if act == 1:
                return [InputEvent(key, pressed=True)]
            if act == 0:
                return [InputEvent(key, pressed=False)]
            return []

        if method == EVENT_JOYSTICK and isinstance(params, dict):
            try:
                angle = float(params.get("a", 0.0))
                distance = float(params.get("d", 0.0))
            except (TypeError, ValueError):
                return []
            if self.config.joystick.pointing:
                # Pointing produces no inputs at all: motion happens on the
                # pointer's own tick, because holding the stick steady stops
                # the pad sending samples and an event-driven cursor would
                # stall exactly when the user is asking it to keep going.
                self.last_vector = self.pointer.update(angle, distance)
                return []
            fired = self._joystick.update(angle, distance)
            return [InputEvent(fired)] if fired else []

        return []

    # -- dispatch ---------------------------------------------------------

    def fire(self, input_id: str, pressed: bool = True) -> Optional[Dispatch]:
        """Run whatever is bound to ``input_id``, now, with no chord logic.

        Returns ``None`` for a release that nothing cares about - which is most
        of them, so callers don't have to filter noise out of their logs.

        The hold-suppression rule still applies here, because it is a safety
        rule and not a routing preference: there is no path through this module
        that may type under a held modifier.
        """
        return self._run(input_id, self._resolve(input_id), pressed)

    def _light(self, input_id: str, light: Optional[Any]) -> None:
        """Say that ``input_id``'s binding just went live, or stopped being.

        "Live" is *the pad key is down*, and nothing more ambitious, because
        that is the only thing this module can actually observe. A ``hold``
        binding is therefore exactly right - it is down for as long as your
        finger is - and a binding that fires and returns is lit for the length
        of a tap, which is honest but rarely useful; the config layer warns
        about that at load time rather than letting it be a surprise.

        Never raises. A colour is not worth a dead key path, and the layer this
        feeds has a timeout of its own for exactly the case where a message
        about letting go never arrives.
        """
        callback = self.on_activity
        if callback is None:
            return
        try:
            callback(input_id, light)
        except Exception:  # noqa: BLE001 - lighting must not break the keys
            pass

    def _end_light(self, input_id: str) -> None:
        """Retire ``input_id``'s light, if it has one up. Idempotent."""
        with self._lock:
            had = self._lit.pop(input_id, False)
        if had:
            self._light(input_id, None)

    def release_lights(self) -> int:
        """Retire every light this bridge has up. Returns how many. Never raises.

        The counterpart of :meth:`release_held_keys`, and called by it: the two
        answer the same question about different things the pad is left holding,
        and a run that lets go of a modifier while still claiming the key is
        down would be telling the user something untrue about their own pad.
        """
        with self._lock:
            lit, self._lit = list(self._lit), {}
        for input_id in lit:
            self._light(input_id, None)
        return len(lit)

    def _run(
        self, input_id: str, action: Optional[Action], pressed: bool
    ) -> Optional[Dispatch]:
        """Deliver one resolved binding. ``input_id`` may be a chord id.

        A binding whose *release* carries meaning - a ``hold``, a push-to-talk
        latch, a long-press answer - is latched on the way down and replayed on
        the way up, so the release runs the same binding the press did even if
        the frontmost app (and therefore the active profile) has changed in
        between. Without it a profile ``hold`` pressed in one app and released in
        another would resolve to the *new* app's binding on release, and the
        real key it pressed would never come back up.
        """
        if pressed:
            if action is not None and (
                is_latching(action) or action.kind in HOLD_KINDS
            ):
                with self._lock:
                    self._pressed[input_id] = action
        else:
            with self._lock:
                latched = self._pressed.pop(input_id, None)
            if latched is not None:
                action = latched
        if is_latching(action):
            # Routed before everything below so a plain hold stays byte-identical:
            # a latching hold never presses a real modifier, never registers in
            # ``_holding`` and never suppresses another key. See _run_latch.
            return self._run_latch(input_id, action, pressed)
        if not pressed:
            # Before the suppression check, not after: a press we refused never
            # lit anything, so this is a no-op there, and a press we *did*
            # deliver must give its light back on every release path there is.
            self._end_light(input_id)
            with self._lock:
                if self._suppressed.pop(input_id, False):
                    # We never sent the press, so sending the release would be
                    # a key-up for a key that was never down. The double-tap
                    # detector was not advanced on the suppressed press either,
                    # so it must not see this key-up.
                    return None
                self._holding.pop(input_id, None)
                self._hold_started.pop(input_id, None)
            self._schedule_hold_cap()
            # A double-tap hold watches its own timing beside the physical hold.
            # The key-up is half the gesture: a quick tap opens the window, the
            # second tap's key-up closes a completed pair. Nothing is emitted on
            # release - the second shortcut fires on the second *press*.
            machine = self._doubletap.get(input_id)
            if machine is not None:
                machine.release(self.clock())
            if action is None or action.kind not in HOLD_KINDS:
                return None
            try:
                with self._deliver:
                    release(action, self.backend)
            except ActionError as exc:
                return Dispatch(input_id, action, ok=False, error=str(exc))
            return Dispatch(input_id, action)

        if action is None:
            return Dispatch(input_id=input_id)

        # A fresh key-down for a key we already believe is physically held is
        # proof its key-up was lost: two downs with no up between them cannot
        # both be real. Let go of the stale hold before starting the new press,
        # so suppression ends and the light clears at once rather than waiting
        # out the cap. Definitive, not a guess, so it always runs here.
        self._reconcile_stale_hold(input_id)

        with self._lock:
            blocker = self._blocking_hold(action)
            if blocker is not None:
                self._suppressed[input_id] = True
            elif action.kind in MODIFIER_HOLDING_KINDS:
                # Registered before it is delivered, so a hold that fails
                # halfway is still something we know to let go of.
                self._holding[input_id] = action
                self._hold_started[input_id] = self.clock()
            if blocker is None and action.light is not None:
                self._lit[input_id] = True
        if blocker is not None:
            # A refused press does nothing, so it must not look like it did.
            return Dispatch(
                input_id=input_id,
                action=action,
                suppressed_by=blocker[0],
                holding=blocker[1],
            )
        if action.kind in MODIFIER_HOLDING_KINDS:
            self._schedule_hold_cap()
        # A double-tap hold fires its *second* shortcut here, on the press that
        # completes two quick taps - and before the physical hold goes down just
        # below, so the first tap is already released and the modifiers are clean
        # for it. The hold itself is never delayed: push-to-talk stays instant.
        self._maybe_double_tap(input_id, action)
        if action.light is not None:
            # Before delivery, for the same reason `_holding` is: the light
            # says "this key is down", which is already true, and a hold that
            # fails halfway still has to be something we know to take back.
            self._light(input_id, action.light)
        try:
            # One action at a time, whichever thread it came from. Before the
            # settle timer existed every action ran on the pad's read thread and
            # was serial by construction; deferring a press must not quietly
            # cost the pad that guarantee. Two `focus_session` presses landing
            # together are the case that shows why: unserialised they are two
            # osascript processes racing to raise a window, and which one wins
            # is anybody's guess. Serialised, the second simply follows the
            # first, which is the same thing that happens if you press the two
            # keys deliberately a moment apart.
            #
            # Held only around delivery, never with ``self._lock``, so a slow
            # action (``shell`` with ``wait``) cannot deadlock the key path -
            # it can only make the key path wait for it, exactly as it did
            # before it was deferred.
            with self._deliver:
                perform(action, self.backend)
        except ActionError as exc:
            with self._lock:
                self._holding.pop(input_id, None)
                self._hold_started.pop(input_id, None)
            self._schedule_hold_cap()
            # It did not happen, so the pad must stop saying it did.
            self._end_light(input_id)
            return Dispatch(input_id=input_id, action=action, ok=False, error=str(exc))
        return Dispatch(input_id=input_id, action=action)

    # -- lost-release backstops ------------------------------------------

    def _emit(self, dispatch: Dispatch) -> None:
        """Surface a dispatch produced off the return path (timer or recovery).

        The same routing :meth:`_expire` and :meth:`_latch_expire` use: straight
        to ``on_dispatch`` when a reader is wired, otherwise queued for the next
        :meth:`drain`. It is how an auto-recovery reaches the log instead of
        happening in silence.
        """
        if self.on_dispatch is not None:
            self.on_dispatch(dispatch)
        else:
            with self._lock:
                self._deferred.append(dispatch)

    def _force_release_hold(self, input_id: str, action: Action) -> None:
        """Let go of one physical hold now: the key, the state and the light.

        Idempotent - releasing a key that is already up is a no-op to the OS -
        and it clears the bridge's own idea of the hold so suppression stops and
        the light goes down. The counterpart, per binding, of
        :meth:`release_held_keys`. Never raises: it runs from a backstop.
        """
        with self._lock:
            self._holding.pop(input_id, None)
            self._hold_started.pop(input_id, None)
            self._doubletap.pop(input_id, None)
            # This *is* the release, so a real key-up that arrives later has
            # nothing left to replay - drop the latch so it does not send a
            # second, redundant key-up.
            self._pressed.pop(input_id, None)
        try:
            with self._deliver:
                release(action, self.backend)
        except ActionError:
            pass  # a stuck key is the worse outcome; take the light down anyway
        self._end_light(input_id)

    def _reconcile_stale_hold(self, input_id: str) -> None:
        """Recover a hold whose key-up was lost, on its own fresh key-down.

        Called at the top of a press: if we still believe this key is held, its
        previous release never arrived, and this new down is the proof. Release
        the stale hold and say so in the log, then let the press proceed. Does
        nothing for a key that is not currently held, so a normal press - and the
        second press of an ordinary double-tap, whose first release *did* arrive
        - never trips it.
        """
        with self._lock:
            stale = self._holding.get(input_id)
        if stale is None:
            return
        self._force_release_hold(input_id, stale)
        self._schedule_hold_cap()
        self._emit(Dispatch(input_id=input_id, action=stale, stuck_release=True))

    def _schedule_hold_cap(self) -> None:
        """Point the hold timer at the earliest hold that could time out.

        ``None`` when nothing is held, which parks the timer. Cheap and called
        on every hold register and release, so the cap always reflects the
        oldest live hold.
        """
        with self._lock:
            started = list(self._hold_started.values())
        earliest = min(started) if started else None
        self.hold_timer.schedule(
            None if earliest is None else earliest + self.max_hold_seconds
        )

    def _hold_expire(self) -> None:
        """The hold timer fired: release every hold past the cap. Timer thread.

        The backstop for a lost key-up on a key nothing touches again. A hold
        still legitimately down is cut here too - the cap cannot tell the two
        apart - which is why it is generous (see :data:`DEFAULT_MAX_HOLD_SECONDS`)
        and why the repeated-key-down reconcile exists to catch the common case
        long before this. Each recovery is logged, the same shape as a latch stop
        the timer produces.
        """
        now = self.clock()
        with self._lock:
            due = [
                (input_id, action)
                for input_id, action in self._holding.items()
                if input_id in self._hold_started
                and now - self._hold_started[input_id] >= self.max_hold_seconds
            ]
        for input_id, action in due:
            self._force_release_hold(input_id, action)
            self._emit(Dispatch(input_id=input_id, action=action, stuck_release=True))
        self._schedule_hold_cap()

    # -- double-tap on a physical hold -----------------------------------

    def _maybe_double_tap(self, input_id: str, action: Action) -> None:
        """Fire the second shortcut if this press completes a double-tap.

        Runs on the press path *before* the physical hold goes down, so the tap
        of the second combo goes out with clean modifiers (the first tap of the
        pair is already released). The physical hold is never delayed for this -
        push-to-talk stays instant - which is why the first tap of a double-tap
        briefly holds ``key``; that blip is harmless and documented on
        :class:`freemicro.input.latch.DoubleTapMachine`. The detector only fires
        the extra tap; it drives no light, because it cannot see the toggle
        app's state and lighting it would be a guess.
        """
        combo = double_tap_combo(action)
        if combo is None:
            return
        machine = self._doubletap.get(input_id)
        if machine is None:
            machine = self._doubletap[input_id] = latchmod.DoubleTapMachine()
        if latchmod.FIRE not in machine.press(self.clock()):
            return
        try:
            with self._deliver:
                self.backend.press_key(combo)
        except ActionError:
            # A double-tap that fails to deliver simply does not toggle; one
            # missed tap must not derail the physical hold that follows it.
            pass

    def _blocking_hold(self, action: Action) -> Optional[Tuple[str, str]]:
        """The held binding that must stop ``action``, or ``None``.

        Caller holds the lock.
        """
        if action.kind in MODIFIER_SAFE_KINDS or not self._holding:
            return None
        held_id, held = next(iter(self._holding.items()))
        return (held_id, str(held.params.get("key", "")))

    # -- push-to-talk latch ----------------------------------------------

    def _run_latch(
        self, input_id: str, action: Action, pressed: bool
    ) -> Optional[Dispatch]:
        """Advance one MIC key's latch machine and deliver what it asks for.

        The machine is the authority on whether recording is on; this only turns
        its ``start`` / ``stop`` into a tap of the toggle shortcut and the light
        on or off, and points the timer at the machine's next window.
        """
        now = self.clock()
        with self._lock:
            entry = self._latch.get(input_id)
            if entry is None:
                entry = self._latch[input_id] = _Latch(
                    latchmod.LatchMachine(), action
                )
            else:
                # Same binding object every event, but a reload could not have
                # swapped it without clearing the machine, so refresh defensively.
                entry.action = action
            machine = entry.machine
            if pressed:
                blocker = self._blocking_hold(action)
                if blocker is not None:
                    # Another key is physically holding modifiers, so a tap now
                    # would come out as a shortcut. Refuse it, and swallow the
                    # matching release, exactly as a normal press would be. The
                    # machine does not advance: nothing was sent.
                    self._suppressed[input_id] = True
            else:
                blocker = None

        if pressed and blocker is not None:
            return Dispatch(
                input_id=input_id, action=action,
                suppressed_by=blocker[0], holding=blocker[1],
            )
        if not pressed:
            with self._lock:
                if self._suppressed.pop(input_id, False):
                    return None

        emits = machine.press(now) if pressed else machine.release(now)
        dispatch = self._apply_latch(input_id, action, emits)
        with self._lock:
            self._reschedule_latch(now)
        return dispatch

    def _apply_latch(
        self, input_id: str, action: Action, emits: List[str]
    ) -> Dispatch:
        """Turn the machine's emits into a toggle tap and a light change."""
        dispatch = Dispatch(input_id=input_id, action=action)
        for emit in emits:
            if emit == latchmod.START and action.light is not None:
                # Before the tap, as ``_holding`` and the hold light are: it says
                # recording is starting, and a tap that fails still has to be
                # something the pad stops claiming.
                with self._lock:
                    self._lit[input_id] = True
                self._light(input_id, action.light)
            try:
                with self._deliver:
                    self.backend.press_key(str(action.params["key"]))
            except ActionError as exc:
                dispatch = Dispatch(
                    input_id=input_id, action=action, ok=False, error=str(exc)
                )
                if emit == latchmod.START:
                    self._end_light(input_id)
            if emit == latchmod.STOP:
                # Recording is over whether or not the tap landed, so the pad
                # must stop saying it is on.
                self._end_light(input_id)
        return dispatch

    def _latch_refresh_interval(self) -> Optional[float]:
        """How long the latch timer may sleep before re-asserting a live light.

        Half the light's own timeout, so the overlay never reaches its deadline
        while a latch is genuinely recording. ``None`` when nothing recording
        carries a light, which is the signal to stop refreshing. Caller holds
        the lock.
        """
        intervals = []
        for entry in self._latch.values():
            if entry.machine.recording and entry.action.light is not None:
                timeout = float(
                    getattr(entry.action.light, "timeout_seconds", 0.0) or 0.0
                )
                intervals.append(max(1.0, timeout * 0.5) if timeout > 0 else 30.0)
        return min(intervals) if intervals else None

    def _reschedule_latch(self, now: float) -> None:
        """Point the latch timer at the earliest thing it owes. Holds the lock."""
        interval = self._latch_refresh_interval()
        if interval is None:
            self._latch_refresh_at = None
        elif self._latch_refresh_at is None:
            self._latch_refresh_at = now + interval
        deadlines = [
            e.machine.deadline for e in self._latch.values()
            if e.machine.deadline is not None
        ]
        if self._latch_refresh_at is not None:
            deadlines.append(self._latch_refresh_at)
        self.latch_timer.schedule(min(deadlines) if deadlines else None)

    def _latch_expire(self) -> None:
        """The latch timer fired: resolve due windows and refresh live lights.

        Runs on the timer thread, the same shape as :meth:`_expire`: a ``stop``
        the clock produced (a waiting window that timed out) is delivered through
        ``on_dispatch`` or queued for the next :meth:`drain`, so the readout
        names it when it happens rather than when the next key does.
        """
        now = self.clock()
        with self._lock:
            entries = list(self._latch.items())
            refresh_due = (
                self._latch_refresh_at is not None
                and now >= self._latch_refresh_at
            )
        produced: List[Dispatch] = []
        for input_id, entry in entries:
            deadline = entry.machine.deadline
            if deadline is not None and now >= deadline:
                emits = entry.machine.tick(now)
                dispatch = self._apply_latch(input_id, entry.action, emits)
                if emits:
                    produced.append(dispatch)
        if refresh_due:
            with self._lock:
                self._latch_refresh_at = None  # _reschedule_latch sets the next
            for input_id, entry in entries:
                if entry.machine.recording and entry.action.light is not None:
                    with self._lock:
                        self._lit[input_id] = True
                    self._light(input_id, entry.action.light)
        with self._lock:
            self._reschedule_latch(now)
        for dispatch in produced:
            if self.on_dispatch is not None:
                self.on_dispatch(dispatch)
            else:
                with self._lock:
                    self._deferred.append(dispatch)

    def stop_latches(self, reason: str = "") -> List[Dispatch]:
        """Force every latch back to idle now. Returns the stops sent. Never raises.

        The recording machine's answer to a lost release, and the counterpart of
        :meth:`release_held_keys` for holds: a pad that drops, a config reload or
        shutdown must not leave a dictation app recording forever. Each machine
        that was recording gets one toggle-off tap so the app actually stops, and
        its light is retired. Called from :meth:`release_held_keys`,
        :meth:`close` and the config setter, and by the run loop the instant it
        knows the pad disconnected.
        """
        now = self.clock()
        with self._lock:
            entries = list(self._latch.items())
        produced: List[Dispatch] = []
        for input_id, entry in entries:
            emits = entry.machine.force_stop(now)
            dispatch = self._apply_latch(input_id, entry.action, emits)
            if emits:
                produced.append(dispatch)
        with self._lock:
            self._latch.clear()
            self._latch_refresh_at = None
        self.latch_timer.schedule(None)
        return produced

    # -- two keys at once -------------------------------------------------

    def press(self, input_id: str) -> List[Dispatch]:
        """Resolve one key-down. See the class docstring for the rule."""
        if input_id not in self._chord_keys:
            return _one(self.fire(input_id, True))

        action = self._resolve(input_id)
        # An explicit "none" is how a key is declared a pure chord partner, and
        # it delivers nothing, so it is reported at once and costs no window.
        solo = action if action is not None and action.kind != "none" else None
        settle = float(self.config.chord_settle_ms) / 1000.0

        with self._lock:
            # A key going down ends whatever its last press resolved to. Says
            # so explicitly because the key-up that would normally clear this
            # can be lost - the pad drops on sleep, on range, on a nudged cable
            # - and a chord mark left behind would swallow the *next* press's
            # release, which for a `hold` means a modifier nobody lets go of.
            self._chorded.pop(input_id, None)
            partner = self._find_partner(input_id)
            if partner is not None:
                members = chord_key((partner, input_id))
                self._unresolved.pop(partner, None)
                self._chorded[partner] = members
                self._chorded[input_id] = members
                chord_action = self.config.chords[members]
                self._open_chords[members] = chord_action
                self._reschedule()
        if partner is not None:
            return _one(self._run(chord_label(members), chord_action, True))

        if solo is None:
            with self._lock:
                self._unresolved[input_id] = _Unresolved(input_id)
            if action is None:
                return [Dispatch(
                    input_id=input_id,
                    chord=", ".join(
                        chord_label((input_id, p))
                        for p in self.config.chord_partners(input_id)
                    ),
                )]
            return _one(self._run(input_id, action, True))

        if settle <= 0.0:
            # Deferring is switched off: fire now and let this key be the
            # second half of a chord only, never the first. parse() warns when
            # that leaves a chord with no way to fire at all.
            return _one(self._run(input_id, solo, True))

        with self._lock:
            self._unresolved[input_id] = _Unresolved(
                input_id, solo, self.clock() + settle
            )
            self._reschedule()
        return []

    def release(self, input_id: str) -> List[Dispatch]:
        """Resolve one key-up, coherently with whatever the press resolved to."""
        with self._lock:
            members = self._chorded.pop(input_id, None)
            chord_action = (
                self._open_chords.pop(members, None) if members is not None else None
            )
            pending = self._unresolved.pop(input_id, None)
            if pending is not None:
                self._reschedule()

        if members is not None:
            # The other member's key-up finds no open chord and does nothing,
            # which is what keeps one chord from releasing twice.
            if chord_action is None:
                return []
            return _one(self._run(chord_label(members), chord_action, False))

        if pending is not None and pending.action is not None:
            # Tapped and let go inside the settle window. No partner came, so
            # the key meant itself - press and release, in that order.
            results = _one(self._run(input_id, pending.action, True))
            results += _one(self._run(input_id, pending.action, False))
            return results

        return _one(self.fire(input_id, False))

    def _find_partner(self, input_id: str) -> Optional[str]:
        """An undecided key that forms a bound chord with ``input_id``.

        Newest first: with three keys down the one you just pressed is the one
        you meant. Caller holds the lock.
        """
        for other in reversed(list(self._unresolved)):
            if chord_key((other, input_id)) in self.config.chords:
                return other
        return None

    def _reschedule(self) -> None:
        """Point the timer at the earliest deadline left. Caller holds the lock."""
        deadlines = [
            p.deadline for p in self._unresolved.values() if p.deadline is not None
        ]
        self.settle.schedule(min(deadlines) if deadlines else None)

    def _expire(self) -> None:
        """Fire every deferred press whose settle window has run out.

        Runs on the timer thread. Deciding under the lock and delivering
        outside it is what stops a slow action (``shell`` with ``wait``) from
        blocking the key path, while still guaranteeing that a press removed
        from ``_unresolved`` has exactly one owner.
        """
        now = self.clock()
        with self._lock:
            due = [
                p for p in self._unresolved.values()
                if p.deadline is not None and p.deadline <= now
            ]
            for pending in due:
                del self._unresolved[pending.input_id]
            self._reschedule()
        for pending in due:
            dispatch = self._run(pending.input_id, pending.action, True)
            if dispatch is None:
                continue
            if self.on_dispatch is not None:
                self.on_dispatch(dispatch)
            else:
                with self._lock:
                    self._deferred.append(dispatch)

    def drain(self) -> List[Dispatch]:
        """Take any dispatch the settle timer produced since the last call."""
        with self._lock:
            results, self._deferred = self._deferred, []
        return results

    def handle(self, message: Mapping[str, Any]) -> List[Dispatch]:
        """Decode a protocol message and run everything it triggered."""
        results = self.drain()
        for event in self.decode(message):
            if event.pressed:
                results.extend(self.press(event.input_id))
            else:
                results.extend(self.release(event.input_id))
        return results


def joystick_sample(message: Mapping[str, Any]) -> Optional[Dict[str, float]]:
    """Extract raw ``{angle, distance}`` from a joystick message.

    Used by ``freemicro keys --dry-run`` so people can watch real numbers while
    tuning ``joystick.deadzone`` and ``joystick.origin``.
    """
    if (message.get("m") or message.get("method")) != EVENT_JOYSTICK:
        return None
    params = message.get("p") or message.get("params")
    if not isinstance(params, dict):
        return None
    try:
        return {"angle": float(params.get("a", 0.0)),
                "distance": float(params.get("d", 0.0))}
    except (TypeError, ValueError):
        return None


def joystick_line(
    message: Mapping[str, Any], bridge: Optional[Bridge] = None
) -> Optional[str]:
    """One tuning line for ``freemicro keys --dry-run``, or ``None``.

    ``gamma`` and ``max_speed`` are chosen by feel, and you cannot tune by feel
    against numbers you cannot see - so in pointer mode this prints the
    resulting velocity next to the raw sample that produced it. Push the stick
    to the deflection that feels like "normal cursor speed", read the px/s, and
    that is your ``max_speed``; if the middle of the range feels too fast for
    its deflection, raise ``gamma``.

    Side-effect free: it asks the pointer what a sample *would* mean rather
    than feeding it one, so it can be called before or after
    :meth:`Bridge.handle` without changing anything.
    """
    sample = joystick_sample(message)
    if sample is None:
        return None
    angle, distance = sample["angle"], sample["distance"]
    if bridge is not None and bridge.config.joystick.pointing:
        return "  joystick " + bridge.pointer.preview(angle, distance).describe()
    return f"  joystick angle={angle:.4f} distance={distance:.4f}"


__all__ = [
    "DEFAULT_MAX_HOLD_SECONDS",
    "MODIFIER_HOLDING_KINDS",
    "MODIFIER_SAFE_KINDS",
    "Bridge",
    "Dispatch",
    "InputEvent",
    "JoystickTracker",
    "SettleTimer",
    "joystick_line",
    "joystick_sample",
]
