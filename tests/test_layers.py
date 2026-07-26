"""Tests for layers: hold a key for a second binding set (a keyboard Fn key).

The whole path a layered press takes, driven with recorded protocol messages
against a recording backend - so nothing here needs hardware or types anything.
The precedence under test throughout is **layer > profile > base**.
"""

from __future__ import annotations

from freemicro.input.actions import RecordingBackend
from freemicro.input.bridge import Bridge
from freemicro.padconfig import PadConfigError, parse


def key_event(key, act=1):
    return {"m": "v.oai.hid", "p": {"k": key, "act": act}}


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, dt: float) -> float:
        self.now += dt
        return self.now


def _bridge(config_dict, **kwargs):
    pad = parse(config_dict)
    backend = RecordingBackend()
    return Bridge(pad, backend, autostart=False, **kwargs), backend


# ---------------------------------------------------------------------------
# Resolution and precedence
# ---------------------------------------------------------------------------

_LAYERED = {
    "version": 1,
    "bindings": {
        "ACT06": {"action": "layer", "layer": "fn"},
        "ENC_CLK": {"action": "mouse", "click": "left"},
        "ACT12": {"action": "key", "key": "return"},
    },
    "layers": {
        "fn": {
            "ENC_CLK": {"action": "text", "text": "/effort", "submit": True},
        }
    },
}


def test_a_key_the_layer_names_resolves_to_the_layer_while_held():
    bridge, backend = _bridge(_LAYERED)
    bridge.handle(key_event("ACT06"))          # hold the layer trigger
    bridge.handle(key_event("ENC_CLK"))
    assert backend.calls[0] == ("type_text", ("/effort",))
    assert backend.calls[-1] == ("press_key", ("return",))


def test_a_key_the_layer_does_not_name_falls_through_to_base():
    bridge, backend = _bridge(_LAYERED)
    bridge.handle(key_event("ACT06"))          # layer held
    bridge.handle(key_event("ACT12"))          # not in the layer
    assert backend.calls == [("press_key", ("return",))]


def test_the_layer_reverts_on_release():
    bridge, backend = _bridge(_LAYERED)
    bridge.handle(key_event("ACT06"))
    bridge.handle(key_event("ACT06", act=0))   # let go of the trigger
    backend.calls.clear()
    bridge.handle(key_event("ENC_CLK"))        # base binding again
    assert backend.calls == [("click_mouse", ("left", 1))]


def test_the_trigger_itself_types_nothing():
    bridge, backend = _bridge(_LAYERED)
    result = bridge.handle(key_event("ACT06"))
    assert backend.calls == []                 # a pure modal switch
    assert result and result[0].bound and result[0].ok


def test_layer_beats_profile_which_beats_base():
    pad = parse({
        "version": 1,
        "bindings": {
            "ACT06": {"action": "layer", "layer": "fn"},
            "ACT12": {"action": "text", "text": "base"},
        },
        "profiles": {"Terminal": {"ACT12": {"action": "text", "text": "profile"}}},
        "layers": {"fn": {"ACT12": {"action": "text", "text": "layer"}}},
    })
    bridge = Bridge(pad, RecordingBackend(), autostart=False)
    backend = bridge.backend
    bridge.set_frontmost("Terminal")

    # Profile active, no layer: profile wins over base.
    bridge.handle(key_event("ACT12"))
    assert backend.calls == [("type_text", ("profile",))]
    backend.calls.clear()

    # Layer held on top of the same profile: layer wins over both.
    bridge.handle(key_event("ACT06"))
    bridge.handle(key_event("ACT12"))
    assert backend.calls == [("type_text", ("layer",))]


def test_the_most_recently_activated_layer_wins():
    pad = parse({
        "version": 1,
        "bindings": {
            "ACT06": {"action": "layer", "layer": "a"},
            "ACT07": {"action": "layer", "layer": "b"},
            "ACT12": {"action": "text", "text": "base"},
        },
        "layers": {
            "a": {"ACT12": {"action": "text", "text": "a"}},
            "b": {"ACT12": {"action": "text", "text": "b"}},
        },
    })
    bridge = Bridge(pad, RecordingBackend(), autostart=False)
    backend = bridge.backend
    bridge.handle(key_event("ACT06"))          # layer a
    bridge.handle(key_event("ACT07"))          # layer b on top
    bridge.handle(key_event("ACT12"))
    assert backend.calls == [("type_text", ("b",))]


# ---------------------------------------------------------------------------
# Composition with holds and chords
# ---------------------------------------------------------------------------

def test_a_hold_reached_through_a_layer_releases_correctly():
    """A hold defined in a layer must press on the way down and release on the
    way up - even when the layer is let go first, because the delivered action
    is latched on press and replayed on release (the profile path's guarantee).
    """
    pad = parse({
        "version": 1,
        "bindings": {
            "ACT06": {"action": "layer", "layer": "fn"},
            "ACT12": {"action": "text", "text": "base"},
        },
        "layers": {
            "fn": {"ACT12": {"action": "hold", "key": "ctrl+cmd+o"}},
        },
    })
    bridge = Bridge(pad, RecordingBackend(), autostart=False)
    backend = bridge.backend
    bridge.handle(key_event("ACT06"))          # hold the layer
    bridge.handle(key_event("ACT12"))          # a hold, reached through it
    assert ("hold_key", ("ctrl+cmd+o", True)) in backend.calls
    bridge.handle(key_event("ACT06", act=0))   # let go of the LAYER first
    bridge.handle(key_event("ACT12", act=0))   # then the hold key
    assert ("hold_key", ("ctrl+cmd+o", False)) in backend.calls
    assert backend.held == []                   # nothing left down


def test_a_chord_stays_resolved_globally_under_a_layer():
    pad = parse({
        "version": 1,
        "bindings": {
            "ACT06": {"action": "layer", "layer": "fn"},
            "ACT07": {"action": "text", "text": "base7"},
            "ACT08": {"action": "none"},
            "ACT07+ACT08": {"action": "text", "text": "chord"},
        },
        "layers": {"fn": {"ACT07": {"action": "text", "text": "layer7"}}},
    })
    bridge = Bridge(pad, RecordingBackend(), autostart=False)
    backend = bridge.backend
    bridge.handle(key_event("ACT06"))          # layer held
    bridge.handle(key_event("ACT07"))          # chord member
    bridge.handle(key_event("ACT08"))          # completes the chord
    # The chord fires, not the layer's ACT07 override: chords are global.
    assert ("type_text", ("chord",)) in backend.calls
    assert ("type_text", ("layer7",)) not in backend.calls


# ---------------------------------------------------------------------------
# Lost-release safety (reused, not reinvented)
# ---------------------------------------------------------------------------

def test_a_repeated_keydown_reconciles_a_lost_layer_release():
    bridge, backend = _bridge(_LAYERED)
    bridge.handle(key_event("ACT06"))          # layer on
    assert list(bridge._layers) == ["ACT06"]
    results = bridge.handle(key_event("ACT06"))  # a second down, no up between
    # The lost release is recovered and logged as a stuck release...
    recovered = bridge.drain() + [r for r in results if r.stuck_release]
    assert any(r.stuck_release for r in recovered)
    # ...and the layer is freshly re-established (still exactly one).
    assert list(bridge._layers) == ["ACT06"]


def test_the_max_hold_cap_releases_a_layer_whose_key_up_never_came():
    clock = FakeClock()
    pad = parse(_LAYERED)
    bridge = Bridge(pad, RecordingBackend(), autostart=False,
                    clock=clock, max_hold_seconds=120.0)
    bridge.handle(key_event("ACT06"))
    assert list(bridge._layers) == ["ACT06"]
    clock.advance(121.0)                        # past the cap
    bridge.hold_timer.step()                    # the timer fires
    assert list(bridge._layers) == []           # the stuck layer is let go
    assert any(d.stuck_release for d in bridge.drain())


def test_close_and_reload_clear_a_held_layer():
    bridge, backend = _bridge(_LAYERED)
    bridge.handle(key_event("ACT06"))
    assert bridge._layers
    bridge.config = parse(_LAYERED)             # a reload drops undecided state
    assert bridge._layers == {}


# ---------------------------------------------------------------------------
# A layer trigger may carry a light
# ---------------------------------------------------------------------------

def test_a_layer_trigger_light_is_on_while_held():
    lights = []
    pad = parse({
        "version": 1,
        "bindings": {
            "ACT06": {"action": "layer", "layer": "fn",
                      "light": {"color": "#2E8B57", "effect": "solid"}},
        },
        "layers": {"fn": {}},
    })
    bridge = Bridge(pad, RecordingBackend(), autostart=False,
                    on_activity=lambda iid, light: lights.append((iid, light)))
    bridge.handle(key_event("ACT06"))
    assert lights[-1][0] == "ACT06" and lights[-1][1] is not None
    bridge.handle(key_event("ACT06", act=0))
    assert lights[-1] == ("ACT06", None)        # retired on release
    # A layer light does not earn the "use hold for dictation" warning.
    assert not any("dictation" in w for w in pad.warnings)


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------

def test_a_layer_may_not_bind_a_chord():
    try:
        parse({"version": 1, "bindings": {},
               "layers": {"fn": {"AG00+AG01": {"action": "none"}}}})
    except PadConfigError as exc:
        assert "chord" in str(exc)
    else:
        raise AssertionError("a chord in a layer must be refused")


def test_a_trigger_naming_an_undefined_layer_warns():
    pad = parse({"version": 1,
                 "bindings": {"ACT06": {"action": "layer", "layer": "ghost"}}})
    assert any("ghost" in w for w in pad.warnings)


def test_a_layer_trigger_needs_a_layer_name():
    try:
        parse({"version": 1, "bindings": {"ACT06": {"action": "layer"}}})
    except PadConfigError as exc:
        assert "layer" in str(exc)
    else:
        raise AssertionError("a layer trigger with no name must be refused")
