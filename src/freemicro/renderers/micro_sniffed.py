"""Replay renderer: drive the pad with the app's own captured LED protocol.

This is the payoff of sniff-and-replay (Path B). It loads the
:class:`SniffedProtocol` learned by ``freemicro learn`` and, for each Claude
Code state, writes the exact HID frames the ChatGPT app was observed sending for
the equivalent Codex state. The Agent Keys light up identically — same colours,
same per-key patterns — but now they follow Claude Code.

It is the highest-priority renderer when a profile exists, because it is the
truest replication. If no profile is present it stays dormant and the other
renderers (or the screen fallback) carry the signal.

Contention note: the ChatGPT desktop app must be quit, or it will keep writing
its own state to the pad and fight this renderer for the HID channel.
"""

from __future__ import annotations

from freemicro.protocol import SniffedProtocol, default_profile_path
from freemicro.renderers.base import Renderer, register
from freemicro.state.engine import AgentState


@register
class MicroSniffedRenderer(Renderer):
    name = "micro-sniffed"
    priority = 10  # truest replication — beats micro-qmk/via when a profile exists.
    experimental = True

    def __init__(self) -> None:
        self._device = None
        self._profile: SniffedProtocol | None = None
        self._load_profile()

    def _load_profile(self) -> None:
        path = default_profile_path()
        if path.exists():
            try:
                self._profile = SniffedProtocol.load(path)
            except Exception:
                self._profile = None

    def available(self) -> bool:
        # Need a learned profile with at least one state and a reachable device.
        if not self._profile or not self._profile.frames_by_state:
            return False
        return self._find_device() is not None

    def _find_device(self):
        try:
            import hid
        except Exception:
            return None
        target = (self._profile.vid_pid or "").lower()
        for dev in hid.enumerate():
            vid_pid = f"{dev.get('vendor_id', 0):04x}:{dev.get('product_id', 0):04x}"
            # If the profile pinned a vid:pid, match it; otherwise take the first
            # device exposing a raw (0xFF60) interface.
            if target:
                if vid_pid == target:
                    return dev
            elif dev.get("usage_page") == 0xFF60:
                return dev
        return None

    def _open(self):  # pragma: no cover - requires hardware
        if self._device is not None:
            return True
        dev = self._find_device()
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
        if not self._profile or not self._open():
            return
        frames = self._profile.frames_for(state)
        for frame in frames:
            padded = list(frame)
            if len(padded) < self._profile.report_length:
                padded += [0] * (self._profile.report_length - len(padded))
            try:
                self._device.write(padded)
            except Exception:
                self.close()
                return

    def close(self) -> None:  # pragma: no cover - hardware
        if self._device is not None:
            try:
                self._device.close()
            except Exception:
                pass
            self._device = None
