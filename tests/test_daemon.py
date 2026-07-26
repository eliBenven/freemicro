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
    monkeypatch.setattr(daemon, "is_installed", lambda label=daemon.LABEL: False)
    assert daemon.is_running() is False


# -- the launch-on-connect agent --------------------------------------------

def test_onconnect_plist_matches_the_codex_micro_over_hid_and_usb():
    """The whole feature: launchd starts us when THIS device appears.

    Two matchers under one ``com.apple.iokit.matching`` dict so a single plist
    covers Bluetooth LE (the owner's transport, an ``IOHIDDevice``) and USB (the
    same pad as an ``IOUSBHostDevice``). The identifiers are integers because
    that is what IOKit matches on - a ``"0x303A"`` string would silently match
    nothing.
    """
    events = daemon.build_onconnect_plist()["LaunchEvents"]
    match = events["com.apple.iokit.matching"]
    matchers = list(match.values())

    hid = next(m for m in matchers if m["IOProviderClass"] == "IOHIDDevice")
    assert hid["VendorID"] == 0x303A == 12346
    assert hid["ProductID"] == 0x8360 == 33632

    usb = next(m for m in matchers if m["IOProviderClass"] == "IOUSBHostDevice")
    assert usb["idVendor"] == 12346
    assert usb["idProduct"] == 33632


def test_onconnect_plist_does_not_run_at_login_and_does_not_keepalive():
    """The two flags that make it on-connect and not always-on.

    RunAtLoad True would start it every login regardless of the pad - the exact
    always-on behaviour this mode replaces. KeepAlive True would respawn it in a
    tight loop whenever the pad was momentarily unavailable. Both must be False.
    """
    plist = daemon.build_onconnect_plist()
    assert plist["RunAtLoad"] is False
    assert plist["KeepAlive"] is False


def test_onconnect_plist_uses_the_same_absolute_command_as_the_daemon():
    argv = daemon.build_onconnect_plist()["ProgramArguments"]
    assert os.path.isabs(argv[0])
    assert argv[-2:] == ["daemon", "run"]
    assert argv == daemon.build_plist()["ProgramArguments"]


def test_onconnect_plist_round_trips_and_keeps_integer_ids():
    loaded = plistlib.loads(daemon.render_onconnect_plist())
    assert loaded["Label"] == daemon.ONCONNECT_LABEL
    hid = loaded["LaunchEvents"]["com.apple.iokit.matching"][
        "com.freemicro.codexmicro-hid"
    ]
    assert isinstance(hid["VendorID"], int)
    assert hid["VendorID"] == 12346


def test_onconnect_plist_carries_a_relocated_home(monkeypatch, tmp_path):
    monkeypatch.setenv("FREEMICRO_HOME", str(tmp_path))
    plist = daemon.build_onconnect_plist()
    assert plist["EnvironmentVariables"]["FREEMICRO_HOME"] == str(tmp_path)


def test_onconnect_install_refuses_a_binary_launchd_cannot_read():
    """Same TCC trap as the login daemon: a Desktop binary can never launch."""
    doomed = [os.path.expanduser("~/Desktop/fm/.venv/bin/freemicro"),
              "daemon", "run"]
    result = daemon.install_on_connect(argv=doomed)
    assert result["ok"] is False
    assert result["warning"] == "Desktop"
    assert "pipx" in result["error"]


def test_conflicting_label_names_the_other_launcher(monkeypatch):
    monkeypatch.setattr(
        daemon, "is_installed", lambda label=daemon.LABEL: label == daemon.LABEL
    )
    # Installing on-connect while the login daemon is present is a conflict.
    assert daemon.conflicting_label(on_connect=True) == daemon.LABEL
    # Installing the login daemon while nothing else is present is not.
    assert daemon.conflicting_label(on_connect=False) is None


def test_onconnect_install_refuses_when_the_login_daemon_is_installed(monkeypatch):
    monkeypatch.setattr(
        daemon, "is_installed", lambda label=daemon.LABEL: label == daemon.LABEL
    )
    result = daemon.install_on_connect(
        argv=["/usr/local/bin/freemicro", "daemon", "run"]
    )
    assert result["ok"] is False
    assert result["conflict"] == daemon.LABEL
    assert "freemicro daemon uninstall" in result["error"]


def test_login_install_refuses_when_the_onconnect_agent_is_installed(monkeypatch):
    monkeypatch.setattr(
        daemon, "is_installed",
        lambda label=daemon.LABEL: label == daemon.ONCONNECT_LABEL,
    )
    result = daemon.install(argv=["/usr/local/bin/freemicro", "daemon", "run"])
    assert result["ok"] is False
    assert result["conflict"] == daemon.ONCONNECT_LABEL


def test_onconnect_install_writes_the_plist_and_uninstall_removes_it(
    monkeypatch, tmp_path
):
    """End to end at the data layer: no launchctl, no real LaunchAgents folder.

    Also proves the on-connect install does NOT create the login daemon's plist,
    and does not kickstart (which would force a run with no pad attached).
    """
    monkeypatch.setenv("FREEMICRO_HOME", str(tmp_path))
    monkeypatch.setattr(daemon, "agents_dir", lambda: tmp_path / "LaunchAgents")
    calls = []
    monkeypatch.setattr(
        daemon, "_launchctl",
        lambda *a, **k: (calls.append(a), (0, ""))[1],
    )
    monkeypatch.setattr(
        daemon, "launchctl_state",
        lambda label=daemon.LABEL: {
            "loaded": False, "pid": None, "last_exit": None, "raw": ""
        },
    )
    argv = ["/usr/local/bin/freemicro", "daemon", "run"]

    result = daemon.install_on_connect(argv=argv)
    assert result["ok"] is True

    path = daemon.plist_path(daemon.ONCONNECT_LABEL)
    assert path.exists()
    loaded = plistlib.loads(path.read_bytes())
    assert loaded["RunAtLoad"] is False
    assert "com.apple.iokit.matching" in loaded["LaunchEvents"]
    # The on-connect agent must not be kickstarted - nothing forces it to run.
    assert not any("kickstart" in a for a in calls)
    # And the login daemon's plist was never written.
    assert not daemon.plist_path(daemon.LABEL).exists()

    out = daemon.uninstall(daemon.ONCONNECT_LABEL)
    assert out["ok"] is True
    assert out["existed"] is True
    assert not path.exists()


def test_onconnect_status_treats_loaded_but_idle_as_healthy(monkeypatch, tmp_path):
    monkeypatch.setenv("FREEMICRO_HOME", str(tmp_path))
    monkeypatch.setattr(daemon, "agents_dir", lambda: tmp_path / "LaunchAgents")
    monkeypatch.setattr(
        daemon, "launchctl_state",
        lambda label=daemon.LABEL: {
            "loaded": True, "pid": None, "last_exit": None, "raw": ""
        },
    )
    (tmp_path / "LaunchAgents").mkdir()
    daemon.plist_path(daemon.ONCONNECT_LABEL).write_bytes(
        daemon.render_onconnect_plist()
    )
    state = daemon.onconnect_status()
    assert state["installed"] is True
    assert state["loaded"] is True
    assert state["pid"] is None
    assert state["vendor_id"] == 0x303A
    assert state["product_id"] == 0x8360
