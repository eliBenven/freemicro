"""There is one store, and one set of rules for reading it.

This file is a tripwire, not a feature test. FreeMicro has five surfaces that
read the same directory of session records - the CLI, the hooks, the menu bar,
the web UI and the pad itself - and for a while each of them built its own
:class:`~freemicro.state.engine.StateStore` out of a hand-written list of
timers. Every list was plausible. Three of them were short. The result was a
``freemicro status`` line and an Agent Key disagreeing about the same session,
in the same process, at the same instant, and a documented setting
(``working_ttl_seconds: 0``) that did nothing on three surfaces out of five.

The refactor made the copies impossible to write: the timers travel as one
:class:`~freemicro.state.engine.DecayPolicy`, and
:func:`~freemicro.state.engine.default_store` is the only construction that
reads the user's config. These tests exist to fail the day a sixth surface
arrives and builds its own anyway.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

from freemicro.config import DEFAULT_CONFIG, Config, config_home
from freemicro.state import engine
from freemicro.state.engine import DEFAULT_DECAY, AgentState, DecayPolicy

#: Timer values that look nothing like the defaults, so a construction path
#: that quietly falls back to them is caught rather than accidentally agreeing.
TUNED = {
    "ttl_seconds": 4321.0,
    "done_ttl_seconds": 11.0,
    "working_ttl_seconds": 0.0,
    "tool_ttl_seconds": 999.0,
}


def write_config(values: dict) -> None:
    """Write a ``config.json`` under the isolated test home."""
    home = config_home()
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.json").write_text(
        json.dumps({"state": values}), encoding="utf-8"
    )


def spy_on_default_store(monkeypatch) -> list:
    """Record every store built through the one construction path.

    Patched wherever it is *looked up*, not only where it is defined: a module
    that did ``from ... import default_store`` at import time holds its own
    reference, and a surface that goes around this spy is a surface that is not
    using the shared construction - which is the thing being tested.
    """
    real = engine.default_store
    made: list = []

    def spy(config=None):
        store = real(config)
        made.append(store)
        return store

    monkeypatch.setattr(engine, "default_store", spy)
    for name, module in list(sys.modules.items()):
        if not name.startswith("freemicro"):
            continue
        if getattr(module, "default_store", None) is real:
            monkeypatch.setattr(module, "default_store", spy)
    return made


def use_the_cli(_):
    from freemicro import cli

    return cli._store(Config.load())


def use_the_web_ui(_):
    from freemicro.webui import api

    return api._store()


def use_the_menu_bar(_):
    from freemicro.menubar import status

    status.resolved_state()


def use_the_pad(_):
    from freemicro.renderers.micro_leds import MicroLedsRenderer

    return MicroLedsRenderer().store


def use_an_agent_key(_):
    from freemicro import focus

    focus.current_slots()


#: Every surface that reads the session store, and how to make it read.
SURFACES = {
    "the CLI (freemicro hook/emit/status)": use_the_cli,
    "the web UI": use_the_web_ui,
    "the menu bar": use_the_menu_bar,
    "the pad LEDs": use_the_pad,
    "an Agent Key press": use_an_agent_key,
}


# ---------------------------------------------------------------------------
# One construction
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("surface", sorted(SURFACES))
def test_every_surface_reads_by_the_same_rules(surface, monkeypatch):
    """A fifth divergent copy fails here, whichever surface grows it.

    The assertion is deliberately about the *whole* policy rather than the
    timer of the day: a store that agrees about three values out of four is
    exactly what shipped, and it looked fine in every review.
    """
    write_config(TUNED)
    expected = DecayPolicy(**TUNED)
    assert expected != DEFAULT_DECAY, "the tuned values must not be the defaults"

    made = spy_on_default_store(monkeypatch)
    returned = SURFACES[surface](None)
    if returned is not None:
        made.append(returned)

    assert made, f"{surface} did not build a store through default_store()"
    for store in made:
        assert store.decay == expected, f"{surface} reads by different rules"
        assert store.directory == config_home() / "state"


def test_the_one_construction_is_the_only_construction():
    """Nothing else in the package may build a store out of the config.

    Two files name the constructor, for two stated reasons. Adding a third is a
    decision, and this test is where it gets made rather than noticed a release
    later:

    * ``state/engine.py`` defines :func:`default_store`, which *is* the shared
      construction.
    * ``selftest.py`` deliberately builds a store over a throwaway home so the
      hook lifecycle can be asserted whatever the user has configured.
    """
    allowed = {"engine.py", "selftest.py"}
    package = Path(__file__).resolve().parents[1] / "src" / "freemicro"
    offenders = sorted(
        str(path.relative_to(package))
        for path in package.rglob("*.py")
        if path.name not in allowed and "StateStore(" in path.read_text("utf-8")
    )
    assert offenders == [], (
        "these modules build their own StateStore instead of calling "
        f"default_store(): {offenders}"
    )


# ---------------------------------------------------------------------------
# One place to add the next timer
# ---------------------------------------------------------------------------

def test_a_new_timer_is_read_off_the_config_without_being_listed_anywhere():
    """``from_config`` must read whatever fields the policy declares.

    The original bug was a hand-written list of ``x=config.x`` lines. Nothing
    lists the timers now, so this asserts the property that replaced the list:
    every declared field is read off the config by name.
    """

    class Stub:
        pass

    stub = Stub()
    for index, name in enumerate(DecayPolicy.names()):
        setattr(stub, name, 7.0 + index)

    policy = DecayPolicy.from_config(stub)
    for index, name in enumerate(DecayPolicy.names()):
        assert getattr(policy, name) == 7.0 + index


def test_every_timer_is_a_documented_config_key():
    """A timer the policy has and the config file does not is unreachable."""
    for name in DecayPolicy.names():
        assert hasattr(Config(), name), f"Config has no {name}"
        assert name in DEFAULT_CONFIG["state"], f"DEFAULT_CONFIG has no {name}"


def test_a_config_that_has_never_heard_of_a_timer_still_builds_a_policy():
    """An older config object, or a stub, gets the documented defaults."""

    class Ancient:
        ttl_seconds = 60.0

    policy = DecayPolicy.from_config(Ancient())
    assert policy.ttl_seconds == 60.0
    assert policy.done_ttl_seconds == DEFAULT_DECAY.done_ttl_seconds
    assert policy.working_ttl_seconds == DEFAULT_DECAY.working_ttl_seconds


def test_nonsense_in_the_config_file_does_not_decide_how_long_a_light_stays_on():
    """A ``null``, a string or a negative is not a duration."""

    class Broken:
        ttl_seconds = None
        done_ttl_seconds = "soon"
        working_ttl_seconds = -5
        tool_ttl_seconds = float("inf")

    policy = DecayPolicy.from_config(Broken())
    assert policy == DEFAULT_DECAY.__class__(
        ttl_seconds=DEFAULT_DECAY.ttl_seconds,
        done_ttl_seconds=DEFAULT_DECAY.done_ttl_seconds,
        working_ttl_seconds=0.0,
        tool_ttl_seconds=DEFAULT_DECAY.tool_ttl_seconds,
    )


def test_the_store_refuses_the_shape_that_caused_all_this():
    """``StateStore(ttl_seconds=...)`` does not exist any more, on purpose."""
    with pytest.raises(TypeError):
        engine.StateStore(directory=config_home() / "state", ttl_seconds=10)
    with pytest.raises(TypeError) as raised:
        engine.StateStore(directory=config_home() / "state", decay=180.0)
    assert "DecayPolicy" in str(raised.value)


# ---------------------------------------------------------------------------
# The setting the docs promise
# ---------------------------------------------------------------------------

def quiet_working_record(age: float) -> None:
    """A session that claimed ``working`` ``age`` seconds ago and went silent.

    Written by hand rather than through ``update()`` so it carries no pid: the
    question here is what the *clock* does, and a record with a live process
    behind it is answered before any timer is consulted.
    """
    state_dir = config_home() / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "s1.json").write_text(
        json.dumps(
            {
                "session_id": "s1",
                "state": "working",
                "updated_at": time.time() - age,
                "cwd": "/code/api",
                "pid": 0,
            }
        ),
        encoding="utf-8",
    )


def test_working_ttl_zero_switches_the_check_off_on_every_surface(monkeypatch):
    """The owner's report from the audit, reproduced and then fixed.

    *"I set ``working_ttl_seconds: 0`` because the docs say 0 switches the
    check off. ``freemicro status`` still says idle two minutes into a long
    silent job, and the console line disagrees with the pad."*

    ``0`` means the ``working`` claim never expires on the clock. What is left
    is the session TTL, which only removes records nothing can vouch for - so
    the ten-minute silence below is still ``working`` everywhere, and every
    surface says so.
    """
    write_config(dict(TUNED, ttl_seconds=DEFAULT_DECAY.ttl_seconds))
    quiet_working_record(age=600)          # well past the 120s default

    made = spy_on_default_store(monkeypatch)
    store = engine.default_store()
    assert store.decay.working_ttl_seconds == 0.0
    assert store.resolved_state() == AgentState.WORKING

    from freemicro.agentkeys import group_projects
    from freemicro.menubar import status
    from freemicro.webui import api

    # The pad's own view of the same record...
    projects = group_projects(
        store.sessions(), now=time.time(), decay=store.decay
    )
    assert [p.state for p in projects] == [AgentState.WORKING]

    # ...the menu bar's...
    assert status.resolved_state()[0] == AgentState.WORKING

    # ...and the web UI's.
    keymap = config_home() / "keymap.json"
    keymap.write_text(
        json.dumps(
            {
                "version": 1,
                "bindings": {
                    "AG00": {"action": "key", "key": "escape", "label": "stop"}
                },
            }
        ),
        encoding="utf-8",
    )
    _, payload = api.Api(keymap).sessions()
    assert [s["state"] for s in payload["sessions"]] == ["working"]
    assert all(store.decay.working_ttl_seconds == 0.0 for store in made)


def test_the_default_working_ttl_still_retires_an_abandoned_claim():
    """The other half: switching the check off must be a *choice*."""
    write_config({})
    quiet_working_record(age=600)
    assert engine.default_store().resolved_state() == AgentState.IDLE
