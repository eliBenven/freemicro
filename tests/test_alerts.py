"""Off-pad alerts: sound and macOS notifications on state transitions.

FreeMicro's pad is the only channel it has, so if you are looking away you miss
everything. These tests pin the two off-pad channels that fix that - a sound and
a Notification Center banner - and they do it **without ever making a sound or
posting a banner**: the subprocess runner is injected and the tests assert the
argv that *would* have been spawned. The three properties that matter are each
covered directly: off unless the config opts in, never blocking (fire-and-forget
argv, never a wait), and debounced so a flap cannot machine-gun.
"""

from __future__ import annotations

import pytest

from freemicro import alerts
from freemicro.state.engine import AgentState


class FakeClock:
    """A clock the debounce tests advance by hand."""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def recorder():
    """A runner that records argv instead of spawning, plus the list it fills."""
    calls: list = []
    return (lambda argv: calls.append(list(argv))), calls


# ---------------------------------------------------------------------------
# config parsing: an unknown or broken block is silently "no alerts"
# ---------------------------------------------------------------------------

def test_no_alerts_block_is_disabled():
    ac = alerts.AlertConfig.from_raw({"renderers": {}})
    assert ac.enabled is False
    assert ac.sounds == {}
    assert ac.notify == ()


@pytest.mark.parametrize("raw", [None, [], "alerts", 42, {"alerts": "on"},
                                 {"alerts": 5}, {"alerts": []}])
def test_malformed_config_never_raises_and_stays_off(raw):
    ac = alerts.AlertConfig.from_raw(raw)
    assert ac.enabled is False


def test_a_present_block_enables_even_when_empty():
    # Enabled is the master switch: the block existing is the opt-in, even if it
    # arms nothing yet.
    ac = alerts.AlertConfig.from_raw({"alerts": {}})
    assert ac.enabled is True
    assert ac.sounds == {}
    assert ac.notify == ()


def test_sound_and_notify_parse():
    ac = alerts.AlertConfig.from_raw({
        "alerts": {
            "sound": {"done": "Glass", "waiting": "Ping", "error": "Basso"},
            "notify": ["waiting", "error"],
        }
    })
    assert ac.sounds == {"done": "Glass", "waiting": "Ping", "error": "Basso"}
    assert ac.notify == ("waiting", "error")
    assert ac.debounce_seconds == alerts.DEFAULT_DEBOUNCE_SECONDS


def test_bogus_states_and_values_are_dropped():
    ac = alerts.AlertConfig.from_raw({
        "alerts": {
            "sound": {"wating": "Glass", "done": 7, "waiting": "Ping", "": "x"},
            "notify": ["waiting", "nonsense", "waiting", 3],
        }
    })
    # Only the real state with a real string name survives.
    assert ac.sounds == {"waiting": "Ping"}
    # Real states only, de-duplicated, order preserved.
    assert ac.notify == ("waiting",)


def test_debounce_override_and_bad_debounce_falls_back():
    assert alerts.AlertConfig.from_raw(
        {"alerts": {"debounce_seconds": 30}}
    ).debounce_seconds == 30.0
    assert alerts.AlertConfig.from_raw(
        {"alerts": {"debounce_seconds": "soon"}}
    ).debounce_seconds == alerts.DEFAULT_DEBOUNCE_SECONDS


# ---------------------------------------------------------------------------
# firing: the exact argv that would be spawned, and off-by-default
# ---------------------------------------------------------------------------

def test_disabled_alerter_fires_nothing():
    run, calls = recorder()
    al = alerts.Alerter(alerts.AlertConfig(), runner=run)
    al.alert(AgentState.WAITING, AgentState.WORKING, project="proj")
    assert calls == []


def test_waiting_fires_sound_then_notification():
    run, calls = recorder()
    ac = alerts.AlertConfig.from_raw({
        "alerts": {"sound": {"waiting": "Ping"}, "notify": ["waiting"]}
    })
    al = alerts.Alerter(ac, runner=run, clock=FakeClock())
    al.alert(AgentState.WAITING, AgentState.WORKING, project="myrepo")

    assert calls[0] == ["afplay", "/System/Library/Sounds/Ping.aiff"]
    assert calls[1][0] == "osascript"
    assert calls[1][1] == "-e"
    script = calls[1][2]
    assert "Waiting for your approval - myrepo" in script
    assert "Claude Code needs you" in script


def test_a_state_with_no_configured_channel_is_silent():
    run, calls = recorder()
    ac = alerts.AlertConfig.from_raw({
        "alerts": {"sound": {"waiting": "Ping"}, "notify": ["error"]}
    })
    al = alerts.Alerter(ac, runner=run, clock=FakeClock())
    # done has neither a sound nor a notify entry here.
    al.alert(AgentState.DONE, AgentState.WORKING)
    assert calls == []


def test_sound_without_notify_and_notify_without_sound():
    run, calls = recorder()
    ac = alerts.AlertConfig.from_raw({
        "alerts": {"sound": {"done": "Glass"}, "notify": ["error"]}
    })
    al = alerts.Alerter(ac, runner=run, clock=FakeClock())
    al.alert(AgentState.DONE, None)          # sound only
    al.alert(AgentState.ERROR, None)         # notify only
    assert calls[0] == ["afplay", "/System/Library/Sounds/Glass.aiff"]
    assert calls[1][0] == "osascript"
    assert len(calls) == 2


def test_missing_project_yields_a_generic_body():
    run, calls = recorder()
    ac = alerts.AlertConfig.from_raw({"alerts": {"notify": ["waiting"]}})
    al = alerts.Alerter(ac, runner=run, clock=FakeClock())
    al.alert(AgentState.WAITING, None)
    assert "Waiting for your approval" in calls[0][2]
    assert " - " not in calls[0][2]


# ---------------------------------------------------------------------------
# debounce: a flap cannot machine-gun
# ---------------------------------------------------------------------------

def test_debounce_suppresses_a_rapid_repeat():
    run, calls = recorder()
    clock = FakeClock()
    ac = alerts.AlertConfig.from_raw({
        "alerts": {"sound": {"waiting": "Ping"}, "notify": ["waiting"],
                   "debounce_seconds": 10},
    })
    al = alerts.Alerter(ac, runner=run, clock=clock)

    al.alert(AgentState.WAITING, AgentState.WORKING)
    assert len(calls) == 2                       # one sound, one notify
    clock.now += 2                               # still inside the window
    al.alert(AgentState.WAITING, AgentState.WORKING)
    assert len(calls) == 2                       # suppressed
    clock.now += 20                              # window has passed
    al.alert(AgentState.WAITING, AgentState.WORKING)
    assert len(calls) == 4                       # fires again


def test_debounce_is_per_state_not_global():
    run, calls = recorder()
    clock = FakeClock()
    ac = alerts.AlertConfig.from_raw({
        "alerts": {"sound": {"waiting": "Ping", "error": "Basso"},
                   "debounce_seconds": 10},
    })
    al = alerts.Alerter(ac, runner=run, clock=clock)
    al.alert(AgentState.WAITING, None)
    al.alert(AgentState.ERROR, None)             # different state, not debounced
    assert [c[1] for c in calls] == [
        "/System/Library/Sounds/Ping.aiff",
        "/System/Library/Sounds/Basso.aiff",
    ]


def test_zero_debounce_never_suppresses():
    run, calls = recorder()
    clock = FakeClock()
    ac = alerts.AlertConfig.from_raw({
        "alerts": {"sound": {"waiting": "Ping"}, "debounce_seconds": 0},
    })
    al = alerts.Alerter(ac, runner=run, clock=clock)
    al.alert(AgentState.WAITING, None)
    al.alert(AgentState.WAITING, None)           # same instant, no window
    assert len(calls) == 2


# ---------------------------------------------------------------------------
# escaping and text
# ---------------------------------------------------------------------------

def test_escape_applescript_neutralises_quotes_and_backslashes():
    assert alerts.escape_applescript('a "b" \\c') == 'a \\"b\\" \\\\c'


def test_a_hostile_project_name_cannot_break_out_of_the_script():
    run, calls = recorder()
    ac = alerts.AlertConfig.from_raw({"alerts": {"notify": ["waiting"]}})
    al = alerts.Alerter(ac, runner=run, clock=FakeClock())
    al.alert(AgentState.WAITING, None, project='"; do shell script "evil')
    script = calls[0][2]
    # The injected quote is escaped, so it stays inside the string literal.
    assert '\\"' in script
    assert 'do shell script "evil' not in script.replace('\\"', "")


def test_notification_text_defaults():
    title, body = alerts.notification_text("waiting", project="repo")
    assert title == "Claude Code needs you"
    assert body == "Waiting for your approval - repo"


# ---------------------------------------------------------------------------
# sound path resolution
# ---------------------------------------------------------------------------

def test_bare_name_resolves_under_system_sounds():
    assert alerts.sound_path("Glass") == alerts.SYSTEM_SOUNDS_DIR / "Glass.aiff"


def test_a_path_or_extension_is_taken_as_given():
    assert alerts.sound_path("/tmp/my.aiff").as_posix() == "/tmp/my.aiff"
    assert alerts.sound_path("beep.wav").name == "beep.wav"


# ---------------------------------------------------------------------------
# the real runner never waits (fire-and-forget)
# ---------------------------------------------------------------------------

def test_spawn_is_fire_and_forget_and_never_raises(monkeypatch):
    started = {}

    class FakePopen:
        def __init__(self, argv, **kwargs):
            started["argv"] = argv
            started["kwargs"] = kwargs

        # Deliberately no wait()/communicate(): if spawn() called either, this
        # would AttributeError and the test would fail.

    monkeypatch.setattr(alerts.subprocess, "Popen", FakePopen)
    alerts.spawn(["afplay", "/System/Library/Sounds/Glass.aiff"])
    assert started["argv"] == ["afplay", "/System/Library/Sounds/Glass.aiff"]

    def boom(*a, **k):
        raise OSError("no fork today")

    monkeypatch.setattr(alerts.subprocess, "Popen", boom)
    alerts.spawn(["afplay", "x"])  # must swallow, not raise
