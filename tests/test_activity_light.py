"""The pad changes colour while a key is held.

The mic key is the case this exists for - push-to-talk with no light is a key
you have to trust - but nothing here is about the mic. A binding carries a
``light``, the bridge says when that binding is live, an overlay decides who
owns the pad, and the renderer composes the two.

Four properties are worth more than the colours, and each has its own section
below:

* the layer never destroys agent state, and letting go shows the state **as it
  is then** rather than as it was when the key went down;
* the layer cannot stick - not on a lost key-up, not on a disconnect;
* auto-dim cannot blank the pad while a key is held;
* nothing pretends to know when a *toggle* stopped recording.

No hardware and no wall clock: the device is a recorder and every clock is
injected, so a two-minute timeout costs a microsecond.
"""

from __future__ import annotations

import json
from argparse import Namespace

import pytest

from freemicro import cli
from freemicro.device.lighting import METHOD_RGBCFG, METHOD_THREAD_STATUS
from freemicro.input.actions import Action, ActionError, Backend, RecordingBackend
from freemicro.input.bridge import Bridge
from freemicro.lighting_owner import (
    REASON_ACTIVITY_DROPPED,
    REASON_ACTIVITY_TIMEOUT,
    ActivityOverlay,
)
from freemicro.padconfig import (
    ACTIVITY_TIMEOUT_MAX,
    DEFAULT_ACTIVITY_TIMEOUT,
    DEFAULT_CONFIG_PATH,
    FACTORY_PALETTE,
    FACTORY_RECORDING,
    ActivityLight,
    PadConfigError,
    factory_recording_light,
    load_default,
    parse,
)
from freemicro.renderers.micro_leds import MicroLedsRenderer
from freemicro.state.engine import AgentState

MIC_LIGHT = {
    "color": FACTORY_RECORDING, "effect": "snake", "speed": 0.4,
    "zones": ["underglow"],
}


def _doc(bindings=None, lighting=None, **rest):
    document = {
        "version": 1,
        "bindings": bindings if bindings is not None else {
            "ACT10": {"action": "hold", "key": "ctrl+cmd+o", "label": "mic",
                      "light": dict(MIC_LIGHT)},
        },
        "lighting": {
            "enabled": True, "zones": ["agent_keys"], "on_exit": "leave",
            "auto_dim_seconds": 0,
        },
        # One look on all six keys, so these tests assert the composition
        # rather than whichever projects happen to be live on this machine.
        "agent_keys": {"policy": "mirror"},
    }
    if lighting is not None:
        document["lighting"].update(lighting)
    document.update(rest)
    return document


def _config(**kwargs):
    return parse(_doc(**kwargs))


class FakeDevice:
    def __init__(self):
        self.sent: list = []

    def send(self, message):
        self.sent.append(message)

    def close(self):
        pass


class FakeClock:
    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _ambient(messages):
    """The underglow side of whatever rgbcfg frame is in ``messages``."""
    for message in messages:
        if message.get("m") == METHOD_RGBCFG:
            return message["p"].get("ambient")
    return None


def _agent_keys(messages):
    for message in messages:
        if message.get("m") == METHOD_THREAD_STATUS:
            return message["p"]
    return None


# ---------------------------------------------------------------------------
# The config: what a binding may say about the pad
# ---------------------------------------------------------------------------

def test_a_binding_can_say_what_the_pad_shows_while_it_is_held():
    light = _config().bindings["ACT10"].light
    assert isinstance(light, ActivityLight)
    assert light.color == factory_recording_light().color
    assert light.zones == ("underglow",)


def test_the_light_never_reaches_the_action_that_carries_it():
    """It describes the binding, not the work - so no kind has to accept it."""
    action = _config().bindings["ACT10"]
    assert "light" not in action.params
    assert action.kind == "hold"


def test_the_defaults_are_the_underglow_and_a_timeout():
    config = _config(bindings={
        "ACT10": {"action": "hold", "key": "f5", "light": {"color": "#123456"}},
    })
    light = config.bindings["ACT10"].light
    assert light.zones == ("underglow",)
    assert light.timeout_seconds == DEFAULT_ACTIVITY_TIMEOUT
    assert light.brightness == 1.0


def test_the_default_colour_is_the_vendors_own_recording_colour():
    """Factory parity, the same promise the five state colours keep."""
    assert FACTORY_RECORDING == "#2E8B57"
    assert factory_recording_light().speed == 0.4


def test_the_default_colour_is_not_any_of_the_five_state_colours():
    """Red is the recording idiom and red is already 'error'. See the docs."""
    assert FACTORY_RECORDING not in FACTORY_PALETTE.values()
    assert FACTORY_PALETTE[AgentState.ERROR] == "#FF0033"


def test_a_light_needs_a_colour():
    with pytest.raises(PadConfigError, match="needs a \"color\""):
        _config(bindings={"ACT10": {"action": "hold", "key": "f5", "light": {}}})


def test_a_misspelled_light_field_is_refused_rather_than_ignored():
    with pytest.raises(PadConfigError, match="unknown field"):
        _config(bindings={"ACT10": {
            "action": "hold", "key": "f5",
            "light": {"color": "#FFFFFF", "colour": "#FFFFFF"},
        }})


@pytest.mark.parametrize("input_id", ["ENC_CW", "ENC_CC", "JOY_UP"])
def test_an_input_with_no_release_cannot_carry_a_light(input_id):
    """Nothing could ever turn it off again, so this is an error, not a warning."""
    with pytest.raises(PadConfigError, match="one event and no release"):
        _config(bindings={input_id: {"action": "key", "key": "a",
                                     "light": {"color": "#FFFFFF"}}})


def test_there_is_no_never_for_the_timeout():
    with pytest.raises(PadConfigError, match="no 'never'"):
        _config(bindings={"ACT10": {
            "action": "hold", "key": "f5",
            "light": {"color": "#FFFFFF", "timeout_seconds": 0},
        }})


def test_the_timeout_is_capped():
    with pytest.raises(PadConfigError, match="at most"):
        _config(bindings={"ACT10": {
            "action": "hold", "key": "f5",
            "light": {"color": "#FFFFFF",
                      "timeout_seconds": ACTIVITY_TIMEOUT_MAX + 1},
        }})


def test_a_light_on_a_non_hold_binding_warns_about_toggles():
    """A tap-to-toggle mic would flash and go dark while still recording."""
    config = _config(bindings={
        "ACT10": {"action": "key", "key": "f5", "light": {"color": "#FFFFFF"}},
    })
    assert any("cannot see a toggle stop" in w for w in config.warnings)


def test_a_hold_binding_earns_no_such_warning():
    assert not any("toggle" in w for w in _config().warnings)


def test_a_light_with_the_leds_off_says_so():
    config = _config(lighting={"enabled": False})
    assert any("freemicro lights --enable" in w for w in config.warnings)


def test_the_zones_a_light_may_claim_are_a_property_of_the_config():
    """Fixed, so every frame can paint every zone and nothing is an undo."""
    assert _config().activity_zones == ("underglow",)
    assert _config(bindings={"ACT10": {"action": "hold", "key": "f5"}}
                   ).activity_zones == ()


def test_a_chord_can_light_the_pad_too():
    config = _config(bindings={
        "AG00": {"action": "none"},
        "AG01": {"action": "none"},
        "AG00+AG01": {"action": "hold", "key": "f5", "label": "both",
                      "light": dict(MIC_LIGHT)},
    })
    assert [i for i, _ in config.activity_lights()] == ["AG00+AG01"]


def test_the_shipped_default_ships_no_light_but_documents_one():
    """MIC is unbound on purpose - a guessed dictation shortcut does nothing.

    So the recipe has to be in the file people open, not only in the docs.
    """
    assert load_default().activity_lights() == ()
    document = json.loads(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))
    readme = " ".join(document["_readme"])
    assert "LIGHT WHILE HELD" in readme
    assert FACTORY_RECORDING in readme
    assert "timeout_seconds" in readme
    # And on the MIC key itself, where somebody editing it will be looking.
    assert FACTORY_RECORDING in " ".join(
        document["bindings"]["ACT10"]["comment"]
    )


# ---------------------------------------------------------------------------
# The bridge: saying when a binding is live
# ---------------------------------------------------------------------------

def _bridge(config=None, backend=None):
    config = config if config is not None else _config()
    seen: list = []
    bridge = Bridge(
        config, backend or RecordingBackend(), autostart=False,
        on_activity=lambda input_id, light: seen.append((input_id, light)),
    )
    return bridge, seen


def test_pressing_a_lit_binding_reports_it_live_and_releasing_retires_it():
    bridge, seen = _bridge()
    bridge.press("ACT10")
    assert seen[-1][0] == "ACT10" and seen[-1][1] is not None
    bridge.release("ACT10")
    assert seen[-1] == ("ACT10", None)


def test_an_unlit_binding_says_nothing_at_all():
    """The 99% case must not pay for this feature."""
    bridge, seen = _bridge(_config(bindings={
        "ACT10": {"action": "key", "key": "escape"},
    }))
    bridge.press("ACT10")
    bridge.release("ACT10")
    assert seen == []


def test_a_press_refused_because_a_hold_is_down_never_lights():
    """It did not happen, so the pad must not say it did."""
    config = _config(bindings={
        "ACT10": {"action": "hold", "key": "ctrl+cmd+o", "label": "mic"},
        "ACT09": {"action": "key", "key": "a", "light": dict(MIC_LIGHT)},
    })
    bridge, seen = _bridge(config)
    bridge.press("ACT10")           # holds real modifiers
    result = bridge.press("ACT09")  # refused
    assert result[0].suppressed
    assert seen == []
    bridge.release("ACT09")
    assert seen == []


def test_an_action_that_fails_takes_its_light_back_down():
    """It did not happen, so the pad must stop saying it did."""

    class Broken(Backend):
        description = "broken"

        def hold_key(self, combo, down):
            raise ActionError("nope")

    bridge, seen = _bridge(backend=Broken())
    result = bridge.press("ACT10")
    assert result[0].ok is False
    assert seen[0][1] is not None      # declared before delivery, as _holding is
    assert seen[-1] == ("ACT10", None)


def test_letting_go_of_the_held_keys_lets_go_of_the_lights():
    """A config reload rebinds the key; nothing would ever send its key-up."""
    bridge, seen = _bridge()
    bridge.press("ACT10")
    bridge.release_held_keys()
    assert seen[-1] == ("ACT10", None)
    # Idempotent: a second sweep says nothing, so a shutdown that calls both
    # release_held_keys() and close() does not double-report.
    seen.clear()
    bridge.release_held_keys()
    assert seen == []


def test_closing_the_bridge_retires_every_light():
    bridge, seen = _bridge()
    bridge.press("ACT10")
    bridge.close()
    assert seen[-1] == ("ACT10", None)


def test_a_callback_that_raises_cannot_break_the_key_path():
    def explode(input_id, light):
        raise RuntimeError("boom")

    backend = RecordingBackend()
    bridge = Bridge(_config(), backend, autostart=False, on_activity=explode)
    result = bridge.press("ACT10")
    assert result[0].ok
    assert backend.held == ["ctrl+cmd+o"]


def test_a_chords_light_is_reported_under_the_chord_id():
    config = _config(bindings={
        "AG00": {"action": "none"},
        "AG01": {"action": "none"},
        "AG00+AG01": {"action": "hold", "key": "f5", "light": dict(MIC_LIGHT)},
    })
    bridge, seen = _bridge(config)
    bridge.press("AG00")
    bridge.press("AG01")
    assert seen[-1][0] == "AG00+AG01" and seen[-1][1] is not None
    bridge.release("AG00")
    assert seen[-1] == ("AG00+AG01", None)


# ---------------------------------------------------------------------------
# The overlay: who owns the pad, and giving it back
# ---------------------------------------------------------------------------

def _overlay(clock=None):
    return ActivityOverlay(clock=clock or FakeClock())


def test_nothing_held_means_plain_agent_state():
    assert _overlay().current is None


def test_the_newest_key_wins_and_the_older_one_comes_back():
    overlay = _overlay()
    first = ActivityLight(color=1, effect=1, brightness=1.0, speed=0.0)
    second = ActivityLight(color=2, effect=1, brightness=1.0, speed=0.0)
    overlay.note("ACT10", first)
    overlay.note("ACT09", second)
    assert overlay.current is second
    overlay.note("ACT09", None)
    assert overlay.current is first


def test_a_lost_key_up_is_taken_down_by_the_clock_and_said_out_loud():
    clock = FakeClock()
    overlay = _overlay(clock)
    overlay.note("ACT10", factory_recording_light())
    clock.advance(DEFAULT_ACTIVITY_TIMEOUT - 1)
    assert overlay.poll() is None
    assert overlay.current is not None
    clock.advance(2)
    event = overlay.poll()
    assert event is not None and event.reason == REASON_ACTIVITY_TIMEOUT
    assert "ACT10" in event.message
    assert overlay.current is None


def test_a_light_honours_its_own_timeout_not_a_global_one():
    clock = FakeClock()
    overlay = _overlay(clock)
    overlay.note("ACT10", ActivityLight(
        color=1, effect=1, brightness=1.0, speed=0.0, timeout_seconds=5.0))
    clock.advance(6)
    assert overlay.poll() is not None


def test_an_ordinary_run_polls_to_nothing():
    overlay = _overlay()
    overlay.note("ACT10", factory_recording_light())
    assert overlay.poll() is None
    overlay.note("ACT10", None)
    assert overlay.poll() is None


def test_the_pad_disconnecting_takes_every_light_down_at_once():
    """The common way a release is lost, and the one we do not have to guess."""
    overlay = _overlay()
    overlay.note("ACT10", factory_recording_light())
    event = overlay.clear("the pad disconnected")
    assert event is not None and event.reason == REASON_ACTIVITY_DROPPED
    assert "the pad disconnected" in event.message
    assert overlay.current is None
    assert overlay.clear("again") is None


def test_the_active_ids_are_readable_for_a_log_line():
    overlay = _overlay()
    overlay.note("ACT10", factory_recording_light())
    assert overlay.active == ("ACT10",)


# ---------------------------------------------------------------------------
# The renderer: composing the layer over the state
# ---------------------------------------------------------------------------

def _renderer(config=None, device=None, clock=None):
    return MicroLedsRenderer(
        device=device or FakeDevice(),
        config=config if config is not None else _config(),
        store=None,
        clock=clock or FakeClock(),
    )


def test_the_layer_wins_only_on_the_zones_it_names():
    device = FakeDevice()
    renderer = _renderer(device=device)
    renderer.set_overlay(factory_recording_light())
    renderer.render(AgentState.WORKING)
    assert _ambient(device.sent)["c"] == factory_recording_light().color
    keys = _agent_keys(device.sent)
    assert keys and all(
        entry["c"] == renderer.light_for(AgentState.WORKING).color
        for entry in keys
    )


def test_the_state_still_changes_underneath_a_held_key():
    device = FakeDevice()
    renderer = _renderer(device=device)
    renderer.set_overlay(factory_recording_light())
    renderer.render(AgentState.IDLE)
    device.sent.clear()
    renderer.render(AgentState.ERROR)
    assert _ambient(device.sent)["c"] == factory_recording_light().color
    assert _agent_keys(device.sent)[0]["c"] == (
        renderer.light_for(AgentState.ERROR).color
    )


def test_letting_go_shows_the_state_as_it_is_then_not_as_it_was():
    """No frame is saved and put back, so there is nothing stale to restore."""
    device = FakeDevice()
    renderer = _renderer(device=device)
    renderer.set_overlay(factory_recording_light())
    renderer.render(AgentState.IDLE)
    renderer.set_overlay(None)
    device.sent.clear()
    renderer.render(AgentState.DONE)
    assert _agent_keys(device.sent)[0]["c"] == (
        renderer.light_for(AgentState.DONE).color
    )
    # And the zone the layer had is handed back dark, not left glowing.
    assert _ambient(device.sent) == {"e": 0, "b": 0.0, "s": 0.0, "c": 0}


def test_a_zone_only_a_light_can_claim_is_dark_the_rest_of_the_time():
    device = FakeDevice()
    renderer = _renderer(device=device)
    renderer.render(AgentState.IDLE)
    assert _ambient(device.sent) == {"e": 0, "b": 0.0, "s": 0.0, "c": 0}


def test_a_config_with_no_lights_sends_nothing_extra():
    device = FakeDevice()
    renderer = _renderer(
        config=_config(bindings={"ACT10": {"action": "hold", "key": "f5"}}),
        device=device,
    )
    renderer.render(AgentState.IDLE)
    assert _ambient(device.sent) is None


def test_putting_the_layer_up_and_down_goes_through_the_ordinary_dedupe():
    device = FakeDevice()
    renderer = _renderer(device=device)
    renderer.render(AgentState.IDLE)
    sends = renderer.sends
    renderer.render(AgentState.IDLE)
    assert renderer.sends == sends           # nothing changed, nothing sent
    renderer.set_overlay(factory_recording_light())
    renderer.render(AgentState.IDLE)
    assert renderer.sends == sends + 1
    renderer.render(AgentState.IDLE)
    assert renderer.sends == sends + 1       # still held, still deduped


def test_set_overlay_writes_nothing_by_itself():
    """The pad is painted from one place, so the layer and the state agree."""
    device = FakeDevice()
    renderer = _renderer(device=device)
    assert renderer.set_overlay(factory_recording_light()) is True
    assert device.sent == []
    assert renderer.set_overlay(factory_recording_light()) is False


def test_auto_dim_cannot_blank_the_pad_while_a_key_is_held():
    clock = FakeClock()
    device = FakeDevice()
    renderer = _renderer(
        config=_config(lighting={"auto_dim_seconds": 180}),
        device=device, clock=clock,
    )
    renderer.render(AgentState.IDLE)
    renderer.set_overlay(factory_recording_light())
    renderer.render(AgentState.IDLE)
    clock.advance(600)
    renderer.render(AgentState.IDLE)
    assert renderer.dimmed is False
    # And it dims normally once the key comes up.
    renderer.set_overlay(None)
    renderer.render(AgentState.IDLE)
    clock.advance(600)
    renderer.render(AgentState.IDLE)
    assert renderer.dimmed is True


def test_the_blank_frame_covers_the_zone_only_a_light_claims():
    """Otherwise auto-dim would leave the underglow glowing behind it."""
    clock = FakeClock()
    device = FakeDevice()
    renderer = _renderer(
        config=_config(lighting={"auto_dim_seconds": 180}),
        device=device, clock=clock,
    )
    renderer.render(AgentState.IDLE)
    clock.advance(600)
    device.sent.clear()
    renderer.render(AgentState.IDLE)
    assert renderer.dimmed is True
    assert _ambient(device.sent) == {"e": 0, "b": 0.0, "s": 0.0, "c": 0}


def test_the_exit_frame_covers_it_too():
    device = FakeDevice()
    renderer = _renderer(
        config=_config(lighting={"on_exit": "off"}), device=device,
    )
    renderer.render(AgentState.IDLE)
    device.sent.clear()
    renderer.hand_back()
    assert _ambient(device.sent) == {"e": 0, "b": 0.0, "s": 0.0, "c": 0}


def test_an_explicit_preview_look_stays_on_the_zones_it_was_asked_about():
    """`freemicro lights --color` shows what it was told to and nothing else."""
    renderer = _renderer()
    messages = renderer.messages_for(
        AgentState.IDLE, light=renderer.light_for(AgentState.DONE)
    )
    assert _ambient(messages) is None


# ---------------------------------------------------------------------------
# The run loop, wired the way `freemicro run` wires it
# ---------------------------------------------------------------------------

class _Loop:
    """Stands in for ``run_with_reconnect`` and hands back its callbacks.

    The four pieces of this feature each work on their own above. This is the
    only test that proves they are actually *connected* in ``freemicro run`` -
    which is where a typo would otherwise cost somebody an evening.
    """

    def __init__(self):
        self.handle = None
        self.tick = None

    def __call__(self, handle, on_tick=None, on_connect=None,
                 on_disconnect=None, **kwargs):
        self.handle = handle
        self.tick = on_tick
        self.connect = on_connect
        self.disconnect = on_disconnect


@pytest.fixture
def pipeline(tmp_path, monkeypatch):
    """A ``_run_pipeline`` driven by hand, over a fake pad and a fake clock.

    The clock is put into the overlay from out here rather than sped up in
    ``time``, because the whole point of the timeout is that it is read from a
    monotonic clock captured once - which is also what makes two minutes cost
    nothing to test.
    """
    from freemicro import device as device_module
    from freemicro import lighting_owner

    clock = FakeClock()
    overlay_class = lighting_owner.ActivityOverlay
    monkeypatch.setattr(
        lighting_owner, "ActivityOverlay", lambda: overlay_class(clock=clock)
    )
    path = tmp_path / "keymap.json"
    path.write_text(json.dumps(_doc()), encoding="utf-8")
    pad = FakeDevice()
    pad.transport = "usb"
    loop = _Loop()
    monkeypatch.setattr(cli, "_open_pad", lambda headless=False: pad)
    monkeypatch.setattr(device_module, "run_with_reconnect", loop)
    monkeypatch.setattr(device_module, "close_shared", lambda: None)
    args = Namespace(config=str(path), dry_run=True, verbose=False,
                     interval=0.25, seconds=0.0)
    assert cli._run_pipeline(args) == 0
    loop.connect(pad)
    pad.sent.clear()
    return loop, pad, clock


def _press(loop, key, act=1):
    loop.handle({"m": "v.oai.hid", "p": {"k": key, "act": act}})


def test_run_lights_the_pad_while_the_mic_key_is_held(pipeline):
    loop, pad, _ = pipeline
    _press(loop, "ACT10", 1)
    loop.tick()
    assert _ambient(pad.sent)["c"] == factory_recording_light().color
    pad.sent.clear()
    _press(loop, "ACT10", 0)
    loop.tick()
    assert _ambient(pad.sent) == {"e": 0, "b": 0.0, "s": 0.0, "c": 0}


def test_run_gives_the_pad_back_when_a_key_up_never_arrives(pipeline, capsys):
    """A Bluetooth drop mid-hold must not leave the mic colour on forever."""
    loop, pad, clock = pipeline
    _press(loop, "ACT10", 1)
    loop.tick()
    assert _ambient(pad.sent)["c"] == factory_recording_light().color
    pad.sent.clear()
    capsys.readouterr()

    clock.advance(DEFAULT_ACTIVITY_TIMEOUT + 1)
    loop.tick()
    assert _ambient(pad.sent) == {"e": 0, "b": 0.0, "s": 0.0, "c": 0}
    # And never silently: a pad that stops showing something without saying so
    # is how a user learns not to trust what it shows.
    assert "never reported a key-up" in capsys.readouterr().out


def test_run_gives_the_pad_back_when_the_pad_goes_away(pipeline, capsys):
    loop, pad, _ = pipeline
    _press(loop, "ACT10", 1)
    loop.tick()
    capsys.readouterr()
    loop.disconnect()
    assert "cleared the light held by ACT10" in capsys.readouterr().out
    # And it is genuinely gone, not merely announced: the pad comes back and
    # shows plain agent state.
    loop.connect(pad)
    pad.sent.clear()
    loop.tick()
    assert _ambient(pad.sent) == {"e": 0, "b": 0.0, "s": 0.0, "c": 0}


# ---------------------------------------------------------------------------
# What the user is told
# ---------------------------------------------------------------------------

def test_keys_list_prints_the_light_and_when_it_goes_out(capsys):
    cli._print_keymap(_config())
    out = capsys.readouterr().out
    assert "Lights while the key is held" in out
    assert FACTORY_RECORDING in out
    assert "underglow" in out
    assert f"{DEFAULT_ACTIVITY_TIMEOUT:g}s from the clock alone" in out


def test_keys_list_says_a_toggle_cannot_be_tracked(capsys):
    cli._print_keymap(_config(bindings={
        "ACT10": {"action": "key", "key": "f5", "light": dict(MIC_LIGHT)},
    }))
    out = capsys.readouterr().out
    assert "cannot see a toggle stop" in out
    assert "hold" in out


def test_keys_list_stays_quiet_when_nothing_is_lit(capsys):
    cli._print_keymap(_config(bindings={"ACT10": {"action": "hold", "key": "f5"}}))
    assert "Lights while the key is held" not in capsys.readouterr().out


# ---------------------------------------------------------------------------
# The web UI
# ---------------------------------------------------------------------------

def test_the_editor_is_offered_the_vendors_recording_look():
    from freemicro.webui.api import Api

    status, payload = Api().schema()
    assert status == 200
    meta = payload["activity_light"]
    assert meta["default"]["color"] == FACTORY_RECORDING
    assert meta["default"]["zones"] == ["underglow"]
    assert meta["timeout_default"] == DEFAULT_ACTIVITY_TIMEOUT
    assert meta["timeout_max"] == ACTIVITY_TIMEOUT_MAX
    assert "hold" in meta["tracked_kinds"]


def test_the_editor_is_told_which_kinds_it_can_honestly_track():
    from freemicro.webui.api import Api

    _, payload = Api().schema()
    assert "key" not in payload["activity_light"]["tracked_kinds"]


def test_the_summary_the_browser_draws_describes_the_light():
    from freemicro.webui import configio

    described = configio.describe(_config())["bindings"]["ACT10"]["light"]
    assert described["hex"] == FACTORY_RECORDING
    assert described["zones"] == ["underglow"]
    assert described["effect_name"] == "snake"


def test_a_binding_with_no_light_says_so_rather_than_omitting_it():
    from freemicro.webui import configio

    config = _config(bindings={"ACT10": {"action": "hold", "key": "f5"}})
    assert configio.describe(config)["bindings"]["ACT10"]["light"] is None


def test_numbers_typed_into_a_form_survive_the_round_trip():
    """An HTML form yields strings; the range check must not blame the user."""
    from freemicro.webui import configio

    document = _doc(bindings={"ACT10": {
        "action": "hold", "key": "f5",
        "light": {"color": "#2E8B57", "speed": "0.4", "brightness": "1",
                  "timeout_seconds": "90"},
    }})
    config = configio.validate(document)
    light = config.bindings["ACT10"].light
    assert light.speed == 0.4
    assert light.timeout_seconds == 90.0


def test_picking_a_hold_dictation_app_writes_the_light_with_it():
    from freemicro.webui import starters

    binding = starters.dictation_binding("wispr")
    assert binding["action"] == "hold"
    assert binding["light"]["color"] == FACTORY_RECORDING
    assert binding["light"]["zones"] == ["underglow"]


def test_picking_a_toggle_dictation_app_deliberately_does_not():
    """It would go out while the mic was still live. Better nothing."""
    from freemicro.webui import starters

    binding = starters.dictation_binding("macos")
    assert binding["action"] == "key"
    assert "light" not in binding


def test_every_starter_still_parses_with_the_light_in_it():
    from freemicro.webui import starters

    for starter in starters.starters():
        document = _doc(bindings=starter["bindings"])
        parse(document)


def test_an_action_carrying_a_light_compares_by_value():
    """`run` reloads in place and diffs configs; a light has to count."""
    plain = Action(kind="hold", params={"key": "f5"})
    lit = Action(kind="hold", params={"key": "f5"},
                 light=factory_recording_light())
    assert plain != lit
    assert lit == Action(kind="hold", params={"key": "f5"},
                         light=factory_recording_light())
