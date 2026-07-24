"""Tests for coexisting with the ChatGPT desktop app.

Two things are asserted here, both without hardware and both without real time:

* **Contention is structured.** Which capability, how bad, and why - because a
  single "is the pad busy" boolean is what made the web UI disable *key capture*
  when the vendor app was open, and keys never contended at all.
* **Reassert fires on exactly the right events.** Driven by a fake clock and a
  fake renderer, asserting the exact messages the user will read.
"""

from __future__ import annotations

import json

import pytest

from freemicro import lighting_owner
from freemicro.lighting_owner import (
    CAPABILITY_INPUT,
    CAPABILITY_LIGHTING,
    QUIET_SECONDS,
    REASON_CONFIG,
    REASON_CONFIG_BROKEN,
    REASON_HEARTBEAT,
    REASON_RECONNECT,
    REASON_VENDOR_QUIT,
    REASON_VENDOR_STARTED,
    SEVERITY_ADVISORY,
    SEVERITY_FATAL,
    SOURCE_FREEMICRO,
    SOURCE_VENDOR_APP,
    VENDOR_QUIET_ZONES,
    Contention,
    LightingOwner,
    coexist_advice,
    contention,
    owns_only_quiet_zones,
)
from freemicro.padconfig import ReassertConfig, load_default, parse


class FakeClock:
    """Time that only moves when a test says so."""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeRenderer:
    """The two hooks the owner uses, and a record of every call."""

    def __init__(self) -> None:
        self.sends = 0
        self.invalidations = 0
        self.applied: list = []
        self.activity = 0

    def invalidate(self) -> None:
        self.invalidations += 1

    def note_activity(self) -> None:
        self.activity += 1

    def apply_config(self, config) -> None:
        self.applied.append(config)

    def send_a_frame(self) -> None:
        """Stand in for the renderer actually writing to the pad."""
        self.sends += 1


def _owner(clock=None, renderer=None, vendor=None, **kwargs):
    clock = clock or FakeClock()
    renderer = renderer if renderer is not None else FakeRenderer()
    running = {"value": False} if vendor is None else vendor
    return (
        LightingOwner(
            renderer,
            clock=clock,
            vendor_probe=lambda: running["value"],
            **kwargs,
        ),
        clock,
        renderer,
        running,
    )


# ---------------------------------------------------------------------------
# Contention
# ---------------------------------------------------------------------------

def _clear(**overrides):
    kwargs = dict(
        lock_holder=lambda: None,
        peer_pids=lambda: (),
        vendor_app=lambda: False,
    )
    kwargs.update(overrides)
    return contention(**kwargs)


def test_nothing_running_means_nothing_contended():
    state = _clear()
    assert state.conflicts == ()
    assert state.fatal is False
    assert state.input_blocked is False
    assert state.lighting_contended is False
    assert state.to_dict() == {
        "input": "", "lighting": "", "fatal": False, "notice": "", "conflicts": [],
    }


def test_chatgpt_is_advisory_and_lighting_only():
    """The heart of the fix: the vendor app must never block input."""
    state = _clear(vendor_app=lambda: True)

    assert state.vendor_app_running is True
    assert state.fatal is False
    assert state.input_blocked is False
    assert state.for_capability(CAPABILITY_INPUT) == ()

    (conflict,) = state.for_capability(CAPABILITY_LIGHTING)
    assert conflict.severity == SEVERITY_ADVISORY
    assert conflict.source == SOURCE_VENDOR_APP
    assert conflict.fatal is False
    # It says plainly that the keys keep working...
    assert "still work exactly as" in conflict.detail
    # ...and names both mitigations.
    assert "as soon as ChatGPT quits" in conflict.mitigation
    assert "freemicro lights --coexist" in conflict.mitigation


def test_a_peer_holding_the_lock_is_fatal_to_both_capabilities():
    state = _clear(lock_holder=lambda: {"pid": 4242, "role": "daemon"})

    assert state.fatal is True
    assert state.input_blocked is True
    assert {c.capability for c in state.conflicts} == {
        CAPABILITY_INPUT, CAPABILITY_LIGHTING
    }
    for conflict in state.conflicts:
        assert conflict.severity == SEVERITY_FATAL
        assert conflict.source == SOURCE_FREEMICRO
        assert conflict.pids == (4242,)
    assert "4242" in state.input_reason
    assert state.to_dict()["input"] == state.input_reason


def test_a_peer_process_without_the_lock_is_still_fatal():
    state = _clear(peer_pids=lambda: (77, 78))
    assert state.fatal is True
    assert state.input_blocked is True
    assert "77, 78" in state.lighting_reason


def test_a_peer_outranks_the_vendor_app():
    """One conflict is worth reporting, and the fatal one wins."""
    state = _clear(
        lock_holder=lambda: {"pid": 9, "role": "run"}, vendor_app=lambda: True
    )
    assert state.vendor_app_running is False
    assert all(c.source == SOURCE_FREEMICRO for c in state.conflicts)


def test_contention_carries_a_notice_through():
    assert _clear(notice="reclaimed a stale lock").to_dict()["notice"] == (
        "reclaimed a stale lock"
    )


def test_the_default_probes_are_wired_up(monkeypatch):
    """The zero-argument call is the one every surface makes."""
    monkeypatch.setattr(lighting_owner, "_default_lock_holder", lambda: None)
    monkeypatch.setattr(lighting_owner, "_default_peer_pids", lambda: ())
    monkeypatch.setattr(lighting_owner, "vendor_app_running", lambda: True)
    assert contention().vendor_app_running is True


def test_vendor_app_running_delegates_to_the_shared_detector(monkeypatch):
    """One detector, so nothing drifts from what doctor and the menubar say."""
    monkeypatch.setattr("freemicro.permissions.chatgpt_running", lambda: True)
    assert lighting_owner.vendor_app_running() is True


# ---------------------------------------------------------------------------
# Zone ownership
# ---------------------------------------------------------------------------

def _lighting(zones, enabled=True):
    return parse({
        "version": 1,
        "bindings": {},
        "lighting": {"enabled": enabled, "zones": list(zones)},
    }).lighting


def test_the_backlight_is_the_only_zone_the_vendor_leaves_alone():
    assert VENDOR_QUIET_ZONES == ("backlight",)
    assert owns_only_quiet_zones(_lighting(["backlight"])) is True
    assert owns_only_quiet_zones(_lighting(["agent_keys"])) is False
    assert owns_only_quiet_zones(_lighting(["backlight", "underglow"])) is False


def test_coexist_is_suggested_only_when_it_would_change_something():
    contested = _lighting(["agent_keys"])
    advice = coexist_advice(contested, vendor_running=True)
    assert "freemicro lights --coexist" in advice
    # Nothing to say when the vendor app is not running...
    assert coexist_advice(contested, vendor_running=False) == ""
    # ...when we are not driving the LEDs at all...
    assert coexist_advice(_lighting(["agent_keys"], enabled=False), True) == ""
    # ...or when the user already took the advice.
    assert coexist_advice(_lighting(["backlight"]), vendor_running=True) == ""


def test_backlight_only_really_avoids_the_contested_zones():
    """The setting has to actually stop us writing the Agent Keys."""
    from freemicro.device.lighting import METHOD_RGBCFG
    from freemicro.renderers.micro_leds import MicroLedsRenderer
    from freemicro.state.engine import AgentState

    config = parse({
        "version": 1,
        "bindings": {},
        "lighting": {"enabled": True, "zones": list(VENDOR_QUIET_ZONES)},
    })
    messages = MicroLedsRenderer(config=config).messages_for(AgentState.DONE)
    assert [m["m"] for m in messages] == [METHOD_RGBCFG]
    assert set(messages[0]["p"]) == {"keys"}  # no ambient, no thstatus


# ---------------------------------------------------------------------------
# Reassert - the main fix
# ---------------------------------------------------------------------------

def test_the_first_look_at_the_vendor_app_never_claims_a_transition():
    owner, clock, renderer, running = _owner()
    running["value"] = True
    assert owner.poll() == []
    assert renderer.invalidations == 0


def test_chatgpt_quitting_reasserts_the_lighting():
    owner, clock, renderer, running = _owner()
    running["value"] = True
    owner.poll()                      # first look: ChatGPT is up
    clock.advance(10.0)
    running["value"] = False

    (event,) = owner.poll()

    assert event.reason == REASON_VENDOR_QUIT
    assert event.message == "reasserted lighting (ChatGPT quit)"
    assert event.reasserted is True
    assert event.verbose_only is False
    assert renderer.invalidations == 1


def test_chatgpt_starting_is_reported_but_reasserts_nothing():
    owner, clock, renderer, running = _owner()
    owner.poll()
    clock.advance(10.0)
    running["value"] = True

    (event,) = owner.poll()

    assert event.reason == REASON_VENDOR_STARTED
    assert event.reasserted is False
    assert "will reassert when it quits" in event.message
    assert renderer.invalidations == 0


def test_the_vendor_probe_is_rate_limited_off_the_tick_path():
    """A pgrep per 0.25s tick would be a fork storm. Once per poll_seconds."""
    calls = {"n": 0}

    def probe():
        calls["n"] += 1
        return False

    clock = FakeClock()
    LightingOwner(
        FakeRenderer(),
        clock=clock,
        vendor_probe=probe,
        reassert=ReassertConfig(poll_seconds=3.0),
    ).poll()
    assert calls["n"] == 1

    owner, clock, _, _ = _owner()
    counted = {"n": 0}
    owner._vendor_probe = lambda: counted.__setitem__("n", counted["n"] + 1) or False
    for _ in range(40):               # ten seconds of 0.25s ticks
        clock.advance(0.25)
        owner.poll()
    assert counted["n"] == 4          # not 40


def test_a_key_event_tells_the_renderer_the_user_is_here():
    """Any HID event wakes a dimmed pad (FACTORY-DEFAULTS 4).

    The owner is the only thing in the process that sees every key, so it is
    the only thing that can say so - but it must still not write anything on
    this path, which is why the renderer only gets told, not asked to send.
    """
    owner, clock, renderer, _ = _owner()
    owner.note_input()
    owner.note_input()
    assert renderer.activity == 2
    assert renderer.invalidations == 0, "waking is not reasserting"


def test_a_renderer_without_the_hook_is_still_fine():
    """The owner drives anything with the hooks it happens to have."""

    class Minimal:
        sends = 0

    owner = LightingOwner(Minimal(), clock=FakeClock())
    owner.note_input()  # must not raise


def test_a_keypress_burst_stops_every_reassert_dead():
    """The channel that carries lighting carries key events. Keys win."""
    owner, clock, renderer, running = _owner()
    running["value"] = True
    owner.poll()
    clock.advance(10.0)
    running["value"] = False

    owner.note_input()
    assert owner.busy is True
    assert owner.poll() == []
    assert renderer.invalidations == 0

    clock.advance(QUIET_SECONDS)
    assert owner.busy is False
    assert [e.reason for e in owner.poll()] == [REASON_VENDOR_QUIT]


def test_reconnecting_reasserts_but_the_first_connect_is_silent():
    owner = LightingOwner(clock=FakeClock(), vendor_probe=lambda: False)
    first = FakeRenderer()

    assert owner.attach(first) is None          # startup, not a reconnect
    assert first.invalidations == 0

    owner.attach(None)                          # pad dropped
    second = FakeRenderer()
    event = owner.attach(second)

    assert event.reason == REASON_RECONNECT
    assert event.message == "reasserted lighting (pad reconnected)"
    assert second.invalidations == 1


def test_a_config_change_is_reloaded_and_reasserted(tmp_path):
    from freemicro import padconfig

    path = tmp_path / "keymap.json"
    document = {
        "version": 1,
        "bindings": {},
        "lighting": {"enabled": True, "zones": ["agent_keys"]},
    }
    path.write_text(json.dumps(document), encoding="utf-8")

    clock = FakeClock()
    renderer = FakeRenderer()
    owner = LightingOwner(
        renderer,
        config=padconfig.load(path),
        clock=clock,
        vendor_probe=lambda: False,
    )
    clock.advance(10.0)
    assert owner.poll() == []                   # untouched file, nothing to do

    document["lighting"]["zones"] = ["backlight"]
    path.write_text(json.dumps(document), encoding="utf-8")
    clock.advance(10.0)

    (event,) = owner.poll()

    assert event.reason == REASON_CONFIG
    assert event.message == "reasserted lighting (config changed)"
    assert renderer.invalidations == 1
    assert renderer.applied[-1].lighting.zones == ("backlight",)
    assert owner.config.lighting.zones == ("backlight",)


def test_a_broken_config_edit_says_so_and_keeps_the_running_one(tmp_path):
    from freemicro import padconfig

    path = tmp_path / "keymap.json"
    path.write_text(json.dumps({"version": 1, "bindings": {}}), encoding="utf-8")
    good = padconfig.load(path)

    clock = FakeClock()
    renderer = FakeRenderer()
    owner = LightingOwner(
        renderer, config=good, clock=clock, vendor_probe=lambda: False
    )
    path.write_text("{ not json", encoding="utf-8")
    clock.advance(10.0)

    (event,) = owner.poll()

    assert event.reason == REASON_CONFIG_BROKEN
    assert event.reasserted is False
    assert "still running the old one" in event.message
    assert renderer.invalidations == 0
    assert owner.config is good


# ---------------------------------------------------------------------------
# The heartbeat
# ---------------------------------------------------------------------------

def test_the_heartbeat_is_off_by_default():
    """Re-sending restarts animated effects, so this must be opt-in."""
    assert ReassertConfig().heartbeat_seconds == 0.0
    assert ReassertConfig().heartbeat_enabled is False
    assert load_default().lighting.reassert.heartbeat_enabled is False

    owner, clock, renderer, _ = _owner()
    for _ in range(400):
        clock.advance(0.25)
        assert owner.poll() == []
    assert renderer.invalidations == 0


def test_an_enabled_heartbeat_fires_on_its_interval_and_is_quiet_about_it():
    owner, clock, renderer, _ = _owner(
        reassert=ReassertConfig(heartbeat_seconds=5.0)
    )
    clock.advance(4.9)
    assert owner.poll() == []

    clock.advance(0.2)
    (event,) = owner.poll()

    assert event.reason == REASON_HEARTBEAT
    assert event.message == "reasserted lighting (heartbeat)"
    assert event.verbose_only is True
    assert renderer.invalidations == 1

    clock.advance(1.0)
    assert owner.poll() == []          # and the clock starts over


def test_a_real_frame_pushes_the_heartbeat_back():
    """The heartbeat must never coalesce ahead of a genuine state change."""
    owner, clock, renderer, _ = _owner(
        reassert=ReassertConfig(heartbeat_seconds=5.0)
    )
    clock.advance(4.0)
    owner.poll()
    renderer.send_a_frame()            # a real state change went out
    clock.advance(2.0)                 # 6s since start, but 2s since the send

    assert owner.poll() == []
    clock.advance(5.1)
    assert [e.reason for e in owner.poll()] == [REASON_HEARTBEAT]


def test_a_real_trigger_beats_the_heartbeat_to_it():
    owner, clock, renderer, running = _owner(
        reassert=ReassertConfig(heartbeat_seconds=5.0)
    )
    running["value"] = True
    owner.poll()
    running["value"] = False
    clock.advance(6.0)

    assert [e.reason for e in owner.poll()] == [REASON_VENDOR_QUIT]
    assert renderer.invalidations == 1


def test_reassert_can_be_switched_off_entirely():
    owner, clock, renderer, running = _owner(
        reassert=ReassertConfig(enabled=False, heartbeat_seconds=1.0)
    )
    running["value"] = True
    owner.poll()
    running["value"] = False
    clock.advance(60.0)
    assert owner.poll() == []
    assert renderer.invalidations == 0


def test_an_owner_with_no_renderer_does_nothing_at_all():
    """`freemicro run` builds the owner before the pad is ever opened."""
    probe = {"n": 0}
    owner = LightingOwner(
        clock=FakeClock(), vendor_probe=lambda: probe.__setitem__("n", 1) or True
    )
    assert owner.poll() == []
    assert probe["n"] == 0


# ---------------------------------------------------------------------------
# The renderer side of the contract
# ---------------------------------------------------------------------------

class RecordingDevice:
    def __init__(self) -> None:
        self.sent: list = []

    def send(self, message) -> None:
        self.sent.append(message)

    def close(self) -> None:
        pass


CONFIG = parse({
    "version": 1,
    "bindings": {},
    # `mirror` keeps every Agent Key on the resolved state, so these tests are
    # about resending frames rather than about which project sits on which key.
    "agent_keys": {"policy": "mirror"},
    "lighting": {
        "enabled": True,
        "zones": ["agent_keys"],
        "on_exit": "leave",
        "states": {"done": {"color": "#00FF00", "effect": "solid"}},
    },
})


def _renderer(config=CONFIG):
    from freemicro.renderers.micro_leds import MicroLedsRenderer

    device = RecordingDevice()
    return MicroLedsRenderer(device=device, config=config), device


def test_invalidate_resends_the_same_frame_without_breaking_dedupe():
    from freemicro.state.engine import AgentState

    renderer, device = _renderer()
    renderer.render(AgentState.DONE)
    renderer.render(AgentState.DONE)
    renderer.render(AgentState.DONE)
    assert len(device.sent) == 1, "dedupe must still swallow repeat renders"

    renderer.invalidate()
    renderer.render(AgentState.DONE)
    assert len(device.sent) == 2
    assert device.sent[0] == device.sent[1], "a reassert re-sends, it never edits"

    renderer.render(AgentState.DONE)
    assert len(device.sent) == 2, "dedupe reasserts itself immediately after"


def test_end_to_end_chatgpt_quits_and_the_pad_gets_its_colours_back():
    """Owner + real renderer + fake device: the whole loop `run` executes."""
    from freemicro.state.engine import AgentState

    renderer, device = _renderer()
    clock = FakeClock()
    running = {"value": True}
    owner = LightingOwner(
        renderer, clock=clock, vendor_probe=lambda: running["value"]
    )

    def tick():
        """Exactly what `_run_pipeline` does, in the same order."""
        events = owner.poll()
        renderer.render(AgentState.DONE)
        return [e.message for e in events]

    assert tick() == []                       # first look: ChatGPT is up
    painted = list(device.sent)
    assert painted, "we light the pad even with the vendor app running"

    clock.advance(1.0)
    assert tick() == []                       # deduped: nothing resent
    assert device.sent == painted

    clock.advance(5.0)
    running["value"] = False                  # the user quits ChatGPT
    assert tick() == ["reasserted lighting (ChatGPT quit)"]
    assert device.sent == painted + painted, "the same frame, sent again"

    clock.advance(5.0)
    assert tick() == []                       # and dedupe is back in charge
    assert len(device.sent) == 2 * len(painted)


def test_the_send_counter_is_what_the_heartbeat_watches():
    from freemicro.state.engine import AgentState

    renderer, _ = _renderer()
    assert renderer.sends == 0
    renderer.render(AgentState.DONE)
    assert renderer.sends == 1
    renderer.render(AgentState.DONE)
    assert renderer.sends == 1, "a deduped render is not a send"


def test_applying_a_new_config_repaints_with_the_new_colours():
    from freemicro.device.lighting import parse_color
    from freemicro.state.engine import AgentState

    renderer, device = _renderer()
    renderer.render(AgentState.DONE)
    assert device.sent[0]["p"][0]["c"] == parse_color("#00FF00")

    renderer.apply_config(parse({
        "version": 1,
        "bindings": {},
        "agent_keys": {"policy": "mirror"},
        "lighting": {
            "enabled": True,
            "zones": ["agent_keys"],
            "on_exit": "leave",
            "states": {"done": {"color": "#0000FF", "effect": "solid"}},
        },
    }))
    renderer.render(AgentState.DONE)
    assert device.sent[-1]["p"][0]["c"] == parse_color("#0000FF")


def test_disabling_lighting_in_a_live_config_hands_the_pad_back():
    """The exit obeys the config that was *driving*, not the one switching off."""
    from freemicro.state.engine import AgentState

    driving = parse({
        "version": 1,
        "bindings": {},
        "agent_keys": {"policy": "mirror"},
        "lighting": {
            "enabled": True, "zones": ["agent_keys"], "on_exit": "off",
            "states": {"done": {"color": "#00FF00"}},
        },
    })
    renderer, device = _renderer(config=driving)
    renderer.render(AgentState.DONE)
    renderer.apply_config(parse({
        "version": 1,
        "bindings": {},
        "agent_keys": {"policy": "mirror"},
        # No zones and no on_exit here: the pad still has to come back dark.
        "lighting": {"enabled": False},
    }))
    blanked = device.sent[-1]
    assert all(entry["b"] == 0 and entry["e"] == 0 for entry in blanked["p"])

    renderer.render(AgentState.WORKING)
    assert device.sent[-1] is blanked, "a disabled renderer must stay quiet"


def test_a_renderer_that_never_drove_the_leds_does_not_blank_them_on_exit():
    """Lighting off means we never touch the pad - not even on the way out."""
    renderer, device = _renderer(config=parse({
        "version": 1,
        "bindings": {},
        "lighting": {"enabled": False, "zones": ["agent_keys"], "on_exit": "off"},
    }))
    renderer.close()
    assert device.sent == []


# ---------------------------------------------------------------------------
# Config schema
# ---------------------------------------------------------------------------

def test_the_reassert_section_is_optional_and_validated():
    from freemicro.padconfig import PadConfigError

    default = parse({"version": 1, "bindings": {}}).lighting.reassert
    assert default == ReassertConfig()

    tuned = parse({
        "version": 1,
        "bindings": {},
        "lighting": {"reassert": {"heartbeat_seconds": 5, "poll_seconds": 1}},
    }).lighting.reassert
    assert tuned.heartbeat_seconds == 5.0
    assert tuned.heartbeat_enabled is True

    for broken in (
        {"heartbeat_seconds": -1},
        {"poll_seconds": 0},
        {"hartbeat_seconds": 5},
    ):
        with pytest.raises(PadConfigError):
            parse({
                "version": 1, "bindings": {}, "lighting": {"reassert": broken},
            })


# ---------------------------------------------------------------------------
# The CLI surface
# ---------------------------------------------------------------------------

def _run_cli(argv):
    from freemicro.cli import main

    return main(argv)


def test_lights_coexist_writes_the_backlight_only_config(capsys, tmp_path):
    from freemicro import padconfig

    assert _run_cli(["lights", "--coexist"]) == 0
    config = padconfig.load(padconfig.user_path())
    assert config.lighting.enabled is True
    assert config.lighting.zones == VENDOR_QUIET_ZONES
    assert owns_only_quiet_zones(config.lighting) is True

    out = capsys.readouterr().out
    assert "coexistence setting" in out
    assert "Trade-off" in out           # the glow-vs-per-key cost, stated

    assert _run_cli(["lights", "--zones", "agent_keys"]) == 0
    assert padconfig.load(padconfig.user_path()).lighting.zones == ("agent_keys",)


def test_lights_rejects_a_zone_that_does_not_exist(capsys):
    assert _run_cli(["lights", "--zones", "eyebrows"]) == 2
    assert "Bad zone" in capsys.readouterr().err


def test_run_reports_reasserts_but_keeps_the_heartbeat_quiet(capsys):
    from freemicro.cli import _print_lighting
    from freemicro.lighting_owner import LightingEvent

    _print_lighting(None)
    _print_lighting(LightingEvent(REASON_VENDOR_QUIT, "reasserted (ChatGPT quit)"))
    _print_lighting(LightingEvent(REASON_HEARTBEAT, "beat", verbose_only=True))
    out = capsys.readouterr().out
    assert out == "  [lighting] reasserted (ChatGPT quit)\n"

    _print_lighting(
        LightingEvent(REASON_HEARTBEAT, "beat", verbose_only=True), verbose=True
    )
    assert capsys.readouterr().out == "  [lighting] beat\n"


def test_contention_is_exported_for_every_surface_to_render():
    """doctor, the menubar and the web UI must all read the same truth."""
    assert issubclass(Contention, object)
    assert set(_clear(vendor_app=lambda: True).to_dict()) == {
        "input", "lighting", "fatal", "notice", "conflicts"
    }
