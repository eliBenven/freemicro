"""The state engine: a tiny, dependency-free store of per-session agent state.

Design notes
------------
* **One file per session.** Each Claude Code session owns exactly one JSON
  file under the state directory. Hooks fire concurrently from independent
  processes, so we never hold a lock across sessions; the filesystem is the
  concurrency primitive. A single-file write is atomic enough for our needs
  (we write to a temp file and ``os.replace``).
* **Priority resolution.** With several agents running at once you want the
  LED to show the one that needs *you*. Priority is
  ``waiting > error > done > working > idle`` — a session waiting on your
  approval always wins.
* **TTL.** A crashed agent never sends ``SessionEnd``. Records older than the
  TTL are treated as stale and ignored (and lazily cleaned up), so a dead
  session can't pin the light on ``working`` forever.

The engine has no idea what a "renderer" or a "hook" is. It is pure state.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class AgentState(str, Enum):
    """The normalized states every agent is mapped onto.

    Inherits from :class:`str` so values serialize straight to JSON and
    compare equal to their names (``AgentState.WORKING == "working"``).
    """

    IDLE = "idle"
    WORKING = "working"
    WAITING = "waiting"
    DONE = "done"
    ERROR = "error"

    @property
    def priority(self) -> int:
        """Higher wins when several sessions are active at once."""
        return _PRIORITY[self]

    @property
    def needs_you(self) -> bool:
        """True for states where the human is the blocker."""
        return self in (AgentState.WAITING, AgentState.ERROR, AgentState.DONE)


# waiting > error > done > working > idle  (see module docstring)
_PRIORITY: dict[AgentState, int] = {
    AgentState.IDLE: 0,
    AgentState.WORKING: 1,
    AgentState.DONE: 2,
    AgentState.ERROR: 3,
    AgentState.WAITING: 4,
}

DEFAULT_TTL_SECONDS = 30 * 60


@dataclass(frozen=True)
class SessionState:
    """An immutable snapshot of one session's state."""

    session_id: str
    state: AgentState
    updated_at: float
    title: str = ""
    cwd: str = ""

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "state": self.state.value,
            "updated_at": self.updated_at,
            "title": self.title,
            "cwd": self.cwd,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "SessionState":
        return cls(
            session_id=str(data["session_id"]),
            state=AgentState(data["state"]),
            updated_at=float(data["updated_at"]),
            title=str(data.get("title", "")),
            cwd=str(data.get("cwd", "")),
        )

    def age(self, now: float | None = None) -> float:
        return (now if now is not None else time.time()) - self.updated_at


@dataclass
class StateStore:
    """Reads and writes per-session state files and resolves the winner.

    Parameters
    ----------
    directory:
        Where session JSON files live. Defaults to ``~/.freemicro/state``.
    ttl_seconds:
        Records older than this are ignored when resolving and are cleaned
        up opportunistically.
    clock:
        Injectable time source (``callable() -> float``) for tests.
    """

    directory: Path = field(
        default_factory=lambda: Path.home() / ".freemicro" / "state"
    )
    ttl_seconds: float = DEFAULT_TTL_SECONDS
    clock: "callable" = time.time

    def __post_init__(self) -> None:
        self.directory = Path(self.directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    # -- writing ---------------------------------------------------------

    def _path_for(self, session_id: str) -> Path:
        # Keep filenames filesystem-safe regardless of what a session id is.
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in session_id)
        return self.directory / f"{safe or 'default'}.json"

    def update(
        self,
        session_id: str,
        state: AgentState,
        *,
        title: str = "",
        cwd: str = "",
    ) -> SessionState:
        """Record ``state`` for ``session_id`` and return the snapshot."""
        record = SessionState(
            session_id=session_id,
            state=state,
            updated_at=self.clock(),
            title=title,
            cwd=cwd,
        )
        path = self._path_for(session_id)
        # Atomic write: temp file in the same dir, then replace.
        fd, tmp = tempfile.mkstemp(dir=self.directory, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(record.to_dict(), fh)
            os.replace(tmp, path)
        except BaseException:
            # Best effort cleanup; never leak a temp file on error.
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return record

    def clear(self, session_id: str) -> None:
        """Remove a session's record (used by ``SessionEnd``)."""
        try:
            self._path_for(session_id).unlink()
        except FileNotFoundError:
            pass

    # -- reading ---------------------------------------------------------

    def sessions(self, *, include_stale: bool = False) -> list[SessionState]:
        """Return all session snapshots, freshest first.

        Stale records (older than the TTL) are filtered out and deleted
        unless ``include_stale`` is set.
        """
        now = self.clock()
        live: list[SessionState] = []
        for path in self.directory.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                record = SessionState.from_dict(data)
            except (OSError, ValueError, KeyError):
                # A partially written or corrupt file — skip it. It will be
                # overwritten on the next update for that session.
                continue
            if not include_stale and record.age(now) > self.ttl_seconds:
                # Opportunistic cleanup of dead sessions.
                try:
                    path.unlink()
                except OSError:
                    pass
                continue
            live.append(record)
        live.sort(key=lambda s: s.updated_at, reverse=True)
        return live

    def resolve(self) -> SessionState | None:
        """Return the highest-priority live session, or ``None`` if idle.

        Ties in priority are broken by recency (most recently updated wins),
        which keeps the light responsive when two agents are both working.
        """
        sessions = self.sessions()
        if not sessions:
            return None
        return max(sessions, key=lambda s: (s.state.priority, s.updated_at))

    def resolved_state(self) -> AgentState:
        """Convenience: the resolved state value, defaulting to ``IDLE``."""
        winner = self.resolve()
        return winner.state if winner else AgentState.IDLE
