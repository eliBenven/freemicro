"""The installer that wires FreeMicro into Claude Code.

Every failure here is silent from the outside - the LEDs just never change -
so the interesting cases are all about *what exactly* lands in someone else's
settings file: an absolute quoted path, one entry per event no matter how many
times you run it, a repaired entry when the venv moves, and other people's
hooks untouched throughout.
"""

from __future__ import annotations

import json
import shlex

import pytest

from freemicro import hooks_install as hi


@pytest.fixture
def settings(tmp_path):
    return tmp_path / "settings.json"


def _command(path):
    return hi.status(path)["expected_command"]


# -- command resolution -----------------------------------------------------

def test_hook_command_is_absolute_and_quoted(monkeypatch, tmp_path):
    # A venv in a directory with a space in it is the classic silent failure:
    # the shell splits the path and the hook "runs" something that isn't there.
    home = tmp_path / "My Tools" / "venv" / "bin"
    home.mkdir(parents=True)
    script = home / "freemicro"
    script.write_text("#!/bin/sh\n")
    script.chmod(0o755)
    monkeypatch.setattr("sys.argv", [str(script), "install"])

    command = hi.hook_command()
    parts = shlex.split(command)
    assert parts == [str(script), "hook"]
    assert " " in command  # it really does contain the awkward path


def test_hook_command_prefers_the_binary_you_typed(monkeypatch, tmp_path):
    typed = tmp_path / "typed" / "freemicro"
    typed.parent.mkdir(parents=True)
    typed.write_text("#!/bin/sh\n")
    typed.chmod(0o755)
    monkeypatch.setattr("sys.argv", [str(typed), "install"])
    monkeypatch.setattr(hi.shutil, "which", lambda _n: "/somewhere/else/freemicro")
    assert shlex.split(hi.hook_command())[0] == str(typed)


def test_hook_command_falls_back_to_module_form(monkeypatch, tmp_path):
    monkeypatch.setattr("sys.argv", ["pytest"])
    monkeypatch.setattr("sys.executable", str(tmp_path / "python"))
    monkeypatch.setattr(hi.shutil, "which", lambda _n: None)
    command = hi.hook_command()
    assert shlex.split(command) == [str(tmp_path / "python"), "-m", "freemicro", "hook"]
    assert hi._is_ours(command)


# -- ownership detection ----------------------------------------------------

@pytest.mark.parametrize("command,ours", [
    ("/opt/venv/bin/freemicro hook", True),
    ("'/My Tools/bin/freemicro' hook", True),
    ("/usr/bin/python3 -m freemicro hook", True),
    ("/opt/venv/bin/freemicro emit done", False),   # someone's own binding
    ("/usr/local/bin/other-tool hook", False),      # not ours at all
    ("", False),
])
def test_is_ours(command, ours):
    assert hi._is_ours(command) is ours


# -- install ----------------------------------------------------------------

def test_install_creates_every_event(settings):
    hi.install_hooks(settings)
    data = json.loads(settings.read_text())
    assert set(data["hooks"]) == set(hi.HOOK_EVENTS)
    for event in hi.HOOK_EVENTS:
        hook = data["hooks"][event][0]["hooks"][0]
        assert hook["type"] == "command"
        assert hook["timeout"] == hi.HOOK_TIMEOUT_SECONDS
        assert hook["command"] == _command(settings)


def test_install_is_idempotent(settings):
    hi.install_hooks(settings)
    hi.install_hooks(settings)
    hi.install_hooks(settings)
    data = json.loads(settings.read_text())
    for event in hi.HOOK_EVENTS:
        assert len(data["hooks"][event]) == 1


def test_install_repairs_a_moved_binary(settings):
    hi.install_hooks(settings, command="/old/venv/bin/freemicro hook")
    hi.install_hooks(settings)
    data = json.loads(settings.read_text())
    commands = {
        data["hooks"][e][0]["hooks"][0]["command"] for e in hi.HOOK_EVENTS
    }
    assert commands == {_command(settings)}
    # Repaired in place, not duplicated.
    assert len(data["hooks"]["Stop"]) == 1


def test_install_leaves_other_hooks_alone(settings):
    settings.write_text(json.dumps({
        "permissions": {"allow": ["Bash"]},
        "hooks": {
            "Stop": [{"matcher": "", "hooks": [
                {"type": "command", "command": "say done"},
            ]}],
            "PreToolUse": [{"matcher": "Bash", "hooks": [
                {"type": "command", "command": "/usr/local/bin/lint"},
            ]}],
        },
    }))
    hi.install_hooks(settings)
    data = json.loads(settings.read_text())
    assert data["permissions"] == {"allow": ["Bash"]}
    stop_commands = [
        h["command"] for g in data["hooks"]["Stop"] for h in g["hooks"]
    ]
    assert "say done" in stop_commands
    assert _command(settings) in stop_commands
    assert data["hooks"]["PreToolUse"][0]["matcher"] == "Bash"


def test_install_dry_run_writes_nothing(settings):
    rendered = hi.install_hooks(settings, dry_run=True)
    assert not settings.exists()
    assert "freemicro" in rendered


def test_install_survives_a_corrupt_settings_file(settings):
    settings.write_text("{ this is not json")
    hi.install_hooks(settings)
    data = json.loads(settings.read_text())
    assert set(data["hooks"]) == set(hi.HOOK_EVENTS)


# -- uninstall --------------------------------------------------------------

def test_uninstall_removes_only_ours(settings):
    settings.write_text(json.dumps({
        "theme": "dark",
        "hooks": {"Stop": [{"hooks": [
            {"type": "command", "command": "say done"},
        ]}]},
    }))
    hi.install_hooks(settings)
    _path, removed = hi.uninstall_hooks(settings)
    assert removed == len(hi.HOOK_EVENTS)
    data = json.loads(settings.read_text())
    assert data["theme"] == "dark"
    assert data["hooks"] == {"Stop": [{"hooks": [
        {"type": "command", "command": "say done"},
    ]}]}


def test_uninstall_prunes_the_hooks_key_it_created(settings):
    hi.install_hooks(settings)
    hi.uninstall_hooks(settings)
    assert "hooks" not in json.loads(settings.read_text())


def test_uninstall_is_safe_when_nothing_is_installed(settings):
    settings.write_text(json.dumps({"theme": "dark"}))
    _path, removed = hi.uninstall_hooks(settings)
    assert removed == 0
    assert json.loads(settings.read_text()) == {"theme": "dark"}


# -- status -----------------------------------------------------------------

def test_status_reports_not_installed(settings):
    state = hi.status(settings)
    assert state["installed"] is False
    assert state["events"] == []
    assert state["missing_events"] == list(hi.HOOK_EVENTS)


def test_status_spots_a_partial_install(settings):
    hi.install_hooks(settings)
    data = json.loads(settings.read_text())
    del data["hooks"]["Stop"]
    settings.write_text(json.dumps(data))
    state = hi.status(settings)
    assert state["partial"] is True
    assert state["missing_events"] == ["Stop"]


def test_status_spots_a_stale_command(settings):
    hi.install_hooks(settings, command="/gone/bin/freemicro hook")
    state = hi.status(settings)
    assert state["stale_commands"] == ["/gone/bin/freemicro hook"]
    assert state["binary_exists"] is False
