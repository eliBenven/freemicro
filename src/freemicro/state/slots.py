"""Remember which project each Agent Key stands for, across processes.

:class:`~freemicro.agentkeys.SlotResolver` keeps the sticky assignment in
memory, which is enough for one process. But two processes need to agree:

* the **renderer** decides what colour each key is, and
* the **key press** has to jump to whatever that key was showing.

If they resolved independently they could disagree - a cold-started resolver
fills slots by recency, a warm one by incumbency - and pressing an amber key
would land you in the wrong project. That is the one failure this feature must
not have, so the assignment is written down: a single small JSON file that both
sides seed their resolver from.

It is a *cache*, never a source of truth. A missing, stale or corrupt file costs
nothing but a cold start, so every read is defensive and every write is best
effort. Nothing here ever raises.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from freemicro.agentkeys import SLOT_COUNT

FILENAME = "slots.json"


def slots_path() -> Path:
    """Where the assignment is cached. Beside the config, not in ``state/``.

    ``state/`` is one file per session and is swept by TTL; this is neither.
    """
    from freemicro.config import config_home

    return config_home() / FILENAME


def load(path: Optional[Path] = None) -> Tuple[str, ...]:
    """The last known project path per slot. Always ``SLOT_COUNT`` long."""
    target = Path(path) if path is not None else slots_path()
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ("",) * SLOT_COUNT
    raw = data.get("slots") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        return ("",) * SLOT_COUNT
    entries: List[str] = [
        str(entry) if isinstance(entry, str) else "" for entry in raw[:SLOT_COUNT]
    ]
    entries.extend([""] * (SLOT_COUNT - len(entries)))
    return tuple(entries)


def save(slots: Sequence[str], path: Optional[Path] = None) -> bool:
    """Write the assignment atomically. ``False`` if it could not be written."""
    target = Path(path) if path is not None else slots_path()
    payload = {"slots": [str(entry or "") for entry in list(slots)[:SLOT_COUNT]]}
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(target.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh)
            os.replace(tmp, target)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except (OSError, ValueError):
        return False
    return True


def clear(path: Optional[Path] = None) -> None:
    """Forget the assignment - the next resolve starts from a cold pad."""
    target = Path(path) if path is not None else slots_path()
    try:
        target.unlink()
    except OSError:
        pass


__all__ = ["FILENAME", "clear", "load", "save", "slots_path"]
