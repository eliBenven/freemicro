"""Input layer helpers (host-side).

The physical inputs on the Codex Micro are standard USB HID and already type
into your terminal — no middleware needed. This package only carries the
*recommended layout* metadata (loaded from ``presets/``) so tooling and docs
can describe the Claude Code layout in one place. The actual key remapping is
done in Work Louder Input or VIA using the exported preset files.
"""

from __future__ import annotations

import json
from pathlib import Path

_PRESETS_DIR = Path(__file__).resolve().parents[3] / "presets"


def load_preset(name: str = "claude-code.input.json") -> dict:
    """Load a layout preset from the repo's ``presets/`` directory."""
    path = _PRESETS_DIR / name
    return json.loads(path.read_text(encoding="utf-8"))


__all__ = ["load_preset"]
