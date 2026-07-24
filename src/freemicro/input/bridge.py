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
    perform,
    release,
)
from freemicro.input.pointer import Pointer, PointerVector
from freemicro.padconfig import (
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

    @property
    def bound(self) -> bool:
        return self.action is not None

    @property
    def suppressed(self) -> bool:
        """Whether this press was refused because a ``hold`` binding is down."""
        return bool(self.suppressed_by)

    def describe(self) -> str:
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
    ) -> None:
        self.on_expire = on_expire
        self.clock = clock or _monotonic
        self.sleep = sleep if sleep is not None else self._wait
        self.autostart = autostart
        self.idle_seconds = idle_seconds
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
            target=self._run, name="freemicro-chord", daemon=True
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
    ) -> None:
        self.backend = backend
        self.clock = clock or _monotonic
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
        self.settle = SettleTimer(
            self._expire, clock=self.clock, sleep=sleep, autostart=autostart
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
        self._config = config
        self._chord_keys = frozenset(
            member for members in config.chords for member in members
        )
        with self._lock:
            self._unresolved.clear()
            self._chorded.clear()
            self._open_chords.clear()
        self.settle.schedule(None)

    @property
    def chord_keys(self) -> frozenset:
        """Every input id that takes part in a chord. Empty for most configs."""
        return self._chord_keys

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
        """
        with self._lock:
            self._holding.clear()
            self._suppressed.clear()
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
        with self._lock:
            self._unresolved.clear()
            self._chorded.clear()
            self._open_chords.clear()
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
        return self._run(input_id, self.config.action_for(input_id), pressed)

    def _run(
        self, input_id: str, action: Optional[Action], pressed: bool
    ) -> Optional[Dispatch]:
        """Deliver one resolved binding. ``input_id`` may be a chord id."""
        if not pressed:
            with self._lock:
                if self._suppressed.pop(input_id, False):
                    # We never sent the press, so sending the release would be
                    # a key-up for a key that was never down.
                    return None
                self._holding.pop(input_id, None)
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

        with self._lock:
            blocker = self._blocking_hold(action)
            if blocker is not None:
                self._suppressed[input_id] = True
            elif action.kind in MODIFIER_HOLDING_KINDS:
                # Registered before it is delivered, so a hold that fails
                # halfway is still something we know to let go of.
                self._holding[input_id] = action
        if blocker is not None:
            return Dispatch(
                input_id=input_id,
                action=action,
                suppressed_by=blocker[0],
                holding=blocker[1],
            )
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
            return Dispatch(input_id=input_id, action=action, ok=False, error=str(exc))
        return Dispatch(input_id=input_id, action=action)

    def _blocking_hold(self, action: Action) -> Optional[Tuple[str, str]]:
        """The held binding that must stop ``action``, or ``None``.

        Caller holds the lock.
        """
        if action.kind in MODIFIER_SAFE_KINDS or not self._holding:
            return None
        held_id, held = next(iter(self._holding.items()))
        return (held_id, str(held.params.get("key", "")))

    # -- two keys at once -------------------------------------------------

    def press(self, input_id: str) -> List[Dispatch]:
        """Resolve one key-down. See the class docstring for the rule."""
        if input_id not in self._chord_keys:
            return _one(self.fire(input_id, True))

        action = self.config.action_for(input_id)
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
