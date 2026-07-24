"""Answering a permission prompt from the pad, and refusing to.

This file has to carry more weight than most, for a reason worth stating: the
owner's own sessions run in ``bypassPermissions`` and therefore **never prompt**
(all 4,420 captured hook payloads say so), so the prompting path cannot be
dogfooded on the machine it was built on. Nothing here is a smoke test.

The dangerous direction is asymmetric. A refusal that should have fired costs
one keystroke on the real keyboard. A keystroke that fires when no prompt is
pending goes into a live shell or a live composer, so the bulk of what follows
is about the cases where **nothing must be sent** - and those tests assert on
the backend having recorded *no calls at all*, not merely the wrong ones.

No hardware, no terminal, no real process: every session record is built by
hand, ``RecordingBackend`` stands in for the outside world, and the clock is
injected.
"""

from __future__ import annotations

import time

import pytest

from freemicro import focus, permission_prompt
from freemicro.input.actions import (
    ANSWER_PERMISSION,
    HOLD_KINDS,
    Action,
    RecordingBackend,
    perform,
    release,
    validate_params,
)
from freemicro.padconfig import PadConfigError, load_default, parse
from freemicro.state.engine import AgentState, SessionState
from freemicro.state.hooks import PROMPT_EVENT, prompt_is_pending
from freemicro.webui import keycaps

NOW = 1_700_000_000.0
TTY = "/dev/ttys009"


@pytest.fixture(autouse=True)
def clean_module_state():
    """Three module-level caches are process-wide. No test inherits another's:
    the answered-once latch, the keys currently held, and derived ttys."""
    from freemicro.input import actions

    for reset in (permission_prompt.forget_answered, focus.clear_tty_cache):
        reset()
    actions._HELD.clear()
    yield
    for reset in (permission_prompt.forget_answered, focus.clear_tty_cache):
        reset()
    actions._HELD.clear()


def waiting(
    sid: str = "s1",
    *,
    cwd: str = "/code/api",
    at: float = NOW,
    tty: str = TTY,
    program: str = "iTerm.app",
    **kwargs,
) -> SessionState:
    """A record exactly as a permission-prompt ``Notification`` would leave it."""
    fields = {
        "state": AgentState.WAITING,
        "last_event": PROMPT_EVENT,
        "permission_mode": "default",
        "process_alive": True,
        "pid": 4242,
        "prompt_id": "turn-1",
    }
    fields.update(kwargs)
    return SessionState(
        session_id=sid,
        updated_at=at,
        cwd=cwd,
        tty=tty,
        term_program=program,
        **fields,
    )


def fresh(**kwargs) -> SessionState:
    """A prompt that arrived just now, for the paths that read the real clock."""
    kwargs.setdefault("at", time.time())
    return waiting(**kwargs)


class FakeStore:
    """Just enough :class:`StateStore` to hand back a fixed session list."""

    def __init__(self, sessions):
        self._sessions = list(sessions)

    def sessions(self):
        return list(self._sessions)


def plan_for(answer="approve", sessions=(), **kwargs):
    return permission_prompt.plan(
        answer, sessions=list(sessions), now=NOW, **kwargs
    )


# ---------------------------------------------------------------------------
# Which records are even admissible
# ---------------------------------------------------------------------------

def test_a_permission_prompt_record_is_pending():
    assert prompt_is_pending(waiting()) is True


@pytest.mark.parametrize(
    "field,value,why",
    [
        ("state", AgentState.WORKING, "not waiting at all"),
        ("state", AgentState.DONE, "finished, not blocked"),
        ("last_event", "", "no event name: `freemicro emit waiting` wrote it"),
        ("last_event", "PreToolUse", "something happened after the prompt"),
        ("permission_mode", "bypassPermissions", "this session never prompts"),
    ],
)
def test_a_record_that_is_not_a_live_prompt_is_not_pending(field, value, why):
    assert prompt_is_pending(waiting(**{field: value})) is False, why


def test_a_retired_claim_is_not_pending():
    """``stale`` means the store stopped believing the record. So do we."""
    stale = waiting(claimed_state=AgentState.WAITING, state=AgentState.IDLE)
    assert stale.stale is True
    assert prompt_is_pending(stale) is False


def test_a_process_we_cannot_prove_is_alive_is_not_answered():
    """Not "probably fine": a pid we cannot vouch for may have changed hands,
    and its tty may name a tab that now belongs to somebody else."""
    assert permission_prompt.pending(
        [waiting(process_alive=None)], now=NOW
    ) == []
    assert permission_prompt.pending(
        [waiting(process_alive=False)], now=NOW
    ) == []


def test_an_old_claim_falls_outside_the_answering_window():
    old = waiting(at=NOW - permission_prompt.MAX_AGE_SECONDS - 1)
    assert permission_prompt.pending([old], now=NOW) == []
    # Still inside it, right up to the edge.
    edge = waiting(at=NOW - permission_prompt.MAX_AGE_SECONDS)
    assert permission_prompt.pending([edge], now=NOW) == [edge]


def test_the_window_can_be_switched_off_per_binding():
    ancient = waiting(at=NOW - 86_400)
    assert permission_prompt.pending([ancient], now=NOW, max_age=0) == [ancient]


def test_a_project_restricted_key_ignores_other_repos():
    api = waiting("a", cwd="/code/api")
    web = waiting("b", cwd="/code/web", at=NOW + 1)
    assert permission_prompt.pending(
        [api, web], now=NOW, project="/code/api"
    ) == [api]


# ---------------------------------------------------------------------------
# Nothing is asking: nothing is sent
# ---------------------------------------------------------------------------

def test_with_nothing_waiting_the_key_says_so_and_sends_nothing():
    plan = plan_for(sessions=[])
    assert plan.actionable is False
    assert "nothing is waiting" in plan.describe()

    backend = RecordingBackend()
    assert permission_prompt.perform(plan, backend) is False
    assert backend.calls == []


def test_a_working_session_is_not_answerable_even_though_it_is_live():
    plan = plan_for(sessions=[waiting(state=AgentState.WORKING)])
    assert plan.actionable is False
    backend = RecordingBackend()
    assert permission_prompt.perform(plan, backend) is False
    assert backend.calls == []


def test_an_emitted_waiting_state_never_causes_a_keystroke():
    """``freemicro emit waiting`` is a demo. It must not type into a session."""
    emitted = waiting(last_event="")
    plan = plan_for(sessions=[emitted])
    assert plan.actionable is False
    assert RecordingBackend().calls == []


def test_an_unreadable_store_refuses_rather_than_guessing():
    class Broken:
        def sessions(self):
            raise OSError("disk gone")

    plan = permission_prompt.plan("approve", store=Broken(), now=NOW)
    assert plan.actionable is False
    assert "session store" in plan.reason


def test_an_answer_we_do_not_know_is_refused_not_improvised():
    plan = plan_for("maybe", sessions=[waiting()])
    assert plan.actionable is False
    assert plan.key == ""


# ---------------------------------------------------------------------------
# The tab has to be nameable
# ---------------------------------------------------------------------------

def test_a_terminal_that_cannot_name_its_tabs_gets_no_keystroke():
    """Ghostty raises the app but not the tab. "Right app, some tab" is not
    good enough to type into: the other tabs are other people's agents."""
    plan = plan_for(sessions=[waiting(program="ghostty")], tty_lookup=lambda pid: "")
    assert plan.focus_plan is not None
    assert plan.focus_plan.method == focus.METHOD_APP
    assert plan.actionable is False
    assert "does nothing" in plan.describe()

    backend = RecordingBackend()
    assert permission_prompt.perform(plan, backend) is False
    assert backend.calls == []


def test_a_session_with_no_identifiable_terminal_gets_no_keystroke():
    plan = plan_for(
        sessions=[waiting(tty="", program="")], tty_lookup=lambda pid: ""
    )
    assert plan.actionable is False
    assert RecordingBackend().calls == []


def test_a_hostile_tty_never_reaches_applescript():
    evil = '/dev/ttys009" then do shell script "rm -rf ~'
    assert focus.valid_tty(evil) is False
    assert permission_prompt.confirm_script("Terminal", evil, "1") == ""
    plan = plan_for(
        sessions=[waiting(tty=evil, program="Apple_Terminal")],
        tty_lookup=lambda pid: "",
    )
    assert plan.actionable is False


# ---------------------------------------------------------------------------
# The script itself
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "answer,expected",
    [
        ("approve", 'keystroke "1"'),   # option 1 is Yes in every dialog
        ("always", 'keystroke "2"'),    # option 2 is the broader yes
        ("reject", "key code 53"),      # escape, the dialog's cancel chord
    ],
)
def test_each_answer_sends_the_key_that_answers_it(answer, expected):
    plan = plan_for(answer, sessions=[waiting()])
    assert plan.actionable is True
    assert expected in plan.script


def test_the_keystroke_is_unreachable_unless_both_checks_passed():
    """The structural guarantee, asserted as text.

    ``activate`` is a request, not a completed fact, so the keystroke is not
    allowed to be a statement that simply follows it. It is the tail of an
    ``if matched and ready``: ``matched`` is set only by the tty walk finding
    and selecting that exact tab, ``ready`` only by the app really being
    frontmost afterwards.
    """
    plan = plan_for(sessions=[waiting()])
    script = plan.script
    last = script.rstrip().splitlines()[-1]
    assert last.startswith("if matched and ready then ")
    assert last.endswith('to keystroke "1"')
    # The keystroke appears exactly once, and only there.
    assert script.count("keystroke") == 1
    assert TTY in script


def test_the_script_raises_the_tab_with_the_agent_keys_own_code():
    """Raising and answering are one script, and the raising half is reused.

    Two ``osascript`` runs would leave a gap between the raise and the
    keystroke in which anything could come to the front. One script closes it,
    and it embeds :func:`freemicro.focus.tab_script` verbatim rather than
    growing a second copy of "find the tab on this tty".
    """
    plan = plan_for(sessions=[waiting()])
    assert plan.focus_plan is not None
    assert plan.focus_plan.script and plan.focus_plan.script in plan.script


@pytest.mark.parametrize("app", ["Terminal", "iTerm2"])
def test_neither_scriptable_emulator_is_ever_launched(app):
    """A keypress that opened an empty Terminal would be an unpleasant
    surprise, so every reference to the app is behind ``is running``."""
    script = permission_prompt.confirm_script(app, TTY, "1")
    for line in script.splitlines():
        if "tell application" in line and "System Events" not in line:
            assert "is running then" in script


def test_an_unscriptable_app_produces_no_script_at_all():
    assert permission_prompt.confirm_script("Ghostty", TTY, "1") == ""


def test_the_guards_are_answerable_even_if_the_app_is_gone():
    """``tab_script`` only defines ``matched`` when the app is running, so the
    guard has to declare both flags itself or the script dies on a name error
    exactly when it should quietly do nothing."""
    script = permission_prompt.confirm_script("Terminal", TTY, "1")
    lines = script.splitlines()
    assert lines[0] == "set matched to false"
    assert lines[1] == "set ready to false"


# ---------------------------------------------------------------------------
# Doing it
# ---------------------------------------------------------------------------

def test_answering_raises_the_asking_tab_and_types_into_that_tab():
    plan = plan_for(sessions=[waiting()])
    backend = RecordingBackend()
    assert permission_prompt.perform(plan, backend) is True

    assert [name for name, _ in backend.calls] == ["run_applescript"]
    script = backend.calls[0][1][0]
    assert script == plan.script
    assert "select" in script and TTY in script


def test_the_same_prompt_is_only_ever_answered_once():
    """A second press must not follow the first "1" with another one.

    Answering does not update the record - the store only finds out when the
    next hook fires a second or two later - so without the latch an impatient
    double tap types a stray digit into the composer that just came back.
    """
    session = waiting()
    backend = RecordingBackend()
    assert permission_prompt.perform(plan_for(sessions=[session]), backend) is True
    assert len(backend.calls) == 1

    assert permission_prompt.already_answered(session) is True
    assert permission_prompt.perform(plan_for(sessions=[session]), backend) is False
    assert len(backend.calls) == 1, "the second press must send nothing"


def test_a_fresh_prompt_in_the_same_session_is_answerable_again():
    first = waiting(at=NOW - 10, prompt_id="turn-1")
    backend = RecordingBackend()
    assert permission_prompt.perform(plan_for(sessions=[first]), backend) is True
    second = waiting(at=NOW, prompt_id="turn-2")
    assert permission_prompt.perform(plan_for(sessions=[second]), backend) is True
    assert len(backend.calls) == 2


# ---------------------------------------------------------------------------
# More than one project waiting
# ---------------------------------------------------------------------------

def test_the_newest_prompt_wins_and_the_choice_is_announced():
    older = waiting("a", cwd="/code/api", at=NOW - 30)
    newer = waiting("b", cwd="/code/web", at=NOW - 2, tty="/dev/ttys010")
    plan = plan_for(sessions=[older, newer])
    assert plan.session is not None and plan.session.session_id == "b"
    assert plan.waiting == 2
    # Never silently: --list and the run log both say a choice was made.
    assert "newest of 2 waiting" in plan.describe()


def test_a_tie_is_broken_the_same_way_every_time():
    left = waiting("bbb", cwd="/code/b")
    right = waiting("aaa", cwd="/code/a")
    assert [s.session_id for s in permission_prompt.pending([left, right], now=NOW)] \
        == ["aaa", "bbb"]


def test_a_key_can_be_nailed_to_one_repo():
    api = waiting("a", cwd="/code/api", at=NOW - 30)
    web = waiting("b", cwd="/code/web", at=NOW)
    plan = plan_for(sessions=[api, web], project="/code/api")
    assert plan.session is not None and plan.session.session_id == "a"
    assert plan.waiting == 1


# ---------------------------------------------------------------------------
# The action kind
# ---------------------------------------------------------------------------

def act(**params) -> Action:
    return Action(kind=ANSWER_PERMISSION, params=params, label="APPR")


@pytest.fixture
def store_of(monkeypatch):
    """Point the action kind at a session list of our choosing.

    This is the only seam the kind offers, and that is the point: a binding
    cannot be handed a session, so there is no configuration under which it
    answers something it did not look up for itself.
    """

    def install(sessions):
        monkeypatch.setattr(
            "freemicro.state.engine.default_store", lambda: FakeStore(sessions)
        )

    return install


def test_the_kind_delivers_an_approval(store_of):
    store_of([fresh()])
    backend = RecordingBackend()
    perform(act(answer="approve"), backend)
    assert [name for name, _ in backend.calls] == ["run_applescript"]
    assert 'keystroke "1"' in backend.calls[-1][1][0]


def test_the_kind_sends_nothing_when_nothing_is_asking(store_of):
    store_of([])
    backend = RecordingBackend()
    perform(act(answer="approve"), backend)
    assert backend.calls == []


def test_the_kind_defaults_to_approve(store_of):
    store_of([fresh()])
    backend = RecordingBackend()
    perform(act(), backend)
    assert 'keystroke "1"' in backend.calls[-1][1][0]


def test_a_binding_without_a_long_press_fires_on_the_press(store_of):
    store_of([fresh()])
    backend = RecordingBackend()
    binding = act(answer="approve")
    perform(binding, backend)
    assert len(backend.calls) == 1
    release(binding, backend)
    assert len(backend.calls) == 1, "release must not answer a second time"


# -- the long press ---------------------------------------------------------

class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch):
    fake = FakeClock()
    monkeypatch.setattr("freemicro.input.actions._clock", fake)
    return fake


def test_the_kind_is_told_about_releases():
    """The bridge routes a release by consulting this list, not by name."""
    assert ANSWER_PERMISSION in HOLD_KINDS


def test_a_long_press_binding_decides_nothing_until_you_let_go(store_of, clock):
    store_of([fresh()])
    backend = RecordingBackend()
    perform(act(answer="approve", long_press="always"), backend)
    assert backend.calls == [], "a hold cannot be judged before it ends"


def test_a_tap_approves_and_a_hold_says_always(store_of, clock):
    store_of([fresh(sid="tap")])
    tap = act(answer="approve", long_press="always")
    backend = RecordingBackend()
    perform(tap, backend)
    clock.now += 0.08
    release(tap, backend)
    assert 'keystroke "1"' in backend.calls[-1][1][0]

    store_of([fresh(sid="hold")])
    held = act(answer="approve", long_press="always")
    backend = RecordingBackend()
    perform(held, backend)
    clock.now += 0.7
    release(held, backend)
    assert 'keystroke "2"' in backend.calls[-1][1][0]


def test_the_hold_threshold_is_configurable(store_of, clock):
    store_of([fresh()])
    binding = act(answer="approve", long_press="always", long_press_ms=2000)
    backend = RecordingBackend()
    perform(binding, backend)
    clock.now += 0.7  # long by the default, short by this binding's own rule
    release(binding, backend)
    assert 'keystroke "1"' in backend.calls[-1][1][0]


def test_a_release_with_no_matching_press_does_nothing(store_of):
    store_of([waiting()])
    backend = RecordingBackend()
    release(act(answer="approve", long_press="always"), backend)
    assert backend.calls == []


def test_a_press_whose_release_never_arrives_is_forgotten(store_of, clock):
    """A pad that drops mid-hold must not leave the key armed forever."""
    from freemicro.input import actions

    store_of([fresh()])
    lost = act(answer="approve", long_press="always")
    perform(lost, RecordingBackend())
    assert actions._HELD

    clock.now += actions._HELD_TTL_SECONDS + 1
    perform(act(answer="approve", long_press="always"), RecordingBackend())
    assert len(actions._HELD) == 1, "the abandoned press was not cleaned up"


# -- validation -------------------------------------------------------------

@pytest.mark.parametrize("field", ["answer", "long_press"])
def test_an_answer_that_is_not_one_of_the_three_is_a_config_error(field):
    with pytest.raises(ValueError) as excinfo:
        validate_params(ANSWER_PERMISSION, {field: "yes"})
    assert "approve" in str(excinfo.value)


@pytest.mark.parametrize("field", ["long_press_ms", "max_age"])
@pytest.mark.parametrize("value", ["soon", -1])
def test_a_nonsense_duration_is_a_config_error(field, value):
    with pytest.raises(ValueError):
        validate_params(ANSWER_PERMISSION, {field: value})


def test_an_unknown_field_is_a_config_error():
    with pytest.raises(ValueError):
        validate_params(ANSWER_PERMISSION, {"anwser": "approve"})


def test_a_bad_answer_stops_the_whole_config_loading():
    with pytest.raises(PadConfigError) as excinfo:
        parse({
            "version": 1,
            "bindings": {
                "ACT07": {"action": ANSWER_PERMISSION, "answer": "sure"}
            },
        })
    assert "ACT07" in str(excinfo.value)


def test_describe_never_raises_even_with_no_store(store_of):
    store_of([])
    text = act(answer="approve", long_press="always").describe()
    assert "hold 500ms for always" in text


# ---------------------------------------------------------------------------
# The shipped default
# ---------------------------------------------------------------------------

def test_the_default_puts_approve_and_reject_on_the_caps_that_mean_them():
    pad = load_default()
    approve = pad.bindings["ACT07"]
    reject = pad.bindings["ACT08"]
    assert approve.kind == ANSWER_PERMISSION
    assert approve.params["answer"] == "approve"
    assert approve.params["long_press"] == "always", (
        "the owner asked for 'yes, and don't ask again' on a hold of the yes key"
    )
    assert reject.kind == ANSWER_PERMISSION
    assert reject.params["answer"] == "reject"


def test_the_default_never_puts_words_in_the_agents_mouth():
    """No shipped binding types a prompt, and none names a third-party app.

    Both were real defects: a stranger's first exploratory press used to send
    someone else's prompt wording into their repo at their expense, and the
    biggest cap on the pad was bound to a paid app they might not own.
    """
    pad = load_default()
    for input_id, binding in pad.bindings.items():
        assert binding.kind != "text", f"{input_id} types words into the agent"
        assert binding.kind != "shell", f"{input_id} runs a command unasked"
        if binding.kind == "app":  # pragma: no cover - none ship today
            raise AssertionError(f"{input_id} names a specific app")


def test_the_default_labels_every_key_with_the_cap_that_is_on_it():
    """The cap tells the truth: the printed glyph and the label agree."""
    import json

    from freemicro.padconfig import DEFAULT_CONFIG_PATH

    document = json.loads(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))
    caps = document["keycaps"]
    assert caps == {
        "ACT06": "FAST",
        "ACT07": "APPR",
        "ACT08": "REJ",
        "ACT09": "SPLIT",
        "ACT10": "MIC",
        "ACT12": "CODEX",
    }
    assert keycaps.clean(caps) == caps, "every cap must be one the vendor ships"
    pad = load_default()
    for input_id, cap in caps.items():
        assert pad.bindings[input_id].label.startswith(cap), (
            f"{input_id} wears {cap} but its label does not say so"
        )


def test_the_default_loads_through_the_real_parser_without_complaint():
    assert load_default().warnings == ()


# ---------------------------------------------------------------------------
# Keycap suggestions
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "binding,expected",
    [
        ({"action": ANSWER_PERMISSION, "answer": "approve"}, "APPR"),
        ({"action": ANSWER_PERMISSION, "answer": "always"}, "APPR"),
        ({"action": ANSWER_PERMISSION}, "APPR"),
        ({"action": ANSWER_PERMISSION, "answer": "reject"}, "REJ"),
    ],
)
def test_the_editor_offers_the_cap_the_vendor_printed_for_this(binding, expected):
    assert keycaps.suggest(binding) == expected


def test_the_shipped_default_agrees_with_its_own_suggestions():
    """What the editor would offer is what the pad already wears.

    Unbound keys are exempt, and deliberately so: the MIC cap is on the pad
    whether or not you have picked a dictation app yet. The cap is a property
    of the object in front of you, not a label for the binding.
    """
    import json

    from freemicro.padconfig import DEFAULT_CONFIG_PATH

    document = json.loads(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))
    for input_id, cap in document["keycaps"].items():
        binding = document["bindings"][input_id]
        if binding.get("action") == "none":
            continue
        suggested = keycaps.suggest(binding)
        assert suggested == cap, f"{input_id} wears {cap}, editor offers {suggested}"
