"""One mic key, two dictation shortcuts, and the lost-release backstops.

The ``double_tap`` option keeps a ``hold`` a true physical push-to-talk *and*
fires a second, different shortcut on a double-tap. Proven three ways, all
without hardware:

* :class:`freemicro.input.latch.DoubleTapMachine` on its own, transition by
  transition from injected timestamps.
* the bridge wiring it beside the physical hold: the second shortcut goes out
  with clean modifiers, the hold is never delayed, and the light stays tied to
  the hold.
* the two backstops a stuck physical hold needs - a repeated key-down reconcile
  and a max-hold cap - because a double-tap's brief first-tap hold must compose
  with them rather than trip them.
"""

from __future__ import annotations

import pytest

from freemicro.input import latch as latchmod
from freemicro.input.actions import (
    Action,
    ActionError,
    RecordingBackend,
    double_tap_combo,
)
from freemicro.input.bridge import DEFAULT_MAX_HOLD_SECONDS, Bridge
from freemicro.input.latch import ARMED, FIRE, DoubleTapMachine
from freemicro.padconfig import DEFAULT_ACTIVITY_TIMEOUT, PadConfigError, parse

W = latchmod.LATCH_WINDOW_SECONDS


# ---------------------------------------------------------------------------
# The machine on its own
# ---------------------------------------------------------------------------

def test_a_first_tap_opens_a_window_a_second_press_fires():
    m = DoubleTapMachine()
    assert m.press(0.0) == []
    assert m.release(0.1) == []              # quick tap -> waiting
    assert m.state == latchmod.WAITING
    assert m.deadline == pytest.approx(0.1 + W)
    assert m.press(0.2) == [FIRE]            # second press inside the window
    assert m.state == ARMED


def test_it_fires_on_the_second_press_not_its_release():
    m = DoubleTapMachine()
    m.press(0.0)
    m.release(0.1)
    assert m.press(0.2) == [FIRE]            # the emit is here, on the press…
    assert m.release(0.25) == []             # …never on the release
    assert m.state == latchmod.IDLE


def test_a_second_press_after_the_window_is_a_fresh_first_tap():
    m = DoubleTapMachine()
    m.press(0.0)
    m.release(0.1)
    assert m.press(0.1 + W + 0.01) == []     # too late: no fire, fresh pair
    assert m.state == latchmod.PRESSED


def test_a_long_hold_is_not_a_tap_and_opens_no_window():
    m = DoubleTapMachine()
    m.press(0.0)
    assert m.release(W) == []                # held past the window: a real hold
    assert m.state == latchmod.IDLE
    assert m.deadline is None


def test_a_triple_tap_fires_exactly_once():
    m = DoubleTapMachine()
    fires = 0
    for i in range(3):
        t = i * 0.15
        fires += m.press(t).count(FIRE)
        m.release(t + 0.05)
    assert fires == 1                        # the pair 1-2; tap 3 starts anew


def test_a_quadruple_tap_fires_twice_on_then_off():
    m = DoubleTapMachine()
    fires = 0
    for i in range(4):
        t = i * 0.15
        fires += m.press(t).count(FIRE)
        m.release(t + 0.05)
    assert fires == 2                        # pairs 1-2 and 3-4: a clean toggle


def test_tick_lapses_a_waiting_window_but_the_bridge_relies_on_press():
    m = DoubleTapMachine()
    m.press(0.0)
    m.release(0.1)
    assert m.tick(0.1 + W) == []             # never emits
    assert m.state == latchmod.IDLE


def test_double_tap_combo_reads_only_a_hold_that_has_one():
    assert double_tap_combo(
        Action(kind="hold", params={"key": "f5", "double_tap": "ctrl+cmd+u"})
    ) == "ctrl+cmd+u"
    assert double_tap_combo(Action(kind="hold", params={"key": "f5"})) is None
    assert double_tap_combo(Action(kind="key", params={"key": "f5"})) is None
    assert double_tap_combo(None) is None


# ---------------------------------------------------------------------------
# The bridge, wiring the machine beside the physical hold
# ---------------------------------------------------------------------------

class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> float:
        self.now += seconds
        return self.now


DT_BINDING = {
    "action": "hold", "key": "ctrl+cmd+o", "double_tap": "ctrl+cmd+u",
    "label": "mic",
}


def _bridge(binding=None, extra=None, backend=None, **kw):
    bindings = {"ACT10": dict(binding or DT_BINDING)}
    if extra:
        bindings.update(extra)
    clock = Clock()
    seen: list = []
    bridge = Bridge(
        parse({"version": 1, "bindings": bindings}),
        backend or RecordingBackend(), clock=clock, autostart=False,
        on_activity=lambda i, light: seen.append((i, light is not None)),
        **kw,
    )
    return bridge, bridge.backend, clock, seen


def _taps(backend):
    return [args[0] for name, args in backend.calls if name == "press_key"]


def _tap(bridge, clock, hold=0.05, gap=0.1):
    bridge.press("ACT10")
    clock.advance(hold)
    bridge.release("ACT10")
    clock.advance(gap)


def test_a_double_tap_fires_the_second_shortcut_before_the_hold():
    """The second combo goes out with the key already up - clean modifiers - and
    the physical hold still happens on every press."""
    bridge, be, clock, seen = _bridge()
    bridge.press("ACT10")
    clock.advance(0.1)
    bridge.release("ACT10")
    clock.advance(0.1)
    bridge.press("ACT10")            # the double-tap's second press
    # The tap of the second shortcut is posted *before* this press's hold-down.
    assert be.calls == [
        ("hold_key", ("ctrl+cmd+o", True)),
        ("hold_key", ("ctrl+cmd+o", False)),
        ("press_key", ("ctrl+cmd+u",)),          # fired, key up, modifiers clean
        ("hold_key", ("ctrl+cmd+o", True)),      # then push-to-talk holds again
    ]


def test_a_single_hold_never_fires_the_second_shortcut():
    bridge, be, clock, seen = _bridge()
    bridge.press("ACT10")
    clock.advance(0.5)               # a real push-to-talk hold
    bridge.release("ACT10")
    assert _taps(be) == []
    assert be.calls == [
        ("hold_key", ("ctrl+cmd+o", True)),
        ("hold_key", ("ctrl+cmd+o", False)),
    ]


def test_two_slow_separate_taps_are_not_a_double_tap():
    bridge, be, clock, seen = _bridge()
    bridge.press("ACT10")
    clock.advance(0.05)
    bridge.release("ACT10")
    clock.advance(W + 0.1)           # gap wider than the window
    bridge.press("ACT10")
    clock.advance(0.05)
    bridge.release("ACT10")
    assert _taps(be) == []


def test_a_triple_tap_through_the_bridge_fires_once():
    bridge, be, clock, seen = _bridge()
    for _ in range(3):
        _tap(bridge, clock)
    assert _taps(be) == ["ctrl+cmd+u"]


def test_the_second_shortcut_is_refused_while_another_key_holds_modifiers():
    """Some other key physically holds modifiers, so the tap would bleed into a
    shortcut - suppressed like any keystroke, and the machine never advances."""
    bridge, be, clock, seen = _bridge(
        extra={"ACT09": {"action": "hold", "key": "ctrl+shift+a"}}
    )
    bridge.press("ACT09")                        # holds real modifiers
    bridge.press("ACT10")
    clock.advance(0.05)
    bridge.release("ACT10")
    clock.advance(0.05)
    result = bridge.press("ACT10")               # would be the double-tap
    assert result[0].suppressed
    assert _taps(be) == []                        # nothing bled out


def test_the_double_tap_does_not_light_the_pad_the_hold_does():
    """The light tracks the physical hold only. A double-tap toggles an app whose
    state FreeMicro cannot see, so it drives no light of its own."""
    bridge, be, clock, seen = _bridge(
        binding={**DT_BINDING, "light": {"color": "#2E8B57"}}
    )
    bridge.press("ACT10")
    clock.advance(0.1)
    bridge.release("ACT10")
    clock.advance(0.1)
    seen.clear()
    bridge.press("ACT10")                        # the double-tap fires here
    # The light change reported is the hold going down, nothing extra for the tap.
    assert seen == [("ACT10", True)]
    bridge.release("ACT10")
    assert seen[-1] == ("ACT10", False)


def test_a_failed_second_tap_does_not_derail_the_hold():
    class Broken(RecordingBackend):
        def press_key(self, combo):
            raise ActionError("nope")

    bridge, be, clock, seen = _bridge(backend=Broken())
    bridge.press("ACT10")
    clock.advance(0.1)
    bridge.release("ACT10")
    clock.advance(0.1)
    bridge.press("ACT10")                        # tap raises, hold must still run
    assert ("hold_key", ("ctrl+cmd+o", True)) in be.calls[-1:]


# ---------------------------------------------------------------------------
# Load-time refusals and surfacing
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("input_id", ["ENC_CW", "ENC_CC", "JOY_UP", "JOY_LEFT"])
def test_an_input_with_no_release_cannot_double_tap(input_id):
    with pytest.raises(PadConfigError) as exc:
        parse({"version": 1, "bindings": {
            input_id: {"action": "hold", "key": "f5", "double_tap": "f6"},
        }})
    assert "double-tap" in str(exc.value).lower()


def test_double_tap_and_latch_cannot_be_combined():
    with pytest.raises(PadConfigError) as exc:
        parse({"version": 1, "bindings": {
            "ACT10": {
                "action": "hold", "key": "f5", "latch": True, "double_tap": "f6",
            },
        }})
    assert "cannot be combined" in str(exc.value)


def test_the_double_tap_combo_is_validated_at_load_time():
    with pytest.raises(PadConfigError):
        parse({"version": 1, "bindings": {
            "ACT10": {"action": "hold", "key": "f5", "double_tap": "notakey"},
        }})


def test_keys_list_describes_the_double_tap():
    cfg = parse({"version": 1, "bindings": {
        "ACT10": {"action": "hold", "key": "ctrl+cmd+o", "double_tap": "ctrl+cmd+u"},
    }})
    described = cfg.action_for("ACT10").describe()
    assert "double-tap" in described and "ctrl+cmd+u" in described


def test_the_schema_offers_double_tap_on_the_hold_action():
    from freemicro.webui.api import Api

    api = Api.__new__(Api)
    _, body = Api.schema(api)
    hold = next(a for a in body["actions"] if a["kind"] == "hold")
    assert "double_tap" in hold["optional"]
    field = next(f for f in hold["fields"] if f["name"] == "double_tap")
    assert field["widget"] == "combo"


# ---------------------------------------------------------------------------
# Backstop 1: a repeated key-down reconciles a lost release
# ---------------------------------------------------------------------------

def test_a_lost_mic_release_no_longer_strands_the_hold():
    """The bug the owner hit: a hold whose key-up is lost leaves ctrl+cmd down.
    The next key-down is proof the up was dropped, so it recovers at once."""
    bridge = Bridge(
        parse({"version": 1, "bindings": {
            "ACT10": {"action": "hold", "key": "ctrl+cmd+o"},
        }}),
        RecordingBackend(), autostart=False, on_dispatch=None,
    )
    bridge.press("ACT10")                        # holds ctrl+cmd+o
    assert bridge.backend.held == ["ctrl+cmd+o"]
    # ... the release event is lost (a Bluetooth blip). The key comes down again.
    bridge.press("ACT10")
    # The stale hold was let go before the new press re-held it: not stranded.
    assert bridge.backend.held == ["ctrl+cmd+o"]  # exactly one hold, the new one
    log = [d for d in bridge.drain() if d.stuck_release]
    assert log and "key-up was lost" in log[0].describe()


def test_the_reconcile_is_silent_for_a_normally_held_key():
    """A key that is not currently held - an ordinary press, and the second press
    of a clean double-tap whose first release *did* arrive - never trips it."""
    bridge, be, clock, seen = _bridge()
    bridge.press("ACT10")
    clock.advance(0.1)
    bridge.release("ACT10")                       # release arrives normally
    clock.advance(0.1)
    bridge.press("ACT10")                         # a real double-tap, not a strand
    assert [d for d in bridge.drain() if d.stuck_release] == []
    assert _taps(be) == ["ctrl+cmd+u"]            # it fired the double-tap, no strand log


def test_a_lost_first_tap_release_recovers_and_fires_no_double_tap():
    """If the first tap's release is lost, the second press is a reconcile, not a
    double-tap: the stuck hold is recovered and the toggle does not fire."""
    bridge, be, clock, seen = _bridge()
    bridge.press("ACT10")
    clock.advance(0.1)
    # release of the first tap is lost.
    bridge.press("ACT10")
    assert [d for d in bridge.drain() if d.stuck_release]     # recovered
    assert _taps(be) == []                                    # no false toggle


# ---------------------------------------------------------------------------
# Backstop 2: the max-hold cap
# ---------------------------------------------------------------------------

def test_the_cap_defaults_to_the_activity_light_timeout():
    assert DEFAULT_MAX_HOLD_SECONDS == DEFAULT_ACTIVITY_TIMEOUT == 120.0


def test_a_hold_past_the_cap_is_released_and_logged():
    bridge, be, clock, seen = _bridge(
        binding={"action": "hold", "key": "ctrl+cmd+o", "light": {"color": "#2E8B57"}},
        max_hold_seconds=10.0,
    )
    bridge.press("ACT10")
    assert be.held == ["ctrl+cmd+o"]
    assert bridge.hold_timer.deadline == pytest.approx(10.0)
    clock.advance(9.0)
    assert not bridge.hold_timer.step()          # still under the cap
    assert be.held == ["ctrl+cmd+o"]
    clock.advance(2.0)
    assert bridge.hold_timer.step()              # past the cap now
    assert be.held == []                         # let go physically
    assert "ACT10" not in bridge._holding        # and in the bridge's own state
    assert seen[-1] == ("ACT10", False)          # light down with it
    log = [d for d in bridge.drain() if d.stuck_release]
    assert log and "key-up was lost" in log[0].describe()


def test_the_cap_does_not_cut_a_hold_that_is_released_in_time():
    bridge, be, clock, seen = _bridge(
        binding={"action": "hold", "key": "ctrl+cmd+o"}, max_hold_seconds=10.0,
    )
    bridge.press("ACT10")
    clock.advance(5.0)                           # a long but real dictation hold
    bridge.release("ACT10")
    assert be.held == []
    clock.advance(20.0)
    assert not bridge.hold_timer.step()          # nothing left to cap
    assert bridge.drain() == []                  # and nothing logged


def test_a_double_taps_brief_first_hold_never_trips_the_cap():
    """The first tap of a double-tap holds ctrl+cmd+o for well under the cap, so
    the cap must never fire during the gesture."""
    bridge, be, clock, seen = _bridge(max_hold_seconds=10.0)
    _tap(bridge, clock)                          # first tap: sub-window hold
    assert not bridge.hold_timer.step()
    bridge.press("ACT10")                        # the double-tap fires
    assert _taps(be) == ["ctrl+cmd+u"]
    assert not bridge.hold_timer.step()          # still no spurious cap
