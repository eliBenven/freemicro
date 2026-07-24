"""Tests for the user-editable pad config: loading, validation, resolution."""

from __future__ import annotations

import json

import pytest

from freemicro import padconfig
from freemicro.padconfig import (
    DEFAULT_CONFIG_PATH,
    KNOWN_INPUTS,
    PadConfigError,
    load,
    load_default,
    parse,
    resolve_path,
    write_starter,
)
from freemicro.state.engine import AgentState


def _write(tmp_path, data, name="keymap.json"):
    path = tmp_path / name
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


MINIMAL = {"version": 1, "bindings": {"AG00": {"action": "key", "key": "escape"}}}


# ---------------------------------------------------------------------------
# The shipped default
# ---------------------------------------------------------------------------

def test_shipped_default_is_valid_and_complete():
    pad = load_default()
    assert pad.warnings == ()
    for input_id in KNOWN_INPUTS:
        assert input_id in pad.bindings, f"{input_id} is unbound in the default"
    for state in AgentState:
        assert pad.lighting.for_state(state) is not None


def test_default_is_used_when_nothing_else_exists():
    pad = load()
    assert pad.source == DEFAULT_CONFIG_PATH
    assert pad.origin == "built-in default"


def test_the_wide_mic_cap_ships_unbound_on_both_of_its_switches():
    """The mic is off by default, and both halves of the cap say so.

    Two separate facts, both pinned here:

    * A dictation shortcut belongs to an app the user may not own. Guessing one
      is a key that silently does nothing, with no feedback anywhere, on the
      biggest cap on the pad - so the shipped default guesses nothing and
      ``freemicro start`` / the web UI ask instead.
    * The wide MIC keycap spans **two** switches and the pad reports both, so
      only one of them may ever act or the shortcut fires twice. Whatever the
      user binds later, ACT11 must stay silent.
    """
    pad = load_default()
    assert pad.bindings["ACT10"].kind == "none", (
        "the default must not assume a dictation app the user may not have"
    )
    assert pad.bindings["ACT11"].kind == "none", (
        "the second half of the wide cap must stay silent or the mic fires twice"
    )


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def test_string_shorthand_means_type_this_text():
    pad = parse({"version": 1, "bindings": {"AG00": "/resume"}})
    action = pad.bindings["AG00"]
    assert action.kind == "text"
    assert action.params["text"] == "/resume"
    assert not action.params.get("submit")


def test_label_defaults_to_the_input_id():
    pad = parse(MINIMAL)
    assert pad.bindings["AG00"].label == "AG00"


def test_comment_may_be_a_list_of_lines():
    pad = parse({
        "version": 1,
        "bindings": {"AG00": {"action": "none", "comment": ["one", "two"]}},
    })
    assert pad.bindings["AG00"].comment == "one two"


def test_underscore_keys_are_treated_as_comments():
    pad = parse({"version": 1, "bindings": {"_note": "ignore me", "AG00": "hi"}})
    assert list(pad.bindings) == ["AG00"]


@pytest.mark.parametrize(
    "data,fragment",
    [
        ([], "JSON object"),
        ({"version": 2, "bindings": {}}, "version"),
        ({"version": 1}, "bindings"),
        ({"version": 1, "bindings": []}, "bindings"),
        ({"version": 1, "bindings": {"AG00": 5}}, "must be an object"),
        ({"version": 1, "bindings": {"AG00": {}}}, "action"),
        ({"version": 1, "bindings": {"AG00": {"action": "nope"}}}, "unknown action"),
        ({"version": 1, "bindings": {"AG00": {"action": "text"}}}, "missing"),
        (
            {"version": 1, "bindings": {"AG00": {"action": "key", "key": "nope"}}},
            "unknown key",
        ),
        (
            {"version": 1, "bindings": {"AG00": {"action": "text", "text": "x",
                                                 "sumbit": True}}},
            "sumbit",
        ),
    ],
)
def test_invalid_configs_are_rejected_with_a_useful_message(data, fragment):
    with pytest.raises(PadConfigError) as exc:
        parse(data)
    assert fragment in str(exc.value)


def test_unknown_input_ids_warn_rather_than_fail():
    """A future firmware key should be bindable without a FreeMicro release."""
    pad = parse({"version": 1, "bindings": {"ACT99": {"action": "none"}}})
    assert "ACT99" in pad.bindings
    assert any("ACT99" in w for w in pad.warnings)


# ---------------------------------------------------------------------------
# Lighting
# ---------------------------------------------------------------------------

def test_lighting_parses_colours_effects_and_zones():
    pad = parse({
        "version": 1,
        "bindings": {},
        "lighting": {
            "zones": ["keys", "agent_keys"],
            "states": {
                "done": {"color": [0, 255, 0], "effect": "breath",
                         "brightness": 0.5, "speed": 0.25},
            },
        },
    })
    light = pad.lighting.for_state(AgentState.DONE)
    assert light.color == 0x00FF00
    assert light.effect == 4
    assert light.brightness == 0.5
    assert pad.lighting.drives_backlight        # "keys" is an alias
    assert pad.lighting.drives_agent_keys
    assert not pad.lighting.drives_underglow


def test_state_light_renders_a_preview_zone():
    pad = parse({
        "version": 1, "bindings": {},
        "lighting": {"states": {"error": {"color": "#FF0000"}}},
    })
    assert pad.lighting.for_state(AgentState.ERROR).to_zone() == {
        "effect": 1, "brightness": 1.0, "speed": 0.0, "color": 0xFF0000,
    }


@pytest.mark.parametrize(
    "lighting,fragment",
    [
        ({"zones": ["nope"]}, "zone"),
        ({"zones": []}, "non-empty"),
        ({"on_exit": "explode"}, "on_exit"),
        ({"states": {"sleeping": {"color": "#000000"}}}, "unknown state"),
        ({"states": {"done": {}}}, "color"),
        ({"states": {"done": {"color": "#000000", "brightness": 4}}}, "brightness"),
        ({"states": {"done": {"color": "#000000", "wat": 1}}}, "unknown field"),
        ({"states": {"done": {"color": "#000000", "effect": "disco"}}}, "effect"),
    ],
)
def test_invalid_lighting_is_rejected(lighting, fragment):
    with pytest.raises(PadConfigError) as exc:
        parse({"version": 1, "bindings": {}, "lighting": lighting})
    assert fragment in str(exc.value)


def test_a_single_zone_may_be_written_as_a_string():
    pad = parse({"version": 1, "bindings": {},
                 "lighting": {"zones": "underglow"}})
    assert pad.lighting.zones == ("underglow",)


# ---------------------------------------------------------------------------
# Joystick
# ---------------------------------------------------------------------------

def test_joystick_maps_angles_to_directions():
    pad = parse({"version": 1, "bindings": {},
                 "joystick": {"directions": ["A", "B", "C", "D"]}})
    joystick = pad.joystick
    assert joystick.direction_for(0.0) == "A"
    assert joystick.direction_for(0.25) == "B"
    assert joystick.direction_for(0.5) == "C"
    assert joystick.direction_for(0.75) == "D"
    assert joystick.direction_for(0.99) == "A"  # wraps


def test_joystick_origin_rotates_the_wheel():
    pad = parse({"version": 1, "bindings": {},
                 "joystick": {"origin": 0.25, "directions": ["A", "B", "C", "D"]}})
    assert pad.joystick.direction_for(0.25) == "A"


@pytest.mark.parametrize(
    "joystick", [{"directions": ["only"]}, {"deadzone": 5}, {"directions": [1, 2]}]
)
def test_invalid_joystick_is_rejected(joystick):
    with pytest.raises(PadConfigError):
        parse({"version": 1, "bindings": {}, "joystick": joystick})


def test_unbound_joystick_direction_warns():
    pad = parse({"version": 1, "bindings": {},
                 "joystick": {"mode": "directions"}})
    assert any("JOY_UP" in w for w in pad.warnings)


# ---------------------------------------------------------------------------
# Joystick: pointer mode
# ---------------------------------------------------------------------------

def test_pointer_is_the_default_mode():
    """The owner wants a cursor, and the shipped flick bindings were only ever
    an approximation of one."""
    pad = parse({"version": 1, "bindings": {}})
    assert pad.joystick.mode == "pointer"
    assert pad.joystick.pointing


def test_pointer_and_action_deadzones_are_not_the_same_number():
    """Conflating them is the bug this split exists to prevent: pointing at a
    0.6 deadzone has no usable travel left."""
    joystick = parse({"version": 1, "bindings": {}}).joystick
    assert joystick.deadzone == 0.6          # action: crossing it types
    assert joystick.pointer_deadzone == 0.1  # motion: only rejects slop
    parsed = parse({"version": 1, "bindings": {}, "joystick": {
        "deadzone": 0.55, "pointer_deadzone": 0.08,
    }}).joystick
    assert (parsed.deadzone, parsed.pointer_deadzone) == (0.55, 0.08)


def test_pointer_defaults_are_the_documented_ones():
    joystick = parse({"version": 1, "bindings": {}}).joystick
    assert joystick.max_speed == 1200.0
    assert joystick.gamma == 2.0
    assert joystick.tick_hz == 90.0
    assert joystick.precision_scale == 0.25
    assert joystick.precision_key == ""
    assert joystick.invert_y is False


@pytest.mark.parametrize("joystick, field", [
    ({"mode": "wiggle"}, "joystick.mode"),
    ({"max_speed": 0}, "joystick.max_speed"),
    ({"max_speed": -100}, "joystick.max_speed"),
    ({"gamma": 0}, "joystick.gamma"),
    ({"gamma": -1}, "joystick.gamma"),
    ({"tick_hz": 2}, "joystick.tick_hz"),
    ({"tick_hz": 5000}, "joystick.tick_hz"),
    ({"pointer_deadzone": 1.0}, "joystick.pointer_deadzone"),
    ({"pointer_deadzone": -0.1}, "joystick.pointer_deadzone"),
    ({"precision_scale": 0}, "joystick.precision_scale"),
    ({"precision_scale": 2}, "joystick.precision_scale"),
    ({"precision_key": "ENC_CW"}, "joystick.precision_key"),
    ({"max_speed": "fast"}, "joystick.max_speed"),
    ({"maxspeed": 800}, "joystick"),
])
def test_bad_pointer_tuning_names_the_field(joystick, field):
    with pytest.raises(PadConfigError) as exc:
        parse({"version": 1, "bindings": {}, "joystick": joystick})
    assert field in str(exc.value)


def test_pointer_mode_warns_that_flick_bindings_will_not_fire():
    pad = parse({"version": 1, "bindings": {
        "JOY_UP": {"action": "key", "key": "up"},
    }})
    assert any("will not fire" in w and "JOY_UP" in w for w in pad.warnings)


def test_pointer_mode_does_not_nag_about_the_shipped_mouse_bindings():
    """The default keymap binds all four to `mouse` moves. Those *are* the
    thing pointer mode replaces, so warning about them would be noise."""
    pad = parse({"version": 1, "bindings": {
        "JOY_UP": {"action": "mouse", "y": -40},
        "JOY_DOWN": {"action": "none"},
    }})
    assert not any("will not fire" in w for w in pad.warnings)


def test_the_old_upside_down_wheel_order_warns_instead_of_failing():
    """Configs written against the old order still work - they just have up
    and down swapped - so this is a warning. Silence would be the bug."""
    pad = parse({"version": 1, "bindings": {}, "joystick": {
        "mode": "directions",
        "directions": ["JOY_RIGHT", "JOY_UP", "JOY_LEFT", "JOY_DOWN"],
    }})
    assert any("old right/up/left/down order" in w for w in pad.warnings)


def test_the_corrected_wheel_order_does_not_warn():
    pad = parse({"version": 1, "bindings": {}, "joystick": {
        "mode": "directions",
        "directions": ["JOY_RIGHT", "JOY_DOWN", "JOY_LEFT", "JOY_UP"],
    }})
    assert not any("order" in w for w in pad.warnings)


def test_precision_key_with_a_binding_warns():
    pad = parse({"version": 1, "bindings": {
        "ACT12": {"action": "key", "key": "escape"},
    }, "joystick": {"precision_key": "ACT12"}})
    assert any("precision_key" in w for w in pad.warnings)


# ---------------------------------------------------------------------------
# Locating and writing
# ---------------------------------------------------------------------------

def test_explicit_path_wins(tmp_path):
    path = _write(tmp_path, MINIMAL, name="mine.json")
    pad = load(path)
    assert pad.source == path
    assert pad.origin == str(path)


def test_missing_explicit_path_is_an_error(tmp_path):
    with pytest.raises(PadConfigError):
        load(tmp_path / "nope.json")


def test_env_var_overrides_the_default(tmp_path, monkeypatch):
    path = _write(tmp_path, MINIMAL)
    monkeypatch.setenv(padconfig.ENV_PATH, str(path))
    assert resolve_path() == path


def test_home_config_beats_xdg(tmp_path, monkeypatch):
    home = tmp_path / "home"
    xdg = tmp_path / "xdg"
    monkeypatch.setenv("FREEMICRO_HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    (xdg / "freemicro").mkdir(parents=True)
    (xdg / "freemicro" / "keymap.json").write_text(json.dumps(MINIMAL))
    assert resolve_path() == padconfig.xdg_path()

    home.mkdir(parents=True)
    (home / "keymap.json").write_text(json.dumps(MINIMAL))
    assert resolve_path() == padconfig.user_path()


def test_broken_json_is_fatal_not_silently_ignored(tmp_path):
    path = tmp_path / "keymap.json"
    path.write_text("{ not json")
    with pytest.raises(PadConfigError) as exc:
        load(path)
    assert "valid JSON" in str(exc.value)


def test_write_starter_creates_an_editable_copy(tmp_path):
    target = tmp_path / "nested" / "keymap.json"
    written = write_starter(target)
    assert written == target
    assert json.loads(target.read_text())["bindings"]
    # It must load cleanly - a starter file that fails validation is worthless.
    assert load(target).bindings


def test_write_starter_refuses_to_clobber(tmp_path):
    target = tmp_path / "keymap.json"
    write_starter(target)
    with pytest.raises(PadConfigError):
        write_starter(target)
    write_starter(target, force=True)  # explicit consent is fine


def test_write_starter_defaults_to_the_home_location(tmp_path, monkeypatch):
    monkeypatch.setenv("FREEMICRO_HOME", str(tmp_path / "fm"))
    assert write_starter() == padconfig.user_path()


# ---------------------------------------------------------------------------
# Chords: two keys pressed together, bound as one thing
# ---------------------------------------------------------------------------

def _chorded(**extra):
    data = {
        "version": 1,
        "bindings": {
            "AG00": {"action": "text", "text": "solo0"},
            "AG01": {"action": "text", "text": "solo1"},
            "AG00+AG01": {"action": "text", "text": "both"},
        },
    }
    data.update(extra)
    return parse(data)


def test_a_chord_binding_is_parsed_out_of_bindings():
    """Chords live in their own map so that everything which walks single
    inputs - the LED renderer, the web editor, `keys --list` - is untouched."""
    pad = _chorded()
    assert set(pad.bindings) == {"AG00", "AG01"}
    assert pad.chord_for(["AG00", "AG01"]).params["text"] == "both"
    assert pad.chord_settle_ms == padconfig.DEFAULT_CHORD_SETTLE_MS


def test_chord_order_does_not_matter():
    """Two keys pressed together have no order the hardware can be trusted to
    report, so the config must not pretend they do."""
    forwards = _chorded()
    backwards = parse({
        "version": 1,
        "bindings": {
            "AG00": {"action": "text", "text": "solo0"},
            "AG01": {"action": "text", "text": "solo1"},
            "AG01+AG00": {"action": "text", "text": "both"},
        },
    })
    assert set(forwards.chords) == set(backwards.chords) == {("AG00", "AG01")}
    assert backwards.chord_for(["AG00", "AG01"]).params["text"] == "both"


def test_a_chord_written_both_ways_is_rejected_not_silently_merged():
    with pytest.raises(PadConfigError) as exc:
        parse({"version": 1, "bindings": {
            "AG00+AG01": {"action": "text", "text": "a"},
            "AG01+AG00": {"action": "text", "text": "b"},
        }})
    assert "same chord" in str(exc.value)


def test_an_unlabelled_chord_is_named_canonically():
    pad = parse({"version": 1, "bindings": {
        "AG01+AG00": {"action": "text", "text": "both"},
    }})
    assert pad.chord_for(["AG00", "AG01"]).label == "AG00+AG01"


def test_chord_partners_answers_per_key():
    """Whether a press pays the settle window is a question about one key, so
    this has to be answerable for one key."""
    pad = _chorded()
    assert pad.chord_partners("AG00") == ("AG01",)
    assert pad.chord_partners("AG05") == ()


def test_three_key_chords_are_refused_with_a_reason():
    with pytest.raises(PadConfigError) as exc:
        parse({"version": 1, "bindings": {
            "AG00+AG01+AG02": {"action": "text", "text": "nope"},
        }})
    message = str(exc.value)
    assert "3 keys" in message and "2 keys only" in message


def test_a_chord_cannot_use_a_momentary_input():
    """A dial detent reports one event and no release, so it can never be held
    down alongside anything - a chord using one could never fire."""
    for bad in ("ENC_CW+AG00", "AG00+ENC_CC", "JOY_UP+AG00"):
        with pytest.raises(PadConfigError) as exc:
            parse({"version": 1, "bindings": {bad: {"action": "text", "text": "x"}}})
        assert "momentary" in str(exc.value)


def test_the_dial_press_may_chord_because_it_has_a_release():
    pad = parse({"version": 1, "bindings": {
        "ENC_CLK+AG00": {"action": "text", "text": "x"},
    }})
    assert pad.chord_for(["AG00", "ENC_CLK"]) is not None


def test_a_malformed_chord_id_says_what_to_write_instead():
    for bad in ("AG00+", "+AG00", "AG00++AG01"):
        with pytest.raises(PadConfigError) as exc:
            parse({"version": 1, "bindings": {bad: {"action": "text", "text": "x"}}})
        assert "empty key" in str(exc.value)


def test_a_chord_of_one_key_with_itself_is_rejected():
    with pytest.raises(PadConfigError) as exc:
        parse({"version": 1, "bindings": {
            "AG00+AG00": {"action": "text", "text": "x"},
        }})
    assert "same key twice" in str(exc.value)


def test_an_unknown_chord_member_warns_rather_than_failing():
    """Same policy as a single input id: a future firmware's key can be bound
    today, it just gets told it may never fire."""
    pad = parse({"version": 1, "bindings": {
        "AG00+ACT99": {"action": "text", "text": "x"},
    }})
    assert pad.chord_for(["AG00", "ACT99"]) is not None
    assert any("ACT99" in w for w in pad.warnings)


def test_settle_ms_is_tunable_and_bounded():
    assert _chorded(chords={"settle_ms": 120}).chord_settle_ms == 120.0
    for bad in (-1, padconfig.CHORD_SETTLE_MS_MAX + 1, "soon"):
        with pytest.raises(PadConfigError) as exc:
            _chorded(chords={"settle_ms": bad})
        assert "settle_ms" in str(exc.value)


def test_the_chords_section_rejects_a_typo():
    with pytest.raises(PadConfigError) as exc:
        _chorded(chords={"settle_msec": 40})
    assert "settle_msec" in str(exc.value)


def test_settle_zero_warns_about_chords_it_makes_unreachable():
    """With no settle window nothing is ever held back, so a chord whose keys
    are both bound individually can never fire. Silently dead config is worse
    than a slow one."""
    pad = _chorded(chords={"settle_ms": 0})
    assert any("can never fire" in w for w in pad.warnings)
    # One key declared a pure chord partner makes it reachable again.
    ok = parse({"version": 1, "bindings": {
        "AG00": {"action": "none"},
        "AG01": {"action": "text", "text": "solo1"},
        "AG00+AG01": {"action": "text", "text": "both"},
    }, "chords": {"settle_ms": 0}})
    assert not any("can never fire" in w for w in ok.warnings)


def test_a_config_with_no_chords_gains_nothing_to_go_wrong():
    pad = load_default()
    assert pad.chords == {}
    assert pad.chord_partners("AG00") == ()
