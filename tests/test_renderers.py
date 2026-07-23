"""Tests for the renderer registry and the always-on screen fallback."""

from __future__ import annotations

from freemicro.renderers import REGISTRY, select
from freemicro.renderers.base import PALETTE, available_renderers
from freemicro.state.engine import AgentState


def test_all_states_have_palette_entries():
    for state in AgentState:
        assert state in PALETTE
        rgb = PALETTE[state]
        assert len(rgb) == 3
        assert all(0 <= c <= 255 for c in rgb)


def test_registry_has_expected_renderers():
    assert set(REGISTRY) >= {"screen", "busylight", "micro-via", "micro-qmk"}


def test_screen_is_always_available():
    screen = REGISTRY["screen"]()
    assert screen.available() is True
    screen.close()


def test_select_always_includes_screen_fallback():
    # Even if we prefer a hardware target that isn't present, screen must be
    # in the chosen set — the alert can never depend on the pad.
    chosen = select(prefer=["micro-via"])
    assert any(r.name == "screen" for r in chosen)
    for r in chosen:
        r.close()


def test_available_renderers_sorted_by_priority():
    live = available_renderers()
    priorities = [r.priority for r in live]
    assert priorities == sorted(priorities, reverse=True)
    for r in live:
        r.close()


def test_screen_render_console_does_not_raise(monkeypatch, capsys):
    # Force console mode (no GUI) and make sure render is safe.
    screen = REGISTRY["screen"]()
    screen._use_gui = False
    for state in AgentState:
        screen.render(state)
    screen.close()
    # Something was written to stdout for the state line.
    out = capsys.readouterr().out
    assert "freemicro" in out
