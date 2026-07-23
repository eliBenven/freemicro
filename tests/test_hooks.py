"""Tests for the Claude Code hook -> AgentState classifier."""

from __future__ import annotations

from freemicro.state.engine import AgentState
from freemicro.state.hooks import classify, session_id_of


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


def test_session_id_extraction():
    assert session_id_of({"session_id": "abc"}) == "abc"
    assert session_id_of({"sessionId": "xyz"}) == "xyz"
    assert session_id_of({}) == "default"
