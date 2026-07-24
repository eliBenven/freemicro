"""Tests for analogue pointing: stick vectors in, whole-pixel deltas out.

Nothing here touches hardware and nothing here moves the real cursor. The clock
is injected and every move goes to a recording backend, so what is asserted is
the exact sequence of deltas the pointer *would* have emitted - which is the
only way to test a thing whose entire job is "the right number of pixels, at
the right moment".
"""

from __future__ import annotations

import math
import threading
import time

import pytest

from freemicro.input.actions import RecordingBackend
from freemicro.input.bridge import Bridge, joystick_line
from freemicro.input.pointer import (
    DEFAULT_STALE_SECONDS,
    Pointer,
    PointerEngine,
    PointerLoop,
    curve_speed,
    screen_vector,
)
from freemicro.padconfig import JoystickConfig, parse


class FakeClock:
    """A clock the test drives by hand. Real time makes timing tests lie."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, dt: float) -> float:
        self.now += dt
        return self.now


def config(**overrides) -> JoystickConfig:
    base = dict(
        mode="pointer", pointer_deadzone=0.1, max_speed=1000.0, gamma=2.0,
        tick_hz=100.0,
    )
    base.update(overrides)
    return JoystickConfig(**base)


# ---------------------------------------------------------------------------
# The response curve
# ---------------------------------------------------------------------------

def test_curve_is_silent_inside_the_deadzone():
    assert curve_speed(0.0, 0.1, 1000.0, 2.0) == 0.0
    assert curve_speed(0.099, 0.1, 1000.0, 2.0) == 0.0
    assert curve_speed(0.1, 0.1, 1000.0, 2.0) == 0.0


def test_curve_starts_from_zero_just_past_the_deadzone():
    """The deadzone is rescaled away, not subtracted: the first usable pixel of
    travel is worth almost no speed, or the cursor lurches the moment it wakes.
    """
    assert curve_speed(0.101, 0.1, 1000.0, 2.0) == pytest.approx(0.0, abs=0.02)


def test_curve_reaches_max_speed_at_full_deflection_and_clamps_past_it():
    assert curve_speed(1.0, 0.1, 1000.0, 2.0) == pytest.approx(1000.0)
    # The pad should never report more than 1, but a clamp is one comparison.
    assert curve_speed(1.4, 0.1, 1000.0, 2.0) == pytest.approx(1000.0)


def test_curve_is_monotonic():
    previous = -1.0
    for step in range(0, 101):
        speed = curve_speed(step / 100.0, 0.1, 1000.0, 2.0)
        assert speed >= previous
        previous = speed


def test_gamma_shapes_the_middle_and_only_the_middle():
    """Higher gamma buys precision in the middle of the range without giving
    up top speed - which is the whole reason it is not a linear scale."""
    linear = curve_speed(0.55, 0.1, 1000.0, 1.0)
    curved = curve_speed(0.55, 0.1, 1000.0, 2.0)
    gentler = curve_speed(0.55, 0.1, 1000.0, 3.0)
    assert gentler < curved < linear
    for gamma in (1.0, 2.0, 3.0):
        assert curve_speed(1.0, 0.1, 1000.0, gamma) == pytest.approx(1000.0)


# ---------------------------------------------------------------------------
# Orientation
# ---------------------------------------------------------------------------

def test_angle_maps_to_screen_axes_the_way_the_factory_firmware_does():
    """docs/FACTORY-DEFAULTS.md §6: right 0.0, down 0.25, left 0.5, up 0.75.

    macOS screen y grows downward, so sin() is taken as-is and up is 0.75.
    """
    cases = {0.0: (1, 0), 0.25: (0, 1), 0.5: (-1, 0), 0.75: (0, -1)}
    for angle, (want_x, want_y) in cases.items():
        x, y = screen_vector(angle)
        assert x == pytest.approx(want_x, abs=1e-9)
        assert y == pytest.approx(want_y, abs=1e-9)


def test_the_vector_is_always_unit_length():
    """Direction and speed are separate concerns: distance must not leak into
    the vector or it would be squared by the curve."""
    for step in range(0, 32):
        x, y = screen_vector(step / 32.0)
        assert math.hypot(x, y) == pytest.approx(1.0)


def test_origin_rotates_the_pointer_like_it_rotates_the_wheel():
    x, y = screen_vector(0.25, origin=0.25)
    assert (x, y) == (pytest.approx(1.0), pytest.approx(0.0, abs=1e-9))


def test_invert_y_flips_only_the_vertical_axis():
    x, y = screen_vector(0.25, invert_y=True)
    assert x == pytest.approx(0.0, abs=1e-9)
    assert y == pytest.approx(-1.0)


# ---------------------------------------------------------------------------
# The engine: velocity, not displacement
# ---------------------------------------------------------------------------

def _engine(**overrides):
    return PointerEngine(config(**overrides)), FakeClock()


def drive(engine, clock, angle, distance, seconds, dt=0.01, resample=True):
    """Hold the stick for ``seconds`` and collect the per-tick deltas.

    ``resample`` mirrors what the pad actually does while the stick is off
    centre: it streams. Set it False to model a channel that has gone quiet,
    which is what the staleness tests are about.
    """
    engine.update(angle, distance, clock.now)
    engine.tick(clock.now)  # baseline
    steps = []
    for _ in range(int(round(seconds / dt))):
        clock.advance(dt)
        if resample:
            engine.update(angle, distance, clock.now)
        steps.append(engine.tick(clock.now))
    return steps


def test_one_sample_keeps_moving_the_cursor_on_every_tick():
    """The crux. The pad stops sending samples while the stick is held steady,
    so an event-driven pointer stalls exactly when the user wants motion."""
    engine, clock = _engine()
    # One sample, then silence - resample=False. The cursor must keep going.
    steps = drive(engine, clock, 0.0, 1.0, seconds=0.1, resample=False)
    assert all(dy == 0 for _, dy in steps)
    assert sum(dx for dx, _ in steps) == pytest.approx(100, abs=2)


def test_speed_follows_deflection_not_distance():
    """Half deflection is not half the travel: it is a slower speed, sustained
    for as long as you hold it."""
    engine, clock = _engine(gamma=1.0)
    steps = drive(engine, clock, 0.0, 0.55, seconds=1.0)
    # (0.55 - 0.1) / 0.9 = 0.5 of max speed, held for one second.
    assert sum(dx for dx, _ in steps) == pytest.approx(500, abs=3)


def test_the_first_tick_only_establishes_a_baseline():
    engine, clock = _engine()
    engine.update(0.0, 1.0, clock.now)
    assert engine.tick(clock.now) == (0, 0)


def test_diagonals_move_both_axes():
    engine, clock = _engine()
    engine.update(0.125, 1.0, clock.now)   # down-right
    engine.tick(clock.now)
    dx, dy = engine.tick(clock.advance(0.1))
    assert dx > 0 and dy > 0
    assert dx == pytest.approx(dy, abs=1)


def test_up_is_a_negative_y_delta():
    engine, clock = _engine()
    engine.update(0.75, 1.0, clock.now)
    engine.tick(clock.now)
    dx, dy = engine.tick(clock.advance(0.1))
    assert dy < 0 and dx == 0


# ---------------------------------------------------------------------------
# Sub-pixel accumulation
# ---------------------------------------------------------------------------

def test_slow_movement_accumulates_instead_of_rounding_to_nothing():
    """At 20 px/s and a 100 Hz tick each step is 0.2 px. Truncating per tick
    would round every one of them to zero and precise movement would simply not
    exist."""
    engine, clock = _engine(max_speed=20.0, gamma=1.0, pointer_deadzone=0.0)
    steps = [dx for dx, _ in drive(engine, clock, 0.0, 1.0, seconds=1.0)]
    assert any(step == 0 for step in steps), "should not move every tick"
    assert sum(steps) == pytest.approx(20, abs=1), "but must total 20 px in 1 s"


def test_the_remainder_is_never_replayed_after_the_stick_recentres():
    """A leftover 0.9 px spent on the next flick would read as a jump."""
    engine, clock = _engine(max_speed=15.0, gamma=1.0, pointer_deadzone=0.0)
    engine.update(0.0, 1.0, clock.now)
    engine.tick(clock.now)
    engine.tick(clock.advance(0.01))                 # banks ~0.15 px
    engine.update(0.0, 0.0, clock.advance(0.01))     # let go
    assert engine.tick(clock.now) == (0, 0)
    engine.update(0.5, 1.0, clock.advance(0.01))     # push the other way
    assert engine.tick(clock.now) == (0, 0)


def test_the_remainder_carries_its_own_sign():
    """Truncation has to follow the direction of travel, or reversing would
    spend the leftover backwards."""
    engine, clock = _engine(max_speed=20.0, gamma=1.0, pointer_deadzone=0.0)
    # 0.5 of a turn is left: negative x.
    steps = [dx for dx, _ in drive(engine, clock, 0.5, 1.0, seconds=1.0)]
    assert all(step <= 0 for step in steps)
    assert sum(steps) == pytest.approx(-20, abs=1)


# ---------------------------------------------------------------------------
# Failing safe
# ---------------------------------------------------------------------------

def test_a_stale_sample_stops_the_cursor_dead():
    """A dropped packet must never leave the pointer sliding off the desk."""
    engine, clock = _engine()
    engine.update(0.0, 1.0, clock.now)
    engine.tick(clock.now)
    assert engine.tick(clock.advance(0.01))[0] > 0
    clock.advance(DEFAULT_STALE_SECONDS + 0.01)      # the pad goes quiet
    assert engine.tick(clock.now) == (0, 0)
    assert engine.vector(clock.now).stale
    # And it stays stopped, however long we wait.
    for _ in range(10):
        assert engine.tick(clock.advance(0.5)) == (0, 0)


def test_a_fresh_sample_revives_a_stalled_pointer():
    engine, clock = _engine()
    engine.update(0.0, 1.0, clock.now)
    engine.tick(clock.now)
    clock.advance(1.0)
    assert engine.tick(clock.now) == (0, 0)
    engine.update(0.0, 1.0, clock.advance(0.01))
    engine.tick(clock.now)
    assert engine.tick(clock.advance(0.01))[0] > 0


def test_returning_to_centre_stops_immediately():
    """The pad reports exactly {a:0, d:0} on release - below every deadzone."""
    engine, clock = _engine()
    engine.update(0.0, 1.0, clock.now)
    engine.tick(clock.now)
    assert engine.tick(clock.advance(0.01))[0] > 0
    engine.update(0.0, 0.0, clock.advance(0.01))
    assert engine.tick(clock.now) == (0, 0)


def test_a_long_gap_between_ticks_cannot_fling_the_cursor():
    """A wake from sleep or a GC pause must not integrate at once."""
    engine, clock = _engine()
    engine.update(0.0, 1.0, clock.now)
    engine.tick(clock.now)
    clock.advance(0.2)
    engine.update(0.0, 1.0, clock.now)   # still held, sample is fresh
    dx, _ = engine.tick(clock.now)
    assert 0 < dx <= 1000.0 * engine.max_step + 1


def test_a_clock_that_does_not_advance_emits_nothing():
    engine, clock = _engine()
    engine.update(0.0, 1.0, clock.now)
    engine.tick(clock.now)
    assert engine.tick(clock.now) == (0, 0)


def test_reset_forgets_everything():
    engine, clock = _engine()
    engine.update(0.0, 1.0, clock.now)
    engine.tick(clock.now)
    engine.reset()
    assert engine.tick(clock.advance(0.01)) == (0, 0)


# ---------------------------------------------------------------------------
# Precision mode
# ---------------------------------------------------------------------------

def test_precision_mode_scales_the_speed_down():
    engine, clock = _engine(precision_scale=0.25)
    engine.update(0.0, 1.0, clock.now)
    engine.tick(clock.now)
    fast = engine.tick(clock.advance(0.1))[0]
    engine.set_precision(True)
    engine.update(0.0, 1.0, clock.advance(0.01))
    engine.tick(clock.now)
    slow = engine.tick(clock.advance(0.1))[0]
    assert slow == pytest.approx(fast * 0.25, abs=2)


def test_precision_mode_releases():
    engine, clock = _engine()
    engine.set_precision(True)
    engine.set_precision(False)
    assert not engine.vector(clock.now).precision


# ---------------------------------------------------------------------------
# Live reconfiguration
# ---------------------------------------------------------------------------

def test_a_reloaded_config_takes_effect_without_dropping_the_stick():
    engine, clock = _engine(max_speed=1000.0)
    fast = drive(engine, clock, 0.0, 1.0, seconds=0.1)
    assert sum(dx for dx, _ in fast) == pytest.approx(100, abs=1)
    engine.configure(config(max_speed=200.0))
    slow = drive(engine, clock, 0.0, 1.0, seconds=0.1)
    assert sum(dx for dx, _ in slow) == pytest.approx(20, abs=1)


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------

def test_the_loop_hands_whole_pixels_to_the_move_callback():
    engine, clock = _engine()
    moves = []
    loop = PointerLoop(engine, lambda dx, dy: moves.append((dx, dy)),
                       tick_hz=100.0, clock=clock)
    engine.update(0.0, 1.0, clock.now)
    loop.step()                       # baseline
    clock.advance(0.01)
    loop.step()
    assert moves and moves[0][0] > 0
    assert all(isinstance(value, int) for move in moves for value in move)


def test_the_loop_calls_nothing_when_the_stick_is_centred():
    engine, clock = _engine()
    moves = []
    loop = PointerLoop(engine, lambda dx, dy: moves.append((dx, dy)),
                       tick_hz=100.0, clock=clock)
    for _ in range(5):
        loop.step()
        clock.advance(0.01)
    assert moves == []


def test_the_loop_stops_on_the_clock_alone_when_the_pad_goes_silent():
    """The safety stop must not need a sample to notice that samples stopped.

    Driven entirely by a fake clock: the wait *is* the clock advance, so no
    wall time passes and the result is the same on every machine.
    """
    clock = FakeClock()
    engine = PointerEngine(config(max_speed=1000.0))
    moves = []
    loop = PointerLoop(engine, lambda dx, dy: moves.append((dx, dy)),
                       tick_hz=100.0, clock=clock, sleep=clock.advance)
    engine.update(0.0, 1.0, clock.now)
    for _ in range(200):                 # 2 s of ticks, no further samples
        loop.step()
        loop.sleep(loop.period)
    # Roughly a quarter second of motion, then silence - not two seconds of it.
    assert 20 <= len(moves) <= 30
    assert engine.vector(clock.now).stale


def test_the_loop_gives_up_the_first_time_the_backend_cannot_move_the_mouse():
    """AppleScript has no move_mouse at all. Ninety exceptions a second is not
    a useful way to find that out.

    Uses a real thread, but no real time: the injected sleep advances a fake
    clock, so the loop reaches its second tick (the first one that moves, and
    therefore raises) immediately and then exits on its own.
    """
    clock = FakeClock()
    engine = PointerEngine(config(max_speed=1000.0))
    seen = []

    def broken(dx, dy):
        raise NotImplementedError("backend cannot move the mouse")

    def sleep(seconds):
        clock.advance(seconds)
        engine.update(0.0, 1.0, clock.now)     # the pad is still streaming

    loop = PointerLoop(engine, broken, tick_hz=100.0, clock=clock, sleep=sleep,
                       on_error=seen.append)
    engine.update(0.0, 1.0, clock.now)
    loop.start()
    thread = loop._thread
    assert thread is not None
    thread.join(2.0)                            # bounded; it ends by itself
    assert not thread.is_alive(), "it kept spinning on a backend that cannot work"
    assert isinstance(loop.error, NotImplementedError)
    assert len(seen) == 1, "one report, not one per tick"
    loop.stop()


def test_the_thread_is_a_daemon_and_stop_actually_joins_it():
    """A pointer must never be the reason a process refuses to exit, and
    stop() must be a real barrier - not a request.

    The only test here that runs the real thread to completion. It is bounded
    by an Event the injected sleep sets after three moves, so it finishes in
    microseconds rather than on a timer.
    """
    clock = FakeClock()
    engine = PointerEngine(config(max_speed=1000.0))
    moves = []
    enough = threading.Event()

    def sleep(seconds):
        clock.advance(seconds)
        engine.update(0.0, 1.0, clock.now)      # the pad is still streaming
        if len(moves) >= 3:
            enough.set()
            time.sleep(0.001)                   # do not spin while we tear down

    loop = PointerLoop(engine, lambda dx, dy: moves.append((dx, dy)),
                       tick_hz=100.0, clock=clock, sleep=sleep)
    engine.update(0.0, 1.0, clock.now)
    loop.start()
    thread = loop._thread
    try:
        assert loop.running
        assert thread is not None and thread.daemon
        assert enough.wait(2.0), "the running loop never moved the cursor"
    finally:
        loop.stop()
    assert not loop.running
    assert not thread.is_alive()
    assert thread not in threading.enumerate()
    assert len(moves) >= 3


def _parked_loop():
    """A loop whose wait never returns on its own. Only stop() ends it."""
    stopped = threading.Event()
    # Capped so an idle loop's 250 ms park does not become the test's runtime.
    loop = PointerLoop(PointerEngine(config()), lambda dx, dy: None,
                       sleep=lambda seconds: stopped.wait(min(seconds, 0.005)))
    return loop


def test_starting_twice_does_not_start_two_threads():
    loop = _parked_loop()
    loop.start()
    first = loop._thread
    loop.start()
    try:
        assert loop._thread is first
    finally:
        loop.stop()
    assert not first.is_alive()


def test_stop_is_safe_before_start_and_twice_after():
    loop = _parked_loop()
    loop.stop()
    loop.start()
    loop.stop()
    loop.stop()
    assert not loop.running


# ---------------------------------------------------------------------------
# The facade
# ---------------------------------------------------------------------------

def _pointer(**overrides):
    clock = FakeClock()
    moves = []
    parked = threading.Event()
    pointer = Pointer(
        config(**overrides),
        move=lambda dx, dy: moves.append((dx, dy)),
        clock=clock,
        # Bounded, and never coupled to wall time: these tests are about the
        # lifecycle, so the loop should idle rather than race a fake clock.
        sleep=lambda seconds: parked.wait(min(seconds, 0.005)),
        autostart=False,
    )
    return pointer, moves, clock


def test_the_pointer_does_not_spawn_a_thread_until_the_stick_moves():
    pointer, _, clock = _pointer()
    pointer.autostart = True
    try:
        pointer.update(0.0, 0.0)
        assert not pointer.loop.running
        pointer.update(0.0, 1.0)
        assert pointer.loop.running
    finally:
        pointer.close()


def test_switching_out_of_pointer_mode_stops_the_loop():
    pointer, _, clock = _pointer()
    pointer.autostart = True
    try:
        pointer.update(0.0, 1.0)
        assert pointer.loop.running
        pointer.configure(config(mode="directions"))
        assert not pointer.loop.running
    finally:
        pointer.close()


def test_preview_reports_a_sample_without_adopting_it():
    pointer, _, clock = _pointer()
    vector = pointer.preview(0.0, 1.0)
    assert vector.speed == pytest.approx(1000.0)
    assert pointer.vector().speed == 0.0, "preview must not move anything"


# ---------------------------------------------------------------------------
# Through the bridge
# ---------------------------------------------------------------------------

POINTER_CONFIG = parse({
    "version": 1,
    "bindings": {"ACT12": {"action": "key", "key": "escape"}},
    "joystick": {"mode": "pointer", "max_speed": 1000.0, "gamma": 1.0,
                 "pointer_deadzone": 0.1, "precision_key": "ACT12"},
})


def _bridge():
    backend = RecordingBackend()
    clock = FakeClock()
    pointer = Pointer(
        POINTER_CONFIG.joystick,
        move=lambda dx, dy: backend.move_mouse(dx, dy, relative=True),
        clock=clock,
        autostart=False,
    )
    return Bridge(POINTER_CONFIG, backend, pointer=pointer), backend, clock


def stick(angle, distance):
    return {"m": "v.oai.rad", "p": {"a": angle, "d": distance}}


def test_a_held_stick_moves_the_mouse_through_the_backend():
    bridge, backend, clock = _bridge()
    bridge.handle(stick(0.0, 1.0))
    bridge.pointer.loop.step()               # baseline
    clock.advance(0.01)
    bridge.pointer.loop.step()
    assert len(backend.calls) == 1
    name, (dx, dy, relative) = backend.calls[0]
    assert (name, dy, relative) == ("move_mouse", 0, True)
    assert dx == pytest.approx(10, abs=1)    # 1000 px/s for 10 ms


def test_the_precision_key_is_consumed_rather_than_dispatched():
    bridge, backend, clock = _bridge()
    assert bridge.handle({"m": "v.oai.hid", "p": {"k": "ACT12", "act": 1}}) == []
    assert backend.calls == [], "the binding must not also fire"
    assert bridge.pointer.vector().precision is False  # centred, but engaged
    bridge.handle(stick(0.0, 1.0))
    assert bridge.pointer.vector().precision
    bridge.handle({"m": "v.oai.hid", "p": {"k": "ACT12", "act": 0}})
    assert not bridge.pointer.vector().precision


def test_the_precision_key_still_runs_its_binding_in_directions_mode():
    """It is only special while the stick is pointing."""
    pad = parse({
        "version": 1,
        "bindings": {"ACT12": {"action": "key", "key": "escape"}},
        "joystick": {"mode": "directions", "precision_key": "ACT12"},
    })
    backend = RecordingBackend()
    bridge = Bridge(pad, backend)
    bridge.handle({"m": "v.oai.hid", "p": {"k": "ACT12", "act": 1}})
    assert backend.calls == [("press_key", ("escape",))]


def test_reloading_the_config_retunes_the_live_pointer():
    """The run loop reloads a changed keymap by assigning bridge.joystick; a
    stick still pointing at the old speed afterwards is a half-applied reload.
    """
    from freemicro.input.bridge import JoystickTracker

    bridge, backend, clock = _bridge()
    slower = parse({
        "version": 1, "bindings": {},
        "joystick": {"mode": "pointer", "max_speed": 100.0, "gamma": 1.0,
                     "pointer_deadzone": 0.1},
    })
    bridge.config = slower
    bridge.joystick = JoystickTracker(slower.joystick)
    del backend.calls[:]                     # the reload also lets go of holds
    bridge.handle(stick(0.0, 1.0))
    bridge.pointer.loop.step()
    clock.advance(0.05)
    bridge.pointer.loop.step()
    assert len(backend.calls) == 1
    dx = backend.calls[0][1][0]
    assert dx == pytest.approx(5, abs=1)     # 100 px/s, not the old 1000


def test_closing_the_bridge_stops_the_pointer():
    bridge, backend, clock = _bridge()
    bridge.pointer.autostart = True
    bridge.handle(stick(0.0, 1.0))
    assert bridge.pointer.loop.running
    bridge.close()
    assert not bridge.pointer.loop.running


# ---------------------------------------------------------------------------
# Tuning readout
# ---------------------------------------------------------------------------

def test_the_dry_run_line_shows_the_velocity_a_sample_implies():
    bridge, backend, clock = _bridge()
    line = joystick_line(stick(0.0, 1.0), bridge)
    assert "angle=0.0000" in line and "distance=1.0000" in line
    assert "1000.0px/s" in line
    assert backend.calls == [], "printing must not move anything"


def test_the_dry_run_line_falls_back_to_raw_numbers_in_directions_mode():
    pad = parse({"version": 1, "bindings": {},
                 "joystick": {"mode": "directions"}})
    bridge = Bridge(pad, RecordingBackend())
    line = joystick_line(stick(0.5275, 0.5594), bridge)
    assert line == "  joystick angle=0.5275 distance=0.5594"


def test_the_dry_run_line_ignores_messages_that_are_not_the_stick():
    bridge, _, _ = _bridge()
    assert joystick_line({"m": "v.oai.hid", "p": {"k": "AG00"}}, bridge) is None


def test_a_stale_readout_says_so():
    bridge, backend, clock = _bridge()
    bridge.handle(stick(0.0, 1.0))
    clock.advance(1.0)
    assert "stale" in bridge.pointer.vector().describe()
