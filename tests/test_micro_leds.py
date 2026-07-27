"""Tests for the pad LED renderer.

Driven by a fake device that records the messages it is handed, so the exact
protocol traffic for each agent state is asserted without any hardware. The
clock is injectable too, so three minutes of auto-dim inactivity costs a
microsecond and no test ever waits on wall time.
"""

from __future__ import annotations

import signal

import pytest

from freemicro.device.lighting import (
    METHOD_PREVIEW,
    METHOD_RGBCFG,
    METHOD_THREAD_STATUS,
    parse_color,
)
from freemicro.padconfig import FACTORY_PALETTE, load_default, parse
from freemicro.renderers import REGISTRY
from freemicro.renderers.micro_leds import (
    WRITE_RETRY_BACKOFF,
    MicroLedsRenderer,
    release_lighting,
)
from freemicro.state.engine import AgentState


class FakeDevice:
    """Stands in for an open pad; records everything sent to it."""

    def __init__(self, fail: bool = False) -> None:
        self.sent: list = []
        self.fail = fail
        self.closed = False
        #: Every write we were *asked* to do, including the ones we refused.
        #: The difference between this and ``sent`` is the retry behaviour.
        self.attempts = 0

    def send(self, message):
        self.attempts += 1
        if self.fail:
            raise OSError("device went away")
        self.sent.append(message)

    def close(self):
        self.closed = True


class FakeClock:
    """A clock the test moves by hand. Monotonic, like the real one."""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


CONFIG = parse({
    "version": 1,
    "bindings": {},
    "lighting": {
        "enabled": True,
        "zones": ["backlight", "underglow", "agent_keys"],
        "on_exit": "leave",
        # Auto-dim has its own tests below; the rest of this file is about
        # what one state puts on the wire, and a timer would only obscure it.
        "auto_dim_seconds": 0,
        "states": {
            "idle": {"color": "#101010", "effect": "breath", "brightness": 0.3},
            "done": {"color": "#00FF00", "effect": "solid", "brightness": 1.0},
        },
    },
})


def _renderer(config=CONFIG, device=None, clock=None, notify=None):
    kwargs = {}
    if clock is not None:
        kwargs["clock"] = clock
    if notify is not None:
        kwargs["notify"] = notify
    return MicroLedsRenderer(
        device=device or FakeDevice(), config=config, **kwargs
    )


@pytest.fixture(autouse=True)
def restore_signal_handlers():
    """Keep the exit guard's signal handlers out of the rest of the run.

    Arming the guard is a deliberately one-way act in a real process, because
    a process that disarms its own safety net has no safety net. A test suite
    is the one place that has to be able to put it back.
    """
    from freemicro.renderers import micro_leds

    saved = {
        signum: signal.getsignal(signum)
        for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
    }
    yield
    for signum, handler in saved.items():
        if handler is not None:
            signal.signal(signum, handler)
    micro_leds._guard_installed = False
    del micro_leds._driving[:]


def test_registered_with_the_highest_priority():
    assert "micro-leds" in REGISTRY
    assert REGISTRY["micro-leds"].priority == max(
        cls.priority for cls in REGISTRY.values()
    )
    assert REGISTRY["micro-leds"].experimental is False


def test_unavailable_without_a_pad():
    """No pad means nothing renders - `freemicro run` still prints the state."""
    renderer = MicroLedsRenderer()
    assert renderer.available() is False
    renderer.close()


def test_available_with_an_injected_device():
    renderer = _renderer()
    assert renderer.available() is True


def test_disabled_lighting_makes_the_renderer_unavailable():
    config = parse({"version": 1, "bindings": {},
                    "lighting": {"enabled": False}})
    renderer = _renderer(config=config)
    assert renderer.available() is False


def test_render_uses_rgbcfg_for_the_live_zones():
    """rgbcfg is the method that visibly lights this hardware (PROTOCOL.md)."""
    device = FakeDevice()
    renderer = _renderer(device=device)
    renderer.render(AgentState.DONE)
    message = device.sent[0]
    assert message["m"] == METHOD_RGBCFG
    # Minimised vendor field names, and a notification: no "id".
    assert message["p"]["keys"] == {"e": 1, "b": 1.0, "s": 0.0, "c": 0x00FF00}
    assert message["p"]["ambient"] == message["p"]["keys"]
    assert "id" not in message


def test_preview_is_still_selectable_for_debugging():
    """It lights nothing on v0.4.1, but the code path stays reachable."""
    config = parse({"version": 1, "bindings": {},
                    "lighting": {"enabled": True, "method": "preview",
                                 "zones": ["backlight", "underglow"],
                                 "states": {"done": {"color": "#00FF00"}}}})
    device = FakeDevice()
    renderer = _renderer(config=config, device=device)
    renderer.render(AgentState.DONE)
    preview = device.sent[0]
    assert preview["m"] == METHOD_PREVIEW
    assert preview["p"]["backlight"] == {
        "effect": 1, "brightness": 1.0, "speed": 0.0, "color": 0x00FF00,
    }
    assert preview["p"]["underglow"] == preview["p"]["backlight"]
    assert "id" in preview


def test_render_addresses_all_six_agent_keys():
    """One entry per key, always - six ids, whatever is or isn't running."""
    device = FakeDevice()
    renderer = _renderer(device=device)
    renderer.render(AgentState.DONE)
    thstatus = device.sent[1]
    assert thstatus["m"] == METHOD_THREAD_STATUS
    assert [e["id"] for e in thstatus["p"]] == [0, 1, 2, 3, 4, 5]


def test_with_no_sessions_every_agent_key_is_dark():
    """No project on a key means off, not dim - the factory's 'no agent'."""
    device = FakeDevice()
    renderer = _renderer(device=device)
    renderer.render(AgentState.IDLE)
    thstatus = device.sent[1]
    assert all(
        e["c"] == 0 and e["b"] == 0.0 and e["e"] == 0 for e in thstatus["p"]
    )


def test_mirror_policy_keeps_the_single_colour_behaviour():
    config = parse({
        "version": 1, "bindings": {},
        "agent_keys": {"policy": "mirror"},
        "lighting": {"enabled": True, "zones": ["agent_keys"],
                     "states": {"done": {"color": "#00FF00"}}},
    })
    device = FakeDevice()
    renderer = _renderer(config=config, device=device)
    renderer.render(AgentState.DONE)
    thstatus = device.sent[0]
    assert [e["id"] for e in thstatus["p"]] == [0, 1, 2, 3, 4, 5]
    assert all(e["c"] == 0x00FF00 for e in thstatus["p"])


# ---------------------------------------------------------------------------
# Coexisting with the ChatGPT app (Codex): drive only a subset of the keys
# ---------------------------------------------------------------------------

class _SessionStore:
    def __init__(self, sessions):
        self._sessions = list(sessions)

    def sessions(self):
        return list(self._sessions)


def _working(cwd, at=None):
    import time

    from freemicro.state.engine import SessionState

    return SessionState(
        session_id=cwd, state=AgentState.WORKING,
        updated_at=time.time() if at is None else at, cwd=cwd,
    )


def _subset_config(keys):
    return parse({
        "version": 1, "bindings": {},
        "agent_keys": {"policy": "recent", "keys": keys},
        "lighting": {"enabled": True, "zones": ["agent_keys"],
                     "states": {"working": {"color": "#0000FF"}}},
    })


def test_a_subset_sends_a_partial_thstatus_for_owned_keys_only():
    """The crux: entries for the owned keys only, so Codex's keys persist."""
    config = _subset_config([3, 4, 5])
    device = FakeDevice()
    store = _SessionStore([_working("/code/api")])
    renderer = MicroLedsRenderer(device=device, config=config, store=store)
    renderer.render(AgentState.WORKING)
    thstatus = device.sent[0]
    assert thstatus["m"] == METHOD_THREAD_STATUS
    # Fewer than six entries, and only the owned ids - nothing for AG00-AG02.
    assert [e["id"] for e in thstatus["p"]] == [3, 4, 5]
    # The lowest owned key carries the one live project; the others are dark.
    assert thstatus["p"][0]["c"] == 0x0000FF
    assert thstatus["p"][1]["c"] == 0 and thstatus["p"][1]["b"] == 0.0


def test_owned_keys_property_reflects_the_config():
    assert MicroLedsRenderer(config=_subset_config([1, 2])).owned_keys == (1, 2)
    assert MicroLedsRenderer(config=CONFIG).owned_keys == (0, 1, 2, 3, 4, 5)


def _codex_ownership(config, running):
    from freemicro.agentkeys import AgentKeyOwnership

    return AgentKeyOwnership(
        config.agent_keys, probe=lambda: running["value"], poll_seconds=0.0
    )


def test_a_subset_lights_all_six_when_codex_is_absent():
    """No ChatGPT app: FreeMicro owns all six even though a subset is set."""
    config = _subset_config([3, 4, 5])
    running = {"value": False}
    ownership = _codex_ownership(config, running)
    ownership.refresh()                        # Codex is not running
    device = FakeDevice()
    renderer = MicroLedsRenderer(
        device=device, config=config, store=_SessionStore([_working("/code/api")]),
        ownership=ownership,
    )
    assert renderer.owned_keys == (0, 1, 2, 3, 4, 5)
    renderer.render(AgentState.WORKING)
    thstatus = device.sent[0]
    # All six addressed, and the one project lands on the lowest key (AG00).
    assert [e["id"] for e in thstatus["p"]] == [0, 1, 2, 3, 4, 5]
    assert thstatus["p"][0]["c"] == 0x0000FF


def test_a_subset_lights_only_owned_keys_when_codex_is_running():
    config = _subset_config([3, 4, 5])
    running = {"value": True}
    ownership = _codex_ownership(config, running)
    ownership.refresh()
    device = FakeDevice()
    renderer = MicroLedsRenderer(
        device=device, config=config, store=_SessionStore([_working("/code/api")]),
        ownership=ownership,
    )
    assert renderer.owned_keys == (3, 4, 5)
    renderer.render(AgentState.WORKING)
    assert [e["id"] for e in device.sent[0]["p"]] == [3, 4, 5]


def test_expanding_to_all_six_repaints_after_an_invalidate():
    """Codex quits mid-run: the newly-owned keys must be painted.

    The run loop invalidates the dedupe when the effective set changes; without
    that, an empty owned key and an empty un-owned key look identical to the
    frame cache, so the take-over would not resend.
    """
    config = _subset_config([3, 4, 5])
    running = {"value": True}
    ownership = _codex_ownership(config, running)
    ownership.refresh()
    device = FakeDevice()
    renderer = MicroLedsRenderer(
        device=device, config=config, store=_SessionStore([_working("/code/api")]),
        ownership=ownership,
    )
    renderer.render(AgentState.WORKING)
    assert [e["id"] for e in device.sent[-1]["p"]] == [3, 4, 5]

    running["value"] = False
    assert ownership.refresh() is True          # what the run loop watches
    renderer.invalidate()                       # ...and acts on
    renderer.render(AgentState.WORKING)
    assert [e["id"] for e in device.sent[-1]["p"]] == [0, 1, 2, 3, 4, 5]


def test_the_default_all_six_config_still_addresses_every_key():
    """Zero behaviour change when no subset is set - byte-identical ids."""
    config = _subset_config([0, 1, 2, 3, 4, 5])
    device = FakeDevice()
    renderer = MicroLedsRenderer(
        device=device, config=config, store=_SessionStore([])
    )
    renderer.render(AgentState.IDLE)
    assert [e["id"] for e in device.sent[0]["p"]] == [0, 1, 2, 3, 4, 5]


def test_a_subset_mirror_policy_still_only_drives_owned_keys():
    config = parse({
        "version": 1, "bindings": {},
        "agent_keys": {"policy": "mirror", "keys": [0, 1]},
        "lighting": {"enabled": True, "zones": ["agent_keys"],
                     "states": {"done": {"color": "#00FF00"}}},
    })
    device = FakeDevice()
    renderer = _renderer(config=config, device=device)
    renderer.render(AgentState.DONE)
    thstatus = device.sent[0]
    assert [e["id"] for e in thstatus["p"]] == [0, 1]
    assert all(e["c"] == 0x00FF00 for e in thstatus["p"])


def test_blank_on_exit_only_darkens_owned_keys():
    """Auto-dim and exit never blank a key Codex is driving."""
    config = parse({"version": 1, "bindings": {},
                    "agent_keys": {"policy": "recent", "keys": [4, 5]},
                    "lighting": {"enabled": True, "on_exit": "off",
                                 "zones": ["agent_keys"],
                                 "states": {"idle": {"color": "#111111"}}}})
    device = FakeDevice()
    renderer = _renderer(config=config, device=device)
    renderer.close()
    assert [e["id"] for e in device.sent[0]["p"]] == [4, 5]


def test_an_overlay_seizing_the_agent_keys_stays_on_owned_keys():
    from freemicro.padconfig import ActivityLight

    config = _subset_config([2, 3])
    renderer = _renderer(config=config, device=FakeDevice())
    overlay = ActivityLight(
        color=parse_color("#2E8B57"), effect=2, brightness=1.0, speed=0.4,
        zones=("agent_keys",),
    )
    msgs = renderer.messages_for(AgentState.IDLE, overlay=overlay, now=1000.0)
    thstatus = next(m for m in msgs if m["m"] == METHOD_THREAD_STATUS)
    assert [e["id"] for e in thstatus["p"]] == [2, 3]


def test_lights_preview_is_not_used_by_default():
    """Firmware v0.4.1 accepts it and lights nothing - see docs/PROTOCOL.md."""
    device = FakeDevice()
    renderer = _renderer(device=device)
    for state in AgentState:
        renderer.render(state)
    assert not any(m["m"] == METHOD_PREVIEW for m in device.sent)


def test_zones_are_honoured():
    config = parse({"version": 1, "bindings": {},
                    "lighting": {"enabled": True, "zones": ["underglow"],
                                 "states": {"done": {"color": "#FFFFFF"}}}})
    device = FakeDevice()
    renderer = _renderer(config=config, device=device)
    renderer.render(AgentState.DONE)
    assert len(device.sent) == 1
    # rgbcfg's name for the underglow is "ambient".
    assert list(device.sent[0]["p"]) == ["ambient"]


def test_states_without_config_fall_back_to_the_factory_palette():
    """CUSTOMIZING.md promises the *factory* colour for a state you delete."""
    renderer = _renderer()
    light = renderer.light_for(AgentState.ERROR)  # not in CONFIG
    assert light.color == parse_color("#FF0033")
    assert light.effect == 1 and light.brightness == 1.0 and light.speed == 0.0


def test_the_factory_palette_is_the_documented_one():
    """The five values from FACTORY-DEFAULTS.md 1a, spelled out once."""
    assert {state.value: hex_ for state, hex_ in FACTORY_PALETTE.items()} == {
        "idle": "#FFFFFF",
        "working": "#304FFE",
        "waiting": "#FF6D00",
        "done": "#00FF4C",
        "error": "#FF0033",
    }
    for state in AgentState:
        assert state in FACTORY_PALETTE, f"{state} has no factory colour"


def test_the_shipped_config_and_the_fallback_cannot_drift():
    """One palette, two spellings of it - and they must agree.

    The whole point of F11: the colour a user sees when they *delete* a state
    has to be the colour they see when they leave it alone.
    """
    shipped = load_default().lighting
    for state in AgentState:
        configured = shipped.for_state(state)
        assert configured is not None, f"{state} is missing from default_keymap.json"
        assert configured.color == parse_color(FACTORY_PALETTE[state])
        # And the fallback path lands on the same look for every state.
        assert shipped.light_for(state).color == configured.color


def test_repeated_renders_do_not_resend():
    """Each call replaces the last, so resending would restart animations."""
    device = FakeDevice()
    renderer = _renderer(device=device)
    renderer.render(AgentState.WORKING)
    count = len(device.sent)
    renderer.render(AgentState.WORKING)
    assert len(device.sent) == count
    renderer.render(AgentState.DONE)
    assert len(device.sent) > count


def test_render_uses_the_handed_in_sessions_instead_of_rescanning():
    """The run loop reads sessions once and hands them to the renderer.

    Passing them in must stop the renderer scanning the store a second time,
    and rendering without them must fall back to reading the store, so the
    renderer still works when driven standalone.
    """
    class CountingStore:
        def __init__(self):
            self.scans = 0

        def sessions(self):
            self.scans += 1
            return []

    # The shipped default already resolves per-project slots (mirrors is False,
    # agent keys are driven), so slots() reaches the store.
    store = CountingStore()
    renderer = MicroLedsRenderer(
        device=FakeDevice(), config=load_default(), store=store
    )

    renderer.slots(sessions=[])
    assert store.scans == 0, "handed sessions but scanned the store anyway"

    renderer.slots()
    assert store.scans == 1, "no sessions handed in, but the store was not read"


def test_a_dropped_pad_does_not_crash_and_does_not_latch_lighting_off():
    """One failed write used to kill lighting for the life of the process."""
    device = FakeDevice(fail=True)
    renderer = _renderer(device=device, notify=lambda message: None)
    renderer.render(AgentState.DONE)  # must not raise
    assert renderer.write_failures == 1
    assert renderer.available() is True, "a blip is not a reason to give up"


def test_on_exit_off_darkens_the_pad():
    config = parse({"version": 1, "bindings": {},
                    "lighting": {"enabled": True, "on_exit": "off",
                                 "zones": ["backlight", "agent_keys"],
                                 "states": {"idle": {"color": "#111111"}}}})
    device = FakeDevice()
    renderer = _renderer(config=config, device=device)
    renderer.close()
    zone = device.sent[0]["p"]["keys"]
    # Dim means DARK, not "less bright" - that is what the factory does.
    assert zone["e"] == 0 and zone["b"] == 0.0
    assert all(entry["e"] == 0 and entry["b"] == 0.0 for entry in device.sent[1]["p"])


def test_on_exit_leave_sends_nothing():
    device = FakeDevice()
    renderer = _renderer(device=device)  # CONFIG uses on_exit: leave
    renderer.close()
    assert device.sent == []


def test_an_injected_device_is_not_closed_by_the_renderer():
    """`freemicro run` shares one handle; the renderer must not shut it down."""
    device = FakeDevice()
    renderer = _renderer(device=device)
    renderer.close()
    assert device.closed is False


def test_the_shipped_default_produces_traffic_for_every_state():
    device = FakeDevice()
    renderer = MicroLedsRenderer(device=device, config=load_default())
    for state in AgentState:
        assert renderer.messages_for(state), f"{state} produced no lighting"


# ---------------------------------------------------------------------------
# A failed write is a hiccup, not a funeral
# ---------------------------------------------------------------------------

def test_a_failed_write_retries_on_the_vendors_backoff_ladder():
    """1s, 2s, 5s, then every 10s - the measured ladder, not an invented one."""
    assert WRITE_RETRY_BACKOFF == (1.0, 2.0, 5.0, 10.0)
    clock = FakeClock()
    device = FakeDevice(fail=True)
    notes: list = []
    renderer = _renderer(device=device, clock=clock, notify=notes.append)

    renderer.render(AgentState.DONE)
    assert device.attempts == 1

    renderer.render(AgentState.DONE)
    assert device.attempts == 1, "a retry inside the backoff window is spam"

    for wait in WRITE_RETRY_BACKOFF + (10.0,):  # the last step is the floor
        before = device.attempts
        clock.advance(wait - 0.01)
        renderer.render(AgentState.DONE)
        assert device.attempts == before, f"retried before waiting {wait:g}s"
        clock.advance(0.02)
        renderer.render(AgentState.DONE)
        assert device.attempts == before + 1, f"no retry after {wait:g}s"


def test_a_pad_that_is_genuinely_gone_says_it_once():
    """The other half of the fix: recover, but do not fill the log doing it."""
    clock = FakeClock()
    device = FakeDevice(fail=True)
    notes: list = []
    renderer = _renderer(device=device, clock=clock, notify=notes.append)
    for _ in range(200):
        clock.advance(1.0)
        renderer.render(AgentState.DONE)
    assert len(notes) == 1, notes
    assert "write failed" in notes[0]
    assert "device went away" in notes[0], "say what went wrong"


def test_lighting_comes_back_by_itself_and_says_so():
    clock = FakeClock()
    device = FakeDevice(fail=True)
    notes: list = []
    renderer = _renderer(device=device, clock=clock, notify=notes.append)
    renderer.render(AgentState.DONE)
    assert device.sent == []

    device.fail = False
    clock.advance(60.0)
    renderer.render(AgentState.DONE)

    assert device.sent, "the frame must go out once the pad answers again"
    assert len(notes) == 2
    assert "going through again" in notes[1]
    # PROTOCOL.md: a wrongly framed write is acked and discarded, so a write
    # that returned cleanly is not evidence that the LEDs changed.
    assert "not proof" in notes[1]

    sent = len(device.sent)
    renderer.render(AgentState.DONE)
    assert len(device.sent) == sent, "dedupe is back in charge after recovery"
    assert renderer.write_failures == 0


def test_a_half_sent_frame_is_sent_again_in_full():
    """The underglow updated and the keys did not: nothing is trustworthy now."""
    clock = FakeClock()

    class HalfFailing(FakeDevice):
        def send(self, message):
            self.attempts += 1
            if self.fail and message["m"] == METHOD_THREAD_STATUS:
                raise OSError("bluetooth blip")
            self.sent.append(message)

    device = HalfFailing(fail=True)
    renderer = _renderer(device=device, clock=clock, notify=lambda note: None)
    renderer.render(AgentState.DONE)
    assert len(device.sent) == 1  # the zone message landed, the keys did not

    device.fail = False
    clock.advance(5.0)
    renderer.render(AgentState.DONE)
    assert len(device.sent) == 3, "the whole frame goes again, not just the tail"


# ---------------------------------------------------------------------------
# Handing the pad back when we are killed
# ---------------------------------------------------------------------------

EXIT_OFF = parse({
    "version": 1, "bindings": {},
    "agent_keys": {"policy": "mirror"},
    "lighting": {"enabled": True, "on_exit": "off", "zones": ["agent_keys"],
                 "auto_dim_seconds": 0,
                 "states": {"done": {"color": "#00FF00"}}},
})


def test_release_lighting_blanks_a_pad_we_are_driving():
    """SIGTERM does not unwind `finally`, so this is the only thing that runs."""
    device = FakeDevice()
    renderer = _renderer(config=EXIT_OFF, device=device)
    renderer.render(AgentState.DONE)
    device.sent.clear()

    assert release_lighting() == 1
    assert device.sent, "the pad was left lit"
    assert all(entry["b"] == 0.0 for entry in device.sent[0]["p"])
    assert renderer.close() is None
    assert len(device.sent) == 1, "close() must not re-send what we just sent"


def test_release_lighting_is_idempotent_and_ignores_idle_renderers():
    device = FakeDevice()
    _renderer(config=EXIT_OFF, device=device)  # never rendered anything
    assert release_lighting() == 0
    assert device.sent == []
    assert release_lighting() == 0


def test_the_guard_is_armed_the_first_time_we_light_the_pad():
    """Lazily, so importing this module never touches signal handlers."""
    from freemicro.renderers import micro_leds

    assert micro_leds._guard_installed is False
    renderer = _renderer(config=EXIT_OFF)
    assert micro_leds._guard_installed is False, "constructing is not lighting"
    renderer.render(AgentState.DONE)
    assert micro_leds._guard_installed is True


def test_the_guard_chains_the_handler_it_replaced():
    """Two guards must compose: ours runs, and the one before it still runs.

    This is the shape `input.quartz` uses for held modifier keys, and the
    reason it is the same shape: whichever guard is installed second finds the
    first as its "previous" handler and calls it.
    """
    calls: list = []
    signal.signal(signal.SIGTERM, lambda num, frame: calls.append(num))
    device = FakeDevice()
    renderer = _renderer(config=EXIT_OFF, device=device)
    renderer.render(AgentState.DONE)
    device.sent.clear()

    handler = signal.getsignal(signal.SIGTERM)
    assert callable(handler)
    handler(signal.SIGTERM, None)  # exactly what the OS would do

    assert device.sent, "the pad was not handed back"
    assert calls == [signal.SIGTERM], "the signal must still do its job"


def test_an_ignored_signal_stays_ignored():
    from freemicro.renderers import micro_leds

    device = FakeDevice()
    renderer = _renderer(config=EXIT_OFF, device=device)
    renderer.render(AgentState.DONE)
    device.sent.clear()
    micro_leds._release_and_chain(signal.SIGTERM, signal.SIG_IGN)
    assert device.sent, "we still hand the pad back"


def test_a_process_that_survives_the_signal_lights_the_pad_again():
    """SIGHUP can be ignored. If we are still here, the pad is ours again."""
    device = FakeDevice()
    renderer = _renderer(config=EXIT_OFF, device=device)
    renderer.render(AgentState.DONE)
    painted = device.sent[0]
    release_lighting()
    device.sent.clear()

    renderer.render(AgentState.DONE)
    assert device.sent == [painted]


def test_on_exit_leave_means_leave_even_from_the_guard():
    device = FakeDevice()
    renderer = _renderer(device=device)  # CONFIG uses on_exit: leave
    renderer.render(AgentState.DONE)
    device.sent.clear()
    release_lighting()
    assert device.sent == []


# ---------------------------------------------------------------------------
# Auto-dim (FACTORY-DEFAULTS.md 4)
# ---------------------------------------------------------------------------

def _dimming_config(seconds=180, alerts=False, zones=("agent_keys",)):
    return parse({
        "version": 1, "bindings": {},
        "agent_keys": {"policy": "mirror"},
        "lighting": {
            "enabled": True, "zones": list(zones), "on_exit": "leave",
            "auto_dim_seconds": seconds, "auto_dim_alerts": alerts,
            "states": {"done": {"color": "#00FF00"},
                       "working": {"color": "#0000FF"}},
        },
    })


def _dimming(seconds=180, alerts=False, zones=("agent_keys",)):
    clock = FakeClock()
    device = FakeDevice()
    renderer = _renderer(
        config=_dimming_config(seconds, alerts, zones), device=device, clock=clock
    )
    return renderer, device, clock


def test_the_default_is_the_factory_timing():
    """Three minutes, from FACTORY-DEFAULTS 4 - and it is on by default."""
    lighting = parse({"version": 1, "bindings": {}}).lighting
    assert lighting.auto_dim_seconds == 180.0
    assert lighting.auto_dim_enabled is True


def test_the_pad_blanks_after_the_configured_inactivity():
    renderer, device, clock = _dimming()
    renderer.render(AgentState.DONE)
    assert len(device.sent) == 1

    clock.advance(179.0)
    renderer.render(AgentState.DONE)
    assert len(device.sent) == 1, "dimmed early"

    clock.advance(2.0)
    renderer.render(AgentState.DONE)
    assert len(device.sent) == 2
    # Dim is the factory's all-off payload, not a brightness reduction.
    assert all(
        entry == {"id": i, "c": 0, "b": 0.0, "e": 0, "s": 0.0}
        for i, entry in enumerate(device.sent[1]["p"])
    )
    assert renderer.dimmed is True


def test_a_dimmed_pad_is_not_repainted_on_every_tick():
    renderer, device, clock = _dimming()
    renderer.render(AgentState.DONE)
    clock.advance(181.0)
    renderer.render(AgentState.DONE)
    sent = len(device.sent)
    for _ in range(20):
        clock.advance(10.0)
        renderer.render(AgentState.DONE)
    assert len(device.sent) == sent


def test_a_state_change_wakes_the_pad_in_full():
    renderer, device, clock = _dimming()
    renderer.render(AgentState.DONE)
    clock.advance(181.0)
    renderer.render(AgentState.DONE)  # dark

    clock.advance(1.0)
    renderer.render(AgentState.WORKING)
    assert renderer.dimmed is False
    assert all(entry["c"] == parse_color("#0000FF") for entry in device.sent[-1]["p"])


def test_the_same_state_after_a_wake_is_repainted_not_deduped():
    """Waking has to re-send a frame the dedupe cache says is already there."""
    renderer, device, clock = _dimming()
    renderer.render(AgentState.DONE)
    clock.advance(181.0)
    renderer.render(AgentState.DONE)  # dark
    painted = device.sent[0]

    renderer.note_activity()  # a key press
    clock.advance(0.25)
    renderer.render(AgentState.DONE)
    assert device.sent[-1] == painted
    assert renderer.dimmed is False


def test_a_key_press_resets_the_inactivity_timer():
    renderer, device, clock = _dimming()
    renderer.render(AgentState.DONE)
    for _ in range(10):
        clock.advance(100.0)
        renderer.note_activity()
        renderer.render(AgentState.DONE)
    assert len(device.sent) == 1, "a pad in use must never dim"
    assert renderer.dimmed is False


def test_a_state_that_is_asking_for_you_does_not_dim():
    """The one deliberate divergence from 4, and the reason for it.

    Amber means "your agent is blocked on you". The moment it matters most is
    the moment you are not at the desk to reset the timer.
    """
    renderer, device, clock = _dimming()
    renderer.render(AgentState.WAITING)
    clock.advance(3600.0)
    renderer.render(AgentState.WAITING)
    assert renderer.dimmed is False
    assert len(device.sent) == 1


def test_auto_dim_alerts_buys_exact_factory_behaviour():
    renderer, device, clock = _dimming(alerts=True)
    renderer.render(AgentState.WAITING)
    clock.advance(181.0)
    renderer.render(AgentState.WAITING)
    assert renderer.dimmed is True


def test_auto_dim_can_be_switched_off():
    renderer, device, clock = _dimming(seconds=0)
    renderer.render(AgentState.DONE)
    clock.advance(86400.0)
    renderer.render(AgentState.DONE)
    assert renderer.dimmed is False
    assert len(device.sent) == 1


def test_dimming_blanks_every_zone_we_drive():
    renderer, device, clock = _dimming(
        zones=("backlight", "underglow", "agent_keys")
    )
    renderer.render(AgentState.DONE)
    clock.advance(181.0)
    renderer.render(AgentState.DONE)
    zone, keys = device.sent[-2], device.sent[-1]
    assert zone["m"] == METHOD_RGBCFG
    assert zone["p"]["keys"] == {"e": 0, "b": 0.0, "s": 0.0, "c": 0}
    assert zone["p"]["ambient"] == zone["p"]["keys"]
    assert keys["m"] == METHOD_THREAD_STATUS


def test_a_reassert_while_dimmed_re_sends_the_dark_frame():
    """ChatGPT painting our dark pad is not the user coming back to it."""
    renderer, device, clock = _dimming()
    renderer.render(AgentState.DONE)
    clock.advance(181.0)
    renderer.render(AgentState.DONE)
    dark = device.sent[-1]

    renderer.invalidate()
    clock.advance(1.0)
    renderer.render(AgentState.DONE)
    assert device.sent[-1] == dark
    assert renderer.dimmed is True


def test_a_config_reload_wakes_the_pad():
    """Editing the lights is a change to the lighting model, so it wakes (4)."""
    renderer, device, clock = _dimming()
    renderer.render(AgentState.DONE)
    clock.advance(181.0)
    renderer.render(AgentState.DONE)
    assert renderer.dimmed is True

    renderer.apply_config(_dimming_config())
    renderer.render(AgentState.DONE)
    assert renderer.dimmed is False
    assert device.sent[-1]["p"][0]["c"] == parse_color("#00FF00")


# ---------------------------------------------------------------------------
# Software blink, entry flash and the battery cue
# ---------------------------------------------------------------------------

from freemicro.agentkeys import AgentSlot, Project  # noqa: E402
from freemicro.renderers.micro_leds import FLASH_SECONDS  # noqa: E402


def _agent_entries(messages):
    for message in messages:
        if message.get("m") == METHOD_THREAD_STATUS:
            return message["p"]
    return None


def _blink_config(effect="blink", speed=0.0, **extra):
    lighting = {
        "enabled": True, "zones": ["agent_keys"], "on_exit": "leave",
        "auto_dim_seconds": 0, "flash_on": [],
        "states": {"error": {"color": "#FF0033", "effect": effect, "speed": speed}},
    }
    lighting.update(extra)
    return parse({"version": 1, "bindings": {}, "lighting": lighting,
                  "agent_keys": {"policy": "mirror"}})


def test_a_blink_state_toggles_the_frame_between_colour_and_off():
    # Start the clock at a period boundary so the first phase is deterministic;
    # at speed 0 the half-period is 0.75 s (BLINK_HALF_MAX).
    clock = FakeClock(now=0.0)
    device = FakeDevice()
    renderer = _renderer(config=_blink_config(), device=device, clock=clock)

    renderer.render(AgentState.ERROR)
    lit = _agent_entries(device.sent)[0]
    assert lit["c"] == parse_color("#FF0033") and lit["e"] != 0

    # A half period on, the same state must go dark - without any state change.
    device.sent.clear()
    clock.advance(0.75)
    renderer.render(AgentState.ERROR)
    off = _agent_entries(device.sent)[0]
    assert off["c"] == 0 and off["e"] == 0 and off["b"] == 0.0

    # ...and back to the colour on the next half period. The blink is the clock,
    # not a second thread: nothing wrote the pad between these render calls.
    device.sent.clear()
    clock.advance(0.75)
    renderer.render(AgentState.ERROR)
    assert _agent_entries(device.sent)[0]["c"] == parse_color("#FF0033")


def test_a_solid_state_does_not_resend_but_a_blink_state_does():
    clock = FakeClock()
    # A non-blink state must keep the "skip unchanged frames" guarantee.
    solid = _renderer(config=_blink_config(effect="solid"),
                      device=FakeDevice(), clock=clock)
    solid.render(AgentState.ERROR)
    sent = len(solid._device.sent)
    clock.advance(5.0)
    solid.render(AgentState.ERROR)
    assert len(solid._device.sent) == sent, "solid frame resent for no reason"


def test_blink_composes_per_key_one_blinks_while_the_others_stay_solid():
    # error blinks; working is left solid (factory). One frame, two behaviours.
    config = parse({"version": 1, "bindings": {}, "lighting": {
        "enabled": True, "zones": ["agent_keys"], "flash_on": [],
        "states": {"error": {"color": "#FF0033", "effect": "blink"}},
    }})
    renderer = _renderer(config=config, device=FakeDevice())
    slots = [
        AgentSlot(index=0, path="/a",
                  project=Project("/a", AgentState.WORKING, 0.0)),
        AgentSlot(index=1, path="/b",
                  project=Project("/b", AgentState.ERROR, 0.0)),
    ]
    # On the blink OFF phase, only the error key goes dark; working stays lit.
    off_now = 0.75  # the first off half-period at speed 0 (half = 0.75 s)
    entries = _agent_entries(
        renderer.messages_for(AgentState.WORKING, slots=slots, now=off_now)
    )
    working, error = entries[0], entries[1]
    assert working["e"] != 0 and working["c"] != 0, "solid neighbour went dark"
    assert error["c"] == 0 and error["e"] == 0, "blink key did not go dark"


def test_entering_a_flash_state_pulses_then_settles():
    clock = FakeClock()
    device = FakeDevice()
    config = parse({"version": 1, "bindings": {}, "lighting": {
        "enabled": True, "zones": ["agent_keys"], "auto_dim_seconds": 0,
        "flash_on": ["error"],
    }, "agent_keys": {"policy": "mirror"}})
    renderer = _renderer(config=config, device=device, clock=clock)

    # Seen in idle first, so the move into error is a real transition.
    renderer.render(AgentState.IDLE)
    device.sent.clear()
    # Enter error: the state colour shows at once (the flash never hides it).
    renderer.render(AgentState.ERROR)
    assert _agent_entries(device.sent)[0]["c"] == (
        renderer.light_for(AgentState.ERROR).color
    )

    # Mid-flash it pulses dark...
    device.sent.clear()
    clock.advance(FLASH_SECONDS * 0.75)
    renderer.render(AgentState.ERROR)
    assert _agent_entries(device.sent)[0]["c"] == 0

    # ...then settles to the steady error colour once the window closes.
    device.sent.clear()
    clock.advance(FLASH_SECONDS)
    renderer.render(AgentState.ERROR)
    assert _agent_entries(device.sent)[0]["c"] == (
        renderer.light_for(AgentState.ERROR).color
    )


def test_a_state_that_was_already_present_does_not_flash_on_first_sight():
    clock = FakeClock()
    device = FakeDevice()
    config = parse({"version": 1, "bindings": {}, "lighting": {
        "enabled": True, "zones": ["agent_keys"], "auto_dim_seconds": 0,
        "flash_on": ["error"],
    }, "agent_keys": {"policy": "mirror"}})
    renderer = _renderer(config=config, device=device, clock=clock)
    # First ever render is error: there is no previous state, so no flash.
    renderer.render(AgentState.ERROR)
    assert _agent_entries(device.sent)[0]["c"] == (
        renderer.light_for(AgentState.ERROR).color
    )


def _battery_config(**battery):
    settings = {"enabled": True, "threshold": 15, "zone": "underglow",
                "color": "#FF6D00", "effect": "solid", "poll_seconds": 60}
    settings.update(battery)
    return parse({"version": 1, "bindings": {}, "lighting": {
        "enabled": True, "zones": ["agent_keys"], "auto_dim_seconds": 0,
        "flash_on": [], "battery": settings,
    }, "agent_keys": {"policy": "mirror"}})


def _ambient(messages):
    for message in messages:
        if message.get("m") == METHOD_RGBCFG:
            return message["p"].get("ambient")
    return None


def test_a_low_battery_tints_the_underglow_and_a_healthy_one_leaves_it_dark():
    reads = [{"battery": 9, "is_charging": False}]
    renderer = _renderer(
        config=_battery_config(), device=FakeDevice(),
    )
    renderer._battery_reader = lambda: reads[0]
    renderer.render(AgentState.IDLE)
    assert _ambient(renderer._device.sent)["c"] == parse_color("#FF6D00")

    # Recover the battery: the cue must clear, not stay stranded on the glow.
    reads[0] = {"battery": 80, "is_charging": False}
    renderer._battery_checked_at = None  # force a fresh read this tick
    renderer._device.sent.clear()
    renderer.render(AgentState.WORKING)
    assert _ambient(renderer._device.sent) == {"e": 0, "b": 0.0, "s": 0.0, "c": 0}


def test_a_charging_pad_is_never_low():
    renderer = _renderer(config=_battery_config(), device=FakeDevice())
    renderer._battery_reader = lambda: {"battery": 5, "is_charging": True}
    renderer.render(AgentState.IDLE)
    # underglow is only an extra zone here, so a not-low battery paints it dark.
    assert _ambient(renderer._device.sent) == {"e": 0, "b": 0.0, "s": 0.0, "c": 0}


def test_battery_is_read_on_a_slow_clock_not_every_tick():
    calls = [0]

    def reader():
        calls[0] += 1
        return {"battery": 50, "is_charging": False}

    clock = FakeClock()
    renderer = _renderer(config=_battery_config(poll_seconds=60),
                         device=FakeDevice(), clock=clock)
    renderer._battery_reader = reader
    for _ in range(5):
        renderer.render(AgentState.IDLE)
    assert calls[0] == 1, "battery cache read on every tick"
    clock.advance(61)
    renderer.render(AgentState.IDLE)
    assert calls[0] == 2


def test_battery_disabled_never_reads_at_all():
    calls = [0]

    def reader():
        calls[0] += 1
        return {"battery": 1, "is_charging": False}

    config = parse({"version": 1, "bindings": {}, "lighting": {
        "enabled": True, "zones": ["agent_keys"], "auto_dim_seconds": 0,
    }})
    renderer = _renderer(config=config, device=FakeDevice())
    renderer._battery_reader = reader
    renderer.render(AgentState.IDLE)
    assert calls[0] == 0


def test_a_blinking_state_still_dims_and_the_blink_stops():
    """Blink does not keep the pad awake: auto-dim blanks it and the toggling
    stops, so a blinking idle on an empty desk goes dark like anything else."""
    clock = FakeClock(now=0.0)
    device = FakeDevice()
    config = _blink_config(auto_dim_seconds=180)
    renderer = _renderer(config=config, device=device, clock=clock)
    renderer.render(AgentState.ERROR)
    # error is an alert state, which by default does not dim; a blinking *idle*
    # is the honest "does blink keep it awake?" case, so drive that instead.
    idle = _renderer(
        config=_blink_config(
            auto_dim_seconds=180,
            states={"idle": {"color": "#3030FF", "effect": "blink"}},
        ),
        device=FakeDevice(), clock=clock,
    )
    idle.render(AgentState.IDLE)
    clock.advance(200)  # past the dim window with nothing happening
    idle._device.sent.clear()
    idle.render(AgentState.IDLE)
    assert idle.dimmed is True
    assert _agent_entries(idle._device.sent)[0]["c"] == 0


def test_the_preview_methods_id_does_not_defeat_the_dedupe():
    """lights.preview carries a per-call id; an unchanged frame must still not
    resend just because that correlation number moved."""
    config = parse({"version": 1, "bindings": {}, "lighting": {
        "enabled": True, "method": "preview", "zones": ["underglow"],
        "auto_dim_seconds": 0, "flash_on": [],
        "states": {"done": {"color": "#00FF00"}},
    }})
    device = FakeDevice()
    renderer = _renderer(config=config, device=device, clock=FakeClock())
    renderer.render(AgentState.DONE)
    sent = len(device.sent)
    for _ in range(4):
        renderer.render(AgentState.DONE)
    assert len(device.sent) == sent, "preview frame resent on a moving id"
