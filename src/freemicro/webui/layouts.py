"""Named layouts - whole pads you can keep and switch between.

A starter preset used to be a one-shot: apply it and it dissolved into the
config, so "put it back the way it was for demos" meant redoing the work by
hand. A layout is the same idea made durable - *this pad, under this name* - 
and switching is one click.

Where they live
---------------
``~/.freemicro/layouts/<name>.json``, one file per layout, each holding a whole
``bindings`` (and optional ``keycaps``) document. Plain JSON in a plain
directory, so they can be copied between machines, kept in a dotfiles repo, or
listed by a future ``freemicro layout`` command without this module being
involved. The built-in starters appear in the same list marked read-only; you
duplicate one to get something you can edit.

What a layout deliberately is *not*
-----------------------------------
It is **not** a copy of your whole config. Switching layouts must not change
your colours, your stick geometry, your comments or anything else you have
tuned once and want to keep across all of them. So a layout carries the two
things that describe "what the pad does and what is written on it" - bindings
and keycaps - and nothing else. Applying one goes through the same delta save
as any other edit, so it writes those keys and leaves the rest of the file
exactly as it found it.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from freemicro.config import config_home
from freemicro.padconfig import PadConfigError

#: Directory under the config home. Created on first save, never on read.
DIRECTORY = "layouts"

#: The parts of a config a layout owns. Everything else is yours, not the
#: layout's, and survives a switch untouched.
CARRIED = ("bindings", "keycaps")

#: Filesystem-safe, human-typable names. Deliberately strict: a layout name
#: becomes a filename, and a name that can escape the directory is a bug with
#: teeth.
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _-]{0,39}$")


def directory() -> Path:
    """Where saved layouts live."""
    return config_home() / DIRECTORY


def check_name(name: str) -> str:
    """Return a validated layout name or raise :class:`PadConfigError`."""
    cleaned = str(name or "").strip()
    if not NAME_RE.match(cleaned):
        raise PadConfigError(
            "A layout name can use letters, numbers, spaces, hyphens and "
            "underscores, up to 40 characters."
        )
    return cleaned


def path_for(name: str) -> Path:
    """The file a layout is stored in. Never escapes :func:`directory`."""
    safe = check_name(name).replace(" ", "-").lower()
    return directory() / f"{safe}.json"


def _read(path: Path) -> Optional[Dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def saved() -> List[Dict[str, Any]]:
    """Every layout the user has saved, newest first."""
    found: List[Dict[str, Any]] = []
    try:
        entries = sorted(directory().glob("*.json"))
    except OSError:
        return found
    for entry in entries:
        data = _read(entry)
        if data is None:
            continue
        found.append(
            {
                "id": entry.stem,
                "name": str(data.get("name") or entry.stem),
                "tagline": str(data.get("tagline") or ""),
                "saved_at": float(data.get("saved_at") or 0.0),
                "builtin": False,
                "bindings": data.get("bindings") or {},
                "keycaps": data.get("keycaps") or {},
                "path": str(entry),
            }
        )
    found.sort(key=lambda item: item["saved_at"], reverse=True)
    return found


def builtins() -> List[Dict[str, Any]]:
    """The shipped starters, presented as read-only layouts."""
    from freemicro.webui import starters

    out: List[Dict[str, Any]] = []
    for starter in starters.starters():
        out.append(
            {
                "id": "starter:" + starter["id"],
                "name": starter["name"],
                "tagline": starter["tagline"],
                "who": starter.get("who", ""),
                "requires": starter.get("requires", []),
                "saved_at": 0.0,
                "builtin": True,
                "bindings": starter["bindings"],
                "keycaps": {},
                "path": "",
            }
        )
    return out


def catalogue() -> List[Dict[str, Any]]:
    """Built-ins first, then the user's own - the order the picker shows."""
    return builtins() + saved()


def find(layout_id: str) -> Dict[str, Any]:
    """One layout by id. Raises :class:`KeyError` if there is no such thing."""
    for layout in catalogue():
        if layout["id"] == layout_id:
            return layout
    raise KeyError(layout_id)


def save(name: str, document: Dict[str, Any]) -> Dict[str, Any]:
    """Store the pad-shaped parts of ``document`` under ``name``.

    Overwrites an existing layout of the same name on purpose: "save as work"
    twice should mean what it says, not accumulate ``work-2``.
    """
    check_name(name)
    target = path_for(name)
    payload = {
        "name": check_name(name),
        "saved_at": time.time(),
    }
    for key in CARRIED:
        value = document.get(key)
        if isinstance(value, dict):
            payload[key] = json.loads(json.dumps(value))
    if not payload.get("bindings"):
        raise PadConfigError("there is nothing to save - this config binds no keys")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
    except OSError as exc:
        raise PadConfigError(f"could not write {target}: {exc}") from exc
    return {"id": target.stem, "name": payload["name"], "path": str(target)}


def delete(layout_id: str) -> bool:
    """Remove a saved layout. Built-ins cannot be deleted."""
    for layout in saved():
        if layout["id"] == layout_id:
            try:
                Path(layout["path"]).unlink()
            except OSError as exc:
                raise PadConfigError(f"could not delete: {exc}") from exc
            return True
    return False


def apply_to(document: Dict[str, Any], layout_id: str) -> Dict[str, Any]:
    """A copy of ``document`` wearing ``layout_id``'s bindings and keycaps."""
    layout = find(layout_id)
    updated = json.loads(json.dumps(document))
    updated["bindings"] = json.loads(json.dumps(layout["bindings"]))
    if layout.get("keycaps"):
        updated["keycaps"] = json.loads(json.dumps(layout["keycaps"]))
    return updated


__all__ = [
    "CARRIED",
    "DIRECTORY",
    "NAME_RE",
    "apply_to",
    "builtins",
    "catalogue",
    "check_name",
    "delete",
    "directory",
    "find",
    "path_for",
    "save",
    "saved",
]
