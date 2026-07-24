"""The self-test - and, through it, the whole loop.

The project's oldest gap was that every piece was tested and the *assembly*
never was. These are the tests that close it: real hook JSON on stdin, run by
a real subprocess, into a real state store on disk, and out through the real
LED renderer's message builder. Nothing is mocked but the agent.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from freemicro import hooks_install, selftest
from freemicro.device.lighting import METHOD_THREAD_STATUS
from freemicro.state.engine import AgentState

SRC = str(Path(__file__).resolve().parents[1] / "src")


@pytest.fixture
def importable(monkeypatch):
    """Let a subprocess `python -m freemicro` find the package under test."""
    monkeypatch.setenv("PYTHONPATH", SRC)
    return SRC


@pytest.fixture
def settings(tmp_path):
    return tmp_path / "settings.json"


# -- the real thing ---------------------------------------------------------

def test_full_loop_through_a_real_subprocess(importable, settings):
    """The headline claim, asserted: a synthetic session lights every state.

    This spawns the hook command per event exactly as Claude Code does. If it
    passes, the four pieces (binary, hook handler, store, renderer) work
    *together*, which is the only thing a user cares about.
    """
    command = f"{sys.executable} -m freemicro hook"
    hooks_install.install_hooks(settings, command=command)

    report = selftest.run(settings_path=settings)

    assert report.ok, "\n".join(
        f"{c.name}: {c.detail}" for c in report.failures
    )
    assert report.command == command
    assert report.source == "installed in Claude Code settings"

    # Every lifecycle event produced a state assertion, and every state the
    # loop is supposed to reach was actually reached.
    observed = [
        c.detail.split("state → ")[1].split()[0]
        for c in report.checks if "state → " in c.detail
    ]
    assert len(observed) == len(selftest.LIFECYCLE)
    assert observed == [state.value for _label, _p, state in selftest.LIFECYCLE]


def test_lifecycle_visits_all_five_states():
    """A self-test that never exercises `error` would not be one."""
    assert {state for _label, _payload, state in selftest.LIFECYCLE} == set(
        AgentState
    )


def test_in_process_mode_covers_the_same_lifecycle():
    report = selftest.run(command="unused", in_process=True)
    assert report.ok, "\n".join(f"{c.name}: {c.detail}" for c in report.failures)


def test_selftest_never_touches_the_real_state_dir(importable, settings, tmp_path):
    """A self-test that clobbered a live session's state would be a bug."""
    from freemicro.config import config_home
    from freemicro.state.engine import StateStore

    live = StateStore(directory=config_home() / "state")
    live.update("a-real-session", AgentState.WORKING)

    hooks_install.install_hooks(
        settings, command=f"{sys.executable} -m freemicro hook"
    )
    selftest.run(settings_path=settings)

    assert live.resolved_state() == AgentState.WORKING


# -- failure modes ----------------------------------------------------------

def test_missing_binary_fails_loudly(settings):
    hooks_install.install_hooks(settings, command="/gone/bin/freemicro hook")
    report = selftest.run(settings_path=settings)
    assert not report.ok
    failed = [c.name for c in report.failures]
    assert "hook binary exists and is executable" in failed
    # …and it says what to do about it.
    fix = next(c.fix for c in report.failures if "binary exists" in c.name)
    assert "freemicro install" in fix


def test_uninstalled_hooks_fail_the_check(settings):
    report = selftest.run(settings_path=settings)
    assert not report.ok
    assert "hooks registered on every lifecycle event" in [
        c.name for c in report.failures
    ]


def test_drifted_commands_are_caught(settings, importable):
    hooks_install.install_hooks(
        settings, command=f"{sys.executable} -m freemicro hook"
    )
    data = json.loads(settings.read_text())
    data["hooks"]["Stop"][0]["hooks"][0]["command"] = "/old/bin/freemicro hook"
    settings.write_text(json.dumps(data))

    report = selftest.run(settings_path=settings)
    assert "every event uses the same hook command" in [
        c.name for c in report.failures
    ]


def test_a_broken_palette_is_reported_not_raised(monkeypatch, tmp_path):
    """Two states the same colour is a real bug that only the eye finds."""
    from freemicro import padconfig

    keymap = tmp_path / "keymap.json"
    base = json.loads(padconfig.DEFAULT_CONFIG_PATH.read_text())
    base["lighting"]["states"]["done"]["color"] = \
        base["lighting"]["states"]["working"]["color"]
    keymap.write_text(json.dumps(base))
    monkeypatch.setenv("FREEMICRO_KEYMAP", str(keymap))

    report = selftest.run(command="unused", in_process=True)
    assert "every state has a distinguishable colour" in [
        c.name for c in report.failures
    ]


# -- the renderer half ------------------------------------------------------

def test_renderer_checks_assert_real_protocol_messages():
    report = selftest.run(command="unused", in_process=True)
    led_checks = [c for c in report.checks if c.name.startswith("LED message for")]
    assert len(led_checks) == len(AgentState)
    assert all(c.ok for c in led_checks)
    assert all(METHOD_THREAD_STATUS in c.detail for c in led_checks)


def test_report_serialises_for_json_output():
    report = selftest.run(command="unused", in_process=True)
    payload = json.loads(json.dumps(report.to_dict()))
    assert payload["ok"] is True
    assert payload["checks"]
    assert set(payload["checks"][0]) == {"name", "ok", "detail", "fix", "warn"}


def test_a_stopped_daemon_warns_but_does_not_fail_the_loop(monkeypatch):
    """The verdict must not depend on what this machine happens to be running.

    "Is anything listening right now" is a real thing to tell someone and a
    terrible thing to fail on: it would make the self-test pass or fail based
    on the state of a LaunchAgent that has nothing to do with whether the loop
    is wired up correctly.
    """
    from freemicro import daemon

    monkeypatch.setattr(daemon, "is_running", lambda: False)
    monkeypatch.setattr(daemon, "is_installed", lambda: True)

    report = selftest.run(command="unused", in_process=True)
    assert report.ok
    assert [c.name for c in report.warnings] == [
        "a process is listening for state changes"
    ]
