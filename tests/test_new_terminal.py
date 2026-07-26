"""Tests for opening a new terminal on an empty Agent Key.

Pressing an unlit Agent Key (no live project) opens a new terminal window rather
than doing nothing. The state store is isolated and empty under the test
fixtures, so every ``focus_session`` press here resolves to an empty slot - which
is exactly the case under test. A recording backend asserts the AppleScript/app
call without a real terminal ever opening.
"""

from __future__ import annotations

from freemicro import focus
from freemicro.input.actions import RecordingBackend, perform
from freemicro.padconfig import parse


def _press(config_dict, input_id="AG00"):
    pad = parse(config_dict)
    backend = RecordingBackend()
    perform(pad.bindings[input_id], backend)
    return backend


# ---------------------------------------------------------------------------
# focus.py: the scripts and the empty-slot flag
# ---------------------------------------------------------------------------

def test_an_empty_slot_is_recognised():
    plan = focus.plan_for_slot(0, slots=[])
    assert plan.slot_empty


def test_a_filled_slot_is_not_an_empty_slot(monkeypatch):
    # A plan that found a session (even one it cannot focus) is not "empty".
    plan = focus.FocusPlan(method=focus.METHOD_APP, app="Terminal",
                           session=object())  # type: ignore[arg-type]
    assert not plan.slot_empty


def test_terminal_dot_app_gets_a_new_window_verb():
    script = focus.new_terminal_script("Terminal")
    assert 'tell application "Terminal"' in script
    assert 'do script ""' in script


def test_iterm2_gets_its_own_new_window_verb():
    script = focus.new_terminal_script("iTerm2")
    assert "create window with default profile" in script


def test_a_cmd_n_terminal_activates_and_sends_cmd_n():
    script = focus.new_terminal_script("Ghostty")
    assert 'tell application "Ghostty" to activate' in script
    assert 'keystroke "n" using command down' in script


def test_an_unknown_app_degrades_to_activate():
    script = focus.new_terminal_script("SomeFutureTerm")
    assert script == 'tell application "SomeFutureTerm" to activate'


def test_open_new_terminal_is_a_no_op_on_a_blank_app():
    backend = RecordingBackend()
    assert focus.open_new_terminal("", backend) is False
    assert backend.calls == []


# ---------------------------------------------------------------------------
# The focus_session action, end to end (empty store => empty slot)
# ---------------------------------------------------------------------------

def test_an_empty_agent_key_opens_a_new_terminal():
    backend = _press({"version": 1, "bindings": {"AG00": {"action": "focus_session"}}})
    scripts = [args[0] for name, args in backend.calls if name == "run_applescript"]
    assert any('do script ""' in s for s in scripts), backend.calls


def test_the_default_terminal_app_is_terminal_dot_app():
    pad = parse({"version": 1, "bindings": {"AG00": {"action": "focus_session"}}})
    assert pad.terminal_app == "Terminal"
    assert pad.bindings["AG00"].params["terminal"] == "Terminal"


def test_a_configured_terminal_app_is_used():
    backend = _press({
        "version": 1,
        "terminal_app": "iTerm2",
        "bindings": {"AG00": {"action": "focus_session"}},
    })
    scripts = [args[0] for name, args in backend.calls if name == "run_applescript"]
    assert any("create window with default profile" in s for s in scripts)


def test_a_per_binding_terminal_overrides_the_top_level():
    backend = _press({
        "version": 1,
        "terminal_app": "Terminal",
        "bindings": {"AG00": {"action": "focus_session", "terminal": "iTerm2"}},
    })
    scripts = [args[0] for name, args in backend.calls if name == "run_applescript"]
    assert any("create window with default profile" in s for s in scripts)


def test_new_terminal_can_be_disabled_per_key():
    backend = _press({
        "version": 1,
        "bindings": {"AG00": {"action": "focus_session", "new_terminal": False}},
    })
    assert backend.calls == [], "an empty key with new_terminal=false stays inert"


def test_new_terminal_can_be_disabled_globally():
    backend = _press({
        "version": 1,
        "terminal_app": False,
        "bindings": {"AG00": {"action": "focus_session"}},
    })
    assert backend.calls == [], "terminal_app=false leaves every empty key inert"
    pad = parse({"version": 1, "terminal_app": False,
                 "bindings": {"AG00": {"action": "focus_session"}}})
    assert "terminal" not in pad.bindings["AG00"].params


def test_new_terminal_must_be_a_bool():
    try:
        parse({"version": 1,
               "bindings": {"AG00": {"action": "focus_session",
                                     "new_terminal": "yes"}}})
    except Exception as exc:  # PadConfigError
        assert "new_terminal" in str(exc)
    else:
        raise AssertionError("a non-bool new_terminal must be refused")


def test_list_describes_the_new_terminal_behaviour():
    pad = parse({"version": 1, "bindings": {"AG00": {"action": "focus_session"}}})
    summary = pad.bindings["AG00"].describe()
    assert "new Terminal window" in summary
