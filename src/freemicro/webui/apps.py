"""What applications are actually installed on this Mac.

The ``app`` action takes an application ``name``, and that name has to be the
exact one macOS knows - ``"Google Chrome"``, not ``"Chrome"``; ``"Terminal"``,
not ``"terminal.app"``. A free-text box for that is a trap: a wrong name is
accepted by the config, saved happily, and then does nothing when the key is
pressed, with no error anywhere the user will look.

So the browser gets a **picker of what is really on the disk**. This module is
the only thing that knows how to find that list, it never executes anything it
finds, and it degrades to an empty list rather than raising - a Mac with an
unreadable ``/Applications`` should cost the user a picker, not the page.

Free text stays available as an escape hatch (a scripted app, a bundle in an
unusual place), but :func:`resolve` lets the UI say "no app called that here"
*while you are typing it*, instead of after the key does nothing.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Optional

#: Where macOS keeps applications, in the order a person would expect them.
#: ``~/Applications`` exists on plenty of machines (Chrome's per-user install,
#: anything from Homebrew Cask with ``--appdir``), so it is not optional.
APP_DIRS = (
    "/Applications",
    "/Applications/Utilities",
    "/System/Applications",
    "/System/Applications/Utilities",
    "~/Applications",
)

#: Sanity cap. A Mac with more bundles than this has something unusual going
#: on and the picker stops being a picker anyway.
MAX_APPS = 600


def _scan(directory: Path) -> List[Dict[str, str]]:
    found: List[Dict[str, str]] = []
    try:
        entries = sorted(directory.iterdir(), key=lambda p: p.name.lower())
    except OSError:
        return found
    for entry in entries:
        # Hidden helper bundles (".Karabiner-VirtualHIDDevice-Manager" and
        # friends) are not things a person points a key at.
        if entry.name.startswith(".") or not entry.name.endswith(".app"):
            continue
        found.append(
            {
                "name": entry.name[: -len(".app")],
                "path": str(entry),
                "where": str(directory),
            }
        )
    return found


def installed_apps() -> List[Dict[str, str]]:
    """Every ``.app`` bundle in the standard locations, de-duplicated by name.

    Sorted case-insensitively by name so the picker reads like the Finder's
    Applications folder. Never raises: an unreadable directory is skipped.
    """
    seen: Dict[str, Dict[str, str]] = {}
    for raw in APP_DIRS:
        directory = Path(os.path.expanduser(raw))
        for app in _scan(directory):
            # First match wins, so /Applications beats /System/Applications
            # for the handful of names that exist in both.
            seen.setdefault(app["name"].lower(), app)
        if len(seen) >= MAX_APPS:
            break
    apps = sorted(seen.values(), key=lambda a: a["name"].lower())
    return apps[:MAX_APPS]


def resolve(name: str, apps: Optional[List[Dict[str, str]]] = None) -> Dict[str, Any]:
    """Is ``name`` an app on this machine? Returns a verdict the UI can show.

    ``{"known": bool, "exact": str, "suggestions": [...]}`` - ``exact`` is the
    correctly-cased name when we can work one out, which lets the UI offer
    "did you mean Google Chrome?" instead of a silent failure at press time.
    """
    wanted = (name or "").strip()
    if not wanted:
        return {"known": False, "exact": "", "suggestions": [], "empty": True}
    pool = installed_apps() if apps is None else apps
    lowered = wanted.lower().rstrip()
    if lowered.endswith(".app"):
        lowered = lowered[: -len(".app")]
    for app in pool:
        if app["name"].lower() == lowered:
            return {"known": True, "exact": app["name"], "suggestions": []}
    near = [a["name"] for a in pool if lowered and lowered in a["name"].lower()]
    return {"known": False, "exact": "", "suggestions": near[:6]}


__all__ = ["APP_DIRS", "MAX_APPS", "installed_apps", "resolve"]
