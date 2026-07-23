"""Tests for the priority-resolving, TTL-expiring state store."""

from __future__ import annotations

import pytest

from freemicro.state.engine import AgentState, StateStore


class Clock:
    """A controllable clock for deterministic TTL tests."""

    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


@pytest.fixture
def store(tmp_path):
    clock = Clock()
    s = StateStore(directory=tmp_path, ttl_seconds=100, clock=clock)
    s.clock = clock  # keep a handle for the tests
    return s


def test_single_session_roundtrip(store):
    store.update("s1", AgentState.WORKING)
    assert store.resolved_state() == AgentState.WORKING


def test_idle_when_empty(store):
    assert store.resolve() is None
    assert store.resolved_state() == AgentState.IDLE


def test_priority_waiting_beats_working(store):
    store.update("busy", AgentState.WORKING)
    store.update("blocked", AgentState.WAITING)
    winner = store.resolve()
    assert winner is not None
    assert winner.state == AgentState.WAITING
    assert winner.session_id == "blocked"


def test_priority_full_order(store):
    store.update("a", AgentState.IDLE)
    store.update("b", AgentState.WORKING)
    store.update("c", AgentState.DONE)
    store.update("d", AgentState.ERROR)
    # error should beat done/working/idle
    assert store.resolved_state() == AgentState.ERROR
    store.update("e", AgentState.WAITING)
    # waiting beats everything
    assert store.resolved_state() == AgentState.WAITING


def test_ttl_expires_stale_sessions(store):
    store.update("old", AgentState.WORKING)
    store.clock.advance(101)  # past ttl
    assert store.resolve() is None
    # stale file should have been cleaned up
    assert list(store.directory.glob("*.json")) == []


def test_recency_breaks_priority_ties(store):
    store.update("first", AgentState.WORKING)
    store.clock.advance(1)
    store.update("second", AgentState.WORKING)
    winner = store.resolve()
    assert winner.session_id == "second"


def test_clear_removes_session(store):
    store.update("s1", AgentState.WORKING)
    store.clear("s1")
    assert store.resolved_state() == AgentState.IDLE


def test_unsafe_session_id_is_sanitized(store):
    store.update("proj/../weird id", AgentState.DONE)
    assert store.resolved_state() == AgentState.DONE


def test_corrupt_file_is_skipped(store):
    store.update("good", AgentState.WORKING)
    bad = store.directory / "broken.json"
    bad.write_text("{not json", encoding="utf-8")
    # should not raise, and should still resolve the good session
    assert store.resolved_state() == AgentState.WORKING


def test_priority_property_ordering():
    assert AgentState.WAITING.priority > AgentState.ERROR.priority
    assert AgentState.ERROR.priority > AgentState.DONE.priority
    assert AgentState.DONE.priority > AgentState.WORKING.priority
    assert AgentState.WORKING.priority > AgentState.IDLE.priority


def test_needs_you_flag():
    assert AgentState.WAITING.needs_you
    assert AgentState.DONE.needs_you
    assert AgentState.ERROR.needs_you
    assert not AgentState.WORKING.needs_you
    assert not AgentState.IDLE.needs_you
