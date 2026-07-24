"""Tests for the renderer registry.

FreeMicro used to have five renderers and a guarantee that the alert never
depended on the pad. Four of them are gone (see docs/PRODUCT-REVIEW.md §7): the
pad's LEDs are verified over USB and Bluetooth, and the fallbacks were either
unreachable, never run, or aimed at hardware this project does not support. The
registry stayed, because a second surface is still one module - what changed is
that nothing claims to be a fallback any more.
"""

from __future__ import annotations

from freemicro.renderers import REGISTRY, REMOVED, removed_names, select
from freemicro.renderers.base import PALETTE, available_renderers
from freemicro.state.engine import AgentState


def test_all_states_have_palette_entries():
    for state in AgentState:
        assert state in PALETTE
        rgb = PALETTE[state]
        assert len(rgb) == 3
        assert all(0 <= c <= 255 for c in rgb)


def test_the_pad_is_the_only_renderer():
    assert set(REGISTRY) == {"micro-leds"}


def test_deleted_renderers_are_not_registered():
    # A name that still resolves is a name that still gets selected, which is
    # how a deleted renderer comes back to life through someone's old config.
    assert set(REGISTRY).isdisjoint(REMOVED)


def test_every_removed_renderer_says_what_to_use_instead():
    assert set(REMOVED) == {"screen", "busylight", "micro-via", "micro-qmk"}
    for name, advice in REMOVED.items():
        assert advice and "removed" in advice, name


def test_a_preference_for_a_removed_renderer_is_reported_not_obeyed():
    assert removed_names(["screen", "micro-leds"]) == ["screen"]
    assert removed_names(None) == []
    assert removed_names(["micro-leds"]) == []


def test_preferring_only_removed_renderers_still_drives_the_pad():
    # The old config said ["screen"]. That must not mean "render nothing" -
    # it means "we ignored a name that no longer exists" and the pad still
    # gets whatever is available.
    chosen = select(prefer=["screen"])
    assert [r.name for r in chosen] == [r.name for r in available_renderers()]
    for r in chosen:
        r.close()


def test_no_renderer_is_appended_as_a_guaranteed_fallback():
    # The old select() always tacked the screen renderer on the end. Nothing
    # may appear in the chosen set that was not actually available.
    live = {r.name for r in available_renderers()}
    chosen = select()
    assert {r.name for r in chosen} <= live
    for r in chosen:
        r.close()


def test_available_renderers_sorted_by_priority():
    live = available_renderers()
    priorities = [r.priority for r in live]
    assert priorities == sorted(priorities, reverse=True)
    for r in live:
        r.close()
