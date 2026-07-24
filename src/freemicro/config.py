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

from freemicro.state.engine import (
    DEFAULT_DONE_TTL_SECONDS,
    DEFAULT_TOOL_TTL_SECONDS,
    DEFAULT_TTL_SECONDS,
    DEFAULT_WORKING_TTL_SECONDS,
)


def config_home() -> Path:
    """Base directory for FreeMicro state and config (override with env)."""
    override = os.environ.get("FREEMICRO_HOME")
    return Path(override) if override else Path.home() / ".freemicro"


DEFAULT_CONFIG: dict = {
    "renderers": {
        # null / empty => auto-select best available. Otherwise a preference
        # list, e.g. ["micro-leds"]. Names of renderers FreeMicro no longer has
        # are ignored, and the CLI says which and what replaced them.
        "prefer": None,
        "chime": True,
    },
    "state": {
        "ttl_seconds": DEFAULT_TTL_SECONDS,
        # "done" is an *unread* marker, not a permanent badge - see
        # freemicro.state.engine.DEFAULT_DONE_TTL_SECONDS. 0 disables the decay.
        "done_ttl_seconds": DEFAULT_DONE_TTL_SECONDS,
        # Interrupting an agent fires no hook, so a "working" claim that has
        # gone quiet this long is retired rather than believed. Raise it if you
        # run very long silent tool calls; 0 disables the check entirely.
        "working_ttl_seconds": DEFAULT_WORKING_TTL_SECONDS,
        # The same, while a tool call is known to be running and silence is
        # expected.
        "tool_ttl_seconds": DEFAULT_TOOL_TTL_SECONDS,
    },
    # Optional colour overrides, state -> [r, g, b].
    "palette": {},
}


@dataclass
class Config:
    prefer: list[str] | None = None
    chime: bool = True
    ttl_seconds: float = DEFAULT_TTL_SECONDS
    done_ttl_seconds: float = DEFAULT_DONE_TTL_SECONDS
    working_ttl_seconds: float = DEFAULT_WORKING_TTL_SECONDS
    tool_ttl_seconds: float = DEFAULT_TOOL_TTL_SECONDS
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
            done_ttl_seconds=float(
                state.get("done_ttl_seconds", DEFAULT_DONE_TTL_SECONDS)
            ),
            working_ttl_seconds=float(
                state.get("working_ttl_seconds", DEFAULT_WORKING_TTL_SECONDS)
            ),
            tool_ttl_seconds=float(
                state.get("tool_ttl_seconds", DEFAULT_TOOL_TTL_SECONDS)
            ),
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
