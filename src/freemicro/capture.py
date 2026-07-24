"""Parse USB-HID captures and learn a :class:`SniffedProtocol` from them.

Input comes from whatever bus sniffer you ran on the machine the pad is plugged
into (see ``docs/SNIFF-RUNBOOK.md``):

* **tshark / Wireshark** ``-T json`` export — we pull the HID payload out of each
  packet's ``usb.capdata`` / ``usbhid.data`` field.
* **Plain hex** — one report per line (``0b 00 ff 00 00 …`` or ``0b00ff0000``),
  which is easy to produce from any tool or by hand.

``learn`` takes a mapping of *Codex state name → capture file* (e.g.
``thinking=thinking.json done=done.json``), extracts the report frames the app
sent for each, and builds a profile that replays them for the matching FreeMicro
state. If you also captured solid-red / solid-green / solid-blue frames, it will
additionally try to infer the RGB byte offsets for parametric colour.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from freemicro.protocol import CODEX_TO_AGENT, ByteLayout, SniffedProtocol

_HEX_RE = re.compile(r"[0-9a-fA-F]{2}")


def _hex_to_bytes(text: str) -> list[int]:
    # Accept "0b:00:ff", "0b 00 ff", or "0b00ff".
    tokens = re.findall(r"[0-9a-fA-F]{2}", text.replace(":", " "))
    return [int(t, 16) for t in tokens]


def parse_capture(path: str | Path) -> list[list[int]]:
    """Return the list of HID report frames found in a capture file."""
    path = Path(path)
    text = path.read_text(encoding="utf-8", errors="replace").strip()
    if not text:
        return []
    if text[0] in "[{":
        try:
            return _parse_tshark_json(json.loads(text))
        except (ValueError, KeyError, TypeError):
            pass
    return _parse_hex_lines(text)


def _parse_hex_lines(text: str) -> list[list[int]]:
    frames = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        frame = _hex_to_bytes(line)
        if frame:
            frames.append(frame)
    return frames


def _parse_tshark_json(packets) -> list[list[int]]:
    frames: list[list[int]] = []
    if isinstance(packets, dict):
        packets = [packets]
    for pkt in packets:
        layers = pkt.get("_source", {}).get("layers", pkt)
        payload = (
            layers.get("usbhid.data")
            or layers.get("usb.capdata")
            or layers.get("usb.data_fragment")
        )
        if isinstance(payload, list):
            payload = payload[0] if payload else None
        if isinstance(payload, str) and payload.strip():
            frame = _hex_to_bytes(payload)
            if frame:
                frames.append(frame)
    return frames


def _dedupe(frames: list[list[int]]) -> list[list[int]]:
    seen = set()
    out = []
    for frame in frames:
        key = tuple(frame)
        if key not in seen:
            seen.add(key)
            out.append(frame)
    return out


def infer_layout(color_frames: dict[str, list[int]]) -> ByteLayout:
    """Infer RGB byte offsets from solid-colour frames.

    ``color_frames`` maps ``"red"``/``"green"``/``"blue"`` to one representative
    report each (same length, same command). We find the byte that is high in
    red-but-low-in-green/blue as the R offset, and so on.
    """
    layout = ByteLayout()
    red, green, blue = (
        color_frames.get("red"),
        color_frames.get("green"),
        color_frames.get("blue"),
    )
    if not (red and green and blue):
        return layout
    n = min(len(red), len(green), len(blue))
    for i in range(n):
        r, g, b = red[i], green[i], blue[i]
        if r > 200 and g < 60 and b < 60 and layout.r_offset is None:
            layout.r_offset = i
        elif g > 200 and r < 60 and b < 60 and layout.g_offset is None:
            layout.g_offset = i
        elif b > 200 and r < 60 and g < 60 and layout.b_offset is None:
            layout.b_offset = i
    # The command byte tends to be identical and non-zero across all three.
    for i in range(n):
        if red[i] == green[i] == blue[i] and red[i] != 0 and i not in (
            layout.r_offset,
            layout.g_offset,
            layout.b_offset,
        ):
            layout.command = red[i]
            layout.command_offset = i
            break
    return layout


def learn(
    state_captures: dict[str, str | Path],
    *,
    color_captures: dict[str, str | Path] | None = None,
    vid_pid: str = "",
) -> SniffedProtocol:
    """Build a :class:`SniffedProtocol` from per-state (and optional colour) captures.

    ``state_captures`` maps a **Codex** state name (``thinking``, ``running``,
    ``awaiting``, ``done``, ``error``, ``idle``) to a capture file. Unknown state
    names are skipped with no error so a stray file can't break the learn.
    """
    frames_by_state: dict[str, list[list[int]]] = {}
    report_length = 33
    for codex_state, capfile in state_captures.items():
        agent = CODEX_TO_AGENT.get(codex_state.lower().strip())
        if agent is None:
            continue
        frames = _dedupe(parse_capture(capfile))
        if not frames:
            continue
        report_length = max(report_length, max(len(f) for f in frames))
        # Merge if multiple Codex states map to the same agent state (e.g.
        # thinking + running -> working); keep the union of distinct frames.
        existing = frames_by_state.setdefault(agent.value, [])
        for frame in frames:
            if frame not in existing:
                existing.append(frame)

    layout = ByteLayout()
    if color_captures:
        reps: dict[str, list[int]] = {}
        for color, capfile in color_captures.items():
            frames = parse_capture(capfile)
            if frames:
                reps[color.lower().strip()] = frames[0]
        layout = infer_layout(reps)

    return SniffedProtocol(
        vid_pid=vid_pid,
        report_length=report_length,
        frames_by_state=frames_by_state,
        layout=layout,
    )
