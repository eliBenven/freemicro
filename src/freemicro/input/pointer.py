"""Velocity-control pointing for the analogue thumbstick - a TrackPoint.

The pad streams the stick as a full analogue vector (``v.oai.rad`` ->
``{a: angle, d: distance}``, both 0-1) and returns to exactly ``{a:0, d:0}`` on
release. :class:`~freemicro.input.bridge.JoystickTracker` throws almost all of
that away: it edge-triggers one discrete flick per deflection, which is exactly
right for *bindings* and completely wrong for *pointing*. One flick, one jump.

This module is the other half. It treats deflection as a **speed**, not a
distance, the way an IBM/Lenovo TrackPoint does, and integrates that speed on a
clock. Three properties matter and each of them is a bug if you skip it:

**Move on a timer, not on events.** The pad only sends a sample when the stick
*changes*. Hold a direction steady and the samples stop - so an event-driven
implementation stalls precisely when the user is asking for continuous motion.
:class:`PointerLoop` runs a fixed tick and keeps integrating the last known
vector until something replaces it.

**A stale sample is a stop, not a hold.** The flip side of integrating the last
known vector is that a dropped Bluetooth packet would otherwise leave the cursor
sliding across the desk forever. Any sample older than
:data:`DEFAULT_STALE_SECONDS` is treated as zero velocity, so a real disconnect
fails safe.

The catch, measured on hardware: **the pad is change-based, not continuous.**
While the stick is swept it streams samples every 15-30 ms, but while it is held
*steady* it goes quiet - a captured hold showed a 2.1 s gap with the stick still
deflected. So the stale window must be well above that quiet gap, or holding a
direction reads as a disconnect and the cursor stops mid-hold (it did, at the
old 0.25 s). Release does not depend on this window at all: the pad sends an
explicit ``{a:0, d:0}`` on release, which the deadzone stops on immediately. The
window is purely the backstop for a genuine drop where even that release is
lost.

**Non-linear response.** Linear deflection feels twitchy: usable speed at full
tilt means no precision at all near centre. The curve is
``speed = max_speed * ((d - deadzone) / (1 - deadzone)) ** gamma``, which is
gentle where your thumb spends most of its time and fast only at the edge.

Everything here is clock-injected and backend-injected, so the whole thing is
tested with a fake clock and a recording backend: no hardware, and the real
cursor never moves.

Orientation
-----------
``a`` is a fraction of a full turn. Taking ``theta = 2*pi*a`` and
``(cos theta, sin theta)`` **directly as screen-space** - macOS y grows downward
- gives ``0.0`` = right, ``0.25`` = down, ``0.5`` = left, ``0.75`` = **up**,
which is exactly the sector table the vendor firmware uses
(``docs/FACTORY-DEFAULTS.md`` §6, confirmed from the shipped binary). So: **up
is angle 0.75**, and no sign flip is applied anywhere.

The discrete wheel (``JOYSTICK_INPUTS``) uses the same convention, and has to:
one stick cannot mean two opposite things depending on ``joystick.mode``. It
formerly placed ``JOY_UP`` at 0.25 on an unchecked maths-convention (y-up)
assumption; that was corrected when this module landed. See
:data:`freemicro.padconfig.JOYSTICK_INPUTS`.

If pointing comes out upside down on your unit, set ``joystick.invert_y`` and
please say so, because it would mean the factory capture and this code are both
wrong about the same axis.
"""

from __future__ import annotations

import atexit
import math
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional, Tuple

from freemicro.padconfig import JoystickConfig

#: How long a sample stays trustworthy. Past this the pointer stops dead.
#:
#: Measured: the pad falls silent while a direction is held steady (a 2.1 s gap
#: was captured with the stick still deflected), so this must sit well above
#: that or a held direction stops the cursor mid-move - which is exactly what
#: 0.25 s did. 4 s clears the observed quiet gap with margin and still stops a
#: genuine disconnect within a few seconds. Release is instant regardless: it
#: arrives as an explicit d=0 the deadzone catches, not as silence.
DEFAULT_STALE_SECONDS = 4.0

#: The largest slice of time one tick may integrate, in seconds.
#:
#: Ticks are not guaranteed to be punctual - a GC pause, a busy machine or a
#: display wake can swallow several. Without a cap the next tick integrates the
#: whole gap at once and the cursor teleports. 50 ms is a few ticks' worth of
#: catch-up and at most a small nudge even at full speed.
DEFAULT_MAX_STEP = 0.05

#: Tap-to-click. The analogue stick has no physical button (the pad reports only
#: ``v.oai.rad {a, d}``), so a *tap* - a quick deflect-and-return that never
#: became real cursor movement - is treated as a left click. That makes the
#: stick a full trackpad.
#:
#: The exact rule, and why each half is here:
#:
#: A tap fires a click when ALL THREE hold:
#:
#: 1. The stick crossed the **action deadzone** (:attr:`JoystickConfig.deadzone`,
#:    0.6) - a deliberate push, not the stick's resting slop. This is the
#:    "exceeded the activity deadzone" half, and it reuses the very threshold
#:    that already means "the user meant this" in ``directions`` mode.
#: 2. It **returned to centre** (below :attr:`pointer_deadzone`) within
#:    :data:`TAP_MAX_SECONDS` of that crossing. The pad sends an explicit
#:    ``d = 0`` on release, so the return is a real sample, not an inferred one.
#: 3. The cursor moved **no more than** :data:`TAP_MAX_TRAVEL_PX` in total
#:    between the crossing and the return.
#:
#: (2) and (3) together are what keep a fast intended *move* from firing a
#: click: a move either lingers past the window or has already carried the
#: cursor well past a few pixels by the time it could return. A sustained hold
#: (the stick held out) trips (2) on the tick clock and disarms even while the
#: pad is silent. Both numbers are deliberately generous enough that an ordinary
#: flick registers and tight enough that pointing never does; they are constants
#: rather than config because they are a property of the gesture, not a taste -
#: the enable switch and the button are the knobs a user actually wants.
TAP_MAX_SECONDS = 0.2
TAP_MAX_TRAVEL_PX = 30.0

#: Move callback: whole-pixel dx, dy, relative to wherever the cursor is.
MoveFn = Callable[[int, int], None]
#: Click callback: the mouse button name a completed tap should click.
ClickFn = Callable[[str], None]
Clock = Callable[[], float]
#: How the loop waits between ticks. Always called with an explicit number of
#: seconds - there is no unbounded wait anywhere in this module, because an
#: unbounded wait is how a motion loop wedges with the cursor still moving.
SleepFn = Callable[[float], None]

#: The default clock. Monotonic, never the wall clock: an NTP step or a
#: daylight-saving jump must not be integrated into cursor motion.
_monotonic = time.monotonic


def curve_speed(
    distance: float, deadzone: float, max_speed: float, gamma: float
) -> float:
    """Deflection -> speed in pixels per second.

    ``deadzone`` is rescaled away rather than subtracted, so the very first
    pixel of usable travel starts from zero speed instead of jumping to
    whatever the curve happens to be worth at the deadzone edge.
    """
    span = 1.0 - deadzone
    if span <= 0.0:  # pragma: no cover - config validation forbids it
        return max_speed if distance > deadzone else 0.0
    if distance <= deadzone:
        return 0.0
    reach = min(1.0, (distance - deadzone) / span)
    return max_speed * (reach ** gamma)


def screen_vector(
    angle: float, origin: float = 0.0, invert_y: bool = False
) -> Tuple[float, float]:
    """Stick angle -> a unit vector in macOS screen coordinates (y grows down).

    See the module docstring for why there is no sign flip: angle 0.75 is up.
    ``origin`` rotates the stick the same way it rotates the discrete wheel.
    """
    theta = 2.0 * math.pi * ((float(angle) - origin) % 1.0)
    dy = math.sin(theta)
    return math.cos(theta), -dy if invert_y else dy


@dataclass(frozen=True)
class PointerVector:
    """What the pointer thinks the stick is asking for, right now.

    Printed by ``freemicro keys --dry-run`` so ``gamma`` and ``max_speed`` can
    be chosen the only way they ever really get chosen: by feel, watching
    numbers move.
    """

    angle: float = 0.0
    distance: float = 0.0
    #: Pixels per second, after the curve and any precision scaling.
    speed: float = 0.0
    vx: float = 0.0
    vy: float = 0.0
    precision: bool = False
    stale: bool = False

    @property
    def moving(self) -> bool:
        return self.speed > 0.0

    def describe(self) -> str:
        note = ""
        if self.stale:
            note = "  [stale - stopped]"
        elif self.precision:
            note = "  [precision]"
        return (
            f"angle={self.angle:.4f} distance={self.distance:.4f} "
            f"speed={self.speed:7.1f}px/s v=({self.vx:+7.1f},{self.vy:+7.1f})"
            f"{note}"
        )


class PointerEngine:
    """The pure half: samples in, whole-pixel deltas out. No threads, no I/O.

    Sub-pixel accumulation lives here and is the reason slow, precise movement
    exists at all: at 20 px/s and a 90 Hz tick each step is 0.22 px, and a
    naive ``int()`` per tick would round every one of them to zero and the
    cursor would simply never move. The remainder is carried in a float and
    spent as soon as it is worth a whole pixel.
    """

    def __init__(
        self,
        config: Optional[JoystickConfig] = None,
        *,
        stale_seconds: float = DEFAULT_STALE_SECONDS,
        max_step: float = DEFAULT_MAX_STEP,
    ) -> None:
        self.config = config or JoystickConfig()
        self.stale_seconds = stale_seconds
        self.max_step = max_step
        self._lock = threading.Lock()
        self._angle = 0.0
        self._distance = 0.0
        self._sampled_at: Optional[float] = None
        self._last_tick: Optional[float] = None
        self._acc_x = 0.0
        self._acc_y = 0.0
        self._precision = False
        #: Tap detection. Armed when the stick crosses the action deadzone;
        #: ``_tap_travel`` is the whole-pixel distance the cursor has moved since,
        #: accumulated on the tick thread. A completed tap increments
        #: ``_pending_taps``, which :meth:`take_tap` drains.
        self._tap_armed = False
        self._tap_start = 0.0
        self._tap_travel = 0.0
        self._pending_taps = 0

    # -- input ------------------------------------------------------------

    def configure(self, config: JoystickConfig) -> None:
        """Adopt a reloaded config without losing the stick's current position."""
        with self._lock:
            self.config = config

    def update(self, angle: float, distance: float, now: float) -> None:
        """Feed one ``v.oai.rad`` sample. Cheap, and never blocks the caller.

        This runs on the thread reading the pad, so it does nothing but store
        three numbers - the integration happens on the tick thread.
        """
        with self._lock:
            self._angle = float(angle)
            self._distance = float(distance)
            self._sampled_at = now
            self._tap_sample(float(distance), now)

    # -- tap-to-click -----------------------------------------------------

    def _tap_sample(self, distance: float, now: float) -> None:
        """Advance the tap machine from one raw sample. Caller holds the lock.

        Arming and completion both live here because they are the two things the
        raw ``d`` stream can tell us: a deliberate push past the action deadzone,
        and the explicit return to centre the pad sends on release. The pixel
        budget that separates a tap from a fast move is tracked on the tick
        thread (see :meth:`_tap_tick`), which is the only place that knows how
        far the cursor actually went.
        """
        config = self.config
        if not config.tap_click or not config.pointing:
            self._tap_armed = False
            return
        if not self._tap_armed:
            if distance >= config.deadzone:
                self._tap_armed = True
                self._tap_start = now
                self._tap_travel = 0.0
            return
        if (now - self._tap_start) > TAP_MAX_SECONDS or (
            self._tap_travel > TAP_MAX_TRAVEL_PX
        ):
            # Too slow, or the cursor has already travelled too far: this is a
            # move or a hold, not a tap. Disarm and wait for the next crossing.
            self._tap_armed = False
        elif distance <= config.pointer_deadzone:
            # Back to centre, in time and within budget: a tap.
            self._tap_armed = False
            self._pending_taps += 1

    def _tap_tick(self, dx: int, dy: int, now: float) -> None:
        """Fold one tick's cursor movement into the armed tap. Holds the lock.

        This is what makes a sustained deflection fail to tap even while the pad
        is silent: the tick clock keeps running, so the window closes on its own,
        and a deflection that moved the cursor is disqualified by its own pixels.
        """
        if not self._tap_armed:
            return
        if dx or dy:
            self._tap_travel += math.hypot(dx, dy)
        if (now - self._tap_start) > TAP_MAX_SECONDS or (
            self._tap_travel > TAP_MAX_TRAVEL_PX
        ):
            self._tap_armed = False

    def take_tap(self) -> int:
        """How many taps have completed since the last call, clearing the count.

        Returned as a count rather than a bool so two quick taps deliver two
        clicks - the caller turns each into one click through the backend.
        """
        with self._lock:
            taps, self._pending_taps = self._pending_taps, 0
            return taps

    def set_precision(self, on: bool) -> None:
        """Engage or release precision mode (see ``joystick.precision_key``)."""
        with self._lock:
            self._precision = bool(on)

    def reset(self) -> None:
        """Forget everything. Used on disconnect and on stop."""
        with self._lock:
            self._distance = 0.0
            self._sampled_at = None
            self._last_tick = None
            self._acc_x = self._acc_y = 0.0
            self._precision = False
            self._tap_armed = False
            self._tap_travel = 0.0
            self._pending_taps = 0

    # -- output -----------------------------------------------------------

    def _vector_locked(self, now: float) -> PointerVector:
        if self._sampled_at is None:
            return PointerVector(angle=self._angle, distance=0.0)
        if (now - self._sampled_at) > self.stale_seconds:
            return PointerVector(
                angle=self._angle, distance=self._distance, stale=True
            )
        return self._solve_locked(self._angle, self._distance)

    def _solve_locked(self, angle: float, distance: float) -> PointerVector:
        config = self.config
        speed = curve_speed(
            distance,
            config.pointer_deadzone,
            config.max_speed,
            config.gamma,
        )
        if self._precision:
            speed *= config.precision_scale
        if speed <= 0.0:
            return PointerVector(
                angle=angle, distance=distance, precision=self._precision
            )
        ux, uy = screen_vector(angle, config.origin, config.invert_y)
        return PointerVector(
            angle=angle,
            distance=distance,
            speed=speed,
            vx=ux * speed,
            vy=uy * speed,
            precision=self._precision,
        )

    def vector(self, now: float) -> PointerVector:
        """What the cursor would be doing at ``now``. Side-effect free."""
        with self._lock:
            return self._vector_locked(now)

    def preview(self, angle: float, distance: float) -> PointerVector:
        """What *this* sample would mean, without adopting it.

        Lets ``keys --dry-run`` print the velocity a sample implies without
        caring whether the bridge has already consumed it.
        """
        with self._lock:
            return self._solve_locked(float(angle), float(distance))

    def moving(self, now: float) -> bool:
        """Whether there is live motion to integrate. Lets the loop idle."""
        with self._lock:
            return self._vector_locked(now).moving

    def tick(self, now: float) -> Tuple[int, int]:
        """Integrate one slice of time; return the whole-pixel move to make.

        Returns ``(0, 0)`` when there is nothing to do, which is the common
        case and costs one comparison.
        """
        with self._lock:
            vector = self._vector_locked(now)
            previous, self._last_tick = self._last_tick, now
            if not vector.moving:
                # Centre, stale or precision-scaled to nothing: drop the
                # remainder so it cannot leak into the next flick as a jump.
                self._acc_x = self._acc_y = 0.0
                self._tap_tick(0, 0, now)
                return (0, 0)
            if previous is None:
                self._tap_tick(0, 0, now)
                return (0, 0)  # first tick only establishes the baseline
            dt = now - previous
            if dt <= 0.0:
                self._tap_tick(0, 0, now)
                return (0, 0)
            dt = min(dt, self.max_step)
            self._acc_x += vector.vx * dt
            self._acc_y += vector.vy * dt
            # int() truncates toward zero, which is what signed accumulation
            # wants: the sign of the remainder must follow the direction of
            # travel or a reversal would replay the leftover backwards.
            dx, dy = int(self._acc_x), int(self._acc_y)
            self._acc_x -= dx
            self._acc_y -= dy
            self._tap_tick(dx, dy, now)
            return (dx, dy)


class PointerLoop:
    """Runs :class:`PointerEngine` on a steady tick, on a daemon thread.

    Daemon, because a pointer must never be the reason a process refuses to
    exit; and :meth:`stop` joins it *with a timeout* so the normal path is an
    orderly shutdown and a wedged loop still cannot outlive the process. It
    parks on an :class:`threading.Event` whenever the stick is centred, so a
    pad sitting idle on a desk costs four wakeups a second rather than ninety.

    Both the clock **and** the wait are injectable. That is not only for tests:
    it is the seam that guarantees the loop's timing is data rather than wall
    time, so the safety stop can be proven to fire on the clock alone. Note
    that the stale check lives in :class:`PointerEngine`, which never waits for
    a sample - so a pad that goes silent mid-deflection is stopped by the very
    next tick and cannot starve the thing that exists to catch it.
    """

    def __init__(
        self,
        engine: PointerEngine,
        move: MoveFn,
        *,
        tick_hz: float = 90.0,
        clock: Optional[Clock] = None,
        sleep: Optional[SleepFn] = None,
        idle_seconds: float = 0.25,
        on_error: Optional[Callable[[BaseException], None]] = None,
    ) -> None:
        self.engine = engine
        self.move = move
        self.clock = clock or _monotonic
        self.sleep = sleep if sleep is not None else self._wait
        self.idle_seconds = idle_seconds
        self.on_error = on_error
        self.error: Optional[BaseException] = None
        self._period = 1.0 / max(1e-6, float(tick_hz))
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    @property
    def period(self) -> float:
        return self._period

    @property
    def running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    def set_tick_hz(self, tick_hz: float) -> None:
        self._period = 1.0 / max(1e-6, float(tick_hz))
        self._wake.set()

    def nudge(self) -> None:
        """Wake the loop now - called when a fresh sample lands."""
        self._wake.set()

    def step(self) -> Tuple[int, int]:
        """One iteration's worth of work, with no waiting. Used by the tests."""
        dx, dy = self.engine.tick(self.clock())
        if dx or dy:
            self.move(dx, dy)
        return (dx, dy)

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self.error = None
        thread = threading.Thread(
            target=self._run, name="freemicro-pointer", daemon=True
        )
        self._thread = thread
        atexit.register(self.stop)
        thread.start()

    def stop(self, timeout: float = 1.0) -> None:
        """Stop the tick and join the thread, with a timeout.

        The timeout is deliberate: a wedged loop must not be able to wedge
        whatever called ``stop()`` as well. The thread is a daemon, so even in
        that case it dies with the process.
        """
        thread = self._thread
        self._stop.set()
        self._wake.set()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)
        self._thread = None
        try:
            atexit.unregister(self.stop)
        except Exception:  # pragma: no cover - unregister never raises on 3.9
            pass
        self.engine.reset()

    def _wait(self, seconds: float) -> None:
        """The default wait: bounded, and interruptible by a fresh sample."""
        self._wake.wait(seconds)

    def _run(self) -> None:
        while not self._stop.is_set():
            # Cleared *before* the work, so a sample that lands mid-tick is
            # still waiting for us at the wait below instead of being lost.
            self._wake.clear()
            try:
                self.step()
            except Exception as exc:  # noqa: BLE001 - see below
                # A backend that cannot move the mouse at all (AppleScript
                # cannot) would otherwise raise ninety times a second forever.
                # One failure is enough to know pointing is not available.
                self.error = exc
                if self.on_error is not None:
                    self.on_error(exc)
                self.engine.reset()
                return
            # Asked on every pass, from the clock alone: when the last sample
            # ages out mid-deflection this flips to False without any new
            # sample arriving, which is exactly the case the safety stop is
            # for. Both branches pass an explicit timeout.
            moving = self.engine.moving(self.clock())
            self.sleep(self._period if moving else self.idle_seconds)


class Pointer:
    """What the bridge holds: an engine, its loop, and a lazy start.

    Nothing is spawned until the stick actually leaves centre, so a user who
    never touches it - or a test that never sends a joystick message - never
    pays for a thread.
    """

    def __init__(
        self,
        config: JoystickConfig,
        move: MoveFn,
        *,
        click: Optional[ClickFn] = None,
        clock: Optional[Clock] = None,
        sleep: Optional[SleepFn] = None,
        engine: Optional[PointerEngine] = None,
        loop: Optional[PointerLoop] = None,
        autostart: bool = True,
    ) -> None:
        self.clock = clock or _monotonic
        self.engine = engine or PointerEngine(config)
        self.loop = loop or PointerLoop(
            self.engine, move, tick_hz=config.tick_hz, clock=self.clock,
            sleep=sleep,
        )
        #: Where a completed tap goes. ``None`` disables click delivery entirely
        #: (the detector still runs but its taps are dropped), which is what a
        #: caller that only wants motion passes.
        self.click = click
        self.autostart = autostart

    # -- lifecycle --------------------------------------------------------

    def configure(self, config: JoystickConfig) -> None:
        """Adopt a reloaded config, live, without dropping the stick."""
        self.engine.configure(config)
        self.loop.set_tick_hz(config.tick_hz)
        if not config.pointing:
            self.close()

    def close(self) -> None:
        """Stop pointing. Safe to call twice, and safe to call at exit."""
        self.loop.stop()

    # -- input ------------------------------------------------------------

    def update(self, angle: float, distance: float) -> PointerVector:
        """Feed one stick sample and return what it means, for the dry run."""
        now = self.clock()
        self.engine.update(angle, distance, now)
        vector = self.engine.vector(now)
        if self.autostart and vector.moving and not self.loop.running:
            self.loop.start()
        self.loop.nudge()
        self._deliver_taps()
        return vector

    def _deliver_taps(self) -> None:
        """Turn any completed tap into a click through the click callback.

        Drained here, on the sample thread, because a tap completes on the
        return-to-centre sample this same call just fed the engine - so the
        click lands with the gesture rather than one tick later, and never on the
        tick thread that only ever *disarms* a tap.
        """
        if self.click is None:
            return
        taps = self.engine.take_tap()
        if not taps:
            return
        button = self.engine.config.tap_click_button
        for _ in range(taps):
            self.click(button)

    def set_precision(self, on: bool) -> None:
        self.engine.set_precision(on)
        self.loop.nudge()

    def vector(self) -> PointerVector:
        return self.engine.vector(self.clock())

    def preview(self, angle: float, distance: float) -> PointerVector:
        """What one sample would mean. Side-effect free; see the engine."""
        return self.engine.preview(angle, distance)


__all__ = [
    "DEFAULT_MAX_STEP",
    "DEFAULT_STALE_SECONDS",
    "TAP_MAX_SECONDS",
    "TAP_MAX_TRAVEL_PX",
    "Pointer",
    "PointerEngine",
    "PointerLoop",
    "PointerVector",
    "curve_speed",
    "screen_vector",
]
