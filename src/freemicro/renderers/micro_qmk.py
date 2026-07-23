"""Per-key renderer: custom QMK firmware with a FreeMicro ``raw_hid_receive``.

This is the *true per-key* path from SPEC.md §5.2: reflash the pad with the
keymap under ``firmware/qmk-keymap`` and each Agent Key can show its own
colour. It requires an open bootloader and a user willing to reflash, so it is
strictly opt-in and **experimental** until validated on hardware (M3).

The host side is trivial: send a one-byte command (0xF1) plus the state id and
an RGB triple to the ``0xFF60`` interface. The firmware maps the state id to
its Agent-Key lighting. Protocol is defined once here and mirrored in
``firmware/qmk-keymap/keymap.c``.
"""

from __future__ import annotations

from freemicro.renderers.base import PALETTE, Renderer, register
from freemicro.state.engine import AgentState

FREEMICRO_CMD = 0xF1  # custom raw-HID command id owned by our firmware.
RAW_USAGE_PAGE = 0xFF60

# Stable numeric ids for states, shared with the firmware.
STATE_ID = {
    AgentState.IDLE: 0,
    AgentState.WORKING: 1,
    AgentState.WAITING: 2,
    AgentState.DONE: 3,
    AgentState.ERROR: 4,
}


@register
class MicroQmkRenderer(Renderer):
    name = "micro-qmk"
    priority = 9  # the best experience — but only after a deliberate reflash.
    experimental = True

    def __init__(self) -> None:
        self._device = None

    def _find(self):
        try:
            import hid
        except Exception:
            return None
        for dev in hid.enumerate():
            # Our firmware advertises a distinctive product string so we don't
            # accidentally poke a stock VIA board with a custom command.
            if dev.get("usage_page") == RAW_USAGE_PAGE and "freemicro" in str(
                dev.get("product_string", "")
            ).lower():
                return dev
        return None

    def available(self) -> bool:
        return self._find() is not None

    def _open(self) -> bool:  # pragma: no cover - hardware
        if self._device is not None:
            return True
        dev = self._find()
        if not dev:
            return False
        try:
            import hid

            handle = hid.device()
            handle.open_path(dev["path"])
            self._device = handle
            return True
        except Exception:
            return False

    def render(self, state: AgentState) -> None:  # pragma: no cover - hardware
        if not self._open():
            return
        r, g, b = PALETTE[state]
        report = [0x00, FREEMICRO_CMD, STATE_ID[state], r, g, b]
        report += [0x00] * (33 - len(report))
        try:
            self._device.write(report)
        except Exception:
            self.close()

    def close(self) -> None:  # pragma: no cover - hardware
        if self._device is not None:
            try:
                self._device.close()
            except Exception:
                pass
            self._device = None
