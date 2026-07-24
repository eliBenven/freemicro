"""Tests for the priority-resolving, liveness-aware state store.

Two rules for anything in this file that builds a :class:`StateStore`.

**Use a realistic epoch.** :data:`EPOCH` is a plausible ``time.time()``, not
``1000.0``. This matters, and it is not cosmetic: the pid-reuse guard asks
whether a process started before the record it wrote (``started <= updated_at``),
so a record dated 1970 is *older than every process on the machine*. A fake
epoch plus the real probe therefore always yields "recycled pid", which reads
as "not alive", which prunes the record. A TTL test written that way passes for
a reason that has nothing to do with the TTL, and would stay green through any
regression in the pruning path. That trap cost a review round; do not rebuild
it.

**Never ask the machine about a real process.** The ``no_real_processes``
fixture below is autouse, so every store in this file sees the ``processes``
table - a dict - instead of the running system. Empty means nothing is alive,
which is the old TTL behaviour and the right default for a test that is not
about liveness. A test that *is* about liveness puts pids in the table, or
injects a :class:`ProcessLiveness` of its own.
"""

from __future__ import annotations

import os
import time

import pytest

from freemicro.state.engine import (
    DEFAULT_TOOL_TTL_SECONDS,
    DEFAULT_TTL_SECONDS,
    DEFAULT_WORKING_TTL_SECONDS,
    AgentState,
    DecayPolicy,
    ProcessLiveness,
    SessionSignals,
    SessionState,
    StateStore,
    TerminalInfo,
    current_terminal,
    effective_state,
    normalise_tty,
    parse_elapsed,
    pid_alive,
    pid_started,
    ps_tty_and_parent,
    tty_for_pid,
)
from freemicro.state.hooks import classify, read_signals


#: A plausible wall-clock reading, mid-2026. Every fake clock in this file
#: starts here rather than at ``1000.0``; see the module docstring for what a
#: 1970 epoch quietly does to the liveness check.
EPOCH = 1_784_000_000.0

#: The pid a hook would have captured: the ``claude`` process in the tab.
CLAUDE_PID = 4242


class Clock:
    """A controllable clock for deterministic TTL tests."""

    def __init__(self, t: float = EPOCH) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


class Processes:
    """A fake process table - ``pid -> start time`` - that counts questions.

    A pid mapped to ``None`` is alive but will not say when it started, which
    is what a locked-down machine (or a missing ``ps``) looks like. A pid that
    is absent is not running.
    """

    def __init__(self, table=None) -> None:
        self.table = dict(table or {})
        self.alive_calls = 0
        self.started_calls = 0

    def alive(self, pid: int) -> bool:
        self.alive_calls += 1
        return int(pid) in self.table

    def started(self, pid: int):
        self.started_calls += 1
        return self.table.get(int(pid))

    def start(self, pid: int, at: float) -> None:
        self.table[int(pid)] = at

    def kill(self, pid: int) -> None:
        self.table.pop(int(pid), None)

    def liveness(self, clock, **kwargs) -> ProcessLiveness:
        return ProcessLiveness(
            alive_probe=self.alive,
            started_probe=self.started,
            clock=clock,
            **kwargs,
        )


@pytest.fixture
def processes():
    """The process table every store in this file sees. Empty = nothing alive."""
    return Processes()


@pytest.fixture(autouse=True)
def no_real_processes(monkeypatch, processes):
    """Replace the process-wide liveness cache with the fake table.

    Autouse on purpose. Without it a store built without an explicit
    ``liveness=`` reaches for the real machine, records pytest's own pid, and
    every assertion downstream quietly depends on whether that process happens
    to be alive and when it started. See the module docstring.
    """
    from freemicro.state import engine

    monkeypatch.setattr(engine, "_SHARED_LIVENESS", processes.liveness(time.time))
    return processes


@pytest.fixture
def store(tmp_path, processes):
    clock = Clock()
    s = StateStore(
        directory=tmp_path,
        decay=DecayPolicy(ttl_seconds=100),
        clock=clock,
        # A stable, fake pid, so a test can decide whether that process is
        # running instead of inheriting whatever pytest's parent is doing.
        terminal_probe=lambda: TerminalInfo(tty="/dev/ttys000", pid=CLAUDE_PID),
        liveness=processes.liveness(clock),
    )
    s.clock = clock          # keep a handle for the tests
    s.processes = processes  # ...and the process table they run against
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


def test_ttl_expires_a_session_nothing_vouches_for(store):
    """The backstop, and the *only* thing it still governs.

    Nothing is in the process table, so the record's pid is not running: no
    liveness answer, and the clock has the last word exactly as it always did.
    """
    store.update("old", AgentState.WORKING)
    assert store.sessions()[0].process_alive is False

    store.clock.advance(101)  # past ttl
    assert store.resolve() is None
    # stale file should have been cleaned up
    assert list(store.directory.glob("*.json")) == []


def test_the_ttl_does_not_expire_a_session_whose_process_is_alive(store):
    """The fix, at the TTL boundary: quiet is not gone.

    Same record, same silence, one difference - the ``claude`` process that
    wrote it is still running, and started before it wrote. Under the old rule
    this record was deleted and its project lost its Agent Key while the
    terminal sat open.
    """
    from freemicro.agentkeys import AgentKeysConfig, resolve_slots

    store.processes.start(CLAUDE_PID, EPOCH - 3600)   # claude, up for an hour
    store.update("open", AgentState.WORKING, cwd="/code/api")

    store.clock.advance(101 * 20)                     # long past the TTL
    live = store.sessions()
    assert [s.session_id for s in live] == ["open"]
    assert live[0].process_alive is True
    assert live[0].kept_by_process is True
    assert list(store.directory.glob("*.json"))       # still on disk

    slots = resolve_slots(
        AgentKeysConfig(), live, previous=(), now=store.clock.t
    )
    assert slots[0].path == "/code/api"               # still on its key


def test_an_uninjected_store_still_sees_the_fake_process_table(tmp_path, processes):
    """The guard rail itself, asserted rather than assumed.

    Without the autouse fixture this store would record pytest's own pid and
    ask the operating system about it, and every liveness-sensitive assertion
    in this file would depend on the machine it ran on.
    """
    store = StateStore(directory=tmp_path, clock=Clock())
    store.update("s1", AgentState.WORKING)
    assert store.sessions()[0].process_alive is False   # nothing in the table
    assert processes.alive_calls > 0                    # and the fake answered


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


def test_done_decays_to_idle(tmp_path):
    """Factory green means *unread*, not *finished* - it has to clear.

    Without this the pad goes green after the first task and stays green
    forever, which stops matching the hardware's own behaviour within minutes.
    """
    now = [EPOCH]
    store = StateStore(
        directory=tmp_path,
        decay=DecayPolicy(done_ttl_seconds=180.0),
        clock=lambda: now[0],
    )
    store.update("s1", AgentState.DONE)
    assert store.resolved_state() == AgentState.DONE
    now[0] += 181
    assert store.resolved_state() == AgentState.IDLE


def test_done_decay_can_be_switched_off(tmp_path):
    now = [EPOCH]
    store = StateStore(
        directory=tmp_path,
        decay=DecayPolicy(done_ttl_seconds=0),
        clock=lambda: now[0],
    )
    store.update("s1", AgentState.DONE)
    now[0] += 600  # well past the done TTL, well inside the session TTL
    assert store.resolved_state() == AgentState.DONE


def test_decay_does_not_touch_other_states(tmp_path):
    now = [EPOCH]
    store = StateStore(
        directory=tmp_path,
        decay=DecayPolicy(done_ttl_seconds=1.0),
        clock=lambda: now[0],
    )
    store.update("s1", AgentState.WAITING)
    now[0] += 100
    assert store.resolved_state() == AgentState.WAITING


# ---------------------------------------------------------------------------
# Which tty is this session running in?
#
# No test here shells out or touches a real terminal: the process tree is a
# dict, so the answers are the same on a laptop and on a headless runner.
# ---------------------------------------------------------------------------

def tree(**procs):
    """A fake ``ps``: ``pid -> (tty, ppid)``, unknown pids look like dead ones."""
    table = {int(pid): value for pid, value in procs.items()}

    def probe(pid):
        return table.get(int(pid), ("", 0))

    return probe


def test_ps_spells_a_tty_without_dev_and_terminal_app_spells_it_with():
    """The two sources disagree; everything downstream must not have to care."""
    assert normalise_tty("ttys003") == "/dev/ttys003"
    assert normalise_tty("/dev/ttys003") == "/dev/ttys003"
    assert normalise_tty(" ttys003 ") == "/dev/ttys003"


def test_a_process_with_no_terminal_reports_no_terminal():
    assert normalise_tty("??") == ""      # what ps prints for launchd, and hooks
    assert normalise_tty("") == ""
    assert normalise_tty(None) == ""


def test_a_tty_that_is_not_a_device_name_is_refused():
    """This value ends up inside an AppleScript, so it is checked at the source."""
    assert normalise_tty('ttys004" \n do shell script "rm -rf ~"') == ""
    assert normalise_tty("/dev/../etc/passwd") == ""
    assert normalise_tty("ttys" + "0" * 80) == ""


def test_the_tty_is_found_by_walking_up_to_the_claude_code_process():
    """The bug, in one test.

    A hook is spawned with pipes and outside the terminal's session, so it has
    no controlling terminal of its own - ``ps`` says ``??``. Its parent is the
    ``claude`` process, which is the one sitting in the tab.
    """
    hook, claude, shell = 900, 800, 700
    probe = tree(**{
        str(hook): ("??", claude),
        str(claude): ("ttys000", shell),
        str(shell): ("ttys000", 1),
    })
    assert tty_for_pid(hook, probe=probe) == "/dev/ttys000"


def test_the_nearest_ancestor_with_a_terminal_wins():
    """Stop at the first one: a deeper walk could name an unrelated tab."""
    probe = tree(**{"900": ("??", 800), "800": ("ttys002", 700),
                    "700": ("ttys009", 1)})
    assert tty_for_pid(900, probe=probe) == "/dev/ttys002"


def test_a_pid_that_has_exited_yields_nothing_rather_than_a_guess():
    assert tty_for_pid(4242, probe=tree()) == ""


def test_a_session_started_outside_a_terminal_yields_nothing():
    probe = tree(**{"900": ("??", 1), "1": ("??", 0)})
    assert tty_for_pid(900, probe=probe) == ""


def test_the_walk_is_bounded_and_survives_a_cycle():
    probe = tree(**{"900": ("??", 800), "800": ("??", 900)})
    assert tty_for_pid(900, probe=probe) == ""
    assert tty_for_pid(900, probe=tree(**{"900": ("??", 900)})) == ""
    deep = {str(p): ("??", p - 1) for p in range(900, 800, -1)}
    assert tty_for_pid(900, probe=tree(**deep), max_hops=3) == ""


def test_a_lookup_that_blows_up_is_not_an_error(tmp_path):
    def boom(pid):
        raise OSError("ps is missing")

    assert tty_for_pid(123, probe=boom) == ""


def test_nonsense_pids_are_not_looked_up():
    def never(pid):  # pragma: no cover - proving it is not called
        raise AssertionError("should not probe")

    assert tty_for_pid(0, probe=never) == ""
    assert tty_for_pid(-1, probe=never) == ""
    assert tty_for_pid(1, probe=never) == ""  # launchd, the ceiling of the walk


def test_a_real_ps_call_for_a_dead_pid_does_not_raise():
    """The one place the real ``ps`` is exercised - and only for its failure."""
    assert ps_tty_and_parent(2 ** 30) == ("", 0)
    assert ps_tty_and_parent("not a pid") == ("", 0)


def test_current_terminal_prefers_the_controlling_terminal():
    info = current_terminal(
        direct=lambda: "/dev/ttys001",
        from_pid=lambda pid: "/dev/ttys999",
    )
    assert info.tty == "/dev/ttys001"


def test_current_terminal_falls_back_to_the_process_tree():
    """What a hook actually goes through: no /dev/tty, so ask ``ps``."""
    info = current_terminal(direct=lambda: "", from_pid=lambda pid: "/dev/ttys004")
    assert info.tty == "/dev/ttys004"
    assert info.pid == os.getppid()


def test_current_terminal_survives_both_answers_failing():
    def boom(*args):
        raise OSError("no")

    assert current_terminal(direct=boom, from_pid=boom).tty == ""


def test_a_hook_now_records_a_tty_the_terminal_can_be_matched_on(tmp_path):
    """End to end through the store: what used to land on disk as ``tty: ""``."""
    store = StateStore(
        directory=tmp_path,
        terminal_probe=lambda: current_terminal(
            direct=lambda: "", from_pid=lambda pid: "ttys000"
        ),
    )
    store.update("s1", AgentState.WORKING, cwd="/code/api")
    assert store.sessions()[0].tty == "/dev/ttys000"


# ---------------------------------------------------------------------------
# A `working` claim that stopped being renewed
#
# Pressing Escape interrupts the agent and fires no hook at all, so the last
# thing the session said stays true forever unless something retires it.
# ---------------------------------------------------------------------------

def working(at: float, **kwargs) -> SessionState:
    return SessionState(
        session_id="s1", state=AgentState.WORKING, updated_at=at,
        cwd="/code/api", **kwargs
    )


def test_a_working_claim_that_went_quiet_is_retired():
    """The bug: the pad sat blue for half an hour after an interrupt."""
    session = working(EPOCH)
    assert effective_state(session, now=EPOCH + 30) == AgentState.WORKING
    assert (
        effective_state(session, now=EPOCH + DEFAULT_WORKING_TTL_SECONDS + 1)
        == AgentState.IDLE
    )


def test_a_running_tool_call_is_allowed_to_be_silent():
    """The opposite lie: a five minute build must not blank its own key."""
    session = working(EPOCH, last_event="PreToolUse")
    quiet = EPOCH + DEFAULT_WORKING_TTL_SECONDS * 2
    assert effective_state(session, now=quiet) == AgentState.WORKING
    assert (
        effective_state(session, now=EPOCH + DEFAULT_TOOL_TTL_SECONDS + 1)
        == AgentState.IDLE
    )


def test_silence_after_a_tool_returned_is_suspicious_again():
    """`PostToolUse` then nothing means it stopped between tools."""
    session = working(EPOCH, last_event="PostToolUse")
    assert (
        effective_state(session, now=EPOCH + DEFAULT_WORKING_TTL_SECONDS + 1)
        == AgentState.IDLE
    )


def test_a_session_with_a_running_subagent_is_never_retired():
    """Its own turn stopped; the work did not."""
    session = working(EPOCH, last_event="PostToolUse", background_tasks=1)
    assert effective_state(session, now=EPOCH + 10_000) == AgentState.WORKING
    done = SessionState(
        session_id="s2", state=AgentState.DONE, updated_at=EPOCH,
        background_tasks=2,
    )
    assert effective_state(done, now=EPOCH + 10_000) == AgentState.DONE


def test_the_states_that_need_you_are_never_retired_on_a_timer():
    """Amber and red mean *you* are the blocker; they wait as long as it takes."""
    for state in (AgentState.WAITING, AgentState.ERROR):
        session = SessionState(session_id="s", state=state, updated_at=EPOCH)
        assert effective_state(session, now=EPOCH + 10_000) == state


def test_the_check_can_be_switched_off():
    session = working(EPOCH)
    assert (
        effective_state(
            session, now=EPOCH + 10_000, decay=DecayPolicy(working_ttl_seconds=0)
        )
        == AgentState.WORKING
    )


def test_the_store_reports_a_retired_claim_honestly(tmp_path):
    """`freemicro status` must not report a retired claim as either extreme."""
    now = [EPOCH]
    store = StateStore(directory=tmp_path, clock=lambda: now[0])
    store.update("s1", AgentState.WORKING, cwd="/code/api")
    now[0] += DEFAULT_WORKING_TTL_SECONDS + 1

    live = store.sessions()[0]
    assert live.state == AgentState.IDLE       # what is true now
    assert live.claim == AgentState.WORKING    # what it last said
    assert live.stale is True
    assert "was working" in live.describe_claim()
    # ...and the raw record is untouched on disk.
    assert store.sessions(decay=False)[0].state == AgentState.WORKING


def test_every_view_agrees_about_a_retired_claim(tmp_path):
    """The resolver, the slots and the status command read the same list."""
    from freemicro.agentkeys import AgentKeysConfig, resolve_slots

    now = [EPOCH]
    store = StateStore(directory=tmp_path, clock=lambda: now[0])
    store.update("s1", AgentState.WORKING, cwd="/code/api")
    now[0] += DEFAULT_WORKING_TTL_SECONDS + 1

    assert store.resolved_state() == AgentState.IDLE
    slots = resolve_slots(
        AgentKeysConfig(), store.sessions(), previous=(), now=now[0]
    )
    assert slots[0].state == AgentState.IDLE
    assert slots[0].path == "/code/api"        # still on its key, just not blue


def test_a_fresh_working_session_is_untouched_everywhere(tmp_path):
    from freemicro.agentkeys import AgentKeysConfig, resolve_slots

    now = [EPOCH]
    store = StateStore(directory=tmp_path, clock=lambda: now[0])
    store.update("s1", AgentState.WORKING, cwd="/code/api")
    now[0] += 30
    assert store.resolved_state() == AgentState.WORKING
    slots = resolve_slots(
        AgentKeysConfig(), store.sessions(), previous=(), now=now[0]
    )
    assert slots[0].state == AgentState.WORKING


# ---------------------------------------------------------------------------
# Turns: what `prompt_id` proves that a timer cannot
# ---------------------------------------------------------------------------

def signals(event: str, prompt: str = "", **kwargs) -> SessionSignals:
    return SessionSignals(event=event, prompt_id=prompt, **kwargs)


def test_a_new_prompt_over_an_open_turn_is_an_interrupt(tmp_path):
    """Escape, then type something else: the old turn never got its `Stop`."""
    store = StateStore(directory=tmp_path)
    store.update("s1", AgentState.WORKING, signals=signals("UserPromptSubmit", "p1"))
    store.update("s1", AgentState.WORKING, signals=signals("PreToolUse", "p1"))
    record = store.update(
        "s1", AgentState.WORKING, signals=signals("UserPromptSubmit", "p2")
    )
    assert record.interrupted is True
    assert record.prompt_id == "p2" and record.turn_open is True


def test_an_abandoned_turn_that_then_goes_quiet_says_so(tmp_path):
    """Both halves together: interrupted, then silent, then honest about it."""
    now = [EPOCH]
    store = StateStore(directory=tmp_path, clock=lambda: now[0])
    store.update("s1", AgentState.WORKING, signals=signals("UserPromptSubmit", "p1"))
    store.update("s1", AgentState.WORKING, signals=signals("UserPromptSubmit", "p2"))
    now[0] += DEFAULT_WORKING_TTL_SECONDS + 1
    live = store.sessions()[0]
    assert live.state == AgentState.IDLE
    assert "interrupted" in live.describe_claim()


def test_a_turn_that_finished_cleanly_is_not_an_interrupt(tmp_path):
    store = StateStore(directory=tmp_path)
    store.update("s1", AgentState.WORKING, signals=signals("UserPromptSubmit", "p1"))
    closed = store.update("s1", AgentState.DONE, signals=signals("Stop", "p1"))
    assert closed.turn_open is False and closed.interrupted is False
    started = store.update(
        "s1", AgentState.WORKING, signals=signals("UserPromptSubmit", "p2")
    )
    assert started.interrupted is False


def test_an_interrupt_is_remembered_for_the_turn_it_happened_in(tmp_path):
    store = StateStore(directory=tmp_path)
    store.update("s1", AgentState.WORKING, signals=signals("UserPromptSubmit", "p1"))
    store.update("s1", AgentState.WORKING, signals=signals("UserPromptSubmit", "p2"))
    carried = store.update("s1", AgentState.WORKING, signals=signals("PreToolUse", "p2"))
    assert carried.interrupted is True
    cleared = store.update("s1", AgentState.DONE, signals=signals("Stop", "p2"))
    assert cleared.interrupted is False


def test_session_end_twice_is_harmless(tmp_path):
    """Observed on hardware: `SessionEnd` fired twice for one session."""
    store = StateStore(directory=tmp_path)
    store.update("s1", AgentState.WORKING, signals=signals("UserPromptSubmit", "p1"))
    store.clear("s1")
    store.clear("s1")
    assert store.sessions() == []


def test_a_record_without_any_signals_behaves_exactly_as_before(tmp_path):
    """`freemicro emit` and older hooks pass nothing; nothing may break."""
    store = StateStore(directory=tmp_path)
    record = store.update("s1", AgentState.WORKING)
    assert record.prompt_id == "" and record.interrupted is False
    assert record.last_event == "" and record.background_tasks == 0


def test_the_signals_survive_a_round_trip_to_disk(tmp_path):
    store = StateStore(directory=tmp_path)
    store.update(
        "s1",
        AgentState.WORKING,
        signals=SessionSignals(
            event="PreToolUse", prompt_id="p1", permission_mode="bypassPermissions",
            effort="high", background_tasks=2,
        ),
    )
    live = store.sessions()[0]
    assert live.last_event == "PreToolUse" and live.prompt_id == "p1"
    assert live.effort == "high" and live.background_tasks == 2
    assert live.prompts_for_permission is False
    assert live.tool_running is True


# ---------------------------------------------------------------------------
# Reading a real payload
#
# Field names and shapes are copied from payloads captured on the owner's
# machine; nothing here needs Claude Code to be running.
# ---------------------------------------------------------------------------

REAL_PRE_TOOL_USE = {
    "session_id": "27961705-b2ca-4c36-be4e-10f59b5a73b6",
    "transcript_path": "/Users/e/.claude/projects/x/27961705.jsonl",
    "cwd": "/Users/e/Desktop/freemicro",
    "prompt_id": "dfdfeca3-f06e-4731-a74a-b34eb611fa11",
    "permission_mode": "bypassPermissions",
    "effort": {"level": "high"},
    "hook_event_name": "PreToolUse",
    "tool_name": "Bash",
    "tool_input": {"command": "pytest -q", "description": "run the suite"},
    "tool_use_id": "toolu_014WGXPYHVeEWGJxa7EcSQCK",
}

REAL_STOP_WITH_SUBAGENT = {
    "session_id": "27961705-b2ca-4c36-be4e-10f59b5a73b6",
    "cwd": "/Users/e/Desktop/freemicro",
    "prompt_id": "10fca6be-8899-48ba-b3ee-db76c079b1d2",
    "permission_mode": "bypassPermissions",
    "effort": {"level": "high"},
    "hook_event_name": "Stop",
    "stop_hook_active": False,
    "last_assistant_message": "592 tests green.",
    "background_tasks": [
        {
            "id": "af9752439c92a5ff6",
            "type": "subagent",
            "status": "running",
            "description": "Fix terminal tab focusing via pid",
            "agent_type": "general-purpose",
        }
    ],
    "session_crons": [],
}

REAL_PERMISSION_NOTIFICATION = {
    "session_id": "eb169617-923e-4fed-b540-ed3169c0b8f5",
    "cwd": "/Users/e/Desktop/braided",
    "prompt_id": "986b2d28-b788-47af-9e47-e4e518705ad7",
    "hook_event_name": "Notification",
    "message": "Claude needs your permission",
    "notification_type": "permission_prompt",
}

REAL_SESSION_END_CLEAR = {
    "session_id": "eb169617-923e-4fed-b540-ed3169c0b8f5",
    "cwd": "/Users/e/Desktop/braided",
    "prompt_id": "792771d6-2905-479a-9162-d99528512f89",
    "hook_event_name": "SessionEnd",
    "reason": "clear",
}


def test_a_real_pre_tool_use_payload_reads_cleanly():
    read = read_signals(REAL_PRE_TOOL_USE)
    assert read.event == "PreToolUse"
    assert read.prompt_id == "dfdfeca3-f06e-4731-a74a-b34eb611fa11"
    assert read.permission_mode == "bypassPermissions"
    assert read.effort == "high"
    assert read.background_tasks == 0
    assert read.closes_turn is False


def test_a_stop_with_a_running_subagent_counts_it():
    read = read_signals(REAL_STOP_WITH_SUBAGENT)
    assert read.closes_turn is True
    assert read.background_tasks == 1


def test_only_running_background_tasks_count():
    event = dict(REAL_STOP_WITH_SUBAGENT)
    event["background_tasks"] = [
        {"status": "running"}, {"status": "completed"}, {"status": "failed"}, "junk",
    ]
    assert read_signals(event).background_tasks == 1


def test_a_permission_notification_is_taken_at_its_word():
    """`notification_type` beats guessing from the message wording."""
    assert classify(REAL_PERMISSION_NOTIFICATION) == AgentState.WAITING
    informational = dict(REAL_PERMISSION_NOTIFICATION)
    informational["notification_type"] = "idle_timeout"
    informational["message"] = "Claude needs your permission"  # wording says amber
    assert classify(informational) is None                     # the type says no


def test_a_notification_without_a_type_still_uses_the_wording():
    assert classify({
        "hook_event_name": "Notification",
        "message": "Claude needs your permission to use Bash",
    }) == AgentState.WAITING


def test_a_clear_is_distinguishable_from_a_real_exit():
    assert read_signals(REAL_SESSION_END_CLEAR).end_reason == "clear"
    assert read_signals(
        dict(REAL_SESSION_END_CLEAR, reason="exit")
    ).end_reason == "exit"


def test_a_hostile_or_empty_payload_reads_as_nothing_known():
    assert read_signals({}).empty is True
    assert read_signals({"effort": "high"}).effort == "high"
    assert read_signals({"background_tasks": "not a list"}).background_tasks == 0
    assert read_signals(None).empty is True


# ---------------------------------------------------------------------------
# Quiet is not gone
#
# A hook only fires on activity, so a clock cannot tell a terminal you have not
# touched since lunch from one you closed. A pid can. Nothing below touches a
# real process: the process table is a dict.
# ---------------------------------------------------------------------------

def open_store(tmp_path, clock, procs, *, pid: int = CLAUDE_PID, **kwargs):
    """A store whose records look like a real hook wrote them.

    The default TTL rather than the fixture's 100s, because these tests are
    about what happens on the far side of half an hour of silence.
    """
    return StateStore(
        directory=tmp_path,
        decay=DecayPolicy(ttl_seconds=DEFAULT_TTL_SECONDS),
        clock=clock,
        terminal_probe=lambda: TerminalInfo(tty="/dev/ttys000", pid=pid),
        liveness=procs.liveness(clock),
        **kwargs,
    )


def running(started_ago: float = 3600.0) -> Processes:
    """A process table in which ``claude`` has been up for an hour."""
    return Processes({CLAUDE_PID: EPOCH - started_ago})


def test_a_project_you_have_not_touched_all_afternoon_keeps_its_key(tmp_path):
    """The bug, in one test.

    Four projects open, three of them quiet for hours. Under the old rule each
    one silently vanished thirty minutes after its last hook, and the Agent Key
    for a terminal that was sitting right there went dark.
    """
    clock = Clock()
    procs = running()
    store = open_store(tmp_path, clock, procs)
    store.update("s1", AgentState.DONE, cwd="/code/api")

    clock.advance(4 * 60 * 60)               # an afternoon in another repo

    live = store.sessions()
    assert [s.session_id for s in live] == ["s1"]
    assert live[0].process_alive is True
    assert live[0].kept_by_process is True
    assert list(store.directory.glob("*.json"))   # and it is still on disk


def test_a_session_whose_terminal_closed_is_removed_at_the_ttl(tmp_path):
    """Something still prunes: a crash that never sent ``SessionEnd``."""
    clock = Clock()
    procs = running()
    store = open_store(tmp_path, clock, procs)
    store.update("s1", AgentState.WORKING, cwd="/code/api")
    procs.kill(CLAUDE_PID)

    clock.advance(DEFAULT_TTL_SECONDS + 1)
    assert store.sessions() == []
    assert list(store.directory.glob("*.json")) == []


def test_a_recycled_pid_does_not_resurrect_a_dead_session(tmp_path):
    """The reuse guard: same number, but it started after the record was written.

    Nothing extra has to be stored for this. Whatever wrote the record was
    holding that pid at ``updated_at``, so a process that started *later*
    cannot be it - only a later tenant of the same number.
    """
    clock = Clock()
    procs = running()
    store = open_store(tmp_path, clock, procs)
    store.update("s1", AgentState.WORKING, cwd="/code/api")
    written_at = clock.t

    procs.kill(CLAUDE_PID)
    clock.advance(DEFAULT_TTL_SECONDS + 1)
    procs.start(CLAUDE_PID, written_at + 60)       # somebody else got the pid

    assert store.sessions() == []
    assert list(store.directory.glob("*.json")) == []


def test_a_pid_we_cannot_verify_falls_back_to_the_old_ttl(tmp_path):
    """Alive, but nothing will say since when: the clock has the last word."""
    clock = Clock()
    procs = Processes({CLAUDE_PID: None})          # alive; start time unknown
    store = open_store(tmp_path, clock, procs)
    store.update("s1", AgentState.WORKING, cwd="/code/api")

    clock.advance(60)
    assert store.sessions()[0].process_alive is True
    clock.advance(DEFAULT_TTL_SECONDS)
    assert store.sessions() == []


def test_a_record_with_no_pid_falls_back_to_the_old_ttl(tmp_path):
    """Written by ``freemicro emit``, or by a build too old to capture one."""
    clock = Clock()
    procs = Processes()
    store = StateStore(
        directory=tmp_path,
        decay=DecayPolicy(ttl_seconds=DEFAULT_TTL_SECONDS),
        clock=clock,
        terminal_probe=TerminalInfo,
        liveness=procs.liveness(clock),
    )
    store.update("s1", AgentState.WORKING, cwd="/code/api")

    clock.advance(60)
    live = store.sessions()[0]
    assert live.process_alive is None              # nothing to ask about
    assert live.kept_by_process is False
    clock.advance(DEFAULT_TTL_SECONDS)
    assert store.sessions() == []
    assert procs.alive_calls == 0                  # and we never asked


def test_liveness_only_ever_extends_a_record_never_shortens_it(tmp_path):
    """A dead-looking pid inside the TTL removes nothing.

    Not every writer of a record is a long-lived session - ``freemicro emit``
    runs from a shell that exits the moment it returns - and being wrong in
    that direction would empty the pad instead of filling it.
    """
    clock = Clock()
    procs = Processes()                            # nothing is alive
    store = open_store(tmp_path, clock, procs)
    store.update("s1", AgentState.WAITING, cwd="/code/api")

    clock.advance(DEFAULT_TTL_SECONDS - 1)
    live = store.sessions()
    assert [s.state for s in live] == [AgentState.WAITING]
    assert live[0].process_alive is False
    assert live[0].process_gone is True
    assert live[0].kept_by_process is False


def test_the_claim_still_decays_while_the_session_stays(tmp_path):
    """Two separate questions: does it exist, and what is it doing.

    An idle session that stays on its Agent Key showing white is exactly right.
    """
    clock = Clock()
    procs = running()
    store = open_store(tmp_path, clock, procs)
    store.update("s1", AgentState.WORKING, cwd="/code/api")

    clock.advance(DEFAULT_WORKING_TTL_SECONDS + 1)
    live = store.sessions()[0]
    assert live.state == AgentState.IDLE
    assert live.claim == AgentState.WORKING
    assert live.stale is True


def test_a_quiet_but_open_session_says_so(tmp_path):
    """`freemicro status` must distinguish "idle, still open" from "gone"."""
    clock = Clock()
    procs = running()
    store = open_store(tmp_path, clock, procs)
    store.update("s1", AgentState.DONE, cwd="/code/api")

    clock.advance(60 * 60)
    live = store.sessions()[0]
    assert "terminal still open" in live.describe_claim()
    assert live.to_dict()["process_alive"] is True
    assert live.to_dict()["kept_by_process"] is True


def test_a_liveness_reading_is_never_written_to_disk(tmp_path):
    """It is a reading of the world, not part of the record."""
    import json

    clock = Clock()
    procs = running()
    store = open_store(tmp_path, clock, procs)
    record = store.update("s1", AgentState.WORKING, cwd="/code/api")
    assert "process_alive" not in record.to_dict()

    on_disk = json.loads(
        next(iter(store.directory.glob("*.json"))).read_text(encoding="utf-8")
    )
    assert "process_alive" not in on_disk and "kept_by_process" not in on_disk
    # ...and a record read back does not claim yesterday's answer.
    assert store.read("s1").process_alive is None


def test_reading_the_store_is_one_syscall_and_no_subprocess(tmp_path):
    """`sessions()` runs on every render tick; it must stay nearly free."""
    clock = Clock()
    procs = running()
    store = open_store(tmp_path, clock, procs)
    for name in ("s1", "s2", "s3", "s4", "s5", "s6"):
        store.update(name, AgentState.WORKING, cwd=f"/code/{name}")

    for _ in range(40):                            # ten seconds at 4 Hz
        store.sessions()
        clock.advance(0.25)

    # One cheap check per pid per cache window, and not one ``ps``: a fresh
    # record has no pid-reuse question to answer.
    assert procs.alive_calls <= 11
    assert procs.started_calls == 0


def test_a_store_rebuilt_on_every_poll_still_gets_a_warm_cache(tmp_path):
    """The menu bar builds a throwaway store twice per poll.

    A cache that died with the store would be no cache at all, and the ``ps``
    half of the check would run several times a second for every quiet project.
    """
    from freemicro.state.engine import default_liveness, default_store

    first = StateStore(directory=tmp_path)
    second = StateStore(directory=tmp_path)
    assert first.liveness is second.liveness is default_liveness()
    # And through the construction every surface actually uses, which is the
    # one the menu bar calls on each poll.
    assert default_store().liveness is default_liveness()


def test_the_identity_check_is_paid_for_once_a_minute_at_most(tmp_path):
    clock = Clock()
    procs = running()
    store = open_store(tmp_path, clock, procs)
    store.update("s1", AgentState.WORKING, cwd="/code/api")
    clock.advance(DEFAULT_TTL_SECONDS + 1)

    for _ in range(240):                           # a minute at 4 Hz
        store.sessions()
        clock.advance(0.25)
    assert procs.started_calls <= 2


# -- the probe itself -------------------------------------------------------

def test_the_liveness_cache_answers_repeat_questions_for_free():
    clock = Clock()
    procs = Processes({7: 100.0})
    probe = procs.liveness(clock)
    assert [probe.alive(7) for _ in range(5)] == [True] * 5
    assert procs.alive_calls == 1
    clock.advance(2)
    assert probe.alive(7) is True
    assert procs.alive_calls == 2


def test_a_pid_seen_dead_forgets_everything_we_knew_about_it():
    """Whoever holds that number next is a different process."""
    clock = Clock()
    procs = Processes({7: 100.0})
    probe = procs.liveness(clock)
    assert probe.started(7) == 100.0
    procs.kill(7)
    clock.advance(2)
    assert probe.alive(7) is False
    procs.table[7] = 900.0                         # recycled
    clock.advance(2)
    assert probe.alive(7) is True
    assert probe.started(7) == 900.0               # not the cached 100.0


def test_the_verdict_is_three_valued():
    clock = Clock()
    procs = Processes({7: 100.0, 8: None})
    probe = procs.liveness(clock)

    def record(pid: int) -> SessionState:
        return SessionState(
            session_id="s", state=AgentState.IDLE, updated_at=500.0, pid=pid
        )

    assert probe.verdict(record(7), verify=True) is True     # alive, and ours
    assert probe.verdict(record(9), verify=True) is False    # gone
    assert probe.verdict(record(8), verify=True) is None     # cannot prove it
    assert probe.verdict(record(8), verify=False) is True    # not asked to
    assert probe.verdict(record(0), verify=True) is None     # no pid at all


def test_a_probe_that_blows_up_is_not_an_error():
    def boom(pid):
        raise OSError("ps is missing")

    probe = ProcessLiveness(alive_probe=boom, started_probe=boom)
    session = SessionState(
        session_id="s", state=AgentState.IDLE, updated_at=1.0, pid=99
    )
    assert probe.verdict(session, verify=True) is False
    probe.forget()
    assert probe.alive(99) is False


def test_the_start_time_tolerance_covers_a_second_of_rounding():
    """`ps` truncates elapsed time, so a derived start can land a shade late."""
    clock = Clock()
    procs = Processes({7: 1000.9})
    probe = procs.liveness(clock)
    session = SessionState(
        session_id="s", state=AgentState.IDLE, updated_at=EPOCH, pid=7
    )
    assert probe.verdict(session, verify=True) is True
    procs.table[7] = EPOCH + probe.tolerance + 1
    probe.forget()
    assert probe.verdict(session, verify=True) is False


def test_a_pid_that_is_not_a_pid_is_never_signalled():
    """`os.kill(0, 0)` asks about our whole process group and answers yes."""
    assert pid_alive(0) is False
    assert pid_alive(-1) is False
    assert pid_alive(1) is False                   # launchd is never a session
    assert pid_alive("nonsense") is False
    assert pid_alive(2 ** 30) is False             # nothing is up there


def test_our_own_process_is_alive():
    assert pid_alive(os.getpid()) is True


def test_ps_elapsed_time_parses_in_every_shape_it_comes_in():
    assert parse_elapsed("05") == 5
    assert parse_elapsed("01:30") == 90
    assert parse_elapsed(" 02:00:00 ") == 7200
    assert parse_elapsed("16-05:28:59") == 16 * 86400 + 5 * 3600 + 28 * 60 + 59
    assert parse_elapsed("nonsense") is None
    assert parse_elapsed("") is None
    assert parse_elapsed(None) is None


def test_a_real_ps_call_for_a_pid_that_cannot_exist_does_not_raise():
    """The one place the real ``ps`` is exercised - and only for its failure."""
    assert pid_started(2 ** 30) is None
    assert pid_started(0) is None
