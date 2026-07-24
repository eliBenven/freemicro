"""Answer Claude Code's permission prompt **in the session that is asking**.

Not to be confused with :mod:`freemicro.permissions`, which is about the two
macOS grants FreeMicro needs. This module is about the amber key.

The gap this closes
-------------------
An Agent Key goes amber to say *this project is blocked on you*. Until now
nothing on the pad could answer it: you looked up, pressed the key to raise the
terminal, and then reached back to the keyboard to press ``1``. The pad saved
you the glance and handed the work back.

The distinction that makes this feature rather than a macro is the one in the
title: **the answer goes to the session that is asking**, not to whatever window
happens to be focused. Typing ``1`` into the frontmost window is precisely wrong
at the moment you reach for the pad, which is the moment you are *not* already
looking at the right window. So the plan is: find the asking session, raise its
exact tab, prove that tab is now frontmost, and only then send the key.

What the keystrokes are, and how we know
----------------------------------------
Claude Code renders a permission prompt as a numbered option list. Read out of
the shipped bundle (2.1.218), the options are built in a fixed order:

1. ``Yes`` - always index 0, in every dialog variant.
2. The broader yes when the tool offers one (``yes-apply-suggestions``,
   ``yes-prefix-edited``, ``yes-dont-ask-again``, ``yes-session``…), i.e. the
   "yes, and don't ask again" the owner asked for.
3. ``No``, always last, and the dialog additionally advertises ``escape`` as
   its cancel chord (``onCancel`` resolves to ``{behavior: "deny"}``).

The list's key handler is explicit about digits::

    if(t!=="numeric"&&/^[0-9]$/.test(E)){...let w=parseInt(E)-1;
      if(w>=0&&w<r.options.length){...r.onChange?.(A.value);return}}

so a digit *selects and submits* in one press. No Return is sent, and none is
needed - which also means a digit that misses its target is a stray character
in a composer, never a command.

Hence:

======== ========= ==================================================
Answer   Key       Why it is that key
======== ========= ==================================================
approve  ``1``     Option 1 is "Yes" in every dialog, unconditionally.
always   ``2``     Option 2 is the broader yes **when one is offered**.
reject   Escape    The dialog's own cancel chord; cancel denies.
======== ========= ==================================================

``always`` is the one with a caveat, and it is stated rather than hidden: when
the tool offers no broader yes, option 2 is whatever came next - "No" in the
common case, which fails in the safe direction, or an "enable auto-accept mode"
option in the rare dialogs that offer one, which is a wider grant than asked
for. Nothing in the hook payload says which dialog is on screen, so this cannot
be resolved from here. It is why ``always`` is not on any key by default and is
reachable only by holding the approve key down on purpose.

Why it cannot fire when nothing is asking
-----------------------------------------
Six independent gates, each of which alone would prevent a stray keystroke:

1. **The kind reads the world itself.** There is no config that makes this send
   a key unconditionally; the binding says *what* to answer, never *whether*.
2. **Provenance.** Only a record written by a ``Notification`` that classified
   as a permission prompt is eligible - see
   :func:`freemicro.state.hooks.prompt_is_pending`. ``freemicro emit waiting``
   and a session in ``bypassPermissions`` are both excluded.
3. **Liveness.** The process that reported the prompt must be *provably* alive.
   "We could not tell" is a refusal, not a maybe.
4. **Age.** A record older than :data:`MAX_AGE_SECONDS` is not acted on. This is
   the backstop for a machine that slept, not the primary guard.
5. **Exact tab or nothing.** Only :data:`freemicro.focus.METHOD_TAB` qualifies.
   An emulator that cannot name its tabs to AppleScript (Ghostty, Warp, kitty…)
   gets a refusal, because "the right app, some tab" is not good enough to type
   into.
6. **The keystroke lives inside the check.** Raising the tab and typing into it
   are one AppleScript, not two calls with a gap between them, and the
   keystroke is the tail of an ``if matched and ready``: ``matched`` is set
   only where :func:`freemicro.focus.tab_script` found and selected that exact
   tty, ``ready`` only once the app is genuinely frontmost. A tab that has been
   closed since, or an app that would not come forward, ends in a script that
   did nothing. That is a structural guarantee rather than a sleep-and-hope.

On top of that, :func:`perform` latches: one record is answered at most once, so
an impatient second press cannot follow the first ``1`` with another one into
the composer that just opened.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from freemicro import focus
from freemicro.agentkeys import normalise_project
from freemicro.input.keys import KeyNameError, applescript_for
from freemicro.state.engine import SessionState
from freemicro.state.hooks import prompt_is_pending

#: Answer once, for this one request.
APPROVE = "approve"
#: Answer no. Claude Code stops and waits for you to say what to do instead.
REJECT = "reject"
#: "Yes, and don't ask again" - the broader yes, when the dialog offers one.
ALWAYS = "always"

ANSWERS: Tuple[str, ...] = (APPROVE, REJECT, ALWAYS)

#: The keystroke each answer sends. See the module docstring for the evidence.
ANSWER_KEYS: Dict[str, str] = {
    APPROVE: "1",
    ALWAYS: "2",
    REJECT: "escape",
}

#: How stale a ``waiting`` claim may be before we refuse to answer it.
#:
#: A permission prompt genuinely can sit unanswered for a long time - you went
#: to lunch - and the state engine never expires ``waiting`` for exactly that
#: reason, so age is a weak signal on its own. This is not the primary guard
#: (see the module docstring); it is the backstop that stops a record forgotten
#: across a lid-close from ever being acted on. Fifteen minutes is "you are
#: still at your desk"; past that, pressing a key on the real keyboard costs
#: nothing and typing into a session that has moved on costs a stray character.
MAX_AGE_SECONDS = 900.0

#: How long :func:`confirm_script` waits for the tab it just raised to become
#: frontmost, as ``(tries, seconds each)``. It exits the moment the check
#: passes, so the common case costs one iteration and no delay at all.
SETTLE_TRIES = 8
SETTLE_DELAY = 0.05

#: How many answered records are remembered by :func:`perform`. Only needs to
#: outlive the second or two between answering a prompt and the next hook
#: rewriting the record.
_LATCH_SIZE = 64

_LATCH: "OrderedDict[Tuple[str, float, str], bool]" = OrderedDict()


def forget_answered() -> None:
    """Clear the answered-once latch. For tests, and for a config reload."""
    _LATCH.clear()


def normalise_answer(value: Any) -> str:
    """``value`` as one of :data:`ANSWERS`, or ``""`` if it is not one."""
    text = str(value or "").strip().lower()
    return text if text in ANSWERS else ""


# ---------------------------------------------------------------------------
# Which session is asking?
# ---------------------------------------------------------------------------

def pending(
    sessions: Sequence[SessionState],
    *,
    now: float,
    max_age: float = MAX_AGE_SECONDS,
    project: str = "",
) -> List[SessionState]:
    """Every session we are willing to believe has a prompt on screen.

    Pure, so the whole admissibility rule is testable without a store, a
    process or a terminal. Ordered **most recently asked first**, which is the
    order :func:`plan` answers them in.
    """
    wanted = normalise_project(project)
    limit = max(0.0, float(max_age))
    candidates = []
    for session in sessions:
        if not prompt_is_pending(session):
            continue
        # "We could not tell" is a refusal. A record whose process cannot be
        # proved alive may belong to a terminal that has since gone away, and
        # its tab number may since have been reused by another one.
        if session.process_alive is not True:
            continue
        if limit and session.age(now) > limit:
            continue
        if wanted and normalise_project(session.cwd) != wanted:
            continue
        candidates.append(session)
    # Most recent first; session id breaks a tie so the choice is reproducible
    # rather than filesystem-order.
    candidates.sort(key=lambda s: (-s.updated_at, s.session_id))
    return candidates


# ---------------------------------------------------------------------------
# The plan
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AnswerPlan:
    """What pressing this key would answer, where, and how - or why not.

    Built without touching the outside world, so ``freemicro keys --list`` and
    ``--dry-run`` can print the live situation and the tests can assert on it.
    """

    answer: str = ""
    key: str = ""
    session: Optional[SessionState] = None
    focus_plan: Optional[focus.FocusPlan] = None
    #: The AppleScript that verifies the tab and then sends :attr:`key`.
    script: str = ""
    project: str = ""
    label: str = ""
    #: How many sessions were waiting when this plan was made.
    waiting: int = 0
    reason: str = ""

    @property
    def actionable(self) -> bool:
        """True only when a keystroke may be sent."""
        return bool(
            self.answer
            and self.script
            and self.session is not None
            and self.focus_plan is not None
            and self.focus_plan.method == focus.METHOD_TAB
        )

    def describe(self) -> str:
        """One line for ``--list``, ``--dry-run`` and the run log."""
        answer = self.answer or "answer"
        target = self.focus_plan
        if not self.actionable or target is None:
            if self.label:
                return f"{answer} {self.label} - does nothing: {self.reason}"
            return f"{answer} - does nothing: {self.reason}"
        line = f"{answer} {self.label} - {target.app} tab {target.tty}"
        if self.waiting > 1:
            # Never pick silently: say that there was a choice and which way it
            # went, so a user with two amber keys is not guessing.
            line += f" (newest of {self.waiting} waiting)"
        return line


def confirm_script(app: str, tty: str, combo: str) -> str:
    """AppleScript that raises the tab on ``tty`` and *then* sends ``combo``.

    One script rather than two, deliberately. Raising a window and typing into
    it are the same act here, and running them as two ``osascript`` calls
    leaves a gap between them in which anything at all could come to the front.
    Inside one script the keystroke is simply not reachable unless both
    conditions hold.

    The raising half is :func:`freemicro.focus.tab_script` verbatim, which is
    the same code an Agent Key press runs and which already refuses to launch
    anything, refuses to guess a tab, and sets ``matched`` only when it found
    the exact tty. This function adds two things:

    ``ready``
        the app really is frontmost. ``activate`` is a request, not a completed
        fact, so it is polled - exiting the instant it is true, which is
        almost always the first pass.
    ``if matched and ready``
        the only route to the keystroke. Tab closed since the plan was made,
        app refused to come forward, tty gone: all three end in a script that
        did nothing rather than a character typed somewhere unintended.

    ``tty`` has already been through :func:`freemicro.focus.valid_tty` and
    ``app`` is one of :data:`freemicro.focus.SCRIPTABLE_APPS`, so neither can
    carry a quote into the script. Both are re-checked by ``tab_script``, which
    returns ``""`` for anything it will not touch - and that empty string is
    what makes this function return one too.
    """
    raise_tab = focus.tab_script(app, tty)
    if not raise_tab:
        return ""
    try:
        keystroke = applescript_for(combo)
    except KeyNameError:  # pragma: no cover - the combos are constants
        return ""
    return (
        # Declared up front so the guard below is still answerable when the
        # app is not running and `tab_script` never reaches its own `set`.
        "set matched to false\n"
        "set ready to false\n"
        f"{raise_tab}\n"
        f'if matched and application "{app}" is running then\n'
        f"  repeat {SETTLE_TRIES} times\n"
        f'    tell application "{app}" to set ready to frontmost\n'
        "    if ready then exit repeat\n"
        f"    delay {SETTLE_DELAY}\n"
        "  end repeat\n"
        "end if\n"
        f"if matched and ready then {keystroke}"
    )


def plan(
    answer: str,
    *,
    project: str = "",
    max_age: float = MAX_AGE_SECONDS,
    now: Optional[float] = None,
    sessions: Optional[Sequence[SessionState]] = None,
    store: Any = None,
    tty_lookup: Optional[Callable[[int], str]] = None,
) -> AnswerPlan:
    """Work out what answering ``answer`` would do right now. Never raises.

    ``project`` restricts the key to one repository, which is how a user with
    several projects nails "approve the API repo" to its own key instead of
    accepting the newest-first rule.
    """
    chosen = normalise_answer(answer)
    if not chosen:
        return AnswerPlan(
            reason=f"{answer!r} is not one of {', '.join(ANSWERS)}"
        )
    key = ANSWER_KEYS[chosen]

    if sessions is None:
        try:
            if store is None:
                from freemicro.state.engine import default_store

                store = default_store()
            sessions = store.sessions()
        except Exception:  # noqa: BLE001 - a key press must never raise here
            return AnswerPlan(
                answer=chosen, key=key, reason="could not read the session store"
            )
    when = time.time() if now is None else float(now)
    try:
        candidates = pending(
            sessions, now=when, max_age=max_age, project=project
        )
    except Exception:  # noqa: BLE001
        return AnswerPlan(
            answer=chosen, key=key, reason="could not read the session store"
        )
    if not candidates:
        where = f" in {normalise_project(project)}" if project else ""
        return AnswerPlan(
            answer=chosen,
            key=key,
            reason=f"nothing is waiting on a permission prompt{where}",
        )

    session = candidates[0]
    target = focus.plan_for_session(
        session, project=session.cwd, tty_lookup=tty_lookup
    )
    label = target.label or target.project
    common = {
        "answer": chosen,
        "key": key,
        "session": session,
        "focus_plan": target,
        "project": target.project,
        "label": label,
        "waiting": len(candidates),
    }
    if target.method != focus.METHOD_TAB:
        # Right app but unknown tab is not good enough to type into: the next
        # character would land in some other tab of the same emulator, which is
        # very likely another live agent.
        return AnswerPlan(
            reason=(
                target.reason
                or "its terminal tab cannot be identified, so the answer would "
                "go somewhere else"
            ),
            **common,
        )
    script = confirm_script(target.app, target.tty, key)
    if not script:  # pragma: no cover - METHOD_TAB implies both are valid
        return AnswerPlan(reason="its tab cannot be addressed safely", **common)
    return AnswerPlan(script=script, **common)


# ---------------------------------------------------------------------------
# Doing it
# ---------------------------------------------------------------------------

def _latch_key(session: SessionState) -> Tuple[str, float, str]:
    return (session.session_id, session.updated_at, session.prompt_id)


def already_answered(session: Optional[SessionState]) -> bool:
    """Has this exact record already been answered from the pad?"""
    if session is None:
        return False
    return _latch_key(session) in _LATCH


def _claim(session: SessionState) -> bool:
    """Take the one answer this record is allowed, or report it is taken.

    Answering a prompt does not update the record: the state store only learns
    what happened when Claude Code's *next* hook fires a second or two later.
    Until then the record still says ``waiting``, and a second press would send
    another ``1`` - into the composer the first one just returned us to. So the
    record is consumed here rather than re-read.
    """
    key = _latch_key(session)
    if key in _LATCH:
        return False
    _LATCH[key] = True
    while len(_LATCH) > _LATCH_SIZE:
        _LATCH.popitem(last=False)
    return True


def perform(answer_plan: AnswerPlan, backend: Any) -> bool:
    """Carry out ``answer_plan``. ``False`` if nothing was sent, and often it is.

    Deliberately quiet, like :func:`freemicro.focus.perform`: a key pressed
    when nothing is asking is a no-op, not an error. What it *is* is visible -
    :meth:`AnswerPlan.describe` re-reads the world, so the run log and
    ``freemicro keys --list`` both say why the key did nothing.
    """
    if not answer_plan.actionable or answer_plan.session is None:
        return False
    if not _claim(answer_plan.session):
        return False
    # Raise the tab and answer in it, in that order, in one script - see
    # :func:`confirm_script` for why they are not two.
    backend.run_applescript(answer_plan.script)
    return True


__all__ = [
    "ALWAYS",
    "ANSWERS",
    "ANSWER_KEYS",
    "APPROVE",
    "MAX_AGE_SECONDS",
    "REJECT",
    "SETTLE_DELAY",
    "SETTLE_TRIES",
    "AnswerPlan",
    "already_answered",
    "confirm_script",
    "forget_answered",
    "normalise_answer",
    "pending",
    "perform",
    "plan",
]
