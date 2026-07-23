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

    # Agent hits a permission prompt — the light must flip to "needs you".
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
    # Must exit 0 — a status light is never worth breaking a Claude session.
    assert cli.cmd_hook(Namespace()) == 0
    assert _resolved(home) == AgentState.IDLE


def test_informational_notification_does_not_flip(home, monkeypatch):
    _fire(monkeypatch, {"hook_event_name": "UserPromptSubmit", "session_id": "s"})
    _fire(
        monkeypatch,
        {"hook_event_name": "Notification", "session_id": "s", "message": "FYI: build ok"},
    )
    # Non-permission notification is ignored; still working.
    assert _resolved(home) == AgentState.WORKING
