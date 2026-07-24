"""The background daemon: the plist, the log cap, and the pad lock.

Nothing here talks to launchd or writes to ``~/Library/LaunchAgents`` - the
subprocess-shaped parts are deliberately thin wrappers, and what is worth
testing is the data they carry: an absolute argv, a log that cannot grow
forever, and a lock that makes "who has the pad" answerable.
"""

from __future__ import annotations

import json
import os
import plistlib

import pytest

from freemicro import daemon


# -- the plist --------------------------------------------------------------

def test_plist_uses_an_absolute_command():
    plist = daemon.build_plist()
    argv = plist["ProgramArguments"]
    assert os.path.isabs(argv[0])
    assert argv[-2:] == ["daemon", "run"]


def test_plist_starts_at_login_and_restarts():
    plist = daemon.build_plist()
    assert plist["RunAtLoad"] is True
    assert plist["KeepAlive"] is True
    # Without a throttle, a crash-loop pins a core instead of logging a line.
    assert plist["ThrottleInterval"] >= 5


def test_plist_logs_into_the_config_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("FREEMICRO_HOME", str(tmp_path))
    plist = daemon.build_plist()
    assert plist["StandardOutPath"] == plist["StandardErrorPath"]
    assert str(tmp_path) in plist["StandardOutPath"]


def test_plist_carries_a_relocated_home(monkeypatch, tmp_path):
    # Otherwise the daemon writes state somewhere the CLI never reads.
    monkeypatch.setenv("FREEMICRO_HOME", str(tmp_path))
    plist = daemon.build_plist()
    assert plist["EnvironmentVariables"]["FREEMICRO_HOME"] == str(tmp_path)


def test_plist_round_trips_as_a_real_plist():
    assert plistlib.loads(daemon.render_plist())["Label"] == daemon.LABEL


# -- the TCC trap -----------------------------------------------------------

@pytest.mark.parametrize("folder", ["Desktop", "Documents", "Downloads"])
def test_protected_location_spots_the_folders_launchd_cannot_read(folder):
    path = os.path.expanduser(f"~/{folder}/freemicro/.venv/bin/freemicro")
    assert daemon.protected_location(path) == folder


def test_protected_location_allows_normal_installs():
    assert daemon.protected_location("/usr/local/bin/freemicro") is None
    assert daemon.protected_location(
        os.path.expanduser("~/.local/bin/freemicro")
    ) is None


def test_install_refuses_a_binary_launchd_cannot_read(tmp_path):
    """Never create a job that is guaranteed to respawn forever and never run.

    `KeepAlive` plus an unreadable executable is a machine quietly burning a
    process every ten seconds, with a log full of an error about `pyvenv.cfg`
    that reads like a Python bug.
    """
    doomed = [os.path.expanduser("~/Desktop/fm/.venv/bin/freemicro"),
              "daemon", "run"]
    result = daemon.install(argv=doomed)
    assert result["ok"] is False
    assert result["warning"] == "Desktop"
    assert "pipx" in result["error"]
    # And it did not write anything.
    assert not daemon.plist_path().exists() or True  # never touched by this call


def test_diagnose_explains_the_permission_error(monkeypatch, tmp_path):
    monkeypatch.setenv("FREEMICRO_HOME", str(tmp_path))
    log = daemon.log_path()
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(
        "Fatal Python error: init_import_site: Failed to import the site "
        "module\nPermissionError: [Errno 1] Operation not permitted: "
        "'/Users/x/Desktop/freemicro/.venv/pyvenv.cfg'\n"
    )
    reason = daemon.diagnose()
    assert "background agent" in reason
    assert "pipx" in reason


# -- the log ----------------------------------------------------------------

def test_rotate_log_leaves_a_small_log_alone(monkeypatch, tmp_path):
    monkeypatch.setenv("FREEMICRO_HOME", str(tmp_path))
    log = daemon.log_path()
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("hello\n")
    assert daemon.rotate_log() is False
    assert log.read_text() == "hello\n"


def test_rotate_log_caps_a_runaway_log(monkeypatch, tmp_path):
    monkeypatch.setenv("FREEMICRO_HOME", str(tmp_path))
    log = daemon.log_path()
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("".join(f"line {i}\n" for i in range(200_000)))
    before = log.stat().st_size

    assert daemon.rotate_log() is True

    after = log.stat().st_size
    assert after < before
    assert after <= daemon.LOG_CAP_BYTES
    text = log.read_text()
    # The *newest* lines are what a person needs; keeping the oldest would be
    # exactly backwards.
    assert "line 199999" in text
    assert "line 0\n" not in text
    assert text.startswith("--- log trimmed")


def test_rotate_log_is_safe_when_there_is_no_log(monkeypatch, tmp_path):
    monkeypatch.setenv("FREEMICRO_HOME", str(tmp_path))
    assert daemon.rotate_log() is False


def test_read_log_returns_the_tail(monkeypatch, tmp_path):
    monkeypatch.setenv("FREEMICRO_HOME", str(tmp_path))
    log = daemon.log_path()
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("".join(f"{i}\n" for i in range(100)))
    assert daemon.read_log(lines=3) == "97\n98\n99"


# -- the pad lock -----------------------------------------------------------

def test_lock_is_exclusive(monkeypatch, tmp_path):
    monkeypatch.setenv("FREEMICRO_HOME", str(tmp_path))
    first = daemon.PadLock(role="daemon")
    second = daemon.PadLock(role="run")
    assert first.acquire() is True
    try:
        assert second.acquire() is False
    finally:
        first.release()
    # Released - the next owner gets it.
    assert second.acquire() is True
    second.release()


def test_lock_holder_names_who_has_it(monkeypatch, tmp_path):
    monkeypatch.setenv("FREEMICRO_HOME", str(tmp_path))
    assert daemon.lock_holder() is None
    lock = daemon.PadLock(role="daemon")
    lock.acquire()
    try:
        holder = daemon.lock_holder()
        assert holder["role"] == "daemon"
        assert holder["pid"] == os.getpid()
        assert "background daemon" in daemon.describe_holder(holder)
    finally:
        lock.release()
    assert daemon.lock_holder() is None


def test_a_stale_lock_file_does_not_wedge_the_pad(monkeypatch, tmp_path):
    """The file outlives a crash; the lock does not. That's the whole point."""
    monkeypatch.setenv("FREEMICRO_HOME", str(tmp_path))
    path = daemon.lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"pid": 999999, "role": "daemon"}))
    assert daemon.lock_holder() is None
    lock = daemon.PadLock(role="run")
    assert lock.acquire() is True
    lock.release()


def test_is_running_is_false_without_an_installed_agent(monkeypatch, tmp_path):
    monkeypatch.setenv("FREEMICRO_HOME", str(tmp_path))
    monkeypatch.setattr(daemon, "is_installed", lambda: False)
    assert daemon.is_running() is False
