"""Taking FreeMicro back off a machine.

The bar here is higher than "the files are gone". An uninstall is the one
command a user runs when they have already decided they are unhappy, and the
three ways it can make that worse are all tested below: deleting a config they
wanted to keep, claiming success it did not achieve, and leaving the pad glowing
with nothing installed that could ever turn it off.

Nothing in this file touches the real ``~/.freemicro``, the real Claude Code
settings, the real LaunchAgents folder or ``launchctl``. The autouse fixture
below redirects every one of them.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from freemicro import cli
from freemicro import uninstall as un


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def home(tmp_path, monkeypatch):
    """An isolated ``~/.freemicro``, empty to start with."""
    path = tmp_path / "freemicro"
    path.mkdir()
    monkeypatch.setenv("FREEMICRO_HOME", str(path))
    return path


@pytest.fixture
def settings(tmp_path):
    return tmp_path / "claude" / "settings.json"


@pytest.fixture(autouse=True)
def no_launchd(tmp_path, monkeypatch):
    """No plist in the real LaunchAgents folder, and no ``launchctl``, ever."""
    from freemicro import daemon

    plist = tmp_path / "LaunchAgents" / f"{daemon.LABEL}.plist"
    monkeypatch.setattr(daemon, "plist_path", lambda: plist)
    monkeypatch.setattr(
        daemon, "launchctl_state",
        lambda: {"loaded": False, "pid": None, "last_exit": None, "raw": ""},
    )

    def _uninstall():
        existed = plist.exists()
        if existed:
            plist.unlink()
        return {"ok": True, "existed": existed, "removed": existed, "error": ""}

    monkeypatch.setattr(daemon, "uninstall", _uninstall)
    return plist


@pytest.fixture(autouse=True)
def no_ps(monkeypatch):
    """``is_freemicro_process`` never shells out during tests."""
    monkeypatch.setattr(un, "_command_of", lambda pid: "")


@pytest.fixture(autouse=True)
def no_inherited_env(monkeypatch):
    """Both redirect env vars point at real files on the developer's machine."""
    monkeypatch.delenv(un.ENV_HOOK_LOG, raising=False)
    monkeypatch.delenv(un.ENV_KEYMAP, raising=False)


def _populate(home: Path, extras: bool = True) -> None:
    """Write one of everything FreeMicro is known to leave behind."""
    (home / "keymap.json").write_text('{"version": 1, "bindings": {}}\n')
    (home / "keymap.json.bak").write_text('{"version": 1, "bindings": {}}\n')
    (home / "config.json").write_text("{}\n")
    (home / "layouts").mkdir()
    (home / "layouts" / "work.json").write_text("{}\n")
    (home / "state").mkdir()
    (home / "state" / "abc.json").write_text("{}\n")
    (home / "logs").mkdir()
    (home / "logs" / "daemon.log").write_text("up\n")
    (home / "slots.json").write_text("{}\n")
    (home / "status.json").write_text("{}\n")
    (home / "pad.lock").write_text('{"pid": 1, "role": "run"}')
    (home / "menubar.lock").write_text("2")
    (home / "tk-probe.json").write_text("{}\n")
    (home / "hook-events.jsonl").write_text('{"at": 1}\n')
    if extras:
        (home / "something-old.json").write_text("{}\n")


def _install_hooks(settings: Path, with_someone_else: bool = True) -> None:
    from freemicro import hooks_install

    settings.parent.mkdir(parents=True, exist_ok=True)
    existing = {}
    if with_someone_else:
        existing = {
            "hooks": {
                "Stop": [{"hooks": [
                    {"type": "command", "command": "/usr/local/bin/say-done"}
                ]}]
            },
            "model": "opus",
        }
    settings.write_text(json.dumps(
        hooks_install.build_settings(existing, command="/opt/bin/freemicro hook")
    ))


def _keys(plan) -> set:
    return {item.key for item in plan.actions}


# ---------------------------------------------------------------------------
# the footprint, enumerated from the code that writes it
# ---------------------------------------------------------------------------

def test_plan_finds_every_file_freemicro_writes(home, settings):
    _populate(home)
    _install_hooks(settings)
    plan = un.plan(home=home, settings_path=settings)
    keys = _keys(plan)

    for name in un.CONFIG_FILES + un.STATE_FILES:
        assert f"config:{name}" in keys or f"state:{name}" in keys, name
    for name in un.CONFIG_DIRS + un.STATE_DIRS:
        assert f"config:{name}" in keys or f"state:{name}" in keys, name
    assert "hooks" in keys
    assert "home" in keys


def test_plan_names_files_it_does_not_recognise(home, settings):
    """A file we no longer know about is still a file we created."""
    _populate(home)
    plan = un.plan(home=home, settings_path=settings)
    leftover = [i for i in plan.actions if i.key == "state:extra:something-old.json"]
    assert leftover and leftover[0].path == home / "something-old.json"


def test_plan_reports_sizes_so_the_big_one_is_visible(home, settings):
    (home / "hook-events.jsonl").write_text("x" * (3 * 1024 * 1024))
    plan = un.plan(home=home, settings_path=settings)
    log = next(i for i in plan.actions if i.key == "state:hook-events.jsonl")
    assert "MB" in log.detail


def test_plan_finds_the_xdg_keymap(tmp_path, home, settings, monkeypatch):
    """The review's list missed this one; it is in ``padconfig.search_paths``."""
    xdg = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    keymap = xdg / "freemicro" / "keymap.json"
    keymap.parent.mkdir(parents=True)
    keymap.write_text('{"version": 1, "bindings": {}}\n')
    plan = un.plan(home=home, settings_path=settings)
    assert "config:xdg_keymap" in _keys(plan)


def test_plan_finds_a_keymap_pointed_at_by_the_env_var(
    tmp_path, home, settings, monkeypatch
):
    elsewhere = tmp_path / "dotfiles" / "keymap.json"
    elsewhere.parent.mkdir(parents=True)
    elsewhere.write_text('{"version": 1, "bindings": {}}\n')
    monkeypatch.setenv(un.ENV_KEYMAP, str(elsewhere))
    plan = un.plan(home=home, settings_path=settings)
    assert "config:env_keymap" in _keys(plan)


def test_plan_finds_a_hook_log_written_outside_the_config_dir(
    tmp_path, home, settings, monkeypatch
):
    """``hook-events.jsonl`` only exists because of this variable."""
    log = tmp_path / "logs" / "hooks.jsonl"
    log.parent.mkdir(parents=True)
    log.write_text("{}\n")
    monkeypatch.setenv(un.ENV_HOOK_LOG, str(log))
    plan = un.plan(home=home, settings_path=settings)
    assert "state:hook_log" in _keys(plan)


def test_plan_includes_the_launchagent_when_one_exists(home, settings, no_launchd):
    no_launchd.parent.mkdir(parents=True, exist_ok=True)
    no_launchd.write_bytes(b"<plist/>")
    plan = un.plan(home=home, settings_path=settings)
    assert "launchagent" in _keys(plan)


# ---------------------------------------------------------------------------
# idempotency: the empty machine, and the second run
# ---------------------------------------------------------------------------

def test_nothing_installed_is_an_empty_plan(tmp_path, settings):
    plan = un.plan(home=tmp_path / "never-created", settings_path=settings)
    assert plan.empty
    assert not plan.actions


def test_running_it_twice_succeeds_and_finds_nothing_the_second_time(
    home, settings
):
    _populate(home)
    _install_hooks(settings)
    first = un.uninstall(un.plan(home=home, settings_path=settings))
    assert first.ok

    second_plan = un.plan(home=home, settings_path=settings)
    assert second_plan.empty
    second = un.uninstall(second_plan)
    assert second.ok
    assert not second.done


# ---------------------------------------------------------------------------
# removal
# ---------------------------------------------------------------------------

def test_uninstall_removes_the_whole_config_directory(home, settings):
    _populate(home)
    result = un.uninstall(un.plan(home=home, settings_path=settings))
    assert result.ok
    assert not home.exists()


def test_uninstall_removes_our_hooks_and_leaves_everyone_elses(home, settings):
    _install_hooks(settings, with_someone_else=True)
    un.uninstall(un.plan(home=home, settings_path=settings))
    data = json.loads(settings.read_text())
    commands = [
        hook["command"]
        for group in data["hooks"]["Stop"]
        for hook in group["hooks"]
    ]
    assert commands == ["/usr/local/bin/say-done"]
    assert data["model"] == "opus"  # nothing else in the file was touched


def test_uninstall_removes_the_xdg_keymap_and_its_empty_folder(
    tmp_path, home, settings, monkeypatch
):
    xdg = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    keymap = xdg / "freemicro" / "keymap.json"
    keymap.parent.mkdir(parents=True)
    keymap.write_text('{"version": 1, "bindings": {}}\n')
    un.uninstall(un.plan(home=home, settings_path=settings))
    assert not keymap.exists()
    assert not keymap.parent.exists()


def test_uninstall_stops_and_removes_the_launchagent(
    home, settings, no_launchd, monkeypatch
):
    from freemicro import daemon

    no_launchd.parent.mkdir(parents=True, exist_ok=True)
    no_launchd.write_bytes(b"<plist/>")
    monkeypatch.setattr(
        daemon, "launchctl_state",
        lambda: {"loaded": True, "pid": 4321, "last_exit": None, "raw": ""},
    )
    plan = un.plan(home=home, settings_path=settings)
    result = un.uninstall(plan)
    assert result.ok
    assert not no_launchd.exists()
    stopped = next(o for o in result.outcomes if o.key == "daemon_running")
    assert "launchd confirms" in stopped.message


# ---------------------------------------------------------------------------
# --keep-config
# ---------------------------------------------------------------------------

def test_keep_config_keeps_the_keymap_and_removes_the_state(home, settings):
    _populate(home)
    plan = un.plan(home=home, settings_path=settings, keep_config=True)
    un.uninstall(plan)

    for name in ("keymap.json", "keymap.json.bak", "config.json"):
        assert (home / name).exists(), name
    assert (home / "layouts" / "work.json").exists()

    for name in ("slots.json", "status.json", "pad.lock", "menubar.lock",
                 "tk-probe.json", "hook-events.jsonl"):
        assert not (home / name).exists(), name
    assert not (home / "state").exists()
    assert not (home / "logs").exists()
    assert home.exists()  # the folder stays, because things are still in it


def test_keep_config_lists_exactly_what_it_kept(home, settings):
    _populate(home)
    plan = un.plan(home=home, settings_path=settings, keep_config=True)
    kept = {i.path for i in plan.kept}
    assert home / "keymap.json" in kept
    assert home / "layouts" in kept
    assert home / "state" not in kept


def test_keep_config_still_removes_the_hooks_and_the_daemon(
    home, settings, no_launchd
):
    """Keeping a keymap is not the same as staying installed."""
    _install_hooks(settings, with_someone_else=False)
    no_launchd.parent.mkdir(parents=True, exist_ok=True)
    no_launchd.write_bytes(b"<plist/>")
    _populate(home)
    un.uninstall(un.plan(home=home, settings_path=settings, keep_config=True))
    assert "hooks" not in json.loads(settings.read_text())
    assert not no_launchd.exists()


# ---------------------------------------------------------------------------
# partial failure must not lie
# ---------------------------------------------------------------------------

def test_one_file_it_cannot_remove_is_named_and_the_rest_still_go(
    home, settings, monkeypatch
):
    _populate(home)
    plan = un.plan(home=home, settings_path=settings)
    real = un._remove_path

    def _stubborn(path: Path):
        if path.name == "slots.json":
            return False, f"could not remove {path}: Operation not permitted"
        return real(path)

    monkeypatch.setattr(un, "_remove_path", _stubborn)
    result = un.uninstall(plan)

    assert not result.ok
    assert [o.key for o in result.failures] == ["state:slots.json"]
    assert not (home / "status.json").exists()   # the rest went anyway
    assert not (home / "state").exists()


def test_a_failure_is_never_rounded_up_to_success(home, settings, monkeypatch):
    _populate(home)
    monkeypatch.setattr(
        un, "_remove_path", lambda path: (False, f"nope: {path}")
    )
    result = un.uninstall(un.plan(home=home, settings_path=settings))
    assert not result.ok
    assert not result.done


def test_unwritable_settings_are_reported_not_swallowed(
    home, settings, monkeypatch
):
    _install_hooks(settings)
    from freemicro import hooks_install

    def _boom(**_kwargs):
        raise OSError("read-only file system")

    monkeypatch.setattr(hooks_install, "uninstall_hooks", _boom)
    result = un.uninstall(un.plan(home=home, settings_path=settings))
    failure = next(o for o in result.failures if o.key == "hooks")
    assert "read-only file system" in failure.message


# ---------------------------------------------------------------------------
# stopping what is running
# ---------------------------------------------------------------------------

def test_lock_is_held_is_answered_by_trying_the_lock(home):
    from freemicro.daemon import PadLock

    lock = home / "pad.lock"
    lock.write_text('{"pid": 99999, "role": "run"}')
    # The file exists and names a pid, and nobody holds it.
    assert not un.lock_is_held(lock)

    held = PadLock(path=lock, role="run")
    assert held.acquire()
    try:
        assert un.lock_is_held(lock)
    finally:
        held.release()


def test_a_stale_lock_is_not_treated_as_a_running_process(home, settings):
    """A pid in a file nobody is holding must never be signalled."""
    _populate(home)
    plan = un.plan(home=home, settings_path=settings)
    assert "pad_holder" not in _keys(plan)
    assert "menubar_running" not in _keys(plan)


def test_stop_pid_refuses_a_process_that_is_not_freemicro(monkeypatch):
    monkeypatch.setattr(un, "_command_of", lambda pid: "/usr/bin/postgres -D /db")
    killed = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: killed.append(pid))
    ok, message = un.stop_pid(4242, held=lambda: True)
    assert not ok
    assert killed == []
    assert "not a FreeMicro process" in message


def test_stop_pid_sigterms_and_waits_for_the_lock_to_free(monkeypatch):
    import signal as signal_mod

    monkeypatch.setattr(un, "_command_of", lambda pid: "/opt/bin/freemicro run")
    sent = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: sent.append((pid, sig)))
    states = [True, True, False]
    ok, message = un.stop_pid(
        321, held=lambda: states.pop(0), sleep=lambda _s: None
    )
    assert ok
    assert sent == [(321, signal_mod.SIGTERM)]
    assert "321" in message


def test_stop_pid_gives_up_out_loud_rather_than_escalating(monkeypatch):
    monkeypatch.setattr(un, "_command_of", lambda pid: "/opt/bin/freemicro run")
    signals = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: signals.append(sig))
    ok, message = un.stop_pid(
        7, held=lambda: True, timeout=0.0, sleep=lambda _s: None
    )
    assert not ok
    assert len(signals) == 1  # SIGTERM only; never a SIGKILL
    assert "kill 7" in message


def test_a_holder_that_will_not_stop_blocks_the_led_blank_rather_than_lying(
    home, settings, monkeypatch
):
    from freemicro.daemon import PadLock

    _write_lit_keymap(home)
    lock = PadLock(path=home / "pad.lock", role="run")
    assert lock.acquire()
    monkeypatch.setattr(un, "_command_of", lambda pid: "")
    try:
        plan = un.plan(home=home, settings_path=settings)
        assert "leds" in _keys(plan)
        result = un.uninstall(plan, sleep=lambda _s: None)
        leds = next(o for o in result.outcomes if o.key == "leds")
        assert not leds.ok
        assert "still has the pad" in leds.message
    finally:
        lock.release()


# ---------------------------------------------------------------------------
# the LEDs
# ---------------------------------------------------------------------------

def _write_lit_keymap(home: Path) -> None:
    (home / "keymap.json").write_text(json.dumps({
        "version": 1,
        "bindings": {"AG00": {"action": "focus_session"}},
        "lighting": {
            "enabled": True,
            "zones": ["agent_keys", "underglow"],
            "on_exit": "breath",
        },
    }))


class _FakeDevice:
    def __init__(self):
        self.sent = []

    def send(self, message):
        self.sent.append(message)


def test_lighting_off_means_the_leds_are_left_alone(home, settings):
    (home / "keymap.json").write_text(json.dumps({
        "version": 1, "bindings": {}, "lighting": {"enabled": False},
    }))
    plan = un.plan(home=home, settings_path=settings)
    assert "leds" not in _keys(plan)
    outcome = un.restore_pad()
    assert not outcome["attempted"]
    assert outcome["ok"]


def test_uninstalling_with_lighting_on_blanks_the_pad(home):
    _write_lit_keymap(home)
    device = _FakeDevice()
    outcome = un.restore_pad(device=device)
    assert outcome["attempted"] and outcome["ok"]
    assert device.sent, "nothing reached the pad"


def test_the_blank_ignores_on_exit_breath_and_really_goes_dark(home):
    """An uninstall is not the moment to leave the pad pulsing."""
    _write_lit_keymap(home)   # on_exit: breath
    device = _FakeDevice()
    un.restore_pad(device=device)
    payload = json.dumps(device.sent)
    assert '"e": 0' in payload or '"e":0' in payload
    assert '"b": 0' in payload or '"b":0' in payload


def test_a_pad_it_cannot_reach_is_a_failure_and_says_what_to_do(home, settings):
    """``ENV_NO_DEVICE`` is set for the whole suite, so no pad is reachable."""
    _write_lit_keymap(home)
    outcome = un.restore_pad()
    assert outcome["attempted"] and not outcome["ok"]

    result = un.uninstall(un.plan(home=home, settings_path=settings))
    leds = next(o for o in result.outcomes if o.key == "leds")
    assert not leds.ok
    assert "lights --disable" in leds.message
    assert not result.ok


def test_the_pad_is_blanked_before_anything_is_deleted(home, settings, monkeypatch):
    """Order is the whole design: the config must still be readable."""
    order = []
    real_restore = un.restore_pad

    def _restore(**kwargs):
        order.append(("blank", (home / "keymap.json").exists()))
        return real_restore(device=_FakeDevice())

    monkeypatch.setattr(un, "restore_pad", _restore)
    real_remove = un._remove_path
    monkeypatch.setattr(
        un, "_remove_path",
        lambda path: (order.append(("remove", path.name)), real_remove(path))[1],
    )
    _write_lit_keymap(home)
    un.uninstall(un.plan(home=home, settings_path=settings))
    assert order[0] == ("blank", True)


# ---------------------------------------------------------------------------
# what it cannot do
# ---------------------------------------------------------------------------

def test_it_says_where_the_permissions_are_and_how_to_remove_the_package():
    text = "\n".join(un.cannot_remove())
    assert "Input Monitoring" in text
    assert "Accessibility" in text
    assert "Privacy & Security" in text
    assert "pipx uninstall freemicro" in text
    assert "pip uninstall freemicro" in text


def test_it_mentions_the_env_var_when_one_is_set(monkeypatch, tmp_path):
    monkeypatch.setenv(un.ENV_HOOK_LOG, str(tmp_path / "log.jsonl"))
    assert any(un.ENV_HOOK_LOG in line for line in un.cannot_remove())


# ---------------------------------------------------------------------------
# the command
# ---------------------------------------------------------------------------

def test_dry_run_prints_the_list_and_touches_nothing(
    home, settings, capsys, monkeypatch
):
    _populate(home)
    _install_hooks(settings)
    before = json.loads(settings.read_text())
    code = cli.main(["uninstall", "--dry-run", "--settings", str(settings)])
    out = capsys.readouterr().out
    assert code == 0
    assert "Dry run" in out
    assert str(home / "slots.json") in out
    assert (home / "slots.json").exists()
    assert json.loads(settings.read_text()) == before


def test_it_refuses_to_delete_anything_unattended_without_yes(
    home, settings, capsys, monkeypatch
):
    _populate(home)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    code = cli.main(["uninstall", "--settings", str(settings)])
    assert code == 1
    assert "Refusing" in capsys.readouterr().err
    assert (home / "slots.json").exists()


def test_answering_no_leaves_everything_alone(home, settings, capsys, monkeypatch):
    _populate(home)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _prompt: "n")
    code = cli.main(["uninstall", "--settings", str(settings)])
    assert code == 1
    assert "Left everything alone" in capsys.readouterr().out
    assert (home / "keymap.json").exists()


def test_yes_removes_it_and_reports_every_step(home, settings, capsys):
    _populate(home)
    _install_hooks(settings)
    code = cli.main(["uninstall", "--yes", "--settings", str(settings)])
    out = capsys.readouterr().out
    assert code == 0
    assert not home.exists()
    assert "Done" in out
    assert "Restart Claude Code" in out
    assert "pipx uninstall freemicro" in out


def test_nothing_installed_says_so_plainly(tmp_path, settings, capsys, monkeypatch):
    monkeypatch.setenv("FREEMICRO_HOME", str(tmp_path / "gone"))
    code = cli.main(["uninstall", "--yes", "--settings", str(settings)])
    out = capsys.readouterr().out
    assert code == 0
    assert "Nothing to remove" in out


def test_the_command_reports_a_partial_failure_as_a_failure(
    home, settings, capsys, monkeypatch
):
    _populate(home)
    monkeypatch.setattr(
        un, "_remove_path",
        lambda path: (False, "could not remove it") if path.name == "state"
        else (True, "removed"),
    )
    code = cli.main(["uninstall", "--yes", "--settings", str(settings)])
    out = capsys.readouterr().out
    assert code == 1
    assert "FAIL" in out
    assert "did not finish" in out
    assert "Done -" not in out


def test_keep_config_is_stated_in_the_output(home, settings, capsys):
    _populate(home)
    code = cli.main(
        ["uninstall", "--yes", "--keep-config", "--settings", str(settings)]
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "Kept" in out
    assert str(home / "keymap.json") in out
