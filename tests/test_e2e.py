"""End-to-end pipeline test: hook events -> store -> resolved state.

This drives the *real* CLI hook handler (`cmd_hook`) with the exact payloads
Claude Code fires over a session, then asserts the resolved state the renderer
loop would show. It's the closest thing to a live run without an agent or
hardware attached.
"""

from __future__ import annotations

import io
import json
from argparse import Namespace

import pytest

from freemicro import cli
from freemicro.config import config_home
from freemicro.state.engine import AgentState, StateStore


@pytest.fixture
def home(tmp_path, monkeypatch):
    # Point FreeMicro's state dir at a temp home for the whole pipeline.
    monkeypatch.setenv("FREEMICRO_HOME", str(tmp_path))
    return tmp_path


def _fire(monkeypatch, event: dict) -> int:
    """Feed one hook event through the real `freemicro hook` handler."""
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(event)))
    return cli.cmd_hook(Namespace())


def _resolved(home) -> AgentState:
    return StateStore(directory=config_home() / "state").resolved_state()


def test_full_session_lifecycle(home, monkeypatch):
    sid = "sess-1"

    # A realistic single-session run.
    _fire(monkeypatch, {"hook_event_name": "UserPromptSubmit", "session_id": sid})
    assert _resolved(home) == AgentState.WORKING

    _fire(monkeypatch, {"hook_event_name": "PreToolUse", "session_id": sid})
    assert _resolved(home) == AgentState.WORKING

    # Agent hits a permission prompt - the light must flip to "needs you".
    _fire(
        monkeypatch,
        {
            "hook_event_name": "Notification",
            "session_id": sid,
            "message": "Claude needs your permission to run a command",
        },
    )
    assert _resolved(home) == AgentState.WAITING

    # Approved; back to work, then done.
    _fire(monkeypatch, {"hook_event_name": "PostToolUse", "session_id": sid})
    assert _resolved(home) == AgentState.WORKING

    _fire(monkeypatch, {"hook_event_name": "Stop", "session_id": sid})
    assert _resolved(home) == AgentState.DONE

    # Session ends -> idle (record cleared).
    _fire(monkeypatch, {"hook_event_name": "SessionEnd", "session_id": sid})
    assert _resolved(home) == AgentState.IDLE


def test_two_sessions_waiting_wins(home, monkeypatch):
    # One agent grinding, another blocked on you: the blocked one must win.
    _fire(monkeypatch, {"hook_event_name": "UserPromptSubmit", "session_id": "a"})
    _fire(
        monkeypatch,
        {
            "hook_event_name": "Notification",
            "session_id": "b",
            "matcher": "permission_prompt",
        },
    )
    assert _resolved(home) == AgentState.WAITING


def test_error_stop_shows_error(home, monkeypatch):
    _fire(monkeypatch, {"hook_event_name": "UserPromptSubmit", "session_id": "x"})
    _fire(
        monkeypatch,
        {"hook_event_name": "Stop", "session_id": "x", "is_error": True},
    )
    assert _resolved(home) == AgentState.ERROR


def test_malformed_stdin_never_crashes(home, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO("{not valid json"))
    # Must exit 0 - a status light is never worth breaking a Claude session.
    assert cli.cmd_hook(Namespace()) == 0
    assert _resolved(home) == AgentState.IDLE


def test_hook_json_becomes_the_exact_bytes_on_the_wire(home, monkeypatch):
    """The whole claim, in one test: hook stdin JSON → LED protocol messages.

    Everything between is real - ``cmd_hook`` parses the payload, the store
    writes and resolves it, and the renderer builds the messages. Only the
    device is absent, and only because a test must never repaint a pad someone
    has plugged in. What is asserted is the message a pad *would* receive.
    """
    from freemicro import padconfig
    from freemicro.device.lighting import (
        AGENT_KEY_COUNT,
        METHOD_THREAD_STATUS,
        parse_color,
    )
    from freemicro.renderers.micro_leds import MicroLedsRenderer

    pad = padconfig.load_default()
    renderer = MicroLedsRenderer(config=pad)
    store = StateStore(directory=config_home() / "state")

    session = "wire"
    steps = [
        ({"hook_event_name": "UserPromptSubmit"}, AgentState.WORKING),
        (
            {
                "hook_event_name": "Notification",
                "message": "Claude needs your permission to use Bash",
            },
            AgentState.WAITING,
        ),
        ({"hook_event_name": "Stop"}, AgentState.DONE),
        ({"hook_event_name": "Stop", "is_error": True}, AgentState.ERROR),
    ]

    seen_colors = set()
    for payload, expected in steps:
        event = dict(payload, session_id=session)
        assert _fire(monkeypatch, event) == 0

        state = store.resolved_state()
        assert state == expected

        messages = renderer.messages_for(state)
        assert len(messages) == 1  # default zones = agent_keys only
        message = messages[0]
        assert message["m"] == METHOD_THREAD_STATUS
        assert "id" not in message  # v.oai.* are notifications; an id gets a 404
        entries = message["p"]
        assert [e["id"] for e in entries] == list(range(AGENT_KEY_COUNT))

        want = parse_color(pad.lighting.for_state(expected).color)
        assert {e["c"] for e in entries} == {want}
        assert all(set(e) == {"id", "c", "b", "e", "s"} for e in entries)
        seen_colors.add(want)

    # Four states, four different colours actually reaching the hardware.
    assert len(seen_colors) == len(steps)


def test_the_wire_message_is_json_serialisable_and_small(home, monkeypatch):
    """Messages are framed into 63-byte reports, so size is not academic."""
    from freemicro import padconfig
    from freemicro.device import frame_message
    from freemicro.renderers.micro_leds import MicroLedsRenderer

    renderer = MicroLedsRenderer(config=padconfig.load_default())
    for state in AgentState:
        for message in renderer.messages_for(state):
            payload = json.dumps(message, separators=(",", ":"))
            frames = frame_message(payload)
            assert frames and all(len(f) == 63 for f in frames)


def test_informational_notification_does_not_flip(home, monkeypatch):
    _fire(monkeypatch, {"hook_event_name": "UserPromptSubmit", "session_id": "s"})
    _fire(
        monkeypatch,
        {"hook_event_name": "Notification", "session_id": "s", "message": "FYI: build ok"},
    )
    # Non-permission notification is ignored; still working.
    assert _resolved(home) == AgentState.WORKING
