"""Tests for key naming and the action registry.

Every test here runs through :class:`RecordingBackend`, so the suite never types
a character into the developer's terminal and never shells out.
"""

from __future__ import annotations

import pytest

from freemicro.input.actions import (
    MODIFIER_HOLDING_KINDS,
    MODIFIER_SAFE_KINDS,
    REGISTRY,
    Action,
    ActionError,
    Backend,
    RecordingBackend,
    action,
    action_help,
    perform,
    validate_params,
)
from freemicro.input.keys import (
    KeyNameError,
    applescript_for,
    applescript_for_text,
    parse_combo,
)


# ---------------------------------------------------------------------------
# Key names
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "combo,modifiers,base",
    [
        ("escape", (), "escape"),
        ("ctrl-r", ("control",), "r"),
        ("shift-tab", ("shift",), "tab"),
        ("cmd+shift+k", ("command", "shift"), "k"),
        ("ctrl+option+cmd+d", ("command", "control", "option"), "d"),
        ("CTRL-R", ("control",), "r"),
        ("minus", (), "minus"),
    ],
)
def test_parse_combo(combo, modifiers, base):
    assert parse_combo(combo) == (modifiers, base)


def test_modifier_order_is_stable():
    """Same combo, different spelling order, identical AppleScript."""
    assert applescript_for("shift+cmd+k") == applescript_for("cmd+shift+k")


@pytest.mark.parametrize(
    "combo,modifiers,base",
    [
        ("page-up", (), "page-up"),
        ("page-down", (), "page-down"),
        ("forward-delete", (), "forward-delete"),
        ("cmd+page-up", ("command",), "page-up"),
        ("cmd-page-down", ("command",), "page-down"),
        ("ctrl+shift+forward-delete", ("control", "shift"), "forward-delete"),
    ],
)
def test_key_names_containing_a_hyphen_are_writable(combo, modifiers, base):
    """``-`` separates a combo *and* appears inside three key names.

    Splitting naively made ``page-up``, ``page-down`` and ``forward-delete``
    impossible to write: they became ``page`` + ``up``, ``page`` is not a
    modifier, and the resulting error listed those very names as the valid
    options. The whole string is tried first, then the longest hyphenated tail.
    """
    assert parse_combo(combo) == (modifiers, base)


def test_rejoining_a_hyphenated_tail_never_eats_a_modifier():
    """The tail rule must not turn a real combo into a bare key.

    Safe only because no modifier name contains a hyphen; if one ever did,
    ``ctrl-r`` could start resolving to a key called ``ctrl-r``. Pinned so that
    change cannot land quietly.
    """
    from freemicro.input.keys import MODIFIER_ALIASES

    assert not [name for name in MODIFIER_ALIASES if "-" in name]
    assert parse_combo("ctrl-r") == (("control",), "r")
    assert parse_combo("shift-tab") == (("shift",), "tab")


@pytest.mark.parametrize("combo", ["", "   ", "hyper-x", "ctrl-nosuchkey", 5, None])
def test_parse_combo_rejects_nonsense(combo):
    with pytest.raises(KeyNameError):
        parse_combo(combo)


def test_named_keys_use_key_code_and_characters_use_keystroke():
    assert "key code 53" in applescript_for("escape")
    assert 'keystroke "r"' in applescript_for("ctrl-r")
    assert "using {control down}" in applescript_for("ctrl-r")


def test_literal_alias_resolves_to_its_character():
    assert 'keystroke "-"' in applescript_for("minus")


def test_text_is_escaped_for_applescript():
    script = applescript_for_text('say "hi" \\ now')
    assert '\\"hi\\"' in script
    assert "\\\\" in script


# ---------------------------------------------------------------------------
# Registry and validation
# ---------------------------------------------------------------------------

def test_builtin_action_kinds_are_registered():
    assert set(REGISTRY) >= {"text", "key", "shell", "applescript", "none"}


def test_action_help_lists_every_kind():
    lines = action_help()
    assert len(lines) == len(REGISTRY)
    assert any(line.startswith("text") for line in lines)


def test_validate_requires_declared_fields():
    with pytest.raises(ValueError) as exc:
        validate_params("text", {})
    assert "text" in str(exc.value)


def test_validate_rejects_unknown_fields():
    with pytest.raises(ValueError) as exc:
        validate_params("text", {"text": "hi", "sumbit": True})
    assert "sumbit" in str(exc.value)


def test_validate_rejects_unknown_kind():
    with pytest.raises(ValueError):
        validate_params("teleport", {})


def test_validate_checks_key_names_at_config_time():
    validate_params("key", {"key": "shift-tab"})
    with pytest.raises(ValueError):
        validate_params("key", {"key": "shift-tabb"})


def test_optional_fields_are_allowed():
    validate_params("shell", {"command": "ls", "cwd": "/tmp", "wait": True})


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

def test_text_action_types_and_submits():
    backend = RecordingBackend()
    perform(Action("text", {"text": "/resume", "submit": True}), backend)
    assert backend.calls == [
        ("type_text", ("/resume",)),
        ("press_key", ("return",)),
    ]


def test_text_action_without_submit_does_not_press_return():
    backend = RecordingBackend()
    perform(Action("text", {"text": "@"}), backend)
    assert backend.calls == [("type_text", ("@",))]


def test_key_action():
    backend = RecordingBackend()
    perform(Action("key", {"key": "escape"}), backend)
    assert backend.calls == [("press_key", ("escape",))]


def test_shell_action_defaults_to_fire_and_forget():
    backend = RecordingBackend()
    perform(Action("shell", {"command": "say hi"}), backend)
    assert backend.calls == [("run_shell", ("say hi", None, False))]


def test_applescript_action():
    backend = RecordingBackend()
    perform(Action("applescript", {"script": 'display dialog "x"'}), backend)
    assert backend.calls == [("run_applescript", ('display dialog "x"',))]


def test_none_action_does_nothing():
    backend = RecordingBackend()
    perform(Action("none", {}), backend)
    assert backend.calls == []


def test_backend_failures_become_action_errors():
    class Broken(Backend):
        def type_text(self, text):
            raise RuntimeError("boom")

    with pytest.raises(ActionError) as exc:
        perform(Action("text", {"text": "hi"}), Broken())
    assert "boom" in str(exc.value)


def test_unimplemented_backend_method_is_reported_not_crashed():
    with pytest.raises(ActionError):
        perform(Action("key", {"key": "escape"}), Backend())


def test_describe_is_human_readable():
    assert "'/clear'" in Action("text", {"text": "/clear", "submit": True}).describe()
    assert "Return" in Action("text", {"text": "/clear", "submit": True}).describe()
    assert Action("none", {}).describe() == "(unbound)"


def test_a_new_action_kind_needs_only_a_decorated_function():
    """The extension point advertised in the module docstring must really work."""
    seen = []

    @action("test-kind", summary="A test action.", required=("value",))
    def _run(act, backend):
        seen.append(act.params["value"])

    try:
        validate_params("test-kind", {"value": 1})
        perform(Action("test-kind", {"value": 1}), RecordingBackend())
        assert seen == [1]
        with pytest.raises(ValueError):
            validate_params("test-kind", {})
    finally:
        REGISTRY.pop("test-kind")


def test_a_kinds_risk_is_declared_where_the_kind_is_written():
    """The two held-modifier questions are properties of a kind, not lists.

    They used to be two hand-written sets in ``input.bridge``, which is the one
    file nobody adding an action kind opens. Deriving them from the registry
    means the classification travels with the kind - and being wrong is a
    failing test rather than a silent misclassification.
    """
    assert MODIFIER_SAFE_KINDS == frozenset(
        kind for kind, spec in REGISTRY.items() if spec.modifier_safe
    )
    assert MODIFIER_HOLDING_KINDS == frozenset(
        kind for kind, spec in REGISTRY.items() if spec.holds_keys
    )
    # A kind that holds real keys down is by definition not safe to run
    # alongside one that does.
    assert MODIFIER_HOLDING_KINDS.isdisjoint(MODIFIER_SAFE_KINDS)


def test_an_unclassified_kind_is_assumed_to_type():
    """The default is the safety property, so it is asserted directly.

    An allowlist that a new kind joins by *forgetting* to say anything would be
    no allowlist at all. Registering a kind without a word about modifiers
    leaves it suppressed, which costs one logged skip; the other way round it
    costs an arbitrary system shortcut.
    """
    @action("test-unclassified", summary="A test action.")
    def _run(act, backend):
        pass

    try:
        assert REGISTRY["test-unclassified"].modifier_safe is False
        assert REGISTRY["test-unclassified"].holds_keys is False
        # And a kind registered after import is not in the snapshot at all,
        # which lands on the same side.
        assert "test-unclassified" not in MODIFIER_SAFE_KINDS
    finally:
        REGISTRY.pop("test-unclassified")


# ---------------------------------------------------------------------------
# Host automation kinds
# ---------------------------------------------------------------------------

def test_app_action_activates_and_can_cycle():
    backend = RecordingBackend()
    perform(Action("app", {"name": "Google Chrome", "cycle": True}), backend)
    assert backend.calls == [("activate_app", ("Google Chrome", True))]


def test_mouse_action_moves_relatively_by_default():
    backend = RecordingBackend()
    perform(Action("mouse", {"x": 40}), backend)
    assert backend.calls == [("move_mouse", (40.0, 0.0, True))]


def test_mouse_action_clicks():
    backend = RecordingBackend()
    perform(Action("mouse", {"click": "left", "count": 2}), backend)
    assert backend.calls == [("click_mouse", ("left", 2))]


def test_mouse_action_can_move_and_click_together():
    backend = RecordingBackend()
    perform(Action("mouse", {"x": 5, "y": 5, "click": "right"}), backend)
    assert [name for name, _ in backend.calls] == ["move_mouse", "click_mouse"]


def test_hold_action_presses_and_release_lets_go():
    from freemicro.input.actions import release

    backend = RecordingBackend()
    act = Action("hold", {"key": "ctrl+option+cmd+d"})
    perform(act, backend)
    release(act, backend)
    assert backend.calls == [
        ("hold_key", ("ctrl+option+cmd+d", True)),
        ("hold_key", ("ctrl+option+cmd+d", False)),
    ]


# ---------------------------------------------------------------------------
# Held keys must always come back up
# ---------------------------------------------------------------------------
#
# A `hold` binding presses real modifier keys. If the matching release is lost
# - Ctrl-C, a Bluetooth drop, a config reload, a self re-exec - macOS believes
# Ctrl and Cmd are physically down, every app misbehaves, and quitting
# FreeMicro does not fix it. The only cure is the process that pressed them
# sending the key-ups on every exit path there is.

def test_the_quartz_registry_tracks_and_releases_a_whole_chord():
    """The real registry, without posting a single event: the poster is
    stubbed, so this asserts the bookkeeping that the safety net depends on."""
    from freemicro.input import quartz

    posted = []
    original_key_event = quartz.key_event
    original_guard = quartz.install_release_guard
    quartz.key_event = lambda code, down, flags=0: posted.append((code, down))
    quartz.install_release_guard = lambda: None
    quartz.release_all()  # start clean
    try:
        quartz.hold_chord(31, ("control", "command"), down=True)  # ctrl+cmd+o
        assert quartz.held_keys() == (59, 55, 31)
        del posted[:]

        released = quartz.release_all()
        assert released == 3
        assert [code for code, down in posted] == [31, 55, 59]  # reverse order
        assert all(down is False for _, down in posted)
        assert quartz.held_keys() == ()

        # Idempotent: a second call must be a harmless no-op, because it runs
        # from atexit *and* a signal handler *and* Bridge.close().
        del posted[:]
        assert quartz.release_all() == 0
        assert posted == []
    finally:
        quartz.key_event = original_key_event
        quartz.install_release_guard = original_guard
        quartz.release_all()


def test_a_normal_release_deregisters_the_chord():
    from freemicro.input import quartz

    original_key_event = quartz.key_event
    original_guard = quartz.install_release_guard
    quartz.key_event = lambda code, down, flags=0: None
    quartz.install_release_guard = lambda: None
    quartz.release_all()
    try:
        quartz.hold_chord(31, ("control",), down=True)
        quartz.hold_chord(31, ("control",), down=False)
        assert quartz.held_keys() == (), "nothing left to release"
    finally:
        quartz.key_event = original_key_event
        quartz.install_release_guard = original_guard
        quartz.release_all()


def test_the_signal_path_releases_first_and_then_does_what_it_was_going_to():
    """A finally block is not enough: Python does not unwind one on a default
    SIGTERM, and os.execv never runs atexit. So the handler has to let go, and
    then still let the signal do its job - swallowing a SIGTERM to save a
    keystroke would be the worse bug."""
    import signal

    from freemicro.input import quartz

    order = []
    original = quartz.release_all
    quartz.release_all = lambda: order.append("released")
    try:
        quartz._release_and_chain(signal.SIGTERM, lambda *a: order.append("chained"))
        assert order == ["released", "chained"]

        del order[:]
        quartz._release_and_chain(signal.SIGINT, signal.SIG_IGN)
        assert order == ["released"], "an ignored signal still gets the keys up"
    finally:
        quartz.release_all = original


def test_release_all_is_safe_when_quartz_is_not_available():
    """Linux, or a Mac without the framework. It must not raise there either."""
    from freemicro.input import quartz

    assert quartz.release_all() >= 0


def test_a_backend_that_cannot_hold_keys_has_nothing_to_release():
    from freemicro.input.actions import AppleScriptBackend, Backend

    assert Backend().release_held_keys() == 0
    assert AppleScriptBackend().release_held_keys() == 0


def test_the_recording_backend_reports_what_was_still_down():
    backend = RecordingBackend()
    backend.hold_key("ctrl+cmd+o", True)
    backend.hold_key("shift+f5", True)
    backend.hold_key("shift+f5", False)      # this one was released normally
    assert backend.release_held_keys() == 1
    assert backend.calls[-2:] == [
        ("hold_key", ("ctrl+cmd+o", False)),
        ("release_held_keys", (1,)),
    ]
    assert backend.held == []
    assert backend.release_held_keys() == 0, "idempotent"


def test_hold_key_names_are_validated_like_key_names():
    validate_params("hold", {"key": "fn-space"})
    with pytest.raises(ValueError):
        validate_params("hold", {"key": "fnn"})


def test_fn_parses_but_applescript_refuses_it():
    """macOS handles fn below the System Events API - say so, don't fail late."""
    from freemicro.input.keys import applescript_for

    assert parse_combo("fn-space") == (("fn",), "space")
    with pytest.raises(KeyNameError) as exc:
        applescript_for("fn-space")
    assert "fn" in str(exc.value)


def test_best_backend_is_usable_everywhere():
    from freemicro.input.actions import best_backend

    backend = best_backend()
    assert backend.description
