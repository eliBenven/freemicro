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

# States.
IDLE = "idle"
PRESSED = "pressed"
WAITING = "waiting"
LATCHED = "latched"
SUPPRESSING = "suppressing"

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


__all__ = [
    "IDLE",
    "LATCHED",
    "LATCH_WINDOW_SECONDS",
    "LatchMachine",
    "PRESSED",
    "START",
    "STOP",
    "SUPPRESSING",
    "WAITING",
]
