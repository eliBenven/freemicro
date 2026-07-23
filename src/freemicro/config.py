"""Configuration loading for FreeMicro.

The config file lives at ``~/.freemicro/config.json``. Its *shape* deliberately
mirrors OpenMicro's (per-layer ``color`` + ``bindings``, plus ``workflows``)
so layouts are familiar and, where possible, interoperable. Only the top-level
``renderers``, ``state`` and ``palette`` keys are consumed by the runtime; the
input-related keys are read by the (host-side) input tooling and are otherwise
carried through untouched.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from freemicro.state.engine import DEFAULT_TTL_SECONDS


def config_home() -> Path:
    """Base directory for FreeMicro state and config (override with env)."""
    override = os.environ.get("FREEMICRO_HOME")
    return Path(override) if override else Path.home() / ".freemicro"


DEFAULT_CONFIG: dict = {
    "renderers": {
        # null / empty => auto-select best available. Otherwise a preference
        # list, e.g. ["micro-via", "screen"].
        "prefer": None,
        "chime": True,
    },
    "state": {
        "ttl_seconds": DEFAULT_TTL_SECONDS,
    },
    # Optional colour overrides, state -> [r, g, b].
    "palette": {},
}


@dataclass
class Config:
    prefer: list[str] | None = None
    chime: bool = True
    ttl_seconds: float = DEFAULT_TTL_SECONDS
    palette: dict[str, list[int]] = field(default_factory=dict)
    raw: dict = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path | None = None) -> "Config":
        path = path or (config_home() / "config.json")
        data = dict(DEFAULT_CONFIG)
        if path.exists():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                data = _deep_merge(data, loaded)
            except (OSError, ValueError):
                # A broken config should never stop the light from working.
                pass
        renderers = data.get("renderers", {})
        state = data.get("state", {})
        return cls(
            prefer=renderers.get("prefer"),
            chime=bool(renderers.get("chime", True)),
            ttl_seconds=float(state.get("ttl_seconds", DEFAULT_TTL_SECONDS)),
            palette=data.get("palette", {}),
            raw=data,
        )


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out
