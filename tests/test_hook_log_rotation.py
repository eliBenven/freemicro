"""The opt-in hook-event log must not grow without bound.

`$FREEMICRO_HOOK_LOG` appends one full payload per Claude Code turn. Left
uncapped it grew to 31 MB on the developer's own machine, which is the file
`freemicro uninstall` had to sweep. These tests pin the size cap and that
rotation keeps the most recent events rather than the oldest.
"""

from __future__ import annotations

import json

import pytest

from freemicro import cli
from freemicro.state.engine import AgentState


@pytest.fixture
def hook_log(tmp_path, monkeypatch):
    path = tmp_path / "hook-events.jsonl"
    monkeypatch.setenv("FREEMICRO_HOOK_LOG", str(path))
    return path


def _emit(n, i):
    cli._log_raw_event(
        {"hook_event_name": "PreToolUse", "cwd": "/x", "i": i}, AgentState.WORKING
    )


def test_no_log_written_when_the_variable_is_unset(tmp_path, monkeypatch):
    monkeypatch.delenv("FREEMICRO_HOOK_LOG", raising=False)
    cli._log_raw_event({"hook_event_name": "Stop"}, AgentState.DONE)
    # Nothing to assert a path for: the point is it must not raise and must not
    # write anywhere. Reaching here without an exception is the whole test.


def test_the_log_rotates_at_the_cap_and_stays_bounded(hook_log, monkeypatch):
    monkeypatch.setattr(cli, "HOOK_LOG_MAX_BYTES", 2000)
    for i in range(400):
        _emit(400, i)

    main = hook_log.stat().st_size
    rotated = hook_log.with_suffix(".jsonl.1")
    assert rotated.exists(), "the log passed the cap but never rotated"
    total = main + rotated.stat().st_size
    # Rotation happens before a write, so the live file can reach the cap plus
    # one final line; the previous generation is at most the cap. Bounded by 2x.
    assert total <= 2 * 2000 + 1024


def test_rotation_keeps_the_newest_events_not_the_oldest(hook_log, monkeypatch):
    monkeypatch.setattr(cli, "HOOK_LOG_MAX_BYTES", 2000)
    for i in range(400):
        _emit(400, i)

    live = [json.loads(line) for line in hook_log.read_text().splitlines()]
    assert live, "the live log is empty after rotation"
    assert any(rec["payload"]["i"] == 399 for rec in live), (
        "the most recent event is gone; rotation kept the wrong end"
    )


def test_a_single_event_under_the_cap_never_rotates(hook_log):
    cli._log_raw_event({"hook_event_name": "Stop", "cwd": "/x"}, AgentState.DONE)
    assert hook_log.exists()
    assert not hook_log.with_suffix(".jsonl.1").exists()
    assert len(hook_log.read_text().splitlines()) == 1
