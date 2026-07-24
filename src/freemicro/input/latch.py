"""The push-to-talk latch machine: hold to talk, or tap-tap to keep recording.

The vendor firmware's MIC key is not a plain hold. ``docs/FACTORY-DEFAULTS.md``
section 8 documents a five-state machine, every threshold 350 ms (the same
constant as the Agent-key double-tap window): hold the key to talk and letting
go stops, or tap it twice quickly to *latch* recording on and tap once more to
stop. This module is that machine, kept pure so it can be proven from data with
no timer thread, no backend and no wall clock. :class:`freemicro.input.bridge.Bridge`
wires it to a real clock, a real timer and the keystroke that toggles a
dictation app.

Why a machine and not a physical hold
-------------------------------------
The plain ``hold`` action presses **real modifier keys** and keeps them down for
as long as the pad key is, which is exactly right for a dictation app whose
push-to-talk shortcut records *while held*. It cannot latch: keeping a chord
physically down for minutes is the stuck-modifier hazard this project just
removed, and a quick release would end recording before the second tap could
ever arrive.

So latch mode targets the other, equally common contract: a dictation app whose
shortcut *toggles* recording - tap to start, tap again to stop. Each ``START``
and ``STOP`` this machine emits is one tap of that shortcut, and **nothing is
ever held down**. Because FreeMicro is the thing tapping, it always knows whether
recording is on - which is the whole point. A toggle observed from outside cannot
be lit honestly (the tap that stops looks identical to the tap that started);
a toggle *driven* by this machine can, because the machine emitted the stop
itself.

What each state means for recording
-----------------------------------
Recording is on in ``PRESSED``, ``WAITING`` and ``LATCHED`` and off in ``IDLE``
and ``SUPPRESSING``. ``START`` is emitted exactly on the edge into recording and
``STOP`` exactly on the edge out of it, so a caller can drive both the app tap
and the activity light off the same two events.
"""

from __future__ import annotations

from typing import List, Optional

#: The one threshold, in seconds. 350 ms, the vendor's double-tap window
#: (``docs/FACTORY-DEFAULTS.md`` section 8): a release quicker than this is a
#: tap that might be doubled; a second press sooner than this latches; and the
#: same window debounces the extra taps of the stop gesture.
LATCH_WINDOW_SECONDS = 0.35

#: The two things the machine tells its caller to do, each one tap of the
#: dictation app's toggle shortcut.
START = "start"
STOP = "stop"

#: The one thing :class:`DoubleTapMachine` tells its caller to do: send a single
#: tap of the *second* shortcut. Named apart from ``START``/``STOP`` because it
#: is a different gesture with a different contract - see the class.
FIRE = "fire"

# States.
IDLE = "idle"
PRESSED = "pressed"
WAITING = "waiting"
LATCHED = "latched"
SUPPRESSING = "suppressing"
#: :class:`DoubleTapMachine` only: the second tap is down, ``FIRE`` already
#: emitted, and its own key-up is all that is left of the gesture.
ARMED = "armed"

#: The states in which the dictation app is recording.
_RECORDING_STATES = frozenset({PRESSED, WAITING, LATCHED})


class LatchMachine:
    """One MIC key's push-to-talk state, advanced by presses, releases and time.

    Every method takes ``now`` (a monotonic timestamp the caller owns) and
    returns the taps to send - ``[]``, ``[START]`` or ``[STOP]`` - never more
    than one, because recording cannot cross more than one edge per event. After
    every call :attr:`deadline` is the next moment the caller must call
    :meth:`tick`, or ``None`` when no timer is needed (an indefinite latch, or a
    hold with nothing pending).
    """

    def __init__(self, window: float = LATCH_WINDOW_SECONDS) -> None:
        self.window = window
        self.state = IDLE
        #: When the timer must next fire, or ``None``. The waiting window and the
        #: suppressing window both set it; a long hold and an indefinite latch
        #: leave it clear, so a quiet latch costs no wakeups at all.
        self.deadline: Optional[float] = None
        self._pressed_at = 0.0

    @property
    def recording(self) -> bool:
        """Whether the dictation app is recording right now."""
        return self.state in _RECORDING_STATES

    def press(self, now: float) -> List[str]:
        """A pad key-down."""
        if self.state == IDLE:
            self.state = PRESSED
            self._pressed_at = now
            self.deadline = None
            return [START]
        if self.state == WAITING:
            # The second tap, inside the window: latch on and record until told
            # to stop. No timer - a latch is indefinite by design.
            self.state = LATCHED
            self.deadline = None
            return []
        if self.state == LATCHED:
            # The stop tap. Recording ends now; the suppressing window swallows
            # any extra taps of the same gesture so a fast double-tap to stop
            # does not immediately start a new session.
            self.state = SUPPRESSING
            self.deadline = now + self.window
            return [STOP]
        if self.state == SUPPRESSING:
            if self.deadline is not None and now >= self.deadline:
                # The window has passed, so this is a fresh session.
                self.state = PRESSED
                self._pressed_at = now
                self.deadline = None
                return [START]
            # Still inside the window: swallow the tap, keep the deadline.
            return []
        # PRESSED and a second press with no intervening release: ignore it.
        return []

    def release(self, now: float) -> List[str]:
        """A pad key-up. Only meaningful in ``PRESSED``."""
        if self.state != PRESSED:
            return []
        if now - self._pressed_at >= self.window:
            # Held past the window: classic hold-to-talk, stop on release.
            self.state = IDLE
            self.deadline = None
            return [STOP]
        # A quick tap: wait to see whether a second one latches it on.
        self.state = WAITING
        self.deadline = now + self.window
        return []

    def tick(self, now: float) -> List[str]:
        """The timer fired. Resolves an expired waiting or suppressing window."""
        if self.deadline is None or now < self.deadline:
            return []
        if self.state == WAITING:
            # No second tap came, so the first was a plain quick hold-to-talk.
            self.state = IDLE
            self.deadline = None
            return [STOP]
        if self.state == SUPPRESSING:
            # The debounce is over; recording is already off, so nothing to emit.
            self.state = IDLE
            self.deadline = None
            return []
        self.deadline = None
        return []

    def force_stop(self, now: float) -> List[str]:
        """Return to idle now, stopping the app if it was recording.

        For the paths that must not leave a dictation app recording under a key
        that is gone or rebound: a disconnect, a config reload, shutdown. Emits
        ``STOP`` exactly when recording was on, so the caller sends the one tap
        that turns the app off and clears the light.
        """
        was = self.recording
        self.state = IDLE
        self.deadline = None
        return [STOP] if was else []


class DoubleTapMachine:
    """Detects a double-tap on a key that is *also* a real physical hold.

    A plain ``hold`` with a ``double_tap`` combo is push-to-talk first: the pad
    key presses ``key`` the instant it goes down and releases it on key-up, and
    that path is left exactly as it was (:class:`LatchMachine` is not involved -
    a double-tap hold never taps ``key``, it truly holds it). This machine sits
    *beside* that hold and watches the timing only, so it can fire one tap of a
    **second, different** shortcut when it sees two quick presses.

    Why it can share the plain-hold path rather than replace it
    -----------------------------------------------------------
    The physical hold must stay instant: the pad cannot wait 350 ms to find out
    whether a second tap is coming without ruining push-to-talk, so it does not.
    The accepted cost is that the *first* tap of a double-tap briefly holds
    ``key`` (a sub-window blip) before the second tap arrives. That is harmless:
    the second shortcut is meant for a *toggle*-mode app, which ignores the
    push-to-talk shortcut, and a push-to-talk app records nothing meaningful in
    under 350 ms. See :meth:`freemicro.input.bridge.Bridge._run`.

    When it fires
    -------------
    On the **second press**, not its release: that is the first moment the
    gesture is unambiguous (a second key-down inside the window), and the first
    tap has already been released, so the modifiers are clean and the tap of the
    second shortcut goes out without ``key`` held over it. Firing on the release
    instead would couple the tap to how long the second press is held - a "tap,
    then hold to talk" would fire the toggle only when the long hold ended, which
    is not what either gesture means.

    Past two taps
    -------------
    Each *completed pair* fires once and then disarms: a triple-tap fires once
    (the third press starts a fresh, incomplete pair), a quadruple-tap fires
    twice (on then off, a coherent toggle round-trip). It never fires twice for
    three taps, which would leave a toggle in the wrong state.

    No timer, on purpose
    --------------------
    The waiting window's only consumer is the *next* press, and it is resolved by
    comparing timestamps there (:meth:`press`), so nothing happens on expiry that
    a wakeup would be needed for: no key is held by this machine, no light is
    driven by it. A stale ``WAITING`` costs nothing and cannot mis-fire, because
    a late press past the window is treated as a fresh first tap. Same injectable
    ``now`` discipline as :class:`LatchMachine`; :meth:`tick` is provided for
    symmetry and tests but the bridge needs no thread for it.
    """

    def __init__(self, window: float = LATCH_WINDOW_SECONDS) -> None:
        self.window = window
        self.state = IDLE
        #: When the waiting window lapses, or ``None``. Read by :meth:`tick` and
        #: by :meth:`press` to decide whether a second press is still in time.
        self.deadline: Optional[float] = None
        self._pressed_at = 0.0

    def press(self, now: float) -> List[str]:
        """A pad key-down. ``[FIRE]`` when it completes a double-tap."""
        if (
            self.state == WAITING
            and self.deadline is not None
            and now < self.deadline
        ):
            # The second tap, inside the window: the double-tap is recognised.
            # Fire once and wait out this press's own key-up before another pair
            # can begin, so a third tap cannot fire it a second time.
            self.state = ARMED
            self.deadline = None
            return [FIRE]
        # Anything else - a first tap, or a second press after the window - opens
        # a fresh pair. A lapsed WAITING lands here too, which is the whole of the
        # expiry logic: no timer required.
        self.state = PRESSED
        self._pressed_at = now
        self.deadline = None
        return []

    def release(self, now: float) -> List[str]:
        """A pad key-up. Advances the gesture; never emits."""
        if self.state == PRESSED:
            if now - self._pressed_at < self.window:
                # A quick tap: open the window for a possible second one.
                self.state = WAITING
                self.deadline = now + self.window
            else:
                # Held past the window: a real push-to-talk hold, not a tap. The
                # physical hold ran the whole time (the bridge owns that); as a
                # double-tap candidate this press is simply over.
                self.state = IDLE
                self.deadline = None
            return []
        if self.state == ARMED:
            # The second tap's key-up: the pair is complete and consumed.
            self.state = IDLE
            self.deadline = None
        return []

    def tick(self, now: float) -> List[str]:
        """Lapse a waiting window. The bridge relies on :meth:`press` instead."""
        if (
            self.state == WAITING
            and self.deadline is not None
            and now >= self.deadline
        ):
            self.state = IDLE
            self.deadline = None
        return []


__all__ = [
    "ARMED",
    "FIRE",
    "IDLE",
    "LATCHED",
    "LATCH_WINDOW_SECONDS",
    "DoubleTapMachine",
    "LatchMachine",
    "PRESSED",
    "START",
    "STOP",
    "SUPPRESSING",
    "WAITING",
]
