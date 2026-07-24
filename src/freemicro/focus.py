"""Bring the terminal running a given session to the front.

An Agent Key stands for a project (see :mod:`freemicro.agentkeys`). Pressing one
should *take you there* - the vendor pad selects that key's chat, and the
equivalent for Claude Code is "raise the terminal tab that session is running
in". It is emphatically **not** "type ``/resume`` into whatever window happens
to be focused", which is what an earlier default did: that types into the wrong
place whenever the pad is not already pointed at the right window, which is
exactly when you reach for it.

How a tab is identified
-----------------------
By **controlling tty**, captured when the session's hooks fire (see
:func:`freemicro.state.engine.current_terminal`). Terminal.app exposes ``tty``
on every tab and iTerm2 on every session, so a tty match names one exact tab.
Nothing else on offer comes close: window titles are user-configurable and
change as the agent works.

A record whose ``tty`` is blank is not given up on. Every record also carries
the session's **pid**, and a pid yields a tty (``ps -o tty= -p <pid>``, walking
up to the process that actually sits in the tab). So the tty is derived here,
at focus time, from the stored pid - see :func:`session_tty`. That is what
makes the feature work on records written before the capture was fixed, rather
than only on sessions that have re-emitted since.

Three outcomes, in order of preference:

``tab``
    tty known **and** the emulator is scriptable → select that exact tab.
``app``
    Emulator known but not scriptable (Ghostty, Warp, WezTerm, kitty, VS Code…)
    → activate the app and stop. Right app, unknown tab.
``none``
    We do not know enough. **Do nothing at all**, and say why.

The last one is a feature. Focusing the *wrong* window is worse than focusing
nothing, because the next thing you type goes somewhere you did not intend -
and unlike a no-op you may not notice. So every path here fails closed: the
AppleScript matches on tty or does nothing, and it never launches an app that
is not already running.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from freemicro.agentkeys import (
    SLOT_COUNT,
    AgentKeysConfig,
    AgentSlot,
    normalise_project,
    resolve_slots,
)
from freemicro.state.engine import SessionState, tty_for_pid

#: ``TERM_PROGRAM`` values mapped to the macOS application name to activate.
#: Terminals that do not export ``TERM_PROGRAM`` at all simply never match, and
#: get the honest "no idea" outcome rather than a guess.
TERMINAL_APPS: Dict[str, str] = {
    "apple_terminal": "Terminal",
    "iterm.app": "iTerm2",
    "iterm2": "iTerm2",
    "vscode": "Code",
    "cursor": "Cursor",
    "ghostty": "Ghostty",
    "warpterminal": "Warp",
    "warp": "Warp",
    "wezterm": "WezTerm",
    "hyper": "Hyper",
    "kitty": "kitty",
    "alacritty": "Alacritty",
    "tabby": "Tabby",
    "rio": "Rio",
}

#: The two emulators whose AppleScript dictionaries expose a tab's tty, which is
#: what lets us select one exact tab instead of raising a whole application.
SCRIPTABLE_APPS = ("Terminal", "iTerm2")

#: A tty must look like a tty. This is the only value from an on-disk record
#: that reaches an AppleScript, so it is pattern-checked rather than escaped -
#: a device path has no legitimate reason to contain a quote or a newline.
_TTY_RE = re.compile(r"^/dev/[A-Za-z0-9._/-]{1,64}$")

METHOD_TAB = "tab"
METHOD_APP = "app"
METHOD_NONE = "none"


def app_name_for(program: str) -> str:
    """macOS application name for a ``TERM_PROGRAM`` value, or ``""``."""
    return TERMINAL_APPS.get(str(program or "").strip().lower(), "")


def is_scriptable(app: str) -> bool:
    return app in SCRIPTABLE_APPS


def valid_tty(tty: str) -> bool:
    return bool(_TTY_RE.match(str(tty or "")))


# ---------------------------------------------------------------------------
# Which tty is this session's tab?
# ---------------------------------------------------------------------------

#: Derived ttys, keyed by pid: ``pid -> (expires_at, tty)``.
#:
#: One ``ps`` per key press would be perfectly affordable; ``freemicro keys
#: --list`` and the menu bar ask for all six at once and repeatedly, which is
#: where it starts to add up. The entries expire because a pid outlives nothing
#: - if the process were to die and the number be reused by something in a
#: different tab, a cached answer would send a key press to the *wrong* tab,
#: which is the one outcome this module refuses to risk. A short life keeps the
#: window in which that is possible closed.
_TTY_CACHE: Dict[int, Tuple[float, str]] = {}
_TTY_CACHE_SECONDS = 15.0


def clear_tty_cache() -> None:
    """Forget every derived tty. For tests, and for anything long-running."""
    _TTY_CACHE.clear()


def tty_from_pid(pid: int, *, lookup: Optional[Callable[[int], str]] = None) -> str:
    """The tty owned by ``pid`` (or its nearest ancestor), cached briefly.

    ``""`` for a pid that has exited or never had a terminal - the honest
    answer, and the one that leads to "tab unknown" rather than a guess.
    """
    try:
        key = int(pid)
    except (TypeError, ValueError):
        return ""
    if key <= 0:
        return ""
    now = time.time()
    hit = _TTY_CACHE.get(key)
    if hit is not None and hit[0] > now:
        return hit[1]
    try:
        found = (lookup or tty_for_pid)(key)
    except Exception:  # noqa: BLE001 - a key press must never raise
        found = ""
    found = found if valid_tty(found) else ""
    _TTY_CACHE[key] = (now + _TTY_CACHE_SECONDS, found)
    return found


def session_tty(
    session: Optional[SessionState],
    *,
    lookup: Optional[Callable[[int], str]] = None,
) -> str:
    """The tty naming ``session``'s tab, or ``""`` if it cannot be named.

    The stored value wins when it is usable; otherwise it is derived from the
    stored pid. Both paths end at :func:`valid_tty`, so whatever comes back is
    safe to put in an AppleScript.
    """
    if session is None:
        return ""
    stored = session.terminal.tty
    if valid_tty(stored):
        return stored
    return tty_from_pid(session.pid, lookup=lookup)


# ---------------------------------------------------------------------------
# The plan
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FocusPlan:
    """What pressing this Agent Key would do, and why.

    Built without touching the outside world so it can be printed by
    ``--dry-run`` and asserted in tests. :func:`perform` is what acts on it.
    """

    method: str = METHOD_NONE
    app: str = ""
    tty: str = ""
    script: str = ""
    project: str = ""
    label: str = ""
    session: Optional[SessionState] = None
    reason: str = ""

    @property
    def actionable(self) -> bool:
        return self.method != METHOD_NONE

    def describe(self) -> str:
        """One line for ``freemicro keys --list`` and the dry-run printer."""
        where = self.label or self.project
        if self.method == METHOD_TAB:
            return f"focus {where} - {self.app} tab {self.tty}"
        if self.method == METHOD_APP:
            return f"focus {where} - {self.app} (tab unknown)"
        if where:
            return f"focus {where} - does nothing: {self.reason}"
        return f"nothing on this key ({self.reason})"


def plan_for_session(
    session: Optional[SessionState],
    *,
    project: str = "",
    label: str = "",
    fallback: bool = True,
    tty_lookup: Optional[Callable[[int], str]] = None,
) -> FocusPlan:
    """Work out how to reach ``session``'s terminal. Never raises.

    ``tty_lookup`` overrides how a pid is turned into a tty, which is how tests
    exercise the derivation without a real process or a real terminal.
    """
    if session is None:
        return FocusPlan(
            project=normalise_project(project),
            label=label,
            reason="no live session for this key",
        )

    project = normalise_project(project or session.cwd)
    label = label or project
    terminal = session.terminal
    app = app_name_for(terminal.program)
    tty = session_tty(session, lookup=tty_lookup)

    if tty and is_scriptable(app):
        return FocusPlan(
            method=METHOD_TAB,
            app=app,
            tty=tty,
            script=tab_script(app, tty),
            project=project,
            label=label,
            session=session,
        )
    if app and fallback:
        return FocusPlan(
            method=METHOD_APP,
            app=app,
            tty=tty,
            project=project,
            label=label,
            session=session,
            reason=(
                f"{app} does not expose its tabs to AppleScript"
                if not is_scriptable(app)
                else (
                    "neither this session nor its process has a controlling "
                    "terminal"
                )
            ),
        )
    reason = (
        "its terminal is not one FreeMicro can identify"
        if not app
        else "tab targeting is off for this key"
    )
    return FocusPlan(
        project=project, label=label, session=session, tty=tty, reason=reason
    )


# ---------------------------------------------------------------------------
# AppleScript
# ---------------------------------------------------------------------------

def tab_script(app: str, tty: str) -> str:
    """AppleScript that raises the tab on ``tty``, or does nothing.

    Two properties every line here is written to preserve:

    * **It never launches anything.** ``if application "X" is running`` is
      checked first, because ``tell application "X"`` on a non-running app
      starts it - a keypress that opens a fresh empty Terminal would be an
      unpleasant surprise.
    * **No match means no action.** ``activate`` only runs inside the
      ``matched`` branch, so a session whose tab has since been closed leaves
      the frontmost window exactly where it was.
    """
    if not valid_tty(tty) or not is_scriptable(app):
        return ""
    if app == "Terminal":
        body = (
            "    repeat with w in windows\n"
            "      repeat with t in tabs of w\n"
            f'        if tty of t is "{tty}" then\n'
            "          set selected of t to true\n"
            "          set frontmost of w to true\n"
            "          set matched to true\n"
            "          exit repeat\n"
            "        end if\n"
            "      end repeat\n"
            "      if matched then exit repeat\n"
            "    end repeat\n"
        )
    else:  # iTerm2
        body = (
            "    repeat with w in windows\n"
            "      repeat with t in tabs of w\n"
            "        repeat with s in sessions of t\n"
            f'          if tty of s is "{tty}" then\n'
            "            select w\n"
            "            select t\n"
            "            select s\n"
            "            set matched to true\n"
            "            exit repeat\n"
            "          end if\n"
            "        end repeat\n"
            "        if matched then exit repeat\n"
            "      end repeat\n"
            "      if matched then exit repeat\n"
            "    end repeat\n"
        )
    return (
        f'if application "{app}" is running then\n'
        f'  tell application "{app}"\n'
        "    set matched to false\n"
        f"{body}"
        "    if matched then activate\n"
        "  end tell\n"
        "end if"
    )


# ---------------------------------------------------------------------------
# Resolving a slot, then acting
# ---------------------------------------------------------------------------

def current_slots(
    config: Optional[AgentKeysConfig] = None,
    *,
    store: Any = None,
    previous: Optional[Sequence[str]] = None,
) -> List[AgentSlot]:
    """The slot assignment as the pad is currently showing it.

    Seeded from the shared assignment cache (:mod:`freemicro.state.slots`) so a
    key press resolves to the same project the LED is lit for, even though the
    press and the light may be handled by different processes.
    """
    from freemicro.state import slots as slot_cache
    from freemicro.state.engine import default_store

    if config is None:
        from freemicro import padconfig

        try:
            config = padconfig.load().agent_keys
        except Exception:  # noqa: BLE001 - a broken keymap must not break a key
            config = AgentKeysConfig()
    if store is None:
        try:
            store = default_store()
        except Exception:  # noqa: BLE001
            return []
    if previous is None:
        previous = slot_cache.load()
    from freemicro.state.engine import decay_of

    try:
        # Already decayed by the store; the policy is passed on anyway so a
        # caller handing in raw records reaches the same verdict. The whole
        # policy, never a timer at a time - that is how the two ever differed.
        sessions = store.sessions()
        decay = decay_of(store)
    except Exception:  # noqa: BLE001
        return []
    return resolve_slots(config, sessions, previous=previous, decay=decay)


def plan_for_slot(
    index: int,
    *,
    project: str = "",
    fallback: bool = True,
    slots: Optional[Sequence[AgentSlot]] = None,
    config: Optional[AgentKeysConfig] = None,
    store: Any = None,
    tty_lookup: Optional[Callable[[int], str]] = None,
) -> FocusPlan:
    """The plan for the Agent Key at ``index`` (or for an explicit ``project``).

    ``project`` short-circuits slot resolution entirely, which is how a user
    binds one key to one repo forever regardless of policy.
    """
    wanted = normalise_project(project)
    if not wanted and not 0 <= int(index) < SLOT_COUNT:
        return FocusPlan(reason=f"slot {index} is not one of the six Agent Keys")

    resolved = slots if slots is not None else current_slots(config, store=store)
    if wanted:
        for slot in resolved:
            if slot.path == wanted and slot.project is not None:
                return plan_for_session(
                    slot.session,
                    project=slot.path,
                    label=slot.label,
                    fallback=fallback,
                    tty_lookup=tty_lookup,
                )
        # Not on a key right now - look straight at the live sessions instead.
        session = _newest_session_in(wanted, resolved)
        return plan_for_session(
            session, project=wanted, fallback=fallback, tty_lookup=tty_lookup
        )

    if int(index) >= len(resolved):
        return FocusPlan(reason="no live sessions")
    slot = resolved[int(index)]
    return plan_for_session(
        slot.session,
        project=slot.path,
        label=slot.label,
        fallback=fallback,
        tty_lookup=tty_lookup,
    )


def _newest_session_in(
    path: str, slots: Sequence[AgentSlot]
) -> Optional[SessionState]:
    for slot in slots:
        if slot.project is not None and slot.project.path == path:
            return slot.project.lead
    return None


def perform(plan: FocusPlan, backend: Any) -> bool:
    """Carry out ``plan`` through an action backend. ``False`` if it did nothing.

    Deliberately quiet: a key that cannot find its terminal is a no-op, not an
    error dialog. ``freemicro keys --dry-run`` is where you find out why.
    """
    if plan.method == METHOD_TAB and plan.script:
        backend.run_applescript(plan.script)
        return True
    if plan.method == METHOD_APP and plan.app:
        backend.activate_app(plan.app)
        return True
    return False


__all__ = [
    "METHOD_APP",
    "METHOD_NONE",
    "METHOD_TAB",
    "SCRIPTABLE_APPS",
    "TERMINAL_APPS",
    "FocusPlan",
    "app_name_for",
    "clear_tty_cache",
    "current_slots",
    "is_scriptable",
    "perform",
    "plan_for_session",
    "plan_for_slot",
    "session_tty",
    "tab_script",
    "tty_from_pid",
    "valid_tty",
]
