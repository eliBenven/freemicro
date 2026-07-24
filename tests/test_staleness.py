"""Running software that is not the installed software.

Every test here stands for a real evening lost to "correct code, invisible
state": a bridge that was not running at all, a menu bar from before an update
rejecting a valid config, a process holding the pad lock after it had been
replaced, a config edited under a bridge that had already read it.

Nothing shells out. ``ps`` is injected as rows, the package mtime is injected
as a float, the verification subprocess is injected as a callable and ``execv``
is injected as a recorder - so the *restart* path is asserted end to end
without a single process ever being replaced.
"""

from __future__ import annotations

import json
import os

from freemicro import staleness


def _rows(*entries):
    """``ps`` output as (pid, elapsed seconds, command) rows."""
    return tuple(entries)


# ---------------------------------------------------------------------------
# Reading the process table
# ---------------------------------------------------------------------------

def test_elapsed_time_parses_every_shape_bsd_ps_emits():
    # macOS has no `etimes`; asking for it makes ps refuse the whole command,
    # which would silently blind every check in this module.
    assert staleness._parse_etime("05") == 5
    assert staleness._parse_etime("01:30") == 90
    assert staleness._parse_etime("02:00:00") == 7200
    assert staleness._parse_etime("1-00:00:00") == 86400
    assert staleness._parse_etime("nonsense") is None


def test_each_long_lived_role_is_recognised():
    rows = _rows(
        (10, 60.0, "/usr/local/bin/freemicro run"),
        (11, 60.0, "/usr/local/bin/freemicro keys --dry-run"),
        (12, 60.0, "/usr/local/bin/freemicro daemon run"),
        (13, 60.0, "/usr/bin/python3 -m freemicro.menubar"),
        (14, 60.0, "/usr/local/bin/freemicro config --web"),
    )
    found = staleness.find_freemicro_processes(rows=rows, exclude=())
    roles = {p.pid: p.role for p in found}
    assert roles == {
        10: staleness.ROLE_RUN,
        11: staleness.ROLE_KEYS,
        12: staleness.ROLE_DAEMON,
        13: staleness.ROLE_MENUBAR,
        14: staleness.ROLE_WEBUI,
    }


def test_the_daemon_is_not_mistaken_for_a_plain_run():
    # "daemon run" contains "run"; getting this wrong would name the wrong
    # process in every message and offer the wrong restart command.
    found = staleness.find_freemicro_processes(
        rows=_rows((7, 1.0, "freemicro daemon run")), exclude=()
    )
    assert [p.role for p in found] == [staleness.ROLE_DAEMON]


def test_short_lived_commands_are_not_processes_we_report():
    rows = _rows(
        (20, 1.0, "freemicro status"),
        (21, 1.0, "freemicro doctor"),
        (22, 1.0, "grep freemicro run"),
    )
    # `grep freemicro run` is the classic false positive; it is excluded only
    # because we match roles, not the word "freemicro".
    assert [p.pid for p in staleness.find_freemicro_processes(rows=rows,
                                                              exclude=())] == [22]


def test_the_caller_never_finds_itself():
    rows = _rows((os.getpid(), 5.0, "freemicro run"))
    assert staleness.find_freemicro_processes(rows=rows) == ()


def test_start_times_come_from_elapsed_time():
    rows = _rows((30, 120.0, "freemicro run"))
    found = staleness.find_freemicro_processes(
        rows=rows, exclude=(), clock=lambda: 1_000.0
    )
    assert found[0].started == 880.0
    assert staleness.process_start_time(30, rows=rows, clock=lambda: 1_000.0) == 880.0
    assert staleness.process_start_time(31, rows=rows) is None


# ---------------------------------------------------------------------------
# The code on disk
# ---------------------------------------------------------------------------

def test_package_mtime_ignores_bytecode(tmp_path):
    # CPython writes a .pyc the first time a fresh process imports a module.
    # Counting it would make every process look older than its own code within
    # a second of starting - a restart loop built out of nothing.
    (tmp_path / "a.py").write_text("x = 1\n")
    os.utime(tmp_path / "a.py", (1000, 1000))
    cache = tmp_path / "__pycache__"
    cache.mkdir()
    (cache / "a.pyc").write_bytes(b"\x00")
    os.utime(cache / "a.pyc", (9_999_999, 9_999_999))
    assert staleness.package_mtime(tmp_path) == 1000


def test_a_process_older_than_the_code_is_stale():
    rows = _rows((40, 100.0, "freemicro run"))
    clock = lambda: 1_000.0  # noqa: E731 - a fake clock is clearer inline
    # Started at t=900, code written at t=950.
    assert staleness.process_started_before_code(
        40, rows=rows, mtime=950.0, clock=clock
    )
    assert not staleness.process_started_before_code(
        40, rows=rows, mtime=800.0, clock=clock
    )


def test_a_process_started_just_after_an_install_is_not_called_stale():
    # ps reports elapsed time to the second, so start times are only accurate
    # to about that. Calling a process stale over rounding is worse than
    # saying nothing at all.
    rows = _rows((41, 10.0, "freemicro run"))
    assert not staleness.process_started_before_code(
        41, rows=rows, mtime=991.0, clock=lambda: 1_000.0
    )


def test_a_config_edited_after_the_bridge_read_it_is_flagged(tmp_path):
    config = tmp_path / "keymap.json"
    config.write_text("{}")
    os.utime(config, (1_000, 1_000))
    assert staleness.config_changed_since(1, config_path=config, started=900.0)
    assert not staleness.config_changed_since(1, config_path=config, started=1_100.0)


# ---------------------------------------------------------------------------
# Is anything listening? The most valuable answer in the module.
# ---------------------------------------------------------------------------

def test_the_lock_holder_is_the_strongest_evidence():
    listener = staleness.pad_listener(
        processes=(), holder=lambda: {"role": "daemon", "pid": 99}
    )
    assert listener.listening
    assert listener.pid == 99
    assert listener.source == "lock"
    assert "background daemon" in listener.summary()


def test_a_take_pad_process_holds_no_lock_but_is_still_listening():
    processes = staleness.find_freemicro_processes(
        rows=_rows((50, 5.0, "freemicro run --take-pad")), exclude=()
    )
    listener = staleness.pad_listener(processes=processes, holder=lambda: None)
    assert listener.listening and listener.source == "process" and listener.pid == 50


def test_a_menu_bar_alone_does_not_count_as_driving_the_pad():
    processes = staleness.find_freemicro_processes(
        rows=_rows((51, 5.0, "freemicro menubar")), exclude=()
    )
    listener = staleness.pad_listener(processes=processes, holder=lambda: None)
    assert not listener.listening


def test_nothing_listening_says_so_and_names_both_fixes():
    listener = staleness.pad_listener(processes=(), holder=lambda: None)
    assert not listener.listening
    assert listener.summary() == "No bridge running - the pad is inert."
    assert "freemicro run" in listener.fix()
    assert "freemicro daemon install" in listener.fix()


# ---------------------------------------------------------------------------
# The whole report
# ---------------------------------------------------------------------------

def test_report_names_the_stale_process_and_how_to_restart_it(tmp_path):
    config = tmp_path / "keymap.json"
    config.write_text("{}")
    os.utime(config, (500, 500))
    report = staleness.report(
        rows=_rows((60, 100.0, "freemicro daemon run")),
        holder=lambda: {"role": "daemon", "pid": 60},
        config_path=config,
        mtime=950.0,
        clock=lambda: 1_000.0,
    )
    assert report.anything_stale
    entry = report.stale[0]
    assert entry.code_stale and not entry.config_stale
    assert "background daemon (pid 60)" in entry.summary()
    assert "the code it loaded" in entry.summary()
    assert entry.fix() == "freemicro daemon install"


def test_report_flags_a_config_edited_under_a_process_that_cannot_reload(tmp_path):
    config = tmp_path / "keymap.json"
    config.write_text("{}")
    os.utime(config, (980, 980))
    report = staleness.report(
        rows=_rows((61, 100.0, "freemicro keys")),
        holder=lambda: None,
        config_path=config,
        mtime=1.0,
        clock=lambda: 1_000.0,
    )
    entry = report.stale[0]
    assert entry.config_stale and not entry.code_stale
    assert "the config it read" in entry.summary()
    assert "Ctrl-C" in entry.fix()


def test_a_bridge_that_reloads_its_config_is_never_called_stale_about_it(tmp_path):
    # `freemicro run` re-reads the file while it runs. Warning about an edit it
    # has already picked up would leave an untrue line on screen forever, which
    # is the exact failure this module exists to delete.
    config = tmp_path / "keymap.json"
    config.write_text("{}")
    os.utime(config, (980, 980))
    report = staleness.report(
        rows=_rows((62, 100.0, "freemicro run")),
        holder=lambda: None,
        config_path=config,
        mtime=1.0,
        clock=lambda: 1_000.0,
    )
    assert report.stale == ()


def test_a_current_process_produces_no_warning(tmp_path):
    config = tmp_path / "keymap.json"
    config.write_text("{}")
    os.utime(config, (100, 100))
    report = staleness.report(
        rows=_rows((62, 100.0, "freemicro run")),
        holder=lambda: None,
        config_path=config,
        mtime=100.0,
        clock=lambda: 1_000.0,
    )
    assert report.stale == () and report.listener.listening


def test_a_long_lived_reader_can_notice_its_own_drift():
    # `report` excludes the caller on purpose, which is exactly why the menu
    # bar needs this: the one process that could have said "I am out of date"
    # was the one not looking.
    mine = staleness.self_staleness(
        staleness.ROLE_MENUBAR,
        mtime=staleness.PROCESS_STARTED + 3600,
        config_path=None,
        started=staleness.PROCESS_STARTED,
    )
    assert mine is not None and mine.code_stale
    assert "menu bar" in mine.summary()
    assert staleness.self_staleness(staleness.ROLE_MENUBAR, mtime=1.0) is None


# ---------------------------------------------------------------------------
# Stale locks: never "go and delete a file"
# ---------------------------------------------------------------------------

def test_a_lock_left_by_a_dead_process_is_reclaimed_with_one_line(tmp_path):
    lock = tmp_path / "pad.lock"
    lock.write_text(json.dumps({"pid": 424242, "role": "run"}))
    note = staleness.reclaim_stale_lock(
        path=lock, holder=lambda: None, alive=lambda pid: False
    )
    assert "reclaimed a stale lock" in note
    assert "delete" not in note.lower()


def test_a_lock_someone_actually_holds_is_left_alone(tmp_path):
    lock = tmp_path / "pad.lock"
    lock.write_text(json.dumps({"pid": 1, "role": "daemon"}))
    assert staleness.reclaim_stale_lock(
        path=lock, holder=lambda: {"role": "daemon", "pid": 1}
    ) == ""


def test_a_cleanly_released_lock_is_not_announced_as_a_reclaim(tmp_path):
    # Releasing truncates the file, so an empty one is the most ordinary thing
    # that happens here - and every restart would otherwise print a warning.
    lock = tmp_path / "pad.lock"
    lock.write_text("")
    assert staleness.reclaim_stale_lock(path=lock, holder=lambda: None) == ""


def test_no_lock_file_means_nothing_to_say(tmp_path):
    assert staleness.reclaim_stale_lock(
        path=tmp_path / "absent.lock", holder=lambda: None
    ) == ""


# ---------------------------------------------------------------------------
# Reloading the config in place
# ---------------------------------------------------------------------------

def _config(tmp_path, text="{}"):
    path = tmp_path / "keymap.json"
    path.write_text(text)
    return path


def test_an_unchanged_config_is_never_reloaded(tmp_path):
    watcher = staleness.ConfigWatcher(
        path=_config(tmp_path), loader=lambda: "loaded", interval=0.0
    )
    assert watcher.poll() is None
    assert watcher.poll() is None


def test_an_edited_config_comes_back_loaded(tmp_path):
    path = _config(tmp_path)
    watcher = staleness.ConfigWatcher(
        path=path, loader=lambda: "the new config", interval=0.0
    )
    path.write_text('{"bindings": {}}')
    os.utime(path, (2_000, 2_000))
    change = watcher.poll()
    assert change is not None and change.ok and change.config == "the new config"


def test_a_config_that_will_not_parse_leaves_the_running_one_alone(tmp_path):
    path = _config(tmp_path)

    def loader():
        raise ValueError("line 1: not JSON")

    watcher = staleness.ConfigWatcher(path=path, loader=loader, interval=0.0)
    path.write_text("{ broken")
    os.utime(path, (2_000, 2_000))
    change = watcher.poll()
    assert change is not None and not change.ok
    assert "not JSON" in change.error
    # Said once, not on every tick until it is fixed.
    assert watcher.poll() is None


def test_a_config_created_while_the_bridge_runs_is_picked_up(tmp_path):
    # Watching a path that does not exist yet is the point: `freemicro run`
    # on the built-in defaults must notice the moment you write your own.
    path = tmp_path / "keymap.json"
    watcher = staleness.ConfigWatcher(
        path=path, loader=lambda: "yours now", interval=0.0
    )
    assert watcher.poll() is None
    path.write_text("{}")
    change = watcher.poll()
    assert change is not None and change.config == "yours now"


def test_the_config_is_stat_ed_on_its_own_clock_not_every_tick(tmp_path):
    path = _config(tmp_path)
    clock = FakeClock()
    watcher = staleness.ConfigWatcher(
        path=path, loader=lambda: "new", clock=clock, interval=5.0
    )
    assert watcher.poll() is None       # nothing changed, and now we wait
    path.write_text('{"a": 1}')
    os.utime(path, (2_000, 2_000))
    assert watcher.poll() is None       # too soon to look again
    clock.advance(5.1)
    assert watcher.poll() is not None


def test_an_explicit_config_flag_is_what_gets_watched(tmp_path):
    chosen = tmp_path / "elsewhere.json"
    assert staleness.config_watch_path(None, override=chosen) == chosen


def test_with_no_config_anywhere_we_watch_where_one_would_go():
    from freemicro import padconfig

    assert staleness.config_watch_path(None) == padconfig.user_path()


# ---------------------------------------------------------------------------
# The self-restart
# ---------------------------------------------------------------------------

class FakeClock:
    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class Recorder:
    """Stands in for ``os.execv``, which would not return."""

    def __init__(self) -> None:
        self.calls = []

    def __call__(self, executable, argv):
        self.calls.append((executable, list(argv)))


def _watcher(clock, mtime, **kwargs):
    kwargs.setdefault("verify", lambda: (True, ""))
    kwargs.setdefault("exec_", Recorder())
    kwargs.setdefault("argv", ["freemicro", "run"])
    kwargs.setdefault("environ", {})
    kwargs.setdefault("executable", "/usr/bin/python3")
    kwargs.setdefault("started_at", 900.0)
    kwargs.setdefault("check_interval", 0.0)
    return staleness.CodeWatcher(clock=clock, mtime=mtime, **kwargs)


def test_code_older_than_the_process_is_never_a_restart():
    clock = FakeClock()
    watcher = _watcher(clock, lambda: 500.0)
    assert watcher.poll() is None
    assert not watcher.pending


def test_a_tree_still_being_written_is_left_to_settle():
    clock = FakeClock()
    mtimes = iter([950.0, 951.0, 952.0])
    watcher = _watcher(clock, lambda: next(mtimes))
    # A `pip install` writes many files. Restarting into a half-written tree
    # turns an update into an outage.
    assert watcher.poll() is None
    clock.advance(1.0)
    assert watcher.poll() is None
    clock.advance(1.0)
    assert watcher.poll() is None
    assert not watcher.pending


def test_a_settled_update_restarts_and_says_why():
    clock = FakeClock()
    watcher = _watcher(clock, lambda: 950.0)
    assert watcher.poll() is None  # first sighting: start the settle timer
    clock.advance(staleness.SETTLE_SECONDS + 0.1)
    decision = watcher.poll()
    assert decision is not None and decision.restart
    assert decision.reason == staleness.REASON_STALE_CODE
    assert "restarting" in decision.message
    assert watcher.pending


def test_a_decision_is_announced_once_not_every_tick():
    clock = FakeClock()
    watcher = _watcher(clock, lambda: 950.0)
    watcher.poll()
    clock.advance(staleness.SETTLE_SECONDS + 0.1)
    assert watcher.poll().restart
    clock.advance(10.0)
    assert watcher.poll() is None


def test_code_that_will_not_import_is_never_restarted_into():
    clock = FakeClock()
    watcher = _watcher(
        clock,
        lambda: 950.0,
        verify=lambda: (False, "SyntaxError: bad"),
    )
    watcher.poll()
    clock.advance(staleness.SETTLE_SECONDS + 0.1)
    decision = watcher.poll()
    assert decision is not None and not decision.restart
    assert decision.reason == staleness.REASON_VERIFY_FAILED
    assert "SyntaxError: bad" in decision.message
    assert not watcher.pending
    # And it does not nag about the same broken tree on every tick.
    clock.advance(60.0)
    assert watcher.poll() is None


def test_a_repaired_tree_is_tried_again():
    clock = FakeClock()
    verdicts = iter([(False, "boom"), (True, "")])
    mtimes = iter([950.0, 950.0, 970.0, 970.0])
    watcher = _watcher(
        clock, lambda: next(mtimes), verify=lambda: next(verdicts)
    )
    watcher.poll()
    clock.advance(staleness.SETTLE_SECONDS + 0.1)
    assert not watcher.poll().restart
    watcher.poll()  # new mtime: a fresh edit, so a fresh chance
    clock.advance(staleness.SETTLE_SECONDS + 0.1)
    assert watcher.poll().restart


def test_the_loop_guard_stops_a_restart_loop_and_says_so():
    clock = FakeClock()
    env = {staleness.RESTART_ENV: f"{staleness.MAX_RESTARTS}:{clock.now - 10}"}
    watcher = _watcher(clock, lambda: 950.0, environ=env)
    watcher.poll()
    clock.advance(staleness.SETTLE_SECONDS + 0.1)
    decision = watcher.poll()
    assert decision is not None and not decision.restart
    assert decision.reason == staleness.REASON_LOOP_GUARD
    assert "restarted itself" in decision.message
    assert not watcher.pending


def test_the_loop_guard_forgets_restarts_older_than_its_window():
    clock = FakeClock()
    env = {
        staleness.RESTART_ENV: (
            f"{staleness.MAX_RESTARTS}:{clock.now - staleness.RESTART_WINDOW - 1}"
        )
    }
    watcher = _watcher(clock, lambda: 950.0, environ=env)
    watcher.poll()
    clock.advance(staleness.SETTLE_SECONDS + 0.1)
    assert watcher.poll().restart


def test_restarting_re_execs_with_the_same_argv_so_flags_survive():
    clock = FakeClock()
    recorder = Recorder()
    env = {}
    watcher = _watcher(
        clock,
        lambda: 950.0,
        exec_=recorder,
        environ=env,
        argv=["freemicro", "run", "--verbose"],
    )
    watcher.restart()
    assert recorder.calls == [
        ("/usr/bin/python3", ["/usr/bin/python3", "freemicro", "run", "--verbose"])
    ]
    assert env[staleness.RESTART_ENV].startswith("1:")


def test_the_device_is_released_before_the_process_is_replaced():
    # A re-exec that leaves the IOKit input-report callback scheduled is the
    # exact shape that has segfaulted this project before.
    order = []
    recorder = Recorder()

    def exec_(executable, argv):
        order.append("exec")
        recorder(executable, argv)

    watcher = _watcher(FakeClock(), lambda: 950.0, exec_=exec_)
    watcher.restart(release=lambda: order.append("release"))
    assert order == ["release", "exec"]


def test_a_failure_while_releasing_never_blocks_the_restart():
    recorder = Recorder()

    def boom():
        raise RuntimeError("the pad went away mid-teardown")

    watcher = _watcher(FakeClock(), lambda: 950.0, exec_=recorder)
    watcher.restart(release=boom)
    assert recorder.calls  # a wedged process is worse than a rough teardown


def test_the_restart_count_survives_the_exec():
    clock = FakeClock()
    env = {}
    for expected in ("1:", "2:", "3:"):
        _watcher(clock, lambda: 950.0, environ=env).restart()
        assert env[staleness.RESTART_ENV].startswith(expected)


def test_verification_runs_in_a_fresh_interpreter():
    # The running process has every module cached, so it is structurally
    # unable to answer this about itself.
    seen = []

    def runner(command):
        seen.append(command)
        return 0, ""

    ok, detail = staleness.verify_new_code(runner=runner)
    assert ok and detail == ""
    assert seen[0][1:] == ["-c", "import freemicro.cli"]


def test_verification_reports_the_last_line_of_the_failure():
    ok, detail = staleness.verify_new_code(
        runner=lambda cmd: (1, "Traceback…\nImportError: no module named x\n")
    )
    assert not ok and detail == "ImportError: no module named x"


def test_verification_treats_its_own_failure_as_a_refusal():
    def runner(command):
        raise OSError("no interpreter")

    ok, detail = staleness.verify_new_code(runner=runner)
    assert not ok and "no interpreter" in detail
