"""Reliable renderer: commercial USB busylights via ``busylight-core``.

blink(1), Luxafor, BlinkStick, Kuando, MuteMe and friends. This is the
"known good" hardware path — if you already own one of these, FreeMicro drives
it today with zero firmware questions. It's the reference other renderers are
measured against.

The dependency is optional. Install with ``pip install "freemicro[busylight]"``.
"""

from __future__ import annotations

from freemicro.renderers.base import PALETTE, Renderer, register
from freemicro.state.engine import AgentState


@register
class BusylightRenderer(Renderer):
    name = "busylight"
    priority = 5  # reliable and colour-accurate; a great primary target.

    def __init__(self) -> None:
        self._light = None

    def _lights(self):  # pragma: no cover - requires hardware
        try:
            from busylight_core import Light
        except Exception:
            return []
        try:
            return Light.all_lights()
        except Exception:
            return []

    def available(self) -> bool:
        return bool(self._lights())

    def render(self, state: AgentState) -> None:  # pragma: no cover - hardware
        lights = self._lights()
        if not lights:
            return
        color = PALETTE[state]
        for light in lights:
            try:
                light.on(color)
            except Exception:
                continue

    def close(self) -> None:  # pragma: no cover - hardware
        for light in self._lights():
            try:
                light.off()
            except Exception:
                continue
