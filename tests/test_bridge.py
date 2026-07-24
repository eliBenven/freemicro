"""Tests for the input bridge: protocol events in, configured actions out.

Every message here is the real wire shape from ``docs/PROTOCOL.md``, and every
action goes to a recording backend - so this exercises the whole path a key
press takes without a pad attached and without typing anything.
"""

from __future__ import annotations

import threading
import time

from freemicro.input.actions import (
    REGISTRY,
    Action,
    ActionError,
    Backend,
    RecordingBackend,
    perform,
)
from freemicro.input.bridge import (
    MODIFIER_HOLDING_KINDS,
    MODIFIER_SAFE_KINDS,
    Bridge,
    JoystickTracker,
    SettleTimer,
    _monotonic,
    joystick_sample,
)
from freemicro.input.pointer import Pointer
from freemicro.padconfig import JoystickConfig, load_default, parse

CONFIG_BINDINGS = {
    "AG00": {"action": "text", "text": "/resume", "submit": True,
             "label": "resume"},
    "ACT10": {"action": "key", "key": "escape", "label": "interrupt"},
    "JOY_UP": {"action": "key", "key": "up", "label": "history-prev"},
    "JOY_RIGHT": {"action": "key", "key": "tab", "label": "complete"},
    "JOY_DOWN": {"action": "none"},
    "JOY_LEFT": {"action": "none"},
}

CONFIG = parse({"version": 1, "bindings": dict(CONFIG_BINDINGS)})


def _bridge():
    backend = RecordingBackend()
    return Bridge(CONFIG, backend), backend


def key_event(key, act=1, agent=0):
    return {"m": "v.oai.hid", "p": {"k": key, "act": act, "ag": agent}}


def stick_event(angle, distance):
    return {"m": "v.oai.rad", "p": {"a": angle, "d": distance}}


# ---------------------------------------------------------------------------
# Key events
# ---------------------------------------------------------------------------

def test_key_down_fires_the_bound_action():
    bridge, backend = _bridge()
    results = bridge.handle(key_event("AG00"))
    assert [r.input_id for r in results] == ["AG00"]
    assert results[0].ok and results[0].bound
    assert backend.calls == [
        ("type_text", ("/resume",)), ("press_key", ("return",)),
    ]


def test_key_up_does_nothing():
    bridge, backend = _bridge()
    assert bridge.handle(key_event("AG00", act=0)) == []
    assert backend.calls == []


def test_unmapped_key_reports_rather_than_raising():
    bridge, backend = _bridge()
    result = bridge.handle(key_event("ACT07"))[0]
    assert not result.bound
    assert result.describe() == "unmapped"
    assert backend.calls == []


def test_standard_jsonrpc_spelling_is_accepted():
    bridge, backend = _bridge()
    bridge.handle({"method": "v.oai.hid", "params": {"k": "ACT10", "act": 1}})
    assert backend.calls == [("press_key", ("escape",))]


def test_unrelated_messages_are_ignored():
    bridge, backend = _bridge()
    assert bridge.decode({"result": {"ok": 1}, "id": None}) == []
    assert bridge.decode({"m": "device.status", "p": {}}) == []
    assert backend.calls == []


def test_malformed_key_events_are_ignored():
    bridge, _ = _bridge()
    assert bridge.decode({"m": "v.oai.hid", "p": {"act": 1}}) == []
    assert bridge.decode({"m": "v.oai.hid", "p": "nope"}) == []
    assert bridge.decode({"m": "v.oai.hid"}) == []


def test_a_failing_action_is_reported_not_raised():
    class Broken(Backend):
        def press_key(self, combo):
            raise RuntimeError("no accessibility permission")

    bridge = Bridge(CONFIG, Broken())
    result = bridge.handle(key_event("ACT10"))[0]
    assert result.bound and not result.ok
    assert "accessibility" in result.error


# ---------------------------------------------------------------------------
# Joystick
# ---------------------------------------------------------------------------

def test_joystick_fires_once_per_flick():
    tracker = JoystickTracker(JoystickConfig(deadzone=0.6))
    assert tracker.update(0.25, 0.1) is None        # at rest
    assert tracker.update(0.25, 0.7) == "JOY_DOWN"  # crossed the deadzone
    assert tracker.update(0.25, 0.9) is None        # held - no repeat
    assert tracker.update(0.25, 0.8) is None
    assert tracker.update(0.25, 0.1) is None        # released, re-armed
    assert tracker.update(0.25, 0.7) == "JOY_DOWN"


def test_the_wheel_matches_the_orientation_the_hardware_reports():
    """docs/FACTORY-DEFAULTS.md §6: right 0.0, down 0.25, left 0.5, up 0.75.

    This used to read right/up/left/down, so pushing the stick down fired
    JOY_UP. Harmless while flicks were the only mode; not harmless next to a
    pointer that moves the cursor the way the stick actually went.
    """
    tracker = JoystickTracker(JoystickConfig(deadzone=0.6))
    for angle, expected in (
        (0.0, "JOY_RIGHT"), (0.25, "JOY_DOWN"),
        (0.5, "JOY_LEFT"), (0.75, "JOY_UP"),
    ):
        tracker.update(angle, 0.0)  # recentre so it re-arms
        assert tracker.update(angle, 0.9) == expected


def test_joystick_hysteresis_stops_chatter_at_the_threshold():
    tracker = JoystickTracker(JoystickConfig(deadzone=0.6))
    assert tracker.update(0.0, 0.65) == "JOY_RIGHT"
    # Hovering just under the deadzone must not re-arm and re-fire.
    assert tracker.update(0.0, 0.55) is None
    assert tracker.update(0.0, 0.65) is None


def _ids(events):
    return [event.input_id for event in events]


#: Discrete flicks are no longer the default mode, so the tests that are about
#: flicks have to ask for them. The pointer gets its own file.
FLICK_CONFIG = parse({
    "version": 1,
    "bindings": dict(CONFIG_BINDINGS),
    "joystick": {"mode": "directions"},
})


def _flick_bridge():
    backend = RecordingBackend()
    return Bridge(FLICK_CONFIG, backend), backend


def test_joystick_directions_follow_the_angle():
    bridge, backend = _flick_bridge()
    assert _ids(bridge.decode(stick_event(0.0, 0.9))) == ["JOY_RIGHT"]
    bridge.decode(stick_event(0.0, 0.0))
    assert _ids(bridge.decode(stick_event(0.75, 0.9))) == ["JOY_UP"]


def test_joystick_dispatches_through_to_the_backend():
    bridge, backend = _flick_bridge()
    bridge.handle(stick_event(0.75, 0.95))
    assert backend.calls == [("press_key", ("up",))]


def test_pointer_mode_fires_no_discrete_inputs():
    """Pointing and flicking are exclusive: the cursor moves on the pointer's
    own tick, so the same push must not also type something."""
    backend = RecordingBackend()
    bridge = Bridge(CONFIG, backend, pointer=Pointer(
        CONFIG.joystick,
        move=lambda dx, dy: backend.move_mouse(dx, dy),
        autostart=False,  # no background thread inside a unit test
    ))
    assert bridge.decode(stick_event(0.25, 0.95)) == []
    assert backend.calls == []
    assert bridge.last_vector is not None and bridge.last_vector.moving


def test_joystick_sample_exposes_raw_numbers_for_calibration():
    assert joystick_sample(stick_event(0.5275, 0.5594)) == {
        "angle": 0.5275, "distance": 0.5594,
    }
    assert joystick_sample(key_event("AG00")) is None
    assert joystick_sample({"m": "v.oai.rad", "p": {"a": "x"}}) is None


# ---------------------------------------------------------------------------
# Against the shipped default
# ---------------------------------------------------------------------------

def test_every_real_key_id_is_bound_in_the_default_config():
    pad = load_default()
    backend = RecordingBackend()
    bridge = Bridge(pad, backend)
    ids = [f"AG{i:02d}" for i in range(6)]
    ids += [f"ACT{i:02d}" for i in range(6, 13)]
    ids += ["ENC_CLK"]
    for input_id in ids:
        results = bridge.handle(key_event(input_id))
        assert results and results[0].bound, f"{input_id} is unmapped"
        assert results[0].ok
    assert backend.calls, "the default config delivered nothing"


# ---------------------------------------------------------------------------
# Encoder ticks
# ---------------------------------------------------------------------------

ENCODER_CONFIG = parse({
    "version": 1,
    "bindings": {
        "ENC_CW": {"action": "key", "key": "up", "label": "effort +"},
        "ENC_CC": {"action": "key", "key": "down", "label": "effort -"},
        "ENC_CLK": {"action": "key", "key": "return", "label": "click"},
    },
})


def test_encoder_ticks_fire_regardless_of_act():
    """Rotation has been seen reporting act values other than 1.

    Filtering on act==1 would silently swallow every dial turn, which is
    indistinguishable from "the dial doesn't work". Ticks are momentary and have
    no matching release, so firing on any act value cannot double-trigger.
    """
    for act in (0, 1, 2, 3, None):
        bridge = Bridge(ENCODER_CONFIG, RecordingBackend())
        message = {"m": "v.oai.hid", "p": {"k": "ENC_CW", "act": act}}
        results = bridge.handle(message)
        assert results and results[0].ok, f"ENC_CW dropped with act={act!r}"
        assert bridge.backend.calls == [("press_key", ("up",))]


def test_encoder_both_directions():
    bridge = Bridge(ENCODER_CONFIG, RecordingBackend())
    bridge.handle({"m": "v.oai.hid", "p": {"k": "ENC_CW", "act": 2}})
    bridge.handle({"m": "v.oai.hid", "p": {"k": "ENC_CC", "act": 2}})
    assert bridge.backend.calls == [
        ("press_key", ("up",)), ("press_key", ("down",)),
    ]


def test_encoder_press_still_uses_the_normal_press_release_rules():
    """ENC_CLK is a real key: it has a release, so act must still be honoured."""
    bridge = Bridge(ENCODER_CONFIG, RecordingBackend())
    bridge.handle({"m": "v.oai.hid", "p": {"k": "ENC_CLK", "act": 1}})
    bridge.handle({"m": "v.oai.hid", "p": {"k": "ENC_CLK", "act": 0}})
    assert bridge.backend.calls == [("press_key", ("return",))]


# ---------------------------------------------------------------------------
# Hold-to-talk
# ---------------------------------------------------------------------------

HOLD_CONFIG = parse({
    "version": 1,
    "bindings": {
        "ACT11": {"action": "hold", "key": "ctrl+option+cmd+d", "label": "talk"},
    },
})


def test_hold_presses_on_key_down_and_releases_on_key_up():
    bridge = Bridge(HOLD_CONFIG, RecordingBackend())
    combo = "ctrl+option+cmd+d"
    bridge.handle(key_event("ACT11", act=1))
    assert bridge.backend.calls == [("hold_key", (combo, True))]
    bridge.handle(key_event("ACT11", act=0))
    assert bridge.backend.calls[-1] == ("hold_key", (combo, False))


def test_release_of_a_non_hold_binding_is_not_reported():
    bridge, backend = _bridge()
    assert bridge.handle(key_event("AG00", act=0)) == []


def test_closing_the_bridge_lets_go_of_a_key_that_is_still_held():
    """Ctrl-C mid-hold, or a re-exec, must not leave modifiers down system
    wide - which is unrecoverable without another app or a logout."""
    bridge = Bridge(HOLD_CONFIG, RecordingBackend())
    bridge.handle(key_event("ACT11", act=1))
    assert bridge.backend.held == ["ctrl+option+cmd+d"]
    bridge.close()
    assert bridge.backend.held == []
    bridge.close()  # idempotent: it also runs from atexit and a signal handler


def test_a_config_reload_lets_go_before_it_rebinds():
    """The release is produced by matching the *old* binding. Rebind the key
    without letting go first and nothing will ever send the key-up."""
    bridge = Bridge(HOLD_CONFIG, RecordingBackend())
    bridge.handle(key_event("ACT11", act=1))
    rebound = parse({"version": 1, "bindings": {
        "ACT11": {"action": "key", "key": "escape"},
    }})
    bridge.config = rebound
    bridge.joystick = JoystickTracker(rebound.joystick)
    assert bridge.backend.held == []


def test_releasing_never_raises_even_if_the_backend_does():
    """close() runs on shutdown paths. An exception there would mask whatever
    we were actually shutting down for."""
    class Hostile(Backend):
        def release_held_keys(self):
            raise RuntimeError("no")

    bridge = Bridge(CONFIG, Hostile())
    assert bridge.release_held_keys() == 0
    bridge.close()


# ---------------------------------------------------------------------------
# Two keys at once, part 1: nothing may type under a held modifier
# ---------------------------------------------------------------------------
#
# `hold` presses REAL modifier keys and leaves them down - that is what makes
# push-to-talk work, and it is also why a second key pressed mid-sentence does
# not send what it says. With ctrl+cmd+o held, typing "continue" sends
# ctrl+cmd+c, ctrl+cmd+o, ctrl+cmd+n… every one of them a live macOS shortcut.
# The MIC keycap is double-width and sits next to the other action keys, so
# brushing one is an ordinary accident.

SUPPRESS_CONFIG = parse({
    "version": 1,
    "bindings": {
        "ACT10": {"action": "hold", "key": "ctrl+cmd+o", "label": "mic"},
        "ACT09": {"action": "text", "text": "continue", "submit": True,
                  "label": "play"},
        "ACT06": {"action": "key", "key": "escape", "label": "stop"},
        "ACT12": {"action": "app", "name": "Terminal", "label": "term"},
        "ENC_CLK": {"action": "mouse", "click": "left", "label": "click"},
        "ACT07": {"action": "shell", "command": "true", "label": "sh"},
        "ACT08": {"action": "applescript", "script": "beep", "label": "osa"},
        "AG00": {"action": "focus_session", "label": "agent 1"},
    },
})


def _suppress_bridge():
    bridge = Bridge(SUPPRESS_CONFIG, RecordingBackend(), autostart=False)
    bridge.handle(key_event("ACT10", act=1))  # mic down, modifiers now held
    return bridge, bridge.backend


def test_typing_is_suppressed_while_a_hold_binding_is_down():
    bridge, backend = _suppress_bridge()
    result = bridge.handle(key_event("ACT09"))[0]
    assert result.suppressed and result.suppressed_by == "ACT10"
    assert result.ok, "a refusal is not a failure"
    assert backend.calls == [("hold_key", ("ctrl+cmd+o", True))]


def test_a_suppressed_press_says_so_in_the_log_line():
    """--dry-run and the run loop both print describe(); a press that silently
    vanishes is indistinguishable from a broken pad."""
    bridge, _ = _suppress_bridge()
    line = bridge.handle(key_event("ACT09"))[0].describe()
    assert "NOT SENT" in line and "ACT10" in line and "ctrl+cmd+o" in line


def test_every_keystroke_kind_is_suppressed():
    for input_id in ("ACT09", "ACT06", "ACT08"):
        bridge, backend = _suppress_bridge()
        assert bridge.handle(key_event(input_id))[0].suppressed, input_id
        assert len(backend.calls) == 1, f"{input_id} reached the backend"


def test_actions_that_cannot_be_modified_are_not_suppressed():
    """Focusing a window or moving the mouse means the same thing whether or
    not Ctrl is down. Over-suppressing would make the pad feel broken."""
    for input_id, expected in (
        ("ACT12", ("activate_app", ("Terminal", False))),
        ("ENC_CLK", ("click_mouse", ("left", 1))),
        ("ACT07", ("run_shell", ("true", None, False))),
    ):
        bridge, backend = _suppress_bridge()
        result = bridge.handle(key_event(input_id))[0]
        assert not result.suppressed, input_id
        assert backend.calls[-1] == expected, f"{input_id} was wrongly suppressed"
    # focus_session reaches no backend at all when nothing is running, which is
    # its documented outcome - so all there is to assert is that it was allowed.
    bridge, _ = _suppress_bridge()
    assert not bridge.handle(key_event("AG00"))[0].suppressed


def test_an_unknown_action_kind_is_assumed_to_type():
    """MODIFIER_SAFE_KINDS is an allowlist. A custom action registered by a
    user is unknown here by definition, and the cost of guessing wrong that way
    is one logged skip - the other way it is an arbitrary system shortcut."""
    assert "text" not in MODIFIER_SAFE_KINDS
    assert "invented_by_a_user" not in MODIFIER_SAFE_KINDS


#: Enough parameters to run each registered kind once against a recording
#: backend. New kinds must be added here, which is the point - see below.
_RUNNABLE = {
    "text": {"text": "x"},
    "key": {"key": "escape"},
    "hold": {"key": "ctrl+cmd+o"},
    "shell": {"command": "true"},
    "applescript": {"script": "beep"},
    "app": {"name": "Terminal"},
    "focus_session": {"slot": 0},
    "mouse": {"click": "left"},
    "none": {},
    "answer_permission": {},
}


def test_only_the_hold_kind_leaves_real_keys_down():
    """The suppression rule turns on "is a real modifier physically down", so
    that has to be measured, not assumed.

    HOLD_KINDS is the wrong question: it only asks whether a kind cares about
    the release. `answer_permission` does - it times a long press - but holds
    nothing down meanwhile, and treating it as a modifier would suppress every
    other key for the length of an ordinary press.

    If this fails because a kind is missing from _RUNNABLE, a new action kind
    has landed and needs classifying against MODIFIER_SAFE_KINDS and
    MODIFIER_HOLDING_KINDS. That is the failure doing its job.
    """
    assert set(REGISTRY) == set(_RUNNABLE), (
        "an action kind was added or removed without being classified for the "
        "held-modifier rule"
    )
    holds = set()
    for kind, params in _RUNNABLE.items():
        backend = RecordingBackend()
        try:
            perform(Action(kind=kind, params=params), backend)
        except ActionError:
            pass  # a kind that cannot run here cannot be holding a key either
        if any(name == "hold_key" and args[1] for name, args in backend.calls):
            holds.add(kind)
    assert holds == MODIFIER_HOLDING_KINDS
    assert MODIFIER_HOLDING_KINDS.isdisjoint(MODIFIER_SAFE_KINDS)


def test_a_kind_with_a_release_but_no_held_key_does_not_suppress():
    """Directly: `answer_permission` held for a long press must not silence
    the rest of the pad while it waits."""
    pad = parse({"version": 1, "bindings": {
        "ACT09": {"action": "answer_permission", "long_press": "always",
                  "label": "approve"},
        "ACT06": {"action": "text", "text": "typed", "label": "type"},
    }})
    bridge = Bridge(pad, RecordingBackend(), autostart=False)
    bridge.handle(key_event("ACT09", act=1))
    assert not bridge.handle(key_event("ACT06"))[0].suppressed


def test_suppression_lifts_the_moment_the_hold_is_released():
    bridge, backend = _suppress_bridge()
    bridge.handle(key_event("ACT09"))
    bridge.handle(key_event("ACT10", act=0))
    assert not bridge.handle(key_event("ACT09"))[0].suppressed
    assert backend.calls[-2:] == [
        ("type_text", ("continue",)), ("press_key", ("return",)),
    ]


def test_a_suppressed_hold_does_not_send_a_stray_release():
    """Its press never happened, so its key-up must not happen either - that
    would be a key-up for a key that was never down."""
    pad = parse({"version": 1, "bindings": {
        "ACT10": {"action": "hold", "key": "ctrl+cmd+o", "label": "mic"},
        "ACT11": {"action": "hold", "key": "cmd+shift+d", "label": "second talk"},
    }})
    bridge = Bridge(pad, RecordingBackend(), autostart=False)
    bridge.handle(key_event("ACT10", act=1))
    assert bridge.handle(key_event("ACT11", act=1))[0].suppressed
    assert bridge.handle(key_event("ACT11", act=0)) == []
    bridge.handle(key_event("ACT10", act=0))
    assert bridge.backend.held == []
    assert bridge.backend.calls == [
        ("hold_key", ("ctrl+cmd+o", True)), ("hold_key", ("ctrl+cmd+o", False)),
    ]


def test_releasing_the_held_keys_also_clears_the_suppression():
    """A stale entry would suppress every press for the rest of the run with
    nothing actually held down to justify it - a silently dead pad."""
    bridge, _ = _suppress_bridge()
    bridge.release_held_keys()
    assert not bridge.handle(key_event("ACT09"))[0].suppressed


def test_a_config_reload_clears_the_suppression_too():
    bridge, _ = _suppress_bridge()
    bridge.joystick = JoystickTracker(SUPPRESS_CONFIG.joystick)  # reload path
    assert not bridge.handle(key_event("ACT09"))[0].suppressed


def test_a_hold_that_fails_does_not_suppress_anything():
    class Broken(Backend):
        def hold_key(self, combo, down):
            raise RuntimeError("no accessibility permission")

        def type_text(self, text):
            self.typed = text

    bridge = Bridge(SUPPRESS_CONFIG, Broken(), autostart=False)
    assert not bridge.handle(key_event("ACT10"))[0].ok
    assert not bridge.handle(key_event("ACT09"))[0].suppressed


# ---------------------------------------------------------------------------
# Two keys at once, part 2: chords
# ---------------------------------------------------------------------------

CHORD_BINDINGS = {
    "AG00": {"action": "text", "text": "solo0"},
    "AG01": {"action": "text", "text": "solo1"},
    "AG02": {"action": "text", "text": "solo2"},
    "AG00+AG01": {"action": "text", "text": "both", "label": "chord"},
}


class FakeClock:
    """A clock the tests move by hand, so timing is data and not a sleep."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> float:
        self.now += seconds
        return self.now


def _chord_bridge(bindings=None, **document):
    data = {"version": 1, "bindings": dict(bindings or CHORD_BINDINGS)}
    data.update(document)
    clock = FakeClock()
    bridge = Bridge(parse(data), RecordingBackend(), clock=clock, autostart=False)
    return bridge, bridge.backend, clock


def _typed(backend):
    return [args[0] for name, args in backend.calls if name == "type_text"]


def test_a_chord_fires_instead_of_its_members_not_as_well_as_them():
    """The whole design problem: key-down for AG00 arrives before anything can
    know AG01 is coming, so 'fire AG00 then notice' would double-act."""
    bridge, backend, _ = _chord_bridge()
    assert bridge.handle(key_event("AG00")) == []
    results = bridge.handle(key_event("AG01"))
    assert [r.input_id for r in results] == ["AG00+AG01"]
    assert _typed(backend) == ["both"]
    bridge.handle(key_event("AG01", act=0))
    bridge.handle(key_event("AG00", act=0))
    assert _typed(backend) == ["both"], "a member's solo binding leaked out"


def test_a_chord_fires_whichever_key_lands_first():
    for first, second in (("AG00", "AG01"), ("AG01", "AG00")):
        bridge, backend, _ = _chord_bridge()
        bridge.handle(key_event(first))
        assert bridge.handle(key_event(second))[0].input_id == "AG00+AG01"
        assert _typed(backend) == ["both"]


def test_a_key_that_cannot_chord_pays_no_latency_at_all():
    """The whole appeal of this pad is instant response. A settle window on
    every press would be a real cost, so it is only paid where it buys
    something."""
    bridge, backend, _ = _chord_bridge()
    assert bridge.handle(key_event("AG02"))[0].bound
    assert _typed(backend) == ["solo2"]
    assert bridge.settle.deadline is None, "a non-chord key armed the timer"


def test_a_chord_key_with_no_binding_of_its_own_pays_no_latency_either():
    """The recommended way to build a chord: give one key {"action": "none"}
    and it simply stands by while held. Nothing to hold back, nothing to wait
    for."""
    bridge, backend, clock = _chord_bridge({
        "AG00": {"action": "none", "label": "leader"},
        "AG01": {"action": "text", "text": "solo1"},
        "AG00+AG01": {"action": "text", "text": "both", "label": "chord"},
    })
    bridge.handle(key_event("AG00"))
    assert bridge.settle.deadline is None
    assert bridge.handle(key_event("AG01"))[0].input_id == "AG00+AG01"
    assert _typed(backend) == ["both"]


def test_an_unbound_chord_key_reports_as_a_chord_key_not_as_unmapped():
    bridge, _, _ = _chord_bridge({
        "AG01": {"action": "text", "text": "solo1"},
        "AG00+AG01": {"action": "text", "text": "both"},
    })
    assert bridge.handle(key_event("AG00"))[0].describe() == (
        "chord key - held, ready for AG00+AG01"
    )


def test_a_lone_press_fires_its_own_binding_when_the_window_runs_out():
    bridge, backend, clock = _chord_bridge()
    assert bridge.handle(key_event("AG00")) == []
    assert bridge.settle.deadline == 0.045
    clock.advance(0.046)
    assert bridge.settle.step()
    assert _typed(backend) == ["solo0"]


def test_the_deferred_dispatch_reaches_the_caller():
    """A press the user made must appear in the log. Wired to on_dispatch it
    is immediate; unwired it rides out on the next event, which in practice is
    the matching key-up milliseconds later."""
    seen = []
    clock = FakeClock()
    bridge = Bridge(
        parse({"version": 1, "bindings": dict(CHORD_BINDINGS)}),
        RecordingBackend(), clock=clock, autostart=False, on_dispatch=seen.append,
    )
    bridge.handle(key_event("AG00"))
    clock.advance(0.05)
    bridge.settle.step()
    assert [d.input_id for d in seen] == ["AG00"]
    assert bridge.drain() == []

    bridge2, _, clock2 = _chord_bridge()
    bridge2.handle(key_event("AG00"))
    clock2.advance(0.05)
    bridge2.settle.step()
    assert [d.input_id for d in bridge2.handle(key_event("AG02"))] == ["AG00", "AG02"]


def test_a_tap_shorter_than_the_window_still_fires_on_release():
    """Releasing is as good an answer as the clock: no partner came."""
    bridge, backend, clock = _chord_bridge()
    bridge.handle(key_event("AG00"))
    clock.advance(0.01)
    results = bridge.handle(key_event("AG00", act=0))
    assert [r.input_id for r in results] == ["AG00"]
    assert _typed(backend) == ["solo0"]
    assert bridge.settle.deadline is None


def test_a_partner_arriving_after_the_solo_already_fired_is_not_a_chord():
    """Otherwise holding one key and pressing the other a second later would
    run both bindings - the double-action this whole rule exists to prevent."""
    bridge, backend, clock = _chord_bridge()
    bridge.handle(key_event("AG00"))
    clock.advance(0.5)
    bridge.settle.step()
    bridge.handle(key_event("AG01"))
    clock.advance(0.5)
    bridge.settle.step()
    assert _typed(backend) == ["solo0", "solo1"]


def test_settle_ms_zero_removes_the_wait_entirely():
    bridge, backend, _ = _chord_bridge(chords={"settle_ms": 0})
    bridge.handle(key_event("AG00"))
    assert _typed(backend) == ["solo0"]
    assert bridge.settle.deadline is None


def test_settle_ms_is_honoured():
    bridge, _, _ = _chord_bridge(chords={"settle_ms": 200})
    bridge.handle(key_event("AG00"))
    assert bridge.settle.deadline == 0.2


# -- chord releases ---------------------------------------------------------

HOLD_CHORD = {
    "AG00": {"action": "none", "label": "leader"},
    "AG01": {"action": "text", "text": "solo1"},
    "AG00+AG01": {"action": "hold", "key": "ctrl+cmd+o", "label": "chord talk"},
}


def test_a_chord_hold_is_released_by_the_first_key_up_and_only_once():
    """There is no coherent meaning to holding a chord you have half let go
    of, and two key-ups must not produce two key-ups on the real keyboard."""
    bridge, backend, _ = _chord_bridge(HOLD_CHORD)
    bridge.handle(key_event("AG00"))
    bridge.handle(key_event("AG01"))
    assert backend.held == ["ctrl+cmd+o"]
    assert [r.input_id for r in bridge.handle(key_event("AG01", act=0))] == [
        "AG00+AG01"
    ]
    assert backend.held == []
    assert bridge.handle(key_event("AG00", act=0)) == []
    assert backend.calls == [
        ("hold_key", ("ctrl+cmd+o", True)), ("hold_key", ("ctrl+cmd+o", False)),
    ]


def test_closing_mid_chord_hold_lets_go(monkeypatch):
    """Cross-check against quartz.release_all: whatever a chord pressed is in
    the same registry as everything else, so every exit path covers it."""
    bridge, backend, _ = _chord_bridge(HOLD_CHORD)
    bridge.handle(key_event("AG00"))
    bridge.handle(key_event("AG01"))
    bridge.close()
    assert backend.held == []


def test_a_press_still_inside_its_window_is_dropped_by_close_not_flushed():
    """We held it back to find out what the user meant and are shutting down
    before finding out. Typing into whatever is frontmost on the way out is the
    one outcome nobody asked for."""
    bridge, backend, clock = _chord_bridge()
    bridge.handle(key_event("AG00"))
    bridge.close()
    clock.advance(1.0)
    assert bridge.settle.step() is False
    assert _typed(backend) == []


def test_a_reload_drops_every_half_made_decision():
    """A press held back was held back against the OLD file. Resolving it
    against the new one could run a binding the user has just deleted."""
    bridge, backend, clock = _chord_bridge()
    bridge.handle(key_event("AG00"))
    bridge.config = parse({"version": 1, "bindings": {
        "AG00": {"action": "text", "text": "rebound"},
    }})
    clock.advance(1.0)
    assert bridge.settle.step() is False
    assert backend.calls == []
    assert bridge.chord_keys == frozenset()


def test_the_default_config_behaves_exactly_as_it_did_before_chords():
    """No chords configured, no timer, no deferral, no behaviour change."""
    pad = load_default()
    bridge = Bridge(pad, RecordingBackend(), autostart=False)
    assert bridge.chord_keys == frozenset()
    for input_id in ("AG00", "ACT09", "ENC_CLK"):
        assert bridge.handle(key_event(input_id))[0].bound
    assert bridge.settle.deadline is None
    assert not bridge.settle.running


# -- the settle timer itself ------------------------------------------------

def test_the_settle_timer_fires_on_its_own_clock():
    clock = FakeClock()
    fired = []
    timer = SettleTimer(
        lambda: fired.append(clock.now), clock=clock, autostart=False
    )
    timer.schedule(0.05)
    assert timer.step() is False and fired == []
    clock.advance(0.05)
    assert timer.step() is True and fired == [0.05]
    assert timer.deadline is None
    assert timer.step() is False, "it must not fire twice for one deadline"


def test_the_settle_timer_really_runs_and_stops():
    """Its thread is the one thing here that is not driven by the tests, so it
    gets exercised for real - a deferred press that never arrives is a key
    that did nothing."""
    fired = threading.Event()
    timer = SettleTimer(fired.set, idle_seconds=0.01)
    timer.schedule(_monotonic() + 0.01)
    assert fired.wait(2.0), "the settle window never expired"
    timer.stop()
    assert not timer.running
    timer.stop()  # idempotent


def test_a_deferred_press_arrives_without_anyone_driving_the_clock():
    """End to end on the real timer: press one chord-capable key, touch
    nothing else, and the binding still runs."""
    pad = parse({"version": 1, "bindings": dict(CHORD_BINDINGS),
                 "chords": {"settle_ms": 10}})
    seen = threading.Event()
    bridge = Bridge(pad, RecordingBackend(), on_dispatch=lambda d: seen.set())
    try:
        assert bridge.handle(key_event("AG00")) == []
        assert seen.wait(2.0), "the press never fired"
        assert _typed(bridge.backend) == ["solo0"]
    finally:
        bridge.close()


# ---------------------------------------------------------------------------
# Two keys at once, part 3: two focus_session presses together
# ---------------------------------------------------------------------------
#
# Two Agent Keys pressed together are two window activations, and the last one
# wins. That is NOT left to chance and it is deliberately NOT suppressed:
#
#   * It is not a race. Actions are delivered one at a time, whichever thread
#     started them - which used to be free (everything ran on the pad's read
#     thread) and is now enforced, because the settle timer added a second one.
#     The test below proves it.
#   * Sequential is also the right answer: "raise A, then raise B" leaves B in
#     front, which is exactly what pressing B second means. Picking the first
#     instead would ignore a deliberate second press.
#   * Focusing a window neither types nor holds anything, which is why
#     focus_session is in MODIFIER_SAFE_KINDS. Damping the flicker would mean a
#     coalescing window on the one action whose whole job is to be instant, paid
#     by every press, to fix something cosmetic.
#   * And anyone who genuinely wants the two keys to mean one thing can now bind
#     "AG00+AG01", after which the second activation cannot happen at all.

def test_two_actions_are_never_delivered_at_the_same_time():
    """The settle timer runs on its own thread, so "one at a time" stopped
    being free and had to become a guarantee. Two osascript window activations
    overlapping is exactly the case: which one ends up in front is otherwise
    anybody's guess."""
    log = []
    start = threading.Barrier(2)

    class Slow(Backend):
        def activate_app(self, name, cycle=False):
            log.append(f"enter {name}")
            time.sleep(0.05)
            log.append(f"exit {name}")

    pad = parse({"version": 1, "bindings": {
        "AG00": {"action": "app", "name": "A"},
        "AG01": {"action": "app", "name": "B"},
    }})
    bridge = Bridge(pad, Slow(), autostart=False)

    def press(input_id):
        start.wait(timeout=5)
        bridge.fire(input_id, True)

    threads = [
        threading.Thread(target=press, args=("AG00",)),
        threading.Thread(target=press, args=("AG01",)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert len(log) == 4
    assert log[0].startswith("enter") and log[1].startswith("exit")
    assert log[0].split()[1] == log[1].split()[1], f"overlapped: {log}"
    assert log[2].split()[1] == log[3].split()[1], f"overlapped: {log}"


def test_a_lost_key_up_cannot_swallow_the_next_press_release():
    """The pad drops on sleep, on range, on a nudged cable. If half a chord's
    key-ups go missing, the leftover must not eat the release of whatever is
    pressed next - for a `hold` that is a modifier nobody lets go of."""
    bridge, backend, _ = _chord_bridge({
        "AG00": {"action": "hold", "key": "ctrl+cmd+o", "label": "talk"},
        "AG01": {"action": "none"},
        "AG00+AG01": {"action": "text", "text": "both"},
    }, chords={"settle_ms": 0})
    bridge.handle(key_event("AG01"))
    bridge.handle(key_event("AG00"))          # chord fires
    bridge.handle(key_event("AG01", act=0))   # only one key-up arrives
    bridge.handle(key_event("AG00"))          # AG00 pressed again, alone
    assert backend.held == ["ctrl+cmd+o"]
    bridge.handle(key_event("AG00", act=0))
    assert backend.held == [], "the stale chord mark swallowed the release"
