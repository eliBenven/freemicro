"""Tests for the Claude Code hook -> AgentState classifier."""

from __future__ import annotations

from freemicro.state.engine import AgentState, SessionState
from freemicro.state.hooks import (
    IDLE_PROMPT,
    PERMISSION_PROMPT,
    PROMPT_EVENT,
    classify,
    prompt_is_pending,
    session_id_of,
)


def test_prompt_submit_is_working():
    assert classify({"hook_event_name": "UserPromptSubmit"}) == AgentState.WORKING


def test_tool_use_is_working():
    assert classify({"hook_event_name": "PreToolUse"}) == AgentState.WORKING
    assert classify({"hook_event_name": "PostToolUse"}) == AgentState.WORKING


def test_notification_permission_is_waiting():
    event = {"hook_event_name": "Notification", "message": "Permission needed to run"}
    assert classify(event) == AgentState.WAITING


def test_notification_permission_via_matcher():
    event = {"hook_event_name": "Notification", "matcher": "permission_prompt"}
    assert classify(event) == AgentState.WAITING


def test_notification_type_is_trusted_over_the_wording():
    """``notification_type`` states outright what the heuristic can only guess.

    The wording heuristic is the fallback, not the authority: it stays because
    ``Notification`` is not guaranteed to carry a type at all.
    """
    typed = {
        "hook_event_name": "Notification",
        "notification_type": PERMISSION_PROMPT,
        "message": "Claude needs your permission",
    }
    assert classify(typed) == AgentState.WAITING
    # A type that is present and is not a permission prompt wins even when the
    # wording would have matched.
    assert classify({**typed, "notification_type": IDLE_PROMPT}) is None


def test_an_idle_prompt_is_not_amber():
    """"You have not typed for a while" is about you, not about the agent.

    It is the state ``idle`` already describes, and lighting it amber would put
    "blocked on you" and "nothing is happening" in the same colour. Amber is
    the one colour on this pad that has to keep meaning *act now*.
    """
    event = {"hook_event_name": "Notification", "notification_type": IDLE_PROMPT}
    assert classify(event) is None


def test_informational_notification_is_ignored():
    event = {"hook_event_name": "Notification", "message": "Build finished"}
    assert classify(event) is None


def test_stop_is_done():
    assert classify({"hook_event_name": "Stop"}) == AgentState.DONE


def test_stop_with_error_is_error():
    assert classify({"hook_event_name": "Stop", "is_error": True}) == AgentState.ERROR
    assert classify({"hook_event_name": "Stop", "status": "failed"}) == AgentState.ERROR


def test_stop_failure_event_is_error():
    assert classify({"hook_event_name": "StopFailure"}) == AgentState.ERROR


def test_session_end_is_idle():
    assert classify({"hook_event_name": "SessionEnd"}) == AgentState.IDLE


def test_subagent_stop_is_ignored():
    assert classify({"hook_event_name": "SubagentStop"}) is None


def test_unknown_event_is_none():
    assert classify({"hook_event_name": "Whatever"}) is None
    assert classify({}) is None


def test_alternate_event_key():
    assert classify({"event": "Stop"}) == AgentState.DONE


def record(**kwargs) -> SessionState:
    fields = {
        "session_id": "s1",
        "state": AgentState.WAITING,
        "updated_at": 1_700_000_000.0,
        "last_event": PROMPT_EVENT,
        "permission_mode": "default",
    }
    fields.update(kwargs)
    return SessionState(**fields)


def test_only_a_notification_leaves_a_record_that_reads_as_a_live_prompt():
    """The read-back half of ``classify``, and the gate on answering a prompt.

    ``WAITING`` plus ``last_event == "Notification"`` is the whole statement:
    a permission-prompt notification wrote this record and nothing has
    happened since, because any later event would have overwritten it with its
    own name. Everything else in the state store is inadmissible.
    """
    assert prompt_is_pending(record()) is True
    assert prompt_is_pending(record(last_event="PreToolUse")) is False
    assert prompt_is_pending(record(last_event="")) is False
    assert prompt_is_pending(record(state=AgentState.WORKING)) is False
    assert prompt_is_pending(record(permission_mode="bypassPermissions")) is False


def test_session_id_extraction():
    assert session_id_of({"session_id": "abc"}) == "abc"
    assert session_id_of({"sessionId": "xyz"}) == "xyz"
    assert session_id_of({}) == "default"
