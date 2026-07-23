"""Best-effort renderer: drive a VIA/QMK pad's LEDs over raw HID — no reflash.

VIA exposes a lighting sub-protocol over its ``0xFF60`` raw-HID interface.
For most VIA boards you can set the global backlight/RGB colour without
touching firmware. Whether the *shipping Codex Micro* actually exposes this
channel is the *single open hardware question* the project hinges on (see
SPEC.md §4 and the ``freemicro detect`` command).

This renderer is therefore **experimental**: it targets the documented VIA
lighting command set and is validated the day a writable pad is in hand. Until
then it stays dormant unless a matching raw-HID interface is found, and the
screen fallback carries the signal.

Protocol reference: QMK ``via.c`` — command ``id_lighting_set_value`` (0x07)
on ``usage_page 0xFF60``. VIA "QMK RGBLIGHT" values: brightness (0x80),
effect (0x81), speed (0x82), colour HS (0x83).
"""

from __future__ import annotations

import colorsys

from freemicro.renderers.base import PALETTE, Renderer, register
from freemicro.state.engine import AgentState

# QMK/VIA raw-HID constants.
RAW_USAGE_PAGE = 0xFF60
RAW_USAGE = 0x61
CMD_LIGHTING_SET_VALUE = 0x07
CMD_LIGHTING_SAVE = 0x09
QMK_RGBLIGHT_BRIGHTNESS = 0x80
QMK_RGBLIGHT_COLOR = 0x83  # value = (hue, sat)

# Known/So-far-suspected Work Louder Creator Micro identifiers. Crowdsourced
# entries live in ``hardware/capabilities.json``; this is only a hint used to
# label the device, never a gate.
KNOWN_VENDOR_IDS = (0x574C,)  # "WL" — placeholder pending M0 confirmation.


def _rgb_to_hs_bytes(rgb: tuple[int, int, int]) -> tuple[int, int]:
    r, g, b = (c / 255.0 for c in rgb)
    h, s, _v = colorsys.rgb_to_hsv(r, g, b)
    return int(h * 255) & 0xFF, int(s * 255) & 0xFF


@register
class MicroViaRenderer(Renderer):
    name = "micro-via"
    priority = 8  # if it works, it's the marquee target — the actual pad.
    experimental = True

    def __init__(self) -> None:
        self._device = None
        self._path = None

    # -- discovery -------------------------------------------------------

    def _find_raw_interface(self):
        try:
            import hid
        except Exception:
            return None
        for dev in hid.enumerate():
            if dev.get("usage_page") == RAW_USAGE_PAGE:
                return dev
        return None

    def available(self) -> bool:
        return self._find_raw_interface() is not None

    def _open(self) -> bool:  # pragma: no cover - requires hardware
        if self._device is not None:
            return True
        dev = self._find_raw_interface()
        if not dev:
            return False
        try:
            import hid

            handle = hid.device()
            handle.open_path(dev["path"])
            self._device, self._path = handle, dev["path"]
            return True
        except Exception:
            return False

    # -- render ----------------------------------------------------------

    def _send(self, report: list[int]) -> None:  # pragma: no cover - hardware
        # VIA raw HID reports are 32 bytes, prefixed with report id 0x00.
        payload = [0x00] + report
        payload += [0x00] * (33 - len(payload))
        self._device.write(payload)

    def render(self, state: AgentState) -> None:  # pragma: no cover - hardware
        if not self._open():
            return
        hue, sat = _rgb_to_hs_bytes(PALETTE[state])
        try:
            self._send([CMD_LIGHTING_SET_VALUE, QMK_RGBLIGHT_BRIGHTNESS, 0xFF])
            self._send([CMD_LIGHTING_SET_VALUE, QMK_RGBLIGHT_COLOR, hue, sat])
        except Exception:
            self.close()

    def close(self) -> None:  # pragma: no cover - hardware
        if self._device is not None:
            try:
                self._device.close()
            except Exception:
                pass
            self._device = None
