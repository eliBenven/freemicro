"""The ``agent_keys.keys`` split covers presses, not only lighting.

When FreeMicro owns a strict subset of the six Agent Keys, the un-owned ones are
the vendor app's (Codex's) entirely: a press of one dispatches nothing - no
focus, no new terminal, no bound action - and its release is a clean no-op. The
owned keys behave exactly as before, new-terminal-on-empty included. With the
all-six default, nothing changes.

Every action here goes to a recording backend and the state store is isolated
and empty (see ``conftest``), so a ``focus_session`` press on an owned key
resolves to an empty slot - which is the case that must still open a terminal.
"""

from __future__ import annotations

from freemicro.agentkeys import AgentKeyOwnership
from freemicro.input.actions import RecordingBackend
from freemicro.input.bridge import Bridge
from freemicro.padconfig import parse


def key_event(key, act=1):
    return {"m": "v.oai.hid", "p": {"k": key, "act": act}}


def _bridge(config_dict, ownership=None):
    pad = parse(config_dict)
    backend = RecordingBackend()
    return Bridge(pad, backend, autostart=False, ownership=ownership), backend


def _dynamic_bridge(config_dict, running):
    """A bridge whose split is driven by a live 'is Codex here' flag."""
    pad = parse(config_dict)
    # poll_seconds=0 so each refresh() re-probes; the cadence itself is covered
    # in test_agentkeys.py.
    ownership = AgentKeyOwnership(
        pad.agent_keys, probe=lambda: running["value"], poll_seconds=0.0
    )
    backend = RecordingBackend()
    bridge = Bridge(pad, backend, autostart=False, ownership=ownership)
    return bridge, backend, ownership


def _terminal_opened(backend) -> bool:
    scripts = [a[0] for name, a in backend.calls if name == "run_applescript"]
    return any('do script ""' in s for s in scripts)


# ---------------------------------------------------------------------------
# The bug: an un-owned Agent Key must do nothing
# ---------------------------------------------------------------------------

def test_pressing_an_un_owned_agent_key_dispatches_nothing():
    bridge, backend = _bridge({
        "version": 1,
        "bindings": {"AG00": {"action": "focus_session"}},
        "agent_keys": {"policy": "recent", "keys": [3, 4, 5]},
    })
    # AG00 is Codex's. This is the exact bug: before the fix it saw an empty
    # slot and opened a terminal on top of Codex's own action.
    assert bridge.press("AG00") == []
    assert backend.calls == []
    assert not _terminal_opened(backend)


def test_releasing_an_un_owned_agent_key_is_a_clean_no_op():
    bridge, backend = _bridge({
        "version": 1,
        "bindings": {"AG00": {"action": "focus_session"}},
        "agent_keys": {"policy": "recent", "keys": [3, 4, 5]},
    })
    bridge.press("AG00")
    assert bridge.release("AG00") == []
    assert backend.calls == []


def test_an_explicit_non_focus_binding_on_an_un_owned_key_is_ignored():
    # The rule is "these keys are Codex's": even a shell command bound
    # deliberately to an un-owned key does not fire.
    bridge, backend = _bridge({
        "version": 1,
        "bindings": {"AG00": {"action": "shell", "command": "echo hi"}},
        "agent_keys": {"policy": "recent", "keys": [3, 4, 5]},
    })
    assert bridge.press("AG00") == []
    assert bridge.release("AG00") == []
    assert backend.calls == []


# ---------------------------------------------------------------------------
# The owned keys are untouched: new-terminal is re-enabled for them
# ---------------------------------------------------------------------------

def test_an_owned_empty_key_still_opens_a_new_terminal():
    # The owner no longer needs terminal_app:false to silence the Codex keys, so
    # new-terminal can be back ON and still fire for an owned empty key.
    bridge, backend = _bridge({
        "version": 1,
        "bindings": {"AG03": {"action": "focus_session"}},
        "agent_keys": {"policy": "recent", "keys": [3, 4, 5]},
    })
    dispatches = bridge.press("AG03")
    assert [d.input_id for d in dispatches] == ["AG03"]
    assert _terminal_opened(backend), backend.calls


def test_the_all_six_default_is_unchanged():
    # No agent_keys.keys: every Agent Key acts exactly as before. AG00 opens a
    # terminal on its empty slot just as it always has.
    bridge, backend = _bridge({
        "version": 1,
        "bindings": {"AG00": {"action": "focus_session"}},
    })
    dispatches = bridge.press("AG00")
    assert [d.input_id for d in dispatches] == ["AG00"]
    assert _terminal_opened(backend), backend.calls


# ---------------------------------------------------------------------------
# Composition with chords, layers and profiles
# ---------------------------------------------------------------------------

def test_a_chord_spanning_an_owned_and_an_un_owned_key_cannot_form():
    # AG00 (Codex's) + AG03 (owned) is bound, but AG00's press is ignored, so it
    # never enters the undecided set and the chord can never form. AG03 falls
    # through to its own solo binding instead.
    bridge, backend = _bridge({
        "version": 1,
        "bindings": {
            "AG03": {"action": "key", "key": "escape"},
            "AG00+AG03": {"action": "key", "key": "enter"},
        },
        "agent_keys": {"policy": "recent", "keys": [3, 4, 5]},
    })
    assert bridge.press("AG00") == []          # ignored, no partner registered
    bridge.press("AG03")                       # a chord key, waits out its window
    bridge.release("AG03")                     # tap: fires the solo binding
    bridge.settle.step()
    # The chord's Enter never went; AG03's own Escape did.
    assert ("press_key", ("enter",)) not in backend.calls
    assert ("press_key", ("escape",)) in backend.calls


def test_a_layer_trigger_on_an_un_owned_key_switches_nothing_on():
    bridge, backend = _bridge({
        "version": 1,
        "bindings": {
            "AG00": {"action": "layer", "layer": "fn"},
            "ACT06": {"action": "key", "key": "escape"},
        },
        "layers": {"fn": {"ACT06": {"action": "key", "key": "enter"}}},
        "agent_keys": {"policy": "recent", "keys": [3, 4, 5]},
    })
    assert bridge.press("AG00") == []          # the trigger is Codex's: inert
    bridge.press("ACT06")                       # so the layer is not held
    # ACT06 resolves to its base Escape, not the layer's Enter.
    assert ("press_key", ("escape",)) in backend.calls
    assert ("press_key", ("enter",)) not in backend.calls


def test_a_profile_override_on_an_un_owned_key_never_fires():
    bridge, backend = _bridge({
        "version": 1,
        "bindings": {"AG00": {"action": "key", "key": "escape"}},
        "profiles": {"Terminal": {"AG00": {"action": "key", "key": "enter"}}},
        "agent_keys": {"policy": "recent", "keys": [3, 4, 5]},
    })
    bridge.set_frontmost("Terminal")
    assert bridge.press("AG00") == []
    assert backend.calls == []


# ---------------------------------------------------------------------------
# The split is conditional on the ChatGPT app running (dynamic ownership)
# ---------------------------------------------------------------------------

_SUBSET = {
    "version": 1,
    "bindings": {"AG00": {"action": "key", "key": "escape"}},
    "agent_keys": {"policy": "recent", "keys": [3, 4, 5]},
}


def test_the_split_gates_presses_only_while_codex_is_running():
    running = {"value": True}
    bridge, backend, ownership = _dynamic_bridge(_SUBSET, running)
    ownership.refresh()                         # Codex here: AG00 is Codex's
    assert bridge.press("AG00") == []
    assert bridge.release("AG00") == []
    assert backend.calls == []


def test_an_un_owned_key_comes_back_to_life_when_codex_quits():
    running = {"value": True}
    bridge, backend, ownership = _dynamic_bridge(_SUBSET, running)
    ownership.refresh()
    assert bridge.press("AG00") == []          # inert while Codex is here

    running["value"] = False
    assert ownership.refresh() is True          # Codex quit -> we own all six
    dispatches = bridge.press("AG00")
    assert [d.input_id for d in dispatches] == ["AG00"]
    bridge.release("AG00")
    assert ("press_key", ("escape",)) in backend.calls


def test_an_owned_key_acts_the_same_whether_or_not_codex_is_running():
    for codex in (True, False):
        running = {"value": codex}
        pad = {
            "version": 1,
            "bindings": {"AG03": {"action": "key", "key": "enter"}},
            "agent_keys": {"policy": "recent", "keys": [3, 4, 5]},
        }
        bridge, backend, ownership = _dynamic_bridge(pad, running)
        ownership.refresh()
        assert [d.input_id for d in bridge.press("AG03")] == ["AG03"]
        bridge.release("AG03")
        assert ("press_key", ("enter",)) in backend.calls


def test_a_bridge_without_an_owner_falls_back_to_the_static_subset():
    # No ownership injected (a standalone bridge / the pre-dynamic path): the
    # configured subset applies exactly as it always did.
    bridge, backend = _bridge(_SUBSET)
    assert bridge.press("AG00") == []
    assert backend.calls == []
