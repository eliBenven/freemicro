"""Build the pad's lighting messages.

The Codex Micro has three independently addressable lighting zones and, awkwardly,
*two different naming conventions* for reaching them (``docs/PROTOCOL.md``):

* ``lights.preview`` - the base firmware's **live** path. Applied immediately and
  deliberately not persisted to flash. Full field names (``effect``,
  ``brightness``, ``speed``, ``color``) and two zones: ``backlight`` (under the
  keycaps) and ``underglow`` (the base strip). **This is what visibly changes
  the lights**, and therefore what the state renderer drives.
* ``v.oai.thstatus`` - the vendor layer's per-Agent-Key accent lighting. Params
  is an *array*, one entry per key, with minimised field names (``e``, ``b``,
  ``s``, ``c``). Verified to set all six Agent Keys independently.
* ``v.oai.rgbcfg`` - the vendor layer's *stored* lighting configuration. It
  acknowledges with ``{"ok":1}`` but does **not** visibly change anything on its
  own, so FreeMicro never uses it for live state. It is kept here because it is
  part of the documented interface.

Every function is pure and returns a plain ``dict``, so the exact bytes
FreeMicro would put on the wire are assertable in unit tests with no pad
attached - the only sane way to develop against hardware CI can't hold.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple, Union

METHOD_PREVIEW = "lights.preview"
METHOD_THREAD_STATUS = "v.oai.thstatus"
METHOD_RGBCFG = "v.oai.rgbcfg"

#: Zones ``lights.preview`` addresses, by their firmware names.
ZONE_BACKLIGHT = "backlight"
ZONE_UNDERGLOW = "underglow"

#: FreeMicro's own name for the six Agent Keys, driven via ``v.oai.thstatus``.
ZONE_AGENT_KEYS = "agent_keys"

#: Every zone a user may name in their config.
ZONES: Tuple[str, ...] = (ZONE_BACKLIGHT, ZONE_UNDERGLOW, ZONE_AGENT_KEYS)

#: Firmware effect ids. ``breath`` is the idle default - it is what makes an
#: untouched pad appear to be slowly pulsing.
EFFECTS: Dict[str, int] = {
    "off": 0,
    "solid": 1,
    "snake": 2,
    "rainbow": 3,
    "breath": 4,
    "gradient": 5,
    "shallow-breath": 6,
}

#: Accepted spellings that are not the canonical name.
_EFFECT_ALIASES: Dict[str, str] = {
    "static": "solid",
    "on": "solid",
    "breathe": "breath",
    "breathing": "breath",
    "shallowbreath": "shallow-breath",
    "shallow_breath": "shallow-breath",
    "pulse": "shallow-breath",
}

#: The number of Agent Keys, and therefore of thread-status slots.
AGENT_KEY_COUNT = 6

ColorLike = Union[int, str, Sequence[int]]


class LightingError(ValueError):
    """Raised for a colour, effect, zone or brightness we cannot encode."""


# ---------------------------------------------------------------------------
# Field normalisation
# ---------------------------------------------------------------------------

def parse_effect(value: Union[str, int]) -> int:
    """Normalise an effect name or raw id to the firmware's integer."""
    if isinstance(value, bool):
        raise LightingError(f"effect must be a name or number, got {value!r}")
    if isinstance(value, int):
        if value in EFFECTS.values():
            return value
        raise LightingError(
            f"unknown effect id {value}; valid ids are {sorted(EFFECTS.values())}"
        )
    if isinstance(value, str):
        key = value.strip().lower().replace(" ", "-")
        key = _EFFECT_ALIASES.get(key, _EFFECT_ALIASES.get(key.replace("-", "_"), key))
        if key in EFFECTS:
            return EFFECTS[key]
    raise LightingError(
        f"unknown effect {value!r}; expected one of {', '.join(sorted(EFFECTS))}"
    )


def effect_name(value: int) -> str:
    """Inverse of :func:`parse_effect`, for human-readable output."""
    for name, number in EFFECTS.items():
        if number == value:
            return name
    return str(value)


def parse_color(value: ColorLike) -> int:
    """Normalise ``"#RRGGBB"`` / ``"0xRRGGBB"`` / ``[r,g,b]`` / int to an int.

    The firmware wants a single packed ``0xRRGGBB`` integer, but nobody wants to
    write ``16711680`` in a config file, so we accept every reasonable spelling
    and convert here.
    """
    if isinstance(value, bool):
        raise LightingError(f"colour must be a hex string, [r,g,b] or int: {value!r}")
    if isinstance(value, int):
        if 0 <= value <= 0xFFFFFF:
            return value
        raise LightingError(f"colour {value} is outside 0x000000-0xFFFFFF")
    if isinstance(value, str):
        text = value.strip().lstrip("#")
        if text.lower().startswith("0x"):
            text = text[2:]
        if len(text) == 3:  # #f0a shorthand
            text = "".join(ch * 2 for ch in text)
        if len(text) == 6:
            try:
                return int(text, 16)
            except ValueError:
                pass
        raise LightingError(f"colour {value!r} is not a #RRGGBB hex string")
    if isinstance(value, (list, tuple)) and len(value) == 3:
        try:
            r, g, b = (int(c) for c in value)
        except (TypeError, ValueError):
            raise LightingError(f"colour {value!r} must be three integers") from None
        if not all(0 <= c <= 255 for c in (r, g, b)):
            raise LightingError(f"colour {value!r} components must be 0-255")
        return (r << 16) | (g << 8) | b
    raise LightingError(f"cannot read colour {value!r}")


def color_to_hex(value: int) -> str:
    """Render an encoded colour back as ``#RRGGBB``."""
    return f"#{value:06X}"


def rgb_tuple(color: int) -> Tuple[int, int, int]:
    """Split an encoded colour into ``(r, g, b)``."""
    return ((color >> 16) & 0xFF, (color >> 8) & 0xFF, color & 0xFF)


def parse_zone(value: str) -> str:
    """Validate a zone name."""
    zone = str(value).strip().lower().replace("-", "_")
    if zone in ("keys", "keycaps"):
        zone = ZONE_BACKLIGHT
    if zone in ("ambient", "base", "glow"):
        zone = ZONE_UNDERGLOW
    if zone in ("agentkeys", "agent"):
        zone = ZONE_AGENT_KEYS
    if zone not in ZONES:
        raise LightingError(
            f"unknown lighting zone {value!r}; expected one of {', '.join(ZONES)}"
        )
    return zone


def _unit(value: float, field: str) -> float:
    """Validate a 0-1 float (brightness, speed, magic)."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise LightingError(f"{field} must be a number between 0 and 1") from None
    if not 0.0 <= number <= 1.0:
        raise LightingError(f"{field} must be between 0 and 1, got {number}")
    return number


# ---------------------------------------------------------------------------
# lights.preview - the live path
# ---------------------------------------------------------------------------

def preview_zone(
    color: ColorLike,
    effect: Union[str, int] = "solid",
    brightness: float = 1.0,
    speed: float = 0.0,
    magic: Optional[float] = None,
) -> Dict[str, Any]:
    """One ``lights.preview`` zone object, with the firmware's full field names."""
    zone: Dict[str, Any] = {
        "effect": parse_effect(effect),
        "brightness": _unit(brightness, "brightness"),
        "speed": _unit(speed, "speed"),
        "color": parse_color(color),
    }
    if magic is not None:
        zone["magic"] = _unit(magic, "magic")
    return zone


def preview_message(
    backlight: Optional[Dict[str, Any]] = None,
    underglow: Optional[Dict[str, Any]] = None,
    request_id: int = 1,
) -> Dict[str, Any]:
    """A complete ``lights.preview`` request.

    Unlike the ``v.oai.*`` notifications this method accepts an ``id`` and
    replies ``{"result": null}``, so we send one and let the caller correlate.
    Omitted zones are simply not included, leaving them untouched.
    """
    params: Dict[str, Any] = {}
    if backlight is not None:
        params[ZONE_BACKLIGHT] = backlight
    if underglow is not None:
        params[ZONE_UNDERGLOW] = underglow
    if not params:
        raise LightingError(
            f"lights.preview needs at least one of '{ZONE_BACKLIGHT}' or "
            f"'{ZONE_UNDERGLOW}'"
        )
    return {"m": METHOD_PREVIEW, "p": params, "id": int(request_id)}


# ---------------------------------------------------------------------------
# v.oai.thstatus - per-Agent-Key accents
# ---------------------------------------------------------------------------

def thread_entry(
    index: int,
    color: ColorLike,
    effect: Union[str, int] = "solid",
    brightness: float = 1.0,
    speed: float = 0.0,
    sync_keys: Optional[bool] = None,
    sync_ambient: Optional[bool] = None,
) -> Dict[str, Any]:
    """One per-Agent-Key entry, using the vendor layer's minimised field names."""
    if not 0 <= int(index) < AGENT_KEY_COUNT:
        raise LightingError(
            f"agent-key index must be 0-{AGENT_KEY_COUNT - 1}, got {index}"
        )
    entry: Dict[str, Any] = {
        "id": int(index),
        "c": parse_color(color),
        "b": _unit(brightness, "brightness"),
        "e": parse_effect(effect),
        "s": _unit(speed, "speed"),
    }
    if sync_keys is not None:
        entry["sk"] = 1 if sync_keys else 0
    if sync_ambient is not None:
        entry["sa"] = 1 if sync_ambient else 0
    return entry


def thstatus_message(entries: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """A complete ``v.oai.thstatus`` notification. Params is an array.

    Sent without an ``id``: every ``v.oai.*`` method is a notification, and
    including one gets a ``404 Method not found`` back.
    """
    payload: List[Dict[str, Any]] = list(entries)
    if not payload:
        raise LightingError("thstatus needs at least one Agent-Key entry")
    return {"m": METHOD_THREAD_STATUS, "p": payload}


def all_agent_keys(
    color: ColorLike,
    effect: Union[str, int] = "solid",
    brightness: float = 1.0,
    speed: float = 0.0,
) -> Dict[str, Any]:
    """Set all six Agent Keys to the same look - the common case."""
    return thstatus_message(
        thread_entry(index, color, effect, brightness, speed)
        for index in range(AGENT_KEY_COUNT)
    )


# ---------------------------------------------------------------------------
# v.oai.rgbcfg - stored configuration (documented, not used for live state)
# ---------------------------------------------------------------------------

def rgbcfg_side(
    color: ColorLike,
    effect: Union[str, int] = "solid",
    brightness: float = 1.0,
    speed: float = 0.0,
) -> Dict[str, Any]:
    """One ``{e, b, s, c}`` side object for ``v.oai.rgbcfg``."""
    return {
        "e": parse_effect(effect),
        "b": _unit(brightness, "brightness"),
        "s": _unit(speed, "speed"),
        "c": parse_color(color),
    }


def rgbcfg_message(
    ambient: Optional[Dict[str, Any]] = None,
    keys: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """A complete ``v.oai.rgbcfg`` notification.

    Provided for completeness of the documented interface. It ACKs but does not
    visibly change the lights, so :func:`preview_message` is what the renderer
    actually sends.
    """
    params: Dict[str, Any] = {}
    if ambient is not None:
        params["ambient"] = ambient
    if keys is not None:
        params["keys"] = keys
    if not params:
        raise LightingError("rgbcfg needs at least one of 'ambient' or 'keys'")
    return {"m": METHOD_RGBCFG, "p": params}


__all__ = [
    "AGENT_KEY_COUNT",
    "EFFECTS",
    "LightingError",
    "METHOD_PREVIEW",
    "METHOD_RGBCFG",
    "METHOD_THREAD_STATUS",
    "ZONES",
    "ZONE_AGENT_KEYS",
    "ZONE_BACKLIGHT",
    "ZONE_UNDERGLOW",
    "all_agent_keys",
    "color_to_hex",
    "effect_name",
    "parse_color",
    "parse_effect",
    "parse_zone",
    "preview_message",
    "preview_zone",
    "rgb_tuple",
    "rgbcfg_message",
    "rgbcfg_side",
    "thread_entry",
    "thstatus_message",
]
