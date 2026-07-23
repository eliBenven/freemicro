"""Renderer layer: turn a resolved :class:`AgentState` into something visible.

A *renderer* is anything that can show state to a human — a macro pad's LEDs,
a USB busylight, or a chip on your screen. Renderers implement a tiny
interface (:class:`~freemicro.renderers.base.Renderer`) and register
themselves in the :data:`~freemicro.renderers.base.REGISTRY`.

The golden rule of FreeMicro: **the alert never depends on the pad.** The
screen renderer is always available, so no matter what the hardware turns out
to be, a "done / needs-you" signal always lands.
"""

from freemicro.renderers.base import REGISTRY, Renderer, available_renderers, select

# Importing the modules registers their renderers as a side effect.
from freemicro.renderers import screen as screen  # noqa: E402,F401
from freemicro.renderers import busylight as busylight  # noqa: E402,F401
from freemicro.renderers import micro_via as micro_via  # noqa: E402,F401
from freemicro.renderers import micro_qmk as micro_qmk  # noqa: E402,F401

__all__ = ["REGISTRY", "Renderer", "available_renderers", "select"]
