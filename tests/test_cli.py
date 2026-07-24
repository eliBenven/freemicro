"""The CLI's job of making invisible state visible.

Two things are asserted here, both of which cost real hours before they
existed:

* ``status`` and ``doctor`` say - first, and in plain words - whether anything
  is driving the pad, and whether what *is* running predates the code or the
  config it loaded.
* a session that has only just opened shows up as idle, so a fresh Claude Code
  window claims its Agent Key immediately instead of leaving a key dark, which
  is indistinguishable from a broken one.

No hardware, no ``ps``, no ``launchctl``: the process table is injected as
rows and the hook installation is stubbed, so the output asserted here is the
output a user would see.
"""

from __future__ import annotations

import io
import json
from argparse import Namespace

import pytest

from freemicro import cli, staleness
from freemicro.config import config_home
from freemicro.state.engine import AgentState, StateStore


@pytest.fixture(autouse=True)
def quiet_process_table(monkeypatch):
    """Nothing in this file may shell out to a real ``ps``."""
    monkeypatch.setattr(staleness, "process_rows", lambda *a, **k: ())
    monkeypatch.setattr(
        "freemicro.hooks_install.status",
        lambda *a, **k: {
            "events": list(range(7)),
            "missing_events": [],
            "installed": True,
            "partial": False,
        },
    )


def _rows(monkeypatch, *entries):
    monkeypatch.setattr(staleness, "process_rows", lambda *a, **k: tuple(entries))


# ---------------------------------------------------------------------------
# status: is anything listening?
# ---------------------------------------------------------------------------

def test_status_leads_with_the_pad_being_inert(capsys):
    # The line that would have ended three separate evenings: `status` used to
    # list live sessions and never mention that nobody was switching the light.
    assert cli.cmd_status(Namespace(json=False)) == 0
    out = capsys.readouterr().out
    first = out.strip().splitlines()[0]
    assert first == "No bridge running - the pad is inert."
    assert "freemicro run" in out
    assert "freemicro daemon install" in out


def test_status_still_reports_state_underneath_the_warning(capsys):
    cli.cmd_status(Namespace(json=False))
    out = capsys.readouterr().out
    assert "Resolved state: idle" in out
    assert "No live sessions." in out


def test_status_names_the_bridge_when_there_is_one(monkeypatch, capsys):
    _rows(monkeypatch, (4242, 30.0, "/usr/local/bin/freemicro run"))
    cli.cmd_status(Namespace(json=False))
    out = capsys.readouterr().out
    assert "Pad driven by `freemicro run` (pid 4242)" in out
    assert "inert" not in out


def test_status_warns_that_a_running_bridge_predates_the_code(monkeypatch, capsys):
    _rows(monkeypatch, (77, 5.0, "/usr/local/bin/freemicro daemon run"))
    monkeypatch.setattr(staleness, "package_mtime", lambda *a, **k: 2_000_000_000.0)
    cli.cmd_status(Namespace(json=False))
    out = capsys.readouterr().out
    assert "started before the code it loaded changed" in out
    assert "Fix: freemicro daemon install" in out


def test_status_json_carries_the_bridge_for_anything_scripting_it(capsys):
    cli.cmd_status(Namespace(json=True))
    payload = json.loads(capsys.readouterr().out)
    assert payload["bridge"]["listener"]["listening"] is False
    assert payload["resolved"] == "idle"


def test_status_notices_hooks_this_version_expects_and_claude_is_not_firing(
    monkeypatch, capsys
):
    monkeypatch.setattr(
        "freemicro.hooks_install.status",
        lambda *a, **k: {
            "events": ["Stop"],
            "missing_events": ["SessionStart"],
            "installed": False,
            "partial": True,
        },
    )
    cli.cmd_status(Namespace(json=False))
    out = capsys.readouterr().out
    assert "SessionStart" in out
    assert "freemicro install" in out


def test_status_says_so_when_the_hooks_were_never_installed(monkeypatch, capsys):
    monkeypatch.setattr(
        "freemicro.hooks_install.status",
        lambda *a, **k: {
            "events": [], "missing_events": [], "installed": False, "partial": False,
        },
    )
    cli.cmd_status(Namespace(json=False))
    assert "freemicro install" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# doctor: "is anything listening" is its own check
# ---------------------------------------------------------------------------

class _Checks:
    """Stands in for doctor's own `check`, which prints and counts failures."""

    def __init__(self) -> None:
        self.calls = []

    def __call__(self, good, label, detail="", fix=""):
        self.calls.append((bool(good), label, detail, fix))
        return bool(good)


def test_doctor_fails_when_the_pad_is_present_but_unlistened(capsys):
    checks = _Checks()
    cli._doctor_liveness(checks, staleness.report(rows=(), holder=lambda: None))
    good, label, _detail, fix = checks.calls[0]
    # A present-but-unlistened pad is the most common broken state there is,
    # and it used to read as all-green.
    assert good is False
    assert "driving the pad" in label
    assert "freemicro run" in fix


def test_doctor_passes_and_names_the_owner_when_one_exists(capsys):
    checks = _Checks()
    live = staleness.report(rows=(), holder=lambda: {"role": "daemon", "pid": 9})
    cli._doctor_liveness(checks, live)
    good, _label, detail, _fix = checks.calls[0]
    assert good is True and "background daemon" in detail


def test_doctor_warns_about_stale_processes_without_failing_on_them(capsys):
    checks = _Checks()
    live = staleness.report(
        rows=((88, 5.0, "freemicro run"),),
        holder=lambda: None,
        mtime=2_000_000_000.0,
    )
    cli._doctor_liveness(checks, live)
    out = capsys.readouterr().out
    assert "[warn]" in out and "Ctrl-C" in out
    # Only the liveness question is a pass/fail; being stale is a visibility
    # failure, not a broken machine.
    assert len(checks.calls) == 1


# ---------------------------------------------------------------------------
# Reloading the config in place
# ---------------------------------------------------------------------------

def _pad(**changes):
    from freemicro import padconfig

    base = padconfig.load()
    return type(base)(
        bindings=changes.get("bindings", base.bindings),
        joystick=changes.get("joystick", base.joystick),
        lighting=changes.get("lighting", base.lighting),
        agent_keys=changes.get("agent_keys", base.agent_keys),
        source=base.source,
    )


def test_a_reload_says_how_many_bindings_changed():
    from freemicro.input.actions import Action

    old = _pad()
    bindings = dict(old.bindings)
    first = sorted(bindings)[0]
    bindings[first] = Action(kind="text", params={"text": "hello"})
    assert cli._describe_config_changes(old, _pad(bindings=bindings)) == (
        "1 binding changed"
    )


def test_a_reload_reports_the_led_switch_in_words_a_user_recognises():
    old = _pad()
    lighting = type(old.lighting)(enabled=not old.lighting.enabled)
    summary = cli._describe_config_changes(old, _pad(lighting=lighting))
    assert summary in ("LEDs on", "LEDs off")


def test_a_reload_that_changed_nothing_functional_says_exactly_that():
    assert cli._describe_config_changes(_pad(), _pad()) == "no functional changes"


# ---------------------------------------------------------------------------
# A session that has only just opened
# ---------------------------------------------------------------------------

def _fire(monkeypatch, event: dict) -> int:
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(event)))
    return cli.cmd_hook(Namespace())


def _sessions():
    return {s.session_id: s for s in StateStore(directory=config_home() / "state")
            .sessions()}


def test_a_freshly_opened_session_is_live_and_idle_before_you_type(monkeypatch):
    # Without this the Agent Key stays dark until the first prompt, and a dark
    # key is indistinguishable from a broken one.
    _fire(monkeypatch, {"hook_event_name": "SessionStart", "session_id": "s1",
                        "cwd": "/tmp/project"})
    live = _sessions()
    assert "s1" in live
    assert live["s1"].state == AgentState.IDLE


def test_closing_the_tab_removes_the_session(monkeypatch):
    _fire(monkeypatch, {"hook_event_name": "SessionStart", "session_id": "s2"})
    _fire(monkeypatch, {"hook_event_name": "SessionEnd", "session_id": "s2",
                        "reason": "exit"})
    assert "s2" not in _sessions()


def test_clearing_a_session_keeps_its_slot(monkeypatch):
    # `/clear` arrives as SessionEnd too. Dropping the record would take the
    # project off its Agent Key until you happened to type into it again.
    _fire(monkeypatch, {"hook_event_name": "SessionStart", "session_id": "s3"})
    _fire(monkeypatch, {"hook_event_name": "SessionEnd", "session_id": "s3",
                        "reason": "clear"})
    live = _sessions()
    assert "s3" in live and live["s3"].state == AgentState.IDLE


def test_a_session_that_starts_then_works_still_goes_to_working(monkeypatch):
    _fire(monkeypatch, {"hook_event_name": "SessionStart", "session_id": "s4"})
    _fire(monkeypatch, {"hook_event_name": "UserPromptSubmit", "session_id": "s4"})
    assert _sessions()["s4"].state == AgentState.WORKING


# ---------------------------------------------------------------------------
# What `keys --list` tells you
# ---------------------------------------------------------------------------

def _parsed(**doc):
    from freemicro import padconfig

    document = {"version": 1, "bindings": {"AG00": {"action": "focus_session"}}}
    document.update(doc)
    return padconfig.parse(document)


def test_the_listing_shows_the_colour_the_pad_will_actually_use(capsys):
    """A state nobody configured still has a colour; say which."""
    from freemicro.padconfig import FACTORY_PALETTE
    from freemicro.state.engine import AgentState

    cli._print_keymap(_parsed())
    out = capsys.readouterr().out
    assert "(default palette)" not in out
    for state in AgentState:
        assert FACTORY_PALETTE[state] in out


def test_the_listing_says_when_the_pad_will_blank_itself(capsys):
    # A pad that has auto-dimmed looks exactly like a pad that has stopped
    # working, so the number has to be visible somewhere.
    cli._print_keymap(_parsed(lighting={"auto_dim_seconds": 180}))
    out = capsys.readouterr().out
    assert "auto-dim: 180s" in out
    assert "waiting and error stay lit" in out


def test_the_listing_says_when_auto_dim_is_switched_off(capsys):
    cli._print_keymap(_parsed(lighting={"auto_dim_seconds": 0}))
    assert "auto-dim: off" in capsys.readouterr().out


def test_the_listing_says_when_alerts_dim_too(capsys):
    cli._print_keymap(
        _parsed(lighting={"auto_dim_seconds": 90, "auto_dim_alerts": True})
    )
    assert "alerts dim too" in capsys.readouterr().out


def test_a_chord_you_bound_appears_in_the_listing(capsys):
    """Chords live outside `bindings`; a listing that walks only that shows
    nothing, and "it did not save" is the wrong conclusion to invite."""
    cli._print_keymap(_parsed(bindings={
        "AG00": {"action": "none"},
        "AG01": {"action": "none"},
        "AG00+AG01": {"action": "key", "key": "escape", "label": "panic"},
    }))
    out = capsys.readouterr().out
    assert "Chords" in out
    assert "AG00+AG01" in out
    assert "panic" in out


def test_the_listing_explains_which_keys_pay_the_settle_window(capsys):
    cli._print_keymap(_parsed(bindings={
        "AG00": {"action": "key", "key": "a"},
        "AG01": {"action": "none"},
        "AG00+AG01": {"action": "key", "key": "escape"},
    }))
    out = capsys.readouterr().out
    assert "AG00 wait up to 45ms" in out
    assert "AG01 wait" not in out          # unbound alone, so it waits for nothing


def test_a_chord_of_two_unbound_keys_is_stated_to_cost_nothing(capsys):
    cli._print_keymap(_parsed(bindings={
        "AG00": {"action": "none"},
        "AG01": {"action": "none"},
        "AG00+AG01": {"action": "key", "key": "escape"},
    }))
    assert "add no delay at all" in capsys.readouterr().out


def test_a_config_with_no_chords_says_nothing_about_them(capsys):
    cli._print_keymap(_parsed())
    assert "Chords" not in capsys.readouterr().out


def test_a_reload_reports_a_changed_chord():
    old = _parsed(bindings={"AG00": {"action": "none"}, "AG01": {"action": "none"}})
    new = _parsed(bindings={
        "AG00": {"action": "none"},
        "AG01": {"action": "none"},
        "AG00+AG01": {"action": "key", "key": "escape"},
    })
    assert "1 chord changed" in cli._describe_config_changes(old, new)


def test_a_reload_reports_a_retuned_settle_window():
    old = _parsed()
    new = _parsed(chords={"settle_ms": 90})
    assert "chord settle 90ms" in cli._describe_config_changes(old, new)


# ---------------------------------------------------------------------------
# The key readout
# ---------------------------------------------------------------------------

def test_a_chord_member_with_no_solo_binding_is_not_called_unmapped(capsys):
    from freemicro.input.bridge import Dispatch

    cli._print_dispatch(Dispatch(input_id="AG01", chord="AG00+AG01"))
    out = capsys.readouterr().out
    assert "unmapped" not in out
    assert "ready for AG00+AG01" in out


def test_a_genuinely_unbound_key_still_reads_as_unmapped(capsys):
    from freemicro.input.bridge import Dispatch

    cli._print_dispatch(Dispatch(input_id="ACT11"))
    assert "unmapped" in capsys.readouterr().out


def test_a_deferred_press_prints_the_moment_it_fires(capsys):
    """The readout has to name what the pad just did, at the moment it did it.

    Unwired, a press held back inside the chord settle window prints when the
    *next* event arrives - in practice its own key-up - which during chord
    tuning reads as the wrong key firing.
    """
    from freemicro.input.actions import RecordingBackend
    from freemicro.input.bridge import Bridge

    class _Clock:
        def __init__(self):
            self.now = 0.0

        def __call__(self):
            return self.now

    clock = _Clock()
    pad = _parsed(bindings={
        "AG00": {"action": "key", "key": "a"},
        "AG01": {"action": "none"},
        "AG00+AG01": {"action": "key", "key": "escape"},
    })
    bridge = Bridge(
        pad, RecordingBackend(), clock=clock, autostart=False,
        on_dispatch=cli._print_dispatch,
    )
    try:
        bridge.handle({"m": "v.oai.hid", "p": {"k": "AG00", "act": 1, "ag": 0}})
        assert capsys.readouterr().out == ""   # still inside the window
        clock.now = 0.05
        bridge.settle.step()
        assert "AG00" in capsys.readouterr().out
        assert bridge.drain() == []            # printed, not also queued
    finally:
        bridge.close()


def test_the_key_bridge_wires_the_readout_to_on_dispatch(monkeypatch, tmp_path):
    """Both bridges the CLI builds must pass it, or the fix is only half done."""
    import freemicro.device as device_mod
    import freemicro.input.bridge as bridge_mod

    built = []

    class _Recorder(bridge_mod.Bridge):
        def __init__(self, *args, **kwargs):
            built.append(kwargs.get("on_dispatch"))
            super().__init__(*args, autostart=False, **kwargs)

    monkeypatch.setattr(bridge_mod, "Bridge", _Recorder)
    monkeypatch.setattr(cli, "_open_pad", lambda *a, **k: _StubDevice())
    monkeypatch.setattr(device_mod, "run_with_reconnect", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_pad_is_taken", lambda role: False)

    assert cli.main([
        "keys", "--dry-run", "--take-pad", "--seconds", "0.01"
    ]) == 0
    assert built == [cli._print_dispatch]


class _StubDevice:
    transport = "usb"

    def send(self, message):
        return None

    def close(self):
        return None
