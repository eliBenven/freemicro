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
``SubagentStop``        (ignored - noise)     ``None``
``SessionEnd``          always                ``idle``
======================  ====================  ==================

Anything we don't recognize returns ``None`` and is left for the caller to
ignore. Keeping this mapping in one small, well-tested function is what lets
the same engine drive Claude Code today and other agents (Codex CLI, Cursor)
later - only this file changes per agent.

Beyond the state
----------------
A payload says a great deal more than which event it is, and
:func:`read_signals` lifts the parts that change what the pad should show into
a :class:`~freemicro.state.engine.SessionSignals`. The field names below were
taken from 152 payloads captured on a real machine, not from documentation:

``prompt_id``
    The turn. Present on every event, stable for its duration. A *new* one
    arriving before the old turn was closed is proof of an interrupt - the one
    thing Claude Code never announces (pressing Escape emits nothing at all).
``permission_mode``
    ``bypassPermissions`` sessions never prompt, so they are never expected to
    go amber.
``effort``
    ``{"level": "high"}`` - the session's reasoning effort, for the dial.
``background_tasks``
    Subagents, each with a ``status``. A session whose own turn has stopped but
    which still has one ``running`` is not idle.
``notification_type``
    ``permission_prompt`` states outright what the old wording heuristic could
    only guess. Not always present, so the heuristic stays as a fallback.
    ``idle_prompt`` is the other value seen in the wild; see
    :data:`IDLE_PROMPT` for why it is deliberately not amber.
``reason`` (on ``SessionEnd``)
    ``exit`` is the tab closing; ``clear`` is a ``/clear``, after which the
    session is very much still there.

``last_assistant_message`` is deliberately *not* kept: it is long free text and
the most privacy-sensitive field in the payload, and a slot label already has a
better source in ``cwd``.

See https://docs.claude.com/en/docs/claude-code/hooks for the event schema.
"""

from __future__ import annotations

from typing import Any, Mapping

from freemicro.state.engine import AgentState, SessionSignals, SessionState

# Hook events that mean "the agent is actively doing something."
_WORKING_EVENTS = frozenset(
    {"UserPromptSubmit", "PreToolUse", "PostToolUse", "PreCompact"}
)

# Substrings that mark a notification as a request for human input rather
# than an informational ping.
_PERMISSION_HINTS = ("permission", "approve", "waiting for your", "needs your")


#: What ``notification_type`` says when the session is genuinely blocked on you.
PERMISSION_PROMPT = "permission_prompt"

#: The other ``notification_type`` in the captured log: "you have not typed
#: anything for a while".
#:
#: Deliberately **not** a state change, and worth saying why rather than
#: leaving it to fall through the ``else``. It is a nudge about *you*, not a
#: report about the agent: the session is sitting at an empty composer, which
#: is exactly what ``idle`` already means. Lighting it amber would put "blocked
#: on you" and "nothing is happening" in the same colour, and amber is the one
#: colour on this pad that has to keep meaning "act now".
IDLE_PROMPT = "idle_prompt"

#: The one hook event that can ever leave a session in ``WAITING``.
#:
#: Load-bearing, and the reason it is a named constant: it is what lets a
#: *record* be read back as "a permission prompt is on screen right now" (see
#: :func:`prompt_is_pending`). :func:`classify` returns ``None`` for every
#: other kind of ``Notification``, and the caller writes nothing at all for a
#: ``None``, so a stored ``WAITING`` can only have come from a notification
#: that passed :func:`_looks_like_permission_prompt`.
PROMPT_EVENT = "Notification"


def _looks_like_permission_prompt(event: dict) -> bool:
    """Is this ``Notification`` a request for the human, or just a ping?

    ``notification_type`` answers it outright and is trusted when present -
    amber should mean *blocked on you*, and a captured payload spells that out
    (``notification_type: "permission_prompt"``, ``message: "Claude needs your
    permission"``). It is not on every notification, though, so the older
    wording heuristic remains for the ones that lack it.
    """
    kind = str(event.get("notification_type", "")).strip().lower()
    if kind:
        return kind == PERMISSION_PROMPT
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

    if name == "SessionStart":
        # A session that has just opened is idle, not absent. Registering it
        # here is what lets a fresh Claude Code window claim an Agent Key
        # immediately - otherwise the key stays dark until the first prompt,
        # and a dark key is indistinguishable from a broken one.
        return AgentState.IDLE

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


def prompt_is_pending(session: SessionState) -> bool:
    """Is there a permission prompt on screen in ``session``'s terminal *now*?

    The read-back half of :func:`classify`, and the admissibility test for
    anything that wants to **answer** a prompt from the pad
    (:mod:`freemicro.permission_prompt`). It lives here because every clause is
    a statement about hook semantics, and this module is the one file that
    changes per agent.

    All four clauses are load-bearing:

    ``state is WAITING``
        after the store's own expiry rules, so a retired claim does not count.
    ``last_event`` is :data:`PROMPT_EVENT`
        the record was written *by a permission-prompt notification*, and
        nothing has happened since - any later event would have overwritten it
        with that event's name and state. This is what excludes a hand-made
        ``freemicro emit waiting``, which carries no event name and must never
        cause a keystroke into a real session.
    not ``stale``
        belt and braces. ``waiting`` has no TTL today, so this cannot fire; it
        is here so that giving it one later cannot silently re-open the gate.
    ``prompts_for_permission``
        a ``bypassPermissions`` session never shows a prompt, so a ``waiting``
        claim from one is a contradiction and is not answered.

    What it is *not* is proof that the prompt is still unanswered: answering it
    at the keyboard emits no hook until Claude Code does the next thing. Every
    guard for that is in :mod:`freemicro.permission_prompt`.
    """
    return (
        session.state == AgentState.WAITING
        and not session.stale
        and session.last_event == PROMPT_EVENT
        and session.prompts_for_permission
    )


def _running_background_tasks(event: Mapping[str, Any]) -> int:
    """How many of the payload's ``background_tasks`` are still running."""
    tasks = event.get("background_tasks")
    if not isinstance(tasks, (list, tuple)):
        return 0
    running = 0
    for task in tasks:
        if not isinstance(task, Mapping):
            continue
        if str(task.get("status", "")).strip().lower() == "running":
            running += 1
    return running


def _effort_level(event: Mapping[str, Any]) -> str:
    """``effort.level`` as a plain string. Tolerates either shape."""
    effort = event.get("effort")
    if isinstance(effort, Mapping):
        return str(effort.get("level", "") or "")
    return str(effort or "")


def read_signals(event: dict) -> SessionSignals:
    """Lift everything the engine can use out of a hook payload. Never raises.

    Defensive throughout: a payload that is missing a field, or has a field of
    the wrong shape, must degrade to "we don't know" rather than break a hook -
    the pad is a status light, not a validator.
    """
    if not isinstance(event, Mapping):
        return SessionSignals()
    return SessionSignals(
        event=str(event.get("hook_event_name") or event.get("event") or ""),
        prompt_id=str(event.get("prompt_id", "") or ""),
        permission_mode=str(event.get("permission_mode", "") or ""),
        effort=_effort_level(event),
        background_tasks=_running_background_tasks(event),
        end_reason=str(event.get("reason", "") or ""),
    )


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
