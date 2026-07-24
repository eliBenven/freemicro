"""Renderer layer: turn a resolved :class:`AgentState` into something visible.

A *renderer* is anything that can show state to a human. Today there is exactly
one: the Codex Micro's own LEDs. Renderers implement a tiny interface
(:class:`~freemicro.renderers.base.Renderer`) and register themselves in the
:data:`~freemicro.renderers.base.REGISTRY`, so a second surface is a new module
and nothing else.

FreeMicro used to ship four more: a Tk chip, a USB busylight, and two renderers
for VIA/QMK pads. They existed to guarantee that "the alert never depends on the
pad", written while the LED path was unproven. The pad's LEDs are now verified
over USB *and* Bluetooth, and the fallbacks were costing more than they carried
(the Tk window could not open on any machine, and could abort the process
trying). They are gone. The pad is the display; ``freemicro run`` prints each
state change to your terminal as well.
"""

from freemicro.renderers.base import (
    REGISTRY,
    REMOVED,
    Renderer,
    available_renderers,
    removed_names,
    select,
)

# Importing the modules registers their renderers as a side effect.
from freemicro.renderers import micro_leds as micro_leds  # noqa: E402,F401
from freemicro.renderers import micro_sniffed as micro_sniffed  # noqa: E402,F401

__all__ = [
    "REGISTRY",
    "REMOVED",
    "Renderer",
    "available_renderers",
    "removed_names",
    "select",
]
