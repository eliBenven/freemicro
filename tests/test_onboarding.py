"""``freemicro start`` - the prompts, not the side effects.

The one property worth pinning down hard: **this can never hang and never
guesses dangerously.** Every prompt resolves to its default on a timeout, on a
pipe, and under ``--yes``; and the LED prompt's default stays *no* through all
three, because taking over someone's hardware is not a decision a flag should
make for them.
"""

from __future__ import annotations

import io
import types

import pytest

from freemicro import onboarding


def _args(**kwargs):
    base = {"yes": False, "lights": None, "hooks": None, "daemon": None}
    base.update(kwargs)
    return types.SimpleNamespace(**base)


# -- ask() ------------------------------------------------------------------

def test_non_interactive_takes_the_default(monkeypatch, capsys):
    monkeypatch.setattr(onboarding, "_interactive", lambda: False)
    assert onboarding.ask("Do the thing?", default=True) is True
    assert onboarding.ask("Do the risky thing?", default=False) is False
    assert "not a terminal" in capsys.readouterr().out


def test_yes_takes_the_default_not_a_yes(monkeypatch, capsys):
    """`--yes` means "don't ask me", not "say yes to everything".

    The LED opt-in defaults to no. If `--yes` overrode that, a scripted setup
    would quietly seize hardware the user never agreed to hand over.
    """
    monkeypatch.setattr(onboarding, "_interactive", lambda: True)
    assert onboarding.ask("Drive the LEDs?", default=False, assume_yes=True) is False
    assert onboarding.ask("Install hooks?", default=True, assume_yes=True) is True


def test_explicit_override_wins_over_everything(monkeypatch):
    monkeypatch.setattr(onboarding, "_interactive", lambda: True)
    assert onboarding.ask("x?", default=False, override=True) is True
    assert onboarding.ask("x?", default=True, override=False) is False


def test_a_timeout_falls_back_to_the_default(monkeypatch, capsys):
    monkeypatch.setattr(onboarding, "_interactive", lambda: True)
    monkeypatch.setattr(onboarding, "_read_line", lambda _t: None)
    assert onboarding.ask("Waiting?", default=True, timeout=0.01) is True
    assert "no answer" in capsys.readouterr().out


@pytest.mark.parametrize("typed,default,expected", [
    ("y\n", False, True),
    ("yes\n", False, True),
    ("n\n", True, False),
    ("no\n", True, False),
    ("\n", True, True),        # bare Return means "the default"
    ("\n", False, False),
    ("garbage\n", True, False),
])
def test_answers_are_read(monkeypatch, typed, default, expected):
    monkeypatch.setattr(onboarding, "_interactive", lambda: True)
    monkeypatch.setattr("sys.stdin", io.StringIO(typed))
    monkeypatch.setattr(onboarding, "_read_line", lambda _t: typed)
    assert onboarding.ask("q?", default=default) is expected


# -- flags ------------------------------------------------------------------

def test_answers_reads_the_command_line():
    answers = onboarding.Answers(_args(yes=True, lights=True, daemon=False))
    assert answers.assume_yes is True
    assert answers.lights is True
    assert answers.daemon is False
    assert answers.hooks is None


# -- steps that must not blow up -------------------------------------------

def test_config_step_is_idempotent(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("FREEMICRO_HOME", str(tmp_path))
    first = onboarding.step_config()
    assert first.exists()
    second = onboarding.step_config()
    assert second == first
    assert "already have one" in capsys.readouterr().out


def test_lights_step_defaults_to_leaving_them_off(monkeypatch, tmp_path):
    import json

    monkeypatch.setenv("FREEMICRO_HOME", str(tmp_path))
    monkeypatch.setattr(onboarding, "_interactive", lambda: False)
    path = onboarding.step_config()
    assert onboarding.step_lights(onboarding.Answers(_args(yes=True)), path) is False
    assert json.loads(path.read_text())["lighting"]["enabled"] is False


def test_lights_step_honours_an_explicit_yes(monkeypatch, tmp_path):
    import json

    monkeypatch.setenv("FREEMICRO_HOME", str(tmp_path))
    monkeypatch.setattr(onboarding, "_interactive", lambda: False)
    path = onboarding.step_config()
    answers = onboarding.Answers(_args(lights=True))
    assert onboarding.step_lights(answers, path) is True
    assert json.loads(path.read_text())["lighting"]["enabled"] is True
