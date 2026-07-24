"""Tests for the six Agent Keys: which project each one is, and what it does.

Everything here runs without hardware, without a real terminal and without a
real process - see ``no_real_processes`` below, and the module docstring of
``test_state_engine.py`` for why a store left to reach the live machine turns
its own assertions into a question about the developer's process table. The
resolver is a pure function, so slot stability - the property the whole feature
rests on - is asserted directly rather than inferred from LED traffic.
"""

from __future__ import annotations

import time

import pytest

from freemicro import focus
from freemicro.agentkeys import (
    POLICY_MANUAL,
    POLICY_MIRROR,
    POLICY_PINNED,
    POLICY_RECENT,
    SLOT_COUNT,
    AgentKeysConfig,
    AgentKeysError,
    SlotResolver,
    group_projects,
    normalise_project,
    parse_agent_keys,
    resolve_slots,
)
from freemicro.input.actions import Action, RecordingBackend, perform
from freemicro.padconfig import PadConfigError, load_default, parse
from freemicro.renderers.micro_leds import MicroLedsRenderer
from freemicro.state import slots as slot_cache
from freemicro.state.engine import (
    DEFAULT_DECAY,
    AgentState,
    DecayPolicy,
    SessionState,
    StateStore,
    TerminalInfo,
)

NOW = 1_000_000.0


@pytest.fixture(autouse=True)
def no_real_processes(monkeypatch):
    """No :class:`StateStore` in this file may ask about a live process.

    Nothing here asserts anything about liveness, and an un-injected store
    would signal whatever pid a fixture happened to invent. Empty table: no
    process is running, which is the pre-liveness behaviour these tests were
    written against.
    """
    from freemicro.state import engine

    monkeypatch.setattr(
        engine,
        "_SHARED_LIVENESS",
        engine.ProcessLiveness(
            alive_probe=lambda pid: False, started_probe=lambda pid: None
        ),
    )

#: The factory Agent-Key colours (docs/FACTORY-DEFAULTS.md §1a).
FACTORY = {
    AgentState.IDLE: 0xFFFFFF,
    AgentState.WORKING: 0x304FFE,
    AgentState.DONE: 0x00FF4C,
    AgentState.WAITING: 0xFF6D00,
    AgentState.ERROR: 0xFF0033,
}


def session(
    sid: str,
    state: AgentState,
    cwd: str,
    at: float = NOW,
    **kwargs,
) -> SessionState:
    return SessionState(
        session_id=sid, state=state, updated_at=at, cwd=cwd, **kwargs
    )


class FakeDevice:
    def __init__(self) -> None:
        self.sent: list = []

    def send(self, message):
        self.sent.append(message)

    def close(self):
        pass


class FakeStore:
    """Just enough StateStore for the renderer, with a fixed session list."""

    def __init__(self, sessions, decay: DecayPolicy = DEFAULT_DECAY) -> None:
        self._sessions = list(sessions)
        self.decay = decay

    def sessions(self):
        return list(self._sessions)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def test_defaults_to_recent_with_six_empty_slots():
    config = parse_agent_keys(None)
    assert config.policy == POLICY_RECENT
    assert config.slots == ("",) * SLOT_COUNT


def test_the_web_uis_shape_round_trips():
    """The UI writes {"policy", "slots": [six strings]}; match it exactly."""
    raw = {"policy": "pinned", "slots": ["/a", "", "", "", "", "/b"]}
    config = parse_agent_keys(raw)
    assert config.to_dict() == raw


def test_nulls_are_accepted_as_empty_slots():
    config = parse_agent_keys({"policy": "manual", "slots": [None, "/b"]})
    assert config.slots == ("", "/b", "", "", "", "")


def test_paths_are_normalised_and_expanded(monkeypatch):
    monkeypatch.setenv("HOME", "/Users/tester")
    config = parse_agent_keys({"policy": "pinned", "slots": ["~/code/api/"]})
    assert config.slots[0] == "/Users/tester/code/api"


@pytest.mark.parametrize(
    "raw, message",
    [
        ({"policy": "nonsense"}, "policy must be one of"),
        ({"slots": "not-a-list"}, "must be a list"),
        ({"slots": [1, 2]}, "must be a project path"),
        ({"slots": [""] * 7}, "the pad has 6 Agent Keys"),
        ({"nope": 1}, "unknown field"),
        ("not an object", "must be an object"),
    ],
)
def test_bad_sections_are_rejected_with_a_useful_message(raw, message):
    with pytest.raises(AgentKeysError) as exc:
        parse_agent_keys(raw)
    assert message in str(exc.value)


def test_padconfig_exposes_the_section():
    pad = parse({
        "version": 1,
        "bindings": {},
        "agent_keys": {"policy": "mirror", "slots": []},
    })
    assert pad.agent_keys.policy == POLICY_MIRROR
    assert pad.agent_keys.mirrors is True


def test_padconfig_rejects_a_broken_section_at_load_time():
    with pytest.raises(PadConfigError):
        parse({"version": 1, "bindings": {}, "agent_keys": {"policy": "??"}})


def test_a_pin_that_is_not_a_directory_warns_rather_than_fails(tmp_path):
    """Projects get archived and drives get unmounted; do not refuse to run."""
    pad = parse({
        "version": 1,
        "bindings": {},
        "agent_keys": {
            "policy": "pinned",
            "slots": [str(tmp_path), str(tmp_path / "gone"), "", "", "", ""],
        },
    })
    assert pad.agent_keys.slots[0] == str(tmp_path)
    assert any("gone" in w for w in pad.warnings)
    assert not any(str(tmp_path) + "'" in w for w in pad.warnings)


def test_manual_with_no_pins_warns_that_the_pad_stays_dark():
    pad = parse({
        "version": 1, "bindings": {}, "agent_keys": {"policy": "manual"},
    })
    assert any("stay dark" in w for w in pad.warnings)


def test_pins_are_ignored_but_kept_by_the_other_policies():
    config = parse_agent_keys({"policy": "recent", "slots": ["/a", "", "", "", "", ""]})
    assert config.slots[0] == "/a"      # preserved for when you switch back
    assert config.pins == ("",) * SLOT_COUNT   # but not honoured today


def test_the_shipped_default_ships_the_section():
    pad = load_default()
    assert pad.agent_keys.policy == POLICY_RECENT
    assert pad.warnings == ()


# ---------------------------------------------------------------------------
# Grouping sessions into projects
# ---------------------------------------------------------------------------

def test_sessions_in_one_directory_collapse_into_one_project():
    projects = group_projects(
        [
            session("a", AgentState.WORKING, "/code/api", NOW),
            session("b", AgentState.WAITING, "/code/api", NOW - 5),
        ],
        now=NOW,
    )
    assert len(projects) == 1
    # Whichever tab needs you decides the colour, not whichever moved last.
    assert projects[0].state == AgentState.WAITING
    assert projects[0].last_active == NOW
    assert projects[0].session_count == 2


def test_the_lead_session_is_the_one_a_key_press_should_reach():
    projects = group_projects(
        [
            session("busy", AgentState.WORKING, "/code/api", NOW),
            session("blocked", AgentState.WAITING, "/code/api", NOW - 5),
        ],
        now=NOW,
    )
    assert projects[0].lead.session_id == "blocked"


def test_projects_come_back_most_recently_active_first():
    projects = group_projects(
        [
            session("a", AgentState.WORKING, "/code/api", NOW - 100),
            session("b", AgentState.WORKING, "/code/web", NOW),
        ],
        now=NOW,
    )
    assert [p.path for p in projects] == ["/code/web", "/code/api"]


def test_done_decays_to_idle_per_project():
    """Green is *unread*. One repo's green must not be cleared by another's."""
    sessions = [
        session("old", AgentState.DONE, "/code/api", NOW - 200),
        session("new", AgentState.DONE, "/code/web", NOW - 10),
    ]
    by_path = {
        p.path: p
        for p in group_projects(
            sessions, now=NOW, decay=DecayPolicy(done_ttl_seconds=180)
        )
    }
    assert by_path["/code/api"].state == AgentState.IDLE
    assert by_path["/code/web"].state == AgentState.DONE


def test_decay_can_be_switched_off():
    projects = group_projects(
        [session("old", AgentState.DONE, "/code/api", NOW - 5000)],
        now=NOW,
        decay=DecayPolicy(done_ttl_seconds=0),
    )
    assert projects[0].state == AgentState.DONE


def test_an_interrupted_project_stops_showing_blue():
    """The pad's half of the interrupt bug: a key that claimed `working` for
    half an hour after the agent was cancelled."""
    sessions = [
        session("busy", AgentState.WORKING, "/code/api", NOW - 10),
        session("cancelled", AgentState.WORKING, "/code/web", NOW - 400),
    ]
    by_path = {p.path: p for p in group_projects(sessions, now=NOW)}
    assert by_path["/code/api"].state == AgentState.WORKING
    assert by_path["/code/web"].state == AgentState.IDLE


def test_a_project_running_a_long_tool_call_keeps_its_colour():
    """The opposite lie: a five minute build must not blank its own key."""
    sessions = [
        session(
            "building", AgentState.WORKING, "/code/api", NOW - 400,
            last_event="PreToolUse",
        ),
    ]
    assert group_projects(sessions, now=NOW)[0].state == AgentState.WORKING


def test_the_working_check_can_be_switched_off_per_call():
    sessions = [session("cancelled", AgentState.WORKING, "/code/web", NOW - 5000)]
    projects = group_projects(
        sessions, now=NOW, decay=DecayPolicy(working_ttl_seconds=0)
    )
    assert projects[0].state == AgentState.WORKING


def test_sessions_without_a_cwd_are_dropped():
    """There is no key to press for a project we cannot name."""
    assert group_projects([session("a", AgentState.WORKING, "")], now=NOW) == []


def test_identical_basenames_are_disambiguated():
    projects = group_projects(
        [
            session("a", AgentState.WORKING, "/code/api/web", NOW),
            session("b", AgentState.WORKING, "/code/site/web", NOW - 1),
            session("c", AgentState.WORKING, "/code/docs", NOW - 2),
        ],
        now=NOW,
    )
    labels = {p.path: p.label for p in projects}
    assert labels["/code/api/web"] == "api/web"
    assert labels["/code/site/web"] == "site/web"
    assert labels["/code/docs"] == "docs"       # untouched by the clash


# ---------------------------------------------------------------------------
# Slot assignment and stability
# ---------------------------------------------------------------------------

def test_recent_fills_from_the_left_newest_first():
    slots = resolve_slots(
        AgentKeysConfig(),
        [
            session("a", AgentState.WORKING, "/code/api", NOW - 10),
            session("b", AgentState.WORKING, "/code/web", NOW),
        ],
        now=NOW,
    )
    assert [s.path for s in slots[:2]] == ["/code/web", "/code/api"]
    assert all(s.empty for s in slots[2:])
    assert slots[0].key_id == "AG00"


def test_an_incumbent_keeps_its_key_when_the_order_changes():
    """The whole point: a glance must mean the same thing it meant before."""
    resolver = SlotResolver()
    first = resolver.resolve(
        [
            session("a", AgentState.WORKING, "/code/api", NOW - 10),
            session("b", AgentState.WORKING, "/code/web", NOW),
        ],
        now=NOW,
    )
    assert [s.path for s in first[:2]] == ["/code/web", "/code/api"]

    # /code/api is now the most recent. It must NOT jump to AG00.
    second = resolver.resolve(
        [
            session("a", AgentState.WORKING, "/code/api", NOW + 50),
            session("b", AgentState.WORKING, "/code/web", NOW),
        ],
        now=NOW + 50,
    )
    assert [s.path for s in second[:2]] == ["/code/web", "/code/api"]


def test_a_new_project_takes_the_lowest_free_key():
    resolver = SlotResolver()
    resolver.resolve([session("a", AgentState.WORKING, "/code/api", NOW)], now=NOW)
    slots = resolver.resolve(
        [
            session("a", AgentState.WORKING, "/code/api", NOW),
            session("b", AgentState.WORKING, "/code/web", NOW + 1),
        ],
        now=NOW + 1,
    )
    assert [s.path for s in slots[:2]] == ["/code/api", "/code/web"]


def test_a_seventh_project_waits_rather_than_evicting_anyone():
    live = [
        session(f"s{i}", AgentState.WORKING, f"/code/p{i}", NOW + i)
        for i in range(6)
    ]
    resolver = SlotResolver()
    first = resolver.resolve(live, now=NOW + 10)
    held = [s.path for s in first]

    late = live + [session("s6", AgentState.WORKING, "/code/late", NOW + 100)]
    second = resolver.resolve(late, now=NOW + 100)
    assert [s.path for s in second] == held      # nobody is bumped
    assert "/code/late" not in held


def test_a_vacated_key_is_refilled_by_the_most_recent_waiting_project():
    live = [
        session(f"s{i}", AgentState.WORKING, f"/code/p{i}", NOW + i)
        for i in range(6)
    ]
    resolver = SlotResolver()
    first = resolver.resolve(
        live + [session("s6", AgentState.WORKING, "/code/late", NOW + 100)],
        now=NOW + 100,
    )
    vacated = first[0].path

    survivors = [s for s in live if s.cwd != vacated]
    second = resolver.resolve(
        survivors + [session("s6", AgentState.WORKING, "/code/late", NOW + 100)],
        now=NOW + 100,
    )
    assert second[0].path == "/code/late"


def test_an_empty_key_remembers_its_project_and_gets_it_back():
    resolver = SlotResolver()
    resolver.resolve(
        [
            session("a", AgentState.WORKING, "/code/api", NOW),
            session("b", AgentState.WORKING, "/code/web", NOW - 5),
        ],
        now=NOW,
    )
    # api's terminal is closed…
    gone = resolver.resolve(
        [session("b", AgentState.WORKING, "/code/web", NOW + 1)], now=NOW + 1
    )
    assert gone[0].empty and gone[1].path == "/code/web"

    # …and comes back to the same key rather than to the first free one.
    back = resolver.resolve(
        [
            session("a2", AgentState.WORKING, "/code/api", NOW + 2),
            session("b", AgentState.WORKING, "/code/web", NOW + 1),
        ],
        now=NOW + 2,
    )
    assert back[0].path == "/code/api"


def test_pinned_keys_are_never_lent_out():
    config = AgentKeysConfig(
        policy=POLICY_PINNED, slots=("/code/api", "", "", "", "", "")
    )
    slots = resolve_slots(
        config,
        [
            session("b", AgentState.WORKING, "/code/web", NOW),
            session("c", AgentState.WORKING, "/code/docs", NOW - 1),
        ],
        now=NOW,
    )
    # AG00 belongs to api even though api is not running: it stays dark.
    assert slots[0].path == "/code/api"
    assert slots[0].empty and slots[0].reserved and slots[0].pinned
    assert [s.path for s in slots[1:3]] == ["/code/web", "/code/docs"]


def test_a_pinned_project_is_not_also_placed_automatically():
    config = AgentKeysConfig(
        policy=POLICY_PINNED, slots=("", "", "/code/api", "", "", "")
    )
    slots = resolve_slots(
        config, [session("a", AgentState.WORKING, "/code/api", NOW)], now=NOW
    )
    assert [s.path for s in slots] == ["", "", "/code/api", "", "", ""]
    assert slots[2].state == AgentState.WORKING


def test_manual_lights_only_what_you_named():
    config = AgentKeysConfig(
        policy=POLICY_MANUAL, slots=("/code/api", "", "", "", "", "")
    )
    slots = resolve_slots(
        config,
        [
            session("a", AgentState.WORKING, "/code/api", NOW),
            session("b", AgentState.WORKING, "/code/web", NOW),
        ],
        now=NOW,
    )
    assert slots[0].state == AgentState.WORKING
    assert all(s.empty for s in slots[1:])


def test_mirror_still_reports_an_assignment_for_status_output():
    slots = resolve_slots(
        AgentKeysConfig(policy=POLICY_MIRROR),
        [session("a", AgentState.WORKING, "/code/api", NOW)],
        now=NOW,
    )
    assert slots[0].path == "/code/api"


def test_slot_describe_is_readable():
    slots = resolve_slots(
        AgentKeysConfig(),
        [
            session("a", AgentState.WAITING, "/code/api", NOW),
            session("b", AgentState.WORKING, "/code/api", NOW),
        ],
        now=NOW,
    )
    assert slots[0].describe() == "api - waiting (2 sessions)"
    assert slots[1].describe() == "(empty)"
    assert slots[0].to_dict()["key"] == "AG00"


def test_normalise_project_is_pure_string_work():
    assert normalise_project("/code/api/") == "/code/api"
    assert normalise_project("/code/./api/../api") == "/code/api"
    assert normalise_project(None) == ""


# ---------------------------------------------------------------------------
# The shared assignment cache
# ---------------------------------------------------------------------------

def test_the_assignment_cache_round_trips(tmp_path):
    path = tmp_path / "slots.json"
    assert slot_cache.load(path) == ("",) * SLOT_COUNT
    assert slot_cache.save(["/a", "/b"], path) is True
    assert slot_cache.load(path) == ("/a", "/b", "", "", "", "")
    slot_cache.clear(path)
    assert slot_cache.load(path) == ("",) * SLOT_COUNT


def test_a_corrupt_cache_costs_a_cold_start_not_a_crash(tmp_path):
    path = tmp_path / "slots.json"
    path.write_text("{not json", encoding="utf-8")
    assert slot_cache.load(path) == ("",) * SLOT_COUNT


# ---------------------------------------------------------------------------
# Protocol: six different thstatus entries
# ---------------------------------------------------------------------------

LIT = parse({
    "version": 1,
    "bindings": {},
    "lighting": {
        "enabled": True,
        "zones": ["agent_keys"],
        "on_exit": "leave",
        "states": {
            "idle": {"color": "#FFFFFF"},
            "working": {"color": "#304FFE"},
            "waiting": {"color": "#FF6D00"},
            "done": {"color": "#00FF4C"},
            "error": {"color": "#FF0033"},
        },
    },
})


def _rendered(sessions):
    device = FakeDevice()
    renderer = MicroLedsRenderer(
        device=device, config=LIT, store=FakeStore(sessions)
    )
    renderer.render(AgentState.WORKING)
    return device.sent[0]


def test_each_key_gets_its_own_colour_from_its_own_project():
    # Wall-clock timestamps: this path renders through the real clock, and a
    # `working` claim from 1970 is one the resolver is now right to disbelieve.
    just_now = time.time()
    message = _rendered([
        session("a", AgentState.WORKING, "/code/api", just_now),
        session("b", AgentState.WAITING, "/code/web", just_now - 1),
        session("c", AgentState.ERROR, "/code/docs", just_now - 2),
    ])
    assert message["m"] == "v.oai.thstatus"
    entries = message["p"]
    assert [e["id"] for e in entries] == [0, 1, 2, 3, 4, 5]
    assert entries[0] == {
        "id": 0, "c": FACTORY[AgentState.WORKING], "b": 1.0, "e": 1, "s": 0.0,
    }
    assert entries[1]["c"] == FACTORY[AgentState.WAITING]
    assert entries[2]["c"] == FACTORY[AgentState.ERROR]
    # The three keys with no project are dark, not idle-white.
    for entry in entries[3:]:
        assert entry == {"id": entry["id"], "c": 0, "b": 0.0, "e": 0, "s": 0.0}


def test_the_keys_follow_the_stores_own_decay_rules():
    """The resolver used to be handed one of the store's timers, not all of it.

    So a user who switched the working check off got a store that believed the
    claim and a pad that retired it anyway - the stricter of two rules winning,
    which is the same contradiction as the store copies, arrived at from the
    reader's end. The policy is the store's now, whole.
    """
    quiet = [session("cancelled", AgentState.WORKING, "/code/api", time.time() - 5000)]

    retired = _rendered(quiet)["p"][0]["c"]
    assert retired == FACTORY[AgentState.IDLE]

    device = FakeDevice()
    renderer = MicroLedsRenderer(
        device=device,
        config=LIT,
        store=FakeStore(quiet, decay=DecayPolicy(working_ttl_seconds=0)),
    )
    renderer.render(AgentState.WORKING)
    assert device.sent[0]["p"][0]["c"] == FACTORY[AgentState.WORKING]


def test_a_slot_changing_state_repaints_even_when_the_winner_does_not():
    """Project A finishing while B keeps working must still move the LEDs."""
    device = FakeDevice()
    store = FakeStore([
        session("a", AgentState.WORKING, "/code/api", NOW),
        session("b", AgentState.WORKING, "/code/web", NOW - 1),
    ])
    renderer = MicroLedsRenderer(device=device, config=LIT, store=store)
    renderer.render(AgentState.WORKING)
    assert len(device.sent) == 1

    renderer.render(AgentState.WORKING)          # nothing changed
    assert len(device.sent) == 1

    store._sessions[0] = session("a", AgentState.WAITING, "/code/api", NOW + 1)
    renderer.render(AgentState.WORKING)          # winner is still "working"
    assert len(device.sent) == 2
    assert device.sent[1]["p"][0]["c"] == FACTORY[AgentState.WAITING]


def test_the_renderer_publishes_the_assignment_for_the_key_handler(monkeypatch):
    """The key you press must resolve to the project the LED is lit for."""
    written: list = []

    def record(slots, path=None):
        written.append(tuple(slots))
        return True

    monkeypatch.setattr(slot_cache, "save", record)
    monkeypatch.setattr(slot_cache, "load", lambda path=None: ("",) * SLOT_COUNT)
    renderer = MicroLedsRenderer(
        device=FakeDevice(),
        config=LIT,
        store=FakeStore([session("a", AgentState.WORKING, "/code/api", NOW)]),
    )
    renderer.render(AgentState.WORKING)
    assert written[-1][0] == "/code/api"


def test_without_a_store_the_pad_mirrors_instead_of_going_dark(monkeypatch):
    """A renderer that cannot read sessions must not blank the whole pad."""
    def unavailable():
        raise OSError("no state directory")

    monkeypatch.setattr(
        "freemicro.state.engine.default_store", unavailable, raising=True
    )
    device = FakeDevice()
    renderer = MicroLedsRenderer(device=device, config=LIT)
    renderer.render(AgentState.DONE)
    assert all(e["c"] == FACTORY[AgentState.DONE] for e in device.sent[0]["p"])


# ---------------------------------------------------------------------------
# Pressing a key: focus the terminal, do not type into one
# ---------------------------------------------------------------------------

def test_a_terminal_tab_is_identified_by_tty():
    plan = focus.plan_for_session(
        session(
            "a", AgentState.WORKING, "/code/api", NOW,
            tty="/dev/ttys004", term_program="Apple_Terminal",
        )
    )
    assert plan.method == focus.METHOD_TAB
    assert plan.app == "Terminal"
    assert "/dev/ttys004" in plan.script
    assert "tty of t" in plan.script
    # Never launches an app that is not running, never acts without a match.
    assert plan.script.startswith('if application "Terminal" is running then')
    assert "if matched then activate" in plan.script


def test_iterm_sessions_are_matched_one_level_deeper():
    plan = focus.plan_for_session(
        session(
            "a", AgentState.WORKING, "/code/api", NOW,
            tty="/dev/ttys009", term_program="iTerm.app",
        )
    )
    assert plan.app == "iTerm2"
    assert "sessions of t" in plan.script and "select s" in plan.script


def test_an_unscriptable_terminal_falls_back_to_the_app():
    plan = focus.plan_for_session(
        session(
            "a", AgentState.WORKING, "/code/api", NOW,
            tty="/dev/ttys004", term_program="ghostty",
        )
    )
    assert plan.method == focus.METHOD_APP and plan.app == "Ghostty"
    assert "tab unknown" in plan.describe()


def test_the_fallback_can_be_turned_off():
    plan = focus.plan_for_session(
        session(
            "a", AgentState.WORKING, "/code/api", NOW, term_program="ghostty",
        ),
        fallback=False,
    )
    assert plan.method == focus.METHOD_NONE


def test_an_unidentifiable_terminal_does_nothing_and_says_why():
    plan = focus.plan_for_session(
        session("a", AgentState.WORKING, "/code/api", NOW)
    )
    assert plan.method == focus.METHOD_NONE
    assert "not one FreeMicro can identify" in plan.describe()


def test_a_dead_session_does_nothing():
    plan = focus.plan_for_session(None, project="/code/api")
    assert plan.method == focus.METHOD_NONE
    assert "no live session" in plan.describe()
    assert focus.perform(plan, RecordingBackend()) is False


def test_a_hostile_tty_never_reaches_applescript():
    """The only on-disk value that reaches osascript is pattern-checked."""
    evil = '/dev/ttys004" \n do shell script "rm -rf ~"'
    assert focus.valid_tty(evil) is False
    assert focus.tab_script("Terminal", evil) == ""
    plan = focus.plan_for_session(
        session(
            "a", AgentState.WORKING, "/code/api", NOW,
            tty=evil, term_program="Apple_Terminal",
        )
    )
    assert plan.method == focus.METHOD_APP     # app-level only, no script
    assert plan.script == ""


def test_performing_a_tab_plan_runs_the_script():
    backend = RecordingBackend()
    plan = focus.plan_for_session(
        session(
            "a", AgentState.WORKING, "/code/api", NOW,
            tty="/dev/ttys004", term_program="Apple_Terminal",
        )
    )
    assert focus.perform(plan, backend) is True
    assert backend.calls[0][0] == "run_applescript"


def test_performing_an_app_plan_activates_the_app():
    backend = RecordingBackend()
    plan = focus.plan_for_session(
        session(
            "a", AgentState.WORKING, "/code/api", NOW, term_program="warp",
        )
    )
    assert focus.perform(plan, backend) is True
    assert backend.calls == [("activate_app", ("Warp", False))]


def test_a_key_press_focuses_the_project_on_that_key(tmp_path):
    store = FakeStore([
        session("a", AgentState.WORKING, "/code/api", NOW),
        session(
            "b", AgentState.WAITING, "/code/web", NOW - 1,
            tty="/dev/ttys007", term_program="Apple_Terminal",
        ),
    ])
    slots = focus.current_slots(AgentKeysConfig(), store=store, previous=())
    plan = focus.plan_for_slot(1, slots=slots)
    assert plan.project == "/code/web"
    assert plan.tty == "/dev/ttys007"


def test_a_key_can_be_nailed_to_one_project_regardless_of_policy():
    store = FakeStore([
        session(
            "a", AgentState.WORKING, "/code/api", NOW,
            tty="/dev/ttys004", term_program="Apple_Terminal",
        ),
    ])
    slots = focus.current_slots(AgentKeysConfig(), store=store, previous=())
    plan = focus.plan_for_slot(5, project="/code/api", slots=slots)
    assert plan.method == focus.METHOD_TAB and plan.project == "/code/api"


# ---------------------------------------------------------------------------
# A tab identified from the session's pid
#
# Records written before the capture was fixed have `tty: ""` and a good pid.
# The lookup is injected, so nothing here runs `ps` or needs a real terminal.
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _no_cached_ttys():
    """Each test starts with an empty derivation cache and leaves one behind."""
    focus.clear_tty_cache()
    yield
    focus.clear_tty_cache()


def test_a_record_with_no_tty_is_repaired_from_its_pid():
    """The bug: every record on disk had `tty: ""`, so every key hit the app."""
    plan = focus.plan_for_session(
        session(
            "a", AgentState.WORKING, "/code/api", NOW,
            pid=4242, term_program="Apple_Terminal",
        ),
        tty_lookup={4242: "/dev/ttys003"}.get,
    )
    assert plan.method == focus.METHOD_TAB
    assert plan.tty == "/dev/ttys003"
    assert 'if tty of t is "/dev/ttys003"' in plan.script


def test_a_stored_tty_is_trusted_and_no_lookup_happens():
    def never(pid):  # pragma: no cover - proving it is not called
        raise AssertionError("the stored tty should have been enough")

    plan = focus.plan_for_session(
        session(
            "a", AgentState.WORKING, "/code/api", NOW,
            tty="/dev/ttys004", pid=4242, term_program="Apple_Terminal",
        ),
        tty_lookup=never,
    )
    assert plan.tty == "/dev/ttys004"


def test_a_dead_pid_says_tab_unknown_rather_than_guessing():
    plan = focus.plan_for_session(
        session(
            "a", AgentState.WORKING, "/code/api", NOW,
            pid=4242, term_program="Apple_Terminal",
        ),
        tty_lookup=lambda pid: "",       # ps: no such process
    )
    assert plan.method == focus.METHOD_APP
    assert "tab unknown" in plan.describe()
    assert plan.script == ""


def test_a_derived_tty_is_pattern_checked_like_a_stored_one():
    """A lookup is not a trusted source either; nothing skips the gate."""
    plan = focus.plan_for_session(
        session(
            "a", AgentState.WORKING, "/code/api", NOW,
            pid=4242, term_program="Apple_Terminal",
        ),
        tty_lookup=lambda pid: '/dev/ttys004" \n do shell script "rm -rf ~"',
    )
    assert plan.method == focus.METHOD_APP and plan.script == ""


def test_a_session_with_no_pid_at_all_is_not_looked_up():
    def never(pid):  # pragma: no cover - proving it is not called
        raise AssertionError("there is nothing to look up")

    plan = focus.plan_for_session(
        session("a", AgentState.WORKING, "/code/api", NOW,
                term_program="Apple_Terminal"),
        tty_lookup=never,
    )
    assert plan.method == focus.METHOD_APP


def test_an_iterm_session_is_repaired_the_same_way():
    plan = focus.plan_for_session(
        session(
            "a", AgentState.WORKING, "/code/api", NOW,
            pid=77, term_program="iTerm.app",
        ),
        tty_lookup={77: "/dev/ttys009"}.get,
    )
    assert plan.method == focus.METHOD_TAB and plan.app == "iTerm2"
    assert "sessions of t" in plan.script and "/dev/ttys009" in plan.script


def test_a_pid_is_looked_up_once_for_a_whole_row_of_keys():
    calls = []

    def counting(pid):
        calls.append(pid)
        return "/dev/ttys005"

    for _ in range(3):
        focus.tty_from_pid(4242, lookup=counting)
    assert calls == [4242]
    focus.clear_tty_cache()
    focus.tty_from_pid(4242, lookup=counting)
    assert calls == [4242, 4242]


def test_a_key_press_reaches_the_right_tab_of_a_repaired_session():
    backend = RecordingBackend()
    store = FakeStore([
        session(
            "a", AgentState.WORKING, "/code/api", NOW,
            pid=4242, term_program="Apple_Terminal",
        ),
    ])
    slots = focus.current_slots(AgentKeysConfig(), store=store, previous=())
    plan = focus.plan_for_slot(
        0, slots=slots, tty_lookup={4242: "/dev/ttys006"}.get
    )
    assert focus.perform(plan, backend) is True
    kind, args = backend.calls[0]
    assert kind == "run_applescript"
    assert args[0] == focus.tab_script("Terminal", "/dev/ttys006")


def test_the_default_keymap_binds_the_agent_keys_to_focus_not_to_typing():
    """The owner's correction: an Agent Key opens a tab, it does not type."""
    pad = load_default()
    for index in range(SLOT_COUNT):
        action = pad.bindings[f"AG{index:02d}"]
        assert action.kind == "focus_session"
        assert action.params["slot"] == index


def test_focus_session_off_an_agent_key_must_say_which_slot():
    with pytest.raises(PadConfigError) as exc:
        parse({
            "version": 1,
            "bindings": {"ACT06": {"action": "focus_session"}},
        })
    assert "slot" in str(exc.value)


def test_pressing_a_key_with_nothing_running_does_nothing_at_all(tmp_path):
    """Silence beats raising the wrong window: the next keystroke lands there."""
    backend = RecordingBackend()
    perform(Action(kind="focus_session", params={"slot": 0}), backend)
    assert backend.calls == []


# ---------------------------------------------------------------------------
# Terminal capture
# ---------------------------------------------------------------------------

def test_the_store_records_the_terminal_it_was_updated_from(tmp_path):
    store = StateStore(
        directory=tmp_path,
        terminal_probe=lambda: TerminalInfo(
            tty="/dev/ttys004", pid=42, program="Apple_Terminal", session="w0t0"
        ),
    )
    store.update("s1", AgentState.WORKING, cwd="/code/api")
    live = store.sessions()[0]
    assert live.tty == "/dev/ttys004"
    assert live.terminal.program == "Apple_Terminal"
    assert live.pid == 42


def test_an_explicit_terminal_suppresses_the_probe(tmp_path):
    store = StateStore(
        directory=tmp_path,
        terminal_probe=lambda: TerminalInfo(tty="/dev/should-not-be-used"),
    )
    store.update("s1", AgentState.WORKING, terminal=TerminalInfo())
    assert store.sessions()[0].tty == ""


def test_a_failing_probe_never_breaks_a_hook(tmp_path):
    def boom():
        raise OSError("no tty here")

    store = StateStore(directory=tmp_path, terminal_probe=boom)
    assert store.update("s1", AgentState.WORKING).tty == ""


def test_records_written_before_this_feature_still_load(tmp_path):
    store = StateStore(directory=tmp_path)
    (tmp_path / "old.json").write_text(
        '{"session_id": "old", "state": "working", "updated_at": %f}' % NOW,
        encoding="utf-8",
    )
    store.clock = lambda: NOW
    assert store.sessions()[0].terminal.empty
