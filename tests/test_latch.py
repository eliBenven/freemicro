"""The push-to-talk latch: hold to talk, or tap-tap to keep recording.

Two layers, both driven from data with no real time passing:

* :class:`freemicro.input.latch.LatchMachine` on its own - the vendor state
  machine from ``docs/FACTORY-DEFAULTS.md`` section 8, proven transition by
  transition.
* the bridge wiring it to a toggle tap, an activity light and a timer, including
  the two things that decide whether this feature is safe: it never leaves a
  modifier bleeding into another key, and it never leaves a dictation app
  recording after the pad is gone.
"""

from __future__ import annotations

import pytest

from freemicro.input import latch as latchmod
from freemicro.input.actions import Action, ActionError, Backend, RecordingBackend
from freemicro.input.bridge import Bridge
from freemicro.input.latch import (
    LATCH_WINDOW_SECONDS,
    START,
    STOP,
    LatchMachine,
)
from freemicro.lighting_owner import ActivityOverlay
from freemicro.padconfig import PadConfigError, parse

W = LATCH_WINDOW_SECONDS


# ---------------------------------------------------------------------------
# The machine on its own
# ---------------------------------------------------------------------------

def test_a_press_from_idle_starts_recording():
    m = LatchMachine()
    assert m.press(0.0) == [START]
    assert m.state == latchmod.PRESSED
    assert m.recording
    assert m.deadline is None  # a hold has no pending window


def test_a_long_hold_stops_on_release_the_classic_way():
    m = LatchMachine()
    m.press(0.0)
    assert m.release(W) == [STOP]        # held for exactly the window counts
    assert m.state == latchmod.IDLE
    assert not m.recording


def test_a_quick_tap_waits_instead_of_stopping():
    m = LatchMachine()
    m.press(0.0)
    assert m.release(0.1) == []          # quicker than the window
    assert m.state == latchmod.WAITING
    assert m.recording                   # still on while we wait for a second tap
    assert m.deadline == pytest.approx(0.1 + W)


def test_a_second_tap_inside_the_window_latches():
    m = LatchMachine()
    m.press(0.0)
    m.release(0.1)
    assert m.press(0.2) == []            # no emit: recording was already on
    assert m.state == latchmod.LATCHED
    assert m.recording
    assert m.deadline is None            # a latch is indefinite, no timer


def test_a_waiting_window_that_lapses_stops():
    m = LatchMachine()
    m.press(0.0)
    m.release(0.1)
    assert m.tick(0.1 + W) == [STOP]
    assert m.state == latchmod.IDLE
    assert not m.recording


def test_a_tap_while_latched_stops_and_debounces():
    m = LatchMachine()
    m.press(0.0)
    m.release(0.1)
    m.press(0.2)                         # latched
    assert m.press(10.0) == [STOP]       # tap again, minutes later
    assert m.state == latchmod.SUPPRESSING
    assert not m.recording
    assert m.deadline == pytest.approx(10.0 + W)


def test_a_second_stop_tap_inside_the_window_is_swallowed():
    m = LatchMachine()
    m.press(0.0)
    m.release(0.1)
    m.press(0.2)   # latched
    m.press(10.0)                                 # stop -> suppressing
    assert m.press(10.0 + W / 2) == []            # the double-tap's second half
    assert m.state == latchmod.SUPPRESSING
    assert not m.recording


def test_a_tap_after_the_suppressing_window_starts_a_new_session():
    m = LatchMachine()
    m.press(0.0)
    m.release(0.1)
    m.press(0.2)   # latched
    m.press(10.0)                                 # stop -> suppressing
    assert m.press(10.0 + W) == [START]           # window passed: fresh start
    assert m.state == latchmod.PRESSED
    assert m.recording


def test_a_suppressing_window_decays_to_idle_without_emitting():
    m = LatchMachine()
    m.press(0.0)
    m.release(0.1)
    m.press(0.2)
    m.press(10.0)                                 # suppressing, recording off
    assert m.tick(10.0 + W) == []                 # nothing to stop; already off
    assert m.state == latchmod.IDLE


def test_a_release_outside_pressed_does_nothing():
    m = LatchMachine()
    m.press(0.0)
    m.release(0.1)
    m.press(0.2)   # latched
    assert m.release(0.3) == []                   # the latching press's key-up
    assert m.state == latchmod.LATCHED
    assert m.recording


def test_force_stop_emits_a_stop_only_when_recording():
    m = LatchMachine()
    m.press(0.0)
    m.release(0.1)
    m.press(0.2)   # latched
    assert m.force_stop(5.0) == [STOP]
    assert m.state == latchmod.IDLE
    assert m.force_stop(6.0) == []                # already idle, nothing to stop


def test_the_window_is_the_vendors_350ms():
    assert LATCH_WINDOW_SECONDS == 0.35


# ---------------------------------------------------------------------------
# The bridge, wiring the machine to a tap, a light and a timer
# ---------------------------------------------------------------------------

class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> float:
        self.now += seconds
        return self.now


LATCH_BINDING = {
    "action": "hold", "key": "ctrl+cmd+o", "latch": True, "label": "mic",
    "light": {"color": "#2E8B57"},
}


def _latch_bridge(binding=None, extra=None, backend=None):
    bindings = {"ACT10": dict(binding or LATCH_BINDING)}
    if extra:
        bindings.update(extra)
    clock = Clock()
    seen: list = []
    bridge = Bridge(
        parse({"version": 1, "bindings": bindings}),
        backend or RecordingBackend(), clock=clock, autostart=False,
        on_activity=lambda input_id, light: seen.append((input_id, light is not None)),
    )
    return bridge, bridge.backend, clock, seen


def _taps(backend):
    return [args[0] for name, args in backend.calls if name == "press_key"]


def test_a_latch_taps_the_shortcut_it_never_holds_a_modifier():
    """The whole safety story: a tap, not a hold_key, so nothing stays down."""
    bridge, be, clock, seen = _latch_bridge()
    bridge.press("ACT10")
    assert _taps(be) == ["ctrl+cmd+o"]
    assert be.held == []                 # nothing physically down
    assert seen[-1] == ("ACT10", True)   # and the light is on


def test_classic_hold_to_talk_taps_on_press_and_on_release():
    bridge, be, clock, seen = _latch_bridge()
    bridge.press("ACT10")
    clock.advance(0.5)                   # a real hold
    bridge.release("ACT10")
    assert _taps(be) == ["ctrl+cmd+o", "ctrl+cmd+o"]   # start, then stop
    assert seen[-1] == ("ACT10", False)                # light back off


def test_tap_tap_latches_on_and_stays_recording_then_a_tap_stops_it():
    bridge, be, clock, seen = _latch_bridge()
    bridge.press("ACT10")                # start
    clock.advance(0.1)
    bridge.release("ACT10")   # quick -> waiting
    clock.advance(0.1)
    bridge.press("ACT10")     # -> latched
    clock.advance(0.05)
    bridge.release("ACT10")  # ignored
    assert _taps(be) == ["ctrl+cmd+o"]            # exactly one tap so far
    machine = bridge._latch["ACT10"].machine
    assert machine.state == latchmod.LATCHED and machine.recording
    # Minutes pass with nothing happening; the timer must not stop it.
    clock.advance(300.0)
    bridge.latch_timer.step()
    assert machine.recording
    # Tap once more.
    clock.advance(1.0)
    bridge.press("ACT10")
    assert _taps(be) == ["ctrl+cmd+o", "ctrl+cmd+o"]   # the stop tap
    assert not machine.recording
    assert seen[-1] == ("ACT10", False)


def test_a_slow_hold_release_hold_is_two_sessions_not_a_latch():
    """The competing gesture: a deliberate quick tap-tap latches, but a slow
    hold, let go, hold again is just two ordinary hold-to-talks."""
    bridge, be, clock, seen = _latch_bridge()
    bridge.press("ACT10")
    clock.advance(0.5)
    bridge.release("ACT10")   # session 1
    clock.advance(1.0)
    bridge.press("ACT10")
    clock.advance(0.5)
    bridge.release("ACT10")   # session 2
    assert _taps(be) == ["ctrl+cmd+o"] * 4             # start,stop,start,stop
    assert not bridge._latch["ACT10"].machine.recording


def test_a_lapsed_waiting_window_stops_through_the_timer():
    seen_dispatch: list = []
    bridge, be, clock, seen = _latch_bridge()
    bridge.on_dispatch = seen_dispatch.append
    bridge.press("ACT10")
    clock.advance(0.1)
    bridge.release("ACT10")        # quick -> waiting
    assert bridge.latch_timer.deadline == pytest.approx(0.1 + W)
    clock.advance(W)
    assert bridge.latch_timer.step()
    assert _taps(be) == ["ctrl+cmd+o", "ctrl+cmd+o"]   # start, then the timed stop
    assert seen[-1] == ("ACT10", False)
    assert seen_dispatch and seen_dispatch[-1].action.kind == "hold"


# ---------------------------------------------------------------------------
# Interaction 1: modifier bleed
# ---------------------------------------------------------------------------

def test_a_stray_key_types_normally_while_latched():
    """A latch holds no real modifier, so a brushed key cannot come out as a
    shortcut - the bug that suppression exists to prevent simply cannot occur."""
    bridge, be, clock, seen = _latch_bridge(
        extra={"ACT06": {"action": "text", "text": "hello"}}
    )
    bridge.press("ACT10")
    clock.advance(0.1)
    bridge.release("ACT10")
    clock.advance(0.1)
    bridge.press("ACT10")          # latched
    result = bridge.press("ACT06")
    bridge.release("ACT06")
    assert not result[0].suppressed
    assert ("type_text", ("hello",)) in be.calls


def test_a_plain_hold_still_suppresses_other_keys():
    """The non-latch path is untouched: a real hold still blocks typing."""
    bridge = Bridge(
        parse({"version": 1, "bindings": {
            "ACT10": {"action": "hold", "key": "ctrl+cmd+o"},
            "ACT06": {"action": "text", "text": "hello"},
        }}),
        RecordingBackend(), autostart=False,
    )
    bridge.press("ACT10")
    result = bridge.press("ACT06")
    assert result[0].suppressed
    assert ("hold_key", ("ctrl+cmd+o", True)) in bridge.backend.calls


def test_a_latch_tap_is_refused_while_another_key_holds_real_modifiers():
    """If some *other* key is a physical hold, the latch tap would bleed too, so
    it is suppressed like any keystroke, and its release is swallowed to match."""
    bridge = Bridge(
        parse({"version": 1, "bindings": {
            "ACT09": {"action": "hold", "key": "ctrl+shift+a"},
            "ACT10": dict(LATCH_BINDING),
        }}),
        RecordingBackend(), autostart=False,
    )
    bridge.press("ACT09")                # holds real modifiers
    result = bridge.press("ACT10")
    assert result[0].suppressed
    assert _taps(bridge.backend) == []   # no tap sent
    bridge.release("ACT10")              # swallowed, no stray tap
    assert _taps(bridge.backend) == []


# ---------------------------------------------------------------------------
# Interaction 2: a lost release must never leave it recording
# ---------------------------------------------------------------------------

def test_stop_latches_sends_the_off_tap_and_clears_the_light():
    bridge, be, clock, seen = _latch_bridge()
    bridge.press("ACT10")
    clock.advance(0.1)
    bridge.release("ACT10")
    clock.advance(0.1)
    bridge.press("ACT10")          # latched, recording
    be.calls.clear()
    seen.clear()
    produced = bridge.stop_latches("the pad disconnected")
    assert _taps(be) == ["ctrl+cmd+o"]                 # the toggle-off tap
    assert seen[-1] == ("ACT10", False)                # light retired
    assert "ACT10" not in bridge._latch                # machine forgotten
    assert produced and produced[0].action.kind == "hold"


def test_stop_latches_is_quiet_when_nothing_is_latched():
    bridge, be, clock, seen = _latch_bridge()
    assert bridge.stop_latches("idle") == []
    assert _taps(be) == []


def test_closing_the_bridge_stops_a_latch():
    bridge, be, clock, seen = _latch_bridge()
    bridge.press("ACT10")
    clock.advance(0.1)
    bridge.release("ACT10")
    clock.advance(0.1)
    bridge.press("ACT10")          # latched
    be.calls.clear()
    seen.clear()
    bridge.close()
    assert _taps(be) == ["ctrl+cmd+o"]                 # off tap on the way out
    assert seen[-1] == ("ACT10", False)


def test_release_held_keys_stops_a_latch():
    bridge, be, clock, seen = _latch_bridge()
    bridge.press("ACT10")
    clock.advance(0.1)
    bridge.release("ACT10")
    clock.advance(0.1)
    bridge.press("ACT10")          # latched
    be.calls.clear()
    seen.clear()
    bridge.release_held_keys()
    assert _taps(be) == ["ctrl+cmd+o"]
    assert seen[-1] == ("ACT10", False)


def test_a_config_reload_stops_a_latch_before_it_rebinds():
    """A reloaded file must not leave a dictation app recording under a binding
    the user may have just deleted."""
    bridge, be, clock, seen = _latch_bridge()
    bridge.press("ACT10")
    clock.advance(0.1)
    bridge.release("ACT10")
    clock.advance(0.1)
    bridge.press("ACT10")          # latched
    be.calls.clear()
    seen.clear()
    bridge.config = parse({"version": 1, "bindings": {
        "ACT10": {"action": "none"},
    }})
    assert _taps(be) == ["ctrl+cmd+o"]                 # stopped on the old binding
    assert seen[-1] == ("ACT10", False)
    assert bridge._latch == {}


# ---------------------------------------------------------------------------
# The light stays honest across a latch that outlives its own timeout
# ---------------------------------------------------------------------------

def test_a_live_latch_is_kept_lit_past_the_lights_timeout():
    """The activity overlay times a light out from the clock, but a latch records
    indefinitely, so the bridge re-asserts the light before the deadline. The
    light must therefore still be up long after ``timeout_seconds`` has passed."""
    clock = Clock()
    overlay = ActivityOverlay(clock=clock)
    bridge = Bridge(
        parse({"version": 1, "bindings": {"ACT10": {
            "action": "hold", "key": "ctrl+cmd+o", "latch": True,
            "light": {"color": "#2E8B57", "timeout_seconds": 10},
        }}}),
        RecordingBackend(), clock=clock, autostart=False,
        on_activity=overlay.note,
    )
    bridge.press("ACT10")
    clock.advance(0.1)
    bridge.release("ACT10")
    clock.advance(0.1)
    bridge.press("ACT10")          # latched, light up
    assert overlay.active == ("ACT10",)
    # Well past the 10 s timeout, driving only the timer the way the run loop's
    # render tick would call step() and poll().
    for _ in range(6):
        clock.advance(4.0)
        bridge.latch_timer.step()
        overlay.poll()
    assert overlay.active == ("ACT10",), "a live latch must not time its own light out"
    # And when it stops, the light goes down at once.
    bridge.stop_latches("done")
    assert overlay.active == ()


# ---------------------------------------------------------------------------
# A tap that fails does not leave the light claiming it recorded
# ---------------------------------------------------------------------------

def test_a_failed_start_tap_takes_its_light_back_down():
    class Broken(Backend):
        description = "broken"

        def press_key(self, combo):
            raise ActionError("nope")

    bridge, be, clock, seen = _latch_bridge(backend=Broken())
    result = bridge.press("ACT10")
    assert result[0].ok is False
    assert seen[-1] == ("ACT10", False)


# ---------------------------------------------------------------------------
# Load-time refusals and surfacing
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("input_id", ["ENC_CW", "ENC_CC", "JOY_UP", "JOY_LEFT"])
def test_an_input_with_no_release_cannot_latch(input_id):
    with pytest.raises(PadConfigError) as exc:
        parse({"version": 1, "bindings": {
            input_id: {"action": "hold", "key": "f5", "latch": True},
        }})
    assert "latch" in str(exc.value)


def test_latch_must_be_a_boolean():
    with pytest.raises(PadConfigError) as exc:
        parse({"version": 1, "bindings": {
            "ACT10": {"action": "hold", "key": "f5", "latch": "yes"},
        }})
    assert "true or false" in str(exc.value)


def test_a_plain_hold_without_latch_is_byte_identical():
    """The option being absent must not change the plain hold at all."""
    bridge = Bridge(
        parse({"version": 1, "bindings": {
            "ACT10": {"action": "hold", "key": "ctrl+cmd+o"},
        }}),
        RecordingBackend(), autostart=False,
    )
    bridge.press("ACT10")
    bridge.release("ACT10")
    assert bridge.backend.calls == [
        ("hold_key", ("ctrl+cmd+o", True)),
        ("hold_key", ("ctrl+cmd+o", False)),
    ]
    assert bridge._latch == {}           # the latch path was never entered


def test_keys_list_describes_the_latch_and_how():
    cfg = parse({"version": 1, "bindings": {
        "ACT10": {"action": "hold", "key": "ctrl+cmd+o", "latch": True},
    }})
    described = cfg.action_for("ACT10").describe()
    assert "tap-tap" in described and "ctrl+cmd+o" in described


def test_a_plain_hold_still_describes_as_a_hold():
    cfg = parse({"version": 1, "bindings": {
        "ACT10": {"action": "hold", "key": "ctrl+cmd+o"},
    }})
    assert cfg.action_for("ACT10").describe() == "hold ctrl+cmd+o while pressed"


# ---------------------------------------------------------------------------
# The web UI editor offers it
# ---------------------------------------------------------------------------

def test_the_schema_offers_latch_on_the_hold_action():
    from freemicro.webui.api import Api

    api = Api.__new__(Api)
    _, body = Api.schema(api)
    hold = next(a for a in body["actions"] if a["kind"] == "hold")
    assert "latch" in hold["optional"]
    field = next(f for f in hold["fields"] if f["name"] == "latch")
    assert field["widget"] == "boolean"


def test_the_editor_coerces_a_latch_string_to_a_boolean():
    from freemicro.webui import configio

    document = configio.normalise({"bindings": {
        "ACT10": {"action": "hold", "key": "f5", "latch": "true"},
    }})
    latch = document["bindings"]["ACT10"]["latch"]
    assert latch is True


# ---------------------------------------------------------------------------
# A latch on a chord input id still resolves through the machine
# ---------------------------------------------------------------------------

def test_a_latching_hold_is_still_a_latching_action():
    from freemicro.input.actions import is_latching

    latch = Action(kind="hold", params={"key": "f5", "latch": True})
    plain = Action(kind="hold", params={"key": "f5"})
    assert is_latching(latch)
    assert not is_latching(plain)
    assert not is_latching(None)
    assert not is_latching(Action(kind="key", params={"key": "f5"}))
