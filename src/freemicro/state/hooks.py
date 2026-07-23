"""Map Claude Code hook events onto :class:`AgentState` values.

Claude Code fires lifecycle hooks as JSON on stdin. We only care about the
handful that mark a transition in what the agent is *doing*:

======================  ====================  ==================
Hook event              Condition             Resulting state
======================  ====================  ==================
``UserPromptSubmit``    always                ``working``
``PreToolUse``          always                ``working``
``PostToolUse``         always                ``working``
``Notification``        permission prompt     ``waiting``
``Stop``                normal completion     ``done``
``Stop``                ``error`` in payload  ``error``
``SubagentStop``        (ignored — noise)     ``None``
``SessionEnd``          always                ``idle``
======================  ====================  ==================

Anything we don't recognize returns ``None`` and is left for the caller to
ignore. Keeping this mapping in one small, well-tested function is what lets
the same engine drive Claude Code today and other agents (Codex CLI, Cursor)
later — only this file changes per agent.

See https://docs.claude.com/en/docs/claude-code/hooks for the event schema.
"""

from __future__ import annotations

from freemicro.state.engine import AgentState

# Hook events that mean "the agent is actively doing something."
_WORKING_EVENTS = frozenset(
    {"UserPromptSubmit", "PreToolUse", "PostToolUse", "PreCompact"}
)

# Substrings that mark a notification as a request for human input rather
# than an informational ping.
_PERMISSION_HINTS = ("permission", "approve", "waiting for your", "needs your")


def _looks_like_permission_prompt(event: dict) -> bool:
    matcher = str(event.get("matcher", "")).lower()
    message = str(event.get("message", "")).lower()
    haystack = f"{matcher} {message}"
    return any(hint in haystack for hint in _PERMISSION_HINTS)


def _stop_was_error(event: dict) -> bool:
    """Best-effort detection of a failed stop.

    Claude Code does not (yet) ship a distinct ``StopFailure`` event, so we
    look for error signals in the payload. If none are present we treat the
    stop as a clean completion.
    """
    if event.get("hook_event_name") in ("StopFailure", "Error"):
        return True
    if event.get("error") or event.get("is_error"):
        return True
    status = str(event.get("status", "")).lower()
    return status in ("error", "failed", "failure")


def classify(event: dict) -> AgentState | None:
    """Return the :class:`AgentState` for a hook ``event``, or ``None``.

    ``event`` is the parsed JSON payload Claude Code writes to the hook's
    stdin. The only field we rely on is ``hook_event_name``; everything else
    is optional and defensively read.
    """
    name = event.get("hook_event_name") or event.get("event")
    if not name:
        return None

    if name in _WORKING_EVENTS:
        return AgentState.WORKING

    if name == "Notification":
        return AgentState.WAITING if _looks_like_permission_prompt(event) else None

    if name in ("Stop", "StopFailure"):
        return AgentState.ERROR if _stop_was_error(event) else AgentState.DONE

    if name == "SessionEnd":
        return AgentState.IDLE

    # SubagentStop and everything else: no state change.
    return None


def session_id_of(event: dict) -> str:
    """Extract a stable session identifier from a hook event.

    Falls back to ``"default"`` so a single-session setup works even if the
    field is missing.
    """
    for key in ("session_id", "sessionId", "session"):
        value = event.get(key)
        if value:
            return str(value)
    return "default"
