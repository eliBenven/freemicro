"""What a pad key *does* - a small, extensible action registry.

Every binding in the user's config names an action ``kind`` (``text``, ``key``,
``shell``, ``applescript``, ``none``) plus that kind's parameters. This module
owns the kinds. Adding a new one is a single decorated function:

.. code-block:: python

    @action("notify", summary="Post a macOS notification.", required=("body",))
    def _run_notify(act, backend):
        backend.run_applescript(f'display notification "{act.params["body"]}"')

…and it is immediately loadable from config, validated, listed by
``freemicro keys --list``, and covered by the dry-run printer. That is the whole
point of the registry: customisation should never require touching a dispatch
``if`` chain.

Two optional arguments carry the parts that used to be exactly that. ``check``
is a kind's own load-time validation, so "that key name is unspellable" and
"that is not one of the three answers" live next to the kind rather than in a
growing chain inside :func:`validate_params`. ``on_release`` says what letting
go of the key means, and having one is what puts a kind in :data:`HOLD_KINDS` -
which is how the bridge routes a release without knowing any kind by name.

Execution goes through a :class:`Backend` rather than calling ``osascript``
directly, so tests can assert *what would have been done* without typing a
single character into the developer's actual terminal.
"""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass, field
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Dict,
    List,
    Mapping,
    Optional,
    Tuple,
)

from freemicro.input import quartz
from freemicro.input.keys import (
    KeyNameError,
    applescript_for,
    applescript_for_text,
    cgevent_spec,
    escape_applescript,
    parse_combo,
)


if TYPE_CHECKING:  # pragma: no cover - typing only
    from freemicro.padconfig import ActivityLight


class ActionError(RuntimeError):
    """Raised when an action could not be delivered."""


@dataclass(frozen=True)
class Action:
    """A resolved binding: what to do, and how to describe it."""

    kind: str
    params: Mapping[str, Any] = field(default_factory=dict)
    label: str = ""
    comment: str = ""
    #: What the pad shows while this binding is live, or ``None``.
    #:
    #: Deliberately *not* a parameter of any action kind: it describes the
    #: binding, not the work. The config layer parses it (it is a
    #: :class:`freemicro.padconfig.ActivityLight`) and the bridge hands it to
    #: whoever is driving the LEDs; nothing in this module reads it, which is
    #: why the type is only imported for checking - ``padconfig`` imports *us*.
    light: Optional["ActivityLight"] = None

    def describe(self) -> str:
        """One-line human summary, used by ``--list`` and ``--dry-run``."""
        spec = REGISTRY.get(self.kind)
        if spec is None:  # pragma: no cover - unreachable via validated config
            return self.kind
        return spec.describe(self)


# ---------------------------------------------------------------------------
# Delivery backends
# ---------------------------------------------------------------------------

class Backend:
    """How an action reaches the outside world.

    Subclasses need not implement everything: an action whose backend method
    raises :class:`NotImplementedError` simply reports a failure instead of
    crashing the bridge.
    """

    #: Shown by the CLI so users know where their keystrokes are going.
    description = "backend"

    def type_text(self, text: str) -> None:
        raise NotImplementedError

    def press_key(self, combo: str) -> None:
        raise NotImplementedError

    def hold_key(self, combo: str, down: bool) -> None:
        """Press (``down``) or release a key without the other half.

        Only some backends can do this; AppleScript cannot. It is what makes
        true hold-to-talk possible, since the pad reports press *and* release.
        """
        raise NotImplementedError

    def run_shell(self, command: str, cwd: Optional[str] = None,
                  wait: bool = False) -> None:
        raise NotImplementedError

    def run_applescript(self, script: str) -> None:
        raise NotImplementedError

    def activate_app(self, name: str, cycle: bool = False) -> None:
        """Bring an app to the front; optionally cycle its windows if it's
        already frontmost."""
        raise NotImplementedError

    def move_mouse(self, x: float, y: float, relative: bool = True) -> None:
        raise NotImplementedError

    def click_mouse(self, button: str = "left", count: int = 1) -> None:
        raise NotImplementedError

    def release_held_keys(self) -> int:
        """Let go of anything a ``hold`` binding left physically down.

        A no-op for backends that cannot hold a key in the first place, which
        is why this returns rather than raising: it is called on shutdown
        paths where "this backend has nothing to release" is a perfectly good
        answer and an exception would mask the real reason we are exiting.
        """
        return 0


class RecordingBackend(Backend):
    """Records calls instead of performing them.

    Powers ``--dry-run`` and every unit test, which is why the test suite can
    exercise the full bridge without hardware and without typing anything.
    """

    description = "dry run (nothing is delivered)"

    def __init__(self) -> None:
        self.calls: List[Tuple[str, Tuple[Any, ...]]] = []
        #: Combos currently "down", mirroring what CGEvent would be holding.
        self.held: List[str] = []

    def type_text(self, text: str) -> None:
        self.calls.append(("type_text", (text,)))

    def press_key(self, combo: str) -> None:
        self.calls.append(("press_key", (combo,)))

    def hold_key(self, combo: str, down: bool) -> None:
        self.calls.append(("hold_key", (combo, down)))
        if down:
            self.held.append(combo)
        elif combo in self.held:
            self.held.remove(combo)

    def run_shell(self, command: str, cwd: Optional[str] = None,
                  wait: bool = False) -> None:
        self.calls.append(("run_shell", (command, cwd, wait)))

    def run_applescript(self, script: str) -> None:
        self.calls.append(("run_applescript", (script,)))

    def activate_app(self, name: str, cycle: bool = False) -> None:
        self.calls.append(("activate_app", (name, cycle)))

    def move_mouse(self, x: float, y: float, relative: bool = True) -> None:
        self.calls.append(("move_mouse", (x, y, relative)))

    def click_mouse(self, button: str = "left", count: int = 1) -> None:
        self.calls.append(("click_mouse", (button, count)))

    def release_held_keys(self) -> int:
        outstanding = list(self.held)
        for combo in reversed(outstanding):
            self.hold_key(combo, False)
        self.calls.append(("release_held_keys", (len(outstanding),)))
        return len(outstanding)


class AppleScriptBackend(Backend):
    """Delivers keystrokes to the frontmost app via ``System Events``.

    macOS requires the *host terminal* to hold **Accessibility** permission for
    synthetic events; without it the OS drops them silently, so we surface
    ``osascript``'s error rather than pretending it worked.
    """

    description = "AppleScript System Events (frontmost app)"

    def __init__(self, timeout: float = 5.0) -> None:
        self.timeout = timeout

    def _osascript(self, script: str) -> None:
        try:
            proc = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True, text=True, timeout=self.timeout,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ActionError(str(exc)) from exc
        if proc.returncode != 0:
            message = (proc.stderr or "").strip() or f"exit {proc.returncode}"
            if "not allowed" in message or "1002" in message:
                message += (
                    " - grant Accessibility to this terminal in System Settings "
                    "→ Privacy & Security → Accessibility"
                )
            raise ActionError(message)

    def type_text(self, text: str) -> None:
        self._osascript(applescript_for_text(text))

    def press_key(self, combo: str) -> None:
        try:
            script = applescript_for(combo)
        except KeyNameError as exc:
            raise ActionError(str(exc)) from exc
        self._osascript(script)

    def run_shell(self, command: str, cwd: Optional[str] = None,
                  wait: bool = False) -> None:
        try:
            if wait:
                proc = subprocess.run(
                    command, shell=True, cwd=cwd, capture_output=True,
                    text=True, timeout=120,
                )
                if proc.returncode != 0:
                    raise ActionError(
                        (proc.stderr or "").strip() or f"exit {proc.returncode}"
                    )
            else:
                # Fire and forget: a slow command must not stall key handling.
                subprocess.Popen(
                    command, shell=True, cwd=cwd,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ActionError(str(exc)) from exc

    def run_applescript(self, script: str) -> None:
        self._osascript(script)

    def activate_app(self, name: str, cycle: bool = False) -> None:
        safe = escape_applescript(name)
        if not cycle:
            self._osascript(f'tell application "{safe}" to activate')
            return
        # Already frontmost? Cycle its windows instead of no-opping. Cmd-` is
        # the system shortcut for that, so we let the OS define "next window".
        script = (
            'tell application "System Events" to set frontApp to '
            "name of first application process whose frontmost is true\n"
            f'if frontApp is "{safe}" then\n'
            '  tell application "System Events" to keystroke "`" '
            "using command down\n"
            "else\n"
            f'  tell application "{safe}" to activate\n'
            "end if"
        )
        self._osascript(script)


class CGEventBackend(AppleScriptBackend):
    """Deliver keystrokes and mouse events through Quartz ``CGEvent``.

    The default when available. It inherits the AppleScript backend for the
    things AppleScript is genuinely the right tool for (running scripts,
    activating apps) and overrides everything that benefits from going lower:

    * **fn is reachable**, which AppleScript cannot do at all.
    * **Press and release are separable**, so hold-to-talk works.
    * **No subprocess per keystroke**, which is the difference between a pad
      that feels instant and one that feels laggy.

    Still requires **Accessibility** - synthetic events are gated regardless of
    which API creates them.
    """

    description = "CGEvent (frontmost app, supports fn and hold)"

    @staticmethod
    def available() -> bool:
        return quartz.is_available()

    def _flags(self, modifiers: Tuple[str, ...]) -> int:
        flags = 0
        for name in modifiers:
            flags |= quartz.MODIFIER_FLAGS.get(name, 0)
        return flags

    def type_text(self, text: str) -> None:
        try:
            quartz.type_text(text)
        except OSError as exc:
            raise ActionError(str(exc)) from exc

    def press_key(self, combo: str) -> None:
        try:
            code, modifiers = cgevent_spec(combo)
            quartz.tap_key(code, self._flags(modifiers))
        except (KeyNameError, OSError) as exc:
            raise ActionError(str(exc)) from exc

    def hold_key(self, combo: str, down: bool) -> None:
        """Hold a whole chord, modifier keys included.

        Sending only the base key with modifier *flags* is enough for a tap -
        the app sees "⌘O was pressed" - but not for a hold, because an app
        asking "is ⌃⌘ held down right now?" reads the modifier keys' own
        events, which flags alone never produce. Push-to-talk dictation is
        exactly that case, and it silently never engages otherwise.
        """
        try:
            code, modifiers = cgevent_spec(combo)
            quartz.hold_chord(code, modifiers, down)
        except (KeyNameError, OSError) as exc:
            raise ActionError(str(exc)) from exc

    def move_mouse(self, x: float, y: float, relative: bool = True) -> None:
        try:
            quartz.mouse_move(x, y, relative=relative)
        except OSError as exc:
            raise ActionError(str(exc)) from exc

    def click_mouse(self, button: str = "left", count: int = 1) -> None:
        try:
            quartz.mouse_click(button, count)
        except (OSError, ValueError) as exc:
            raise ActionError(str(exc)) from exc

    def release_held_keys(self) -> int:
        """Get every held key back up. See :func:`quartz.release_all`.

        This is the *only* backend that can leave a key down, and therefore
        the only one that can leave the whole machine in a bad state, so it is
        the one that owes a way out.
        """
        return quartz.release_all()


def best_backend() -> Backend:
    """The best delivery backend this machine supports.

    CGEvent when it loads, AppleScript otherwise. Both are gated behind the
    same Accessibility permission, so this choice never changes what the user
    has to grant - only what FreeMicro can express once granted.
    """
    if CGEventBackend.available():
        return CGEventBackend()
    return AppleScriptBackend()


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ActionSpec:
    """Everything the config layer needs to know about one action kind."""

    kind: str
    summary: str
    run: Callable[[Action, Backend], None]
    describe: Callable[[Action], str]
    required: Tuple[str, ...] = ()
    optional: Tuple[str, ...] = ()
    #: Extra load-time validation beyond "the right fields are present", e.g.
    #: "that key name is spellable" or "that is not one of the three answers".
    #: Raises :class:`ValueError`. Lives on the spec rather than in an ``if``
    #: chain inside :func:`validate_params` so a new kind can bring its own
    #: rules with it - the same reason ``run`` and ``describe`` are here.
    check: Optional[Callable[[Mapping[str, Any]], None]] = None
    #: What the *release* of the key means, if anything. A kind with one is
    #: listed in :data:`HOLD_KINDS`, which is how the bridge knows to send it
    #: the second half of a press without knowing any kind's name.
    on_release: Optional[Callable[[Action, Backend], None]] = None
    #: Does this kind still mean what it says while another key is holding
    #: **real** modifier keys down? See :data:`MODIFIER_SAFE_KINDS`.
    #:
    #: Defaults to ``False`` on purpose, and that default is the safety
    #: property: a kind that has not thought about the question - including
    #: every kind a user registers from outside this file - is treated as
    #: something that types.
    modifier_safe: bool = False
    #: Does this kind call :meth:`Backend.hold_key`, leaving a real key
    #: physically down between press and release? See
    #: :data:`MODIFIER_HOLDING_KINDS`.
    holds_keys: bool = False

    @property
    def fields(self) -> Tuple[str, ...]:
        return self.required + self.optional


REGISTRY: Dict[str, ActionSpec] = {}


def action(
    kind: str,
    *,
    summary: str,
    required: Tuple[str, ...] = (),
    optional: Tuple[str, ...] = (),
    describe: Optional[Callable[[Action], str]] = None,
    check: Optional[Callable[[Mapping[str, Any]], None]] = None,
    on_release: Optional[Callable[[Action, Backend], None]] = None,
    modifier_safe: bool = False,
    holds_keys: bool = False,
) -> Callable[[Callable[[Action, Backend], None]], Callable[[Action, Backend], None]]:
    """Register an action kind. See the module docstring for the pattern."""

    def decorate(
        fn: Callable[[Action, Backend], None]
    ) -> Callable[[Action, Backend], None]:
        REGISTRY[kind] = ActionSpec(
            kind=kind,
            summary=summary,
            run=fn,
            describe=describe or (lambda act: act.kind),
            required=tuple(required),
            optional=tuple(optional),
            check=check,
            on_release=on_release,
            modifier_safe=modifier_safe,
            holds_keys=holds_keys,
        )
        return fn

    return decorate


def validate_params(kind: str, params: Mapping[str, Any]) -> None:
    """Check one binding's parameters, raising ``ValueError`` on any problem.

    Called at config-*load* time so mistakes are reported while the user is
    editing, not silently swallowed when they press a key hours later.
    """
    spec = REGISTRY.get(kind)
    if spec is None:
        raise ValueError(
            f"unknown action {kind!r}; expected one of {', '.join(sorted(REGISTRY))}"
        )
    missing = [name for name in spec.required if name not in params]
    if missing:
        raise ValueError(
            f"action '{kind}' is missing required field(s): {', '.join(missing)}"
        )
    unknown = [name for name in params if name not in spec.fields]
    if unknown:
        allowed = ", ".join(spec.fields) or "(none)"
        raise ValueError(
            f"action '{kind}' does not take {', '.join(sorted(unknown))}; "
            f"it takes: {allowed}"
        )
    if spec.check is not None:
        spec.check(params)


def perform(act: Action, backend: Backend) -> None:
    """Run one action, translating any backend failure into ActionError."""
    spec = REGISTRY.get(act.kind)
    if spec is None:
        raise ActionError(f"unknown action {act.kind!r}")
    try:
        spec.run(act, backend)
    except ActionError:
        raise
    except NotImplementedError as exc:
        raise ActionError(f"backend cannot perform '{act.kind}'") from exc
    except Exception as exc:  # noqa: BLE001 - one bad key must not kill the bridge
        raise ActionError(str(exc)) from exc


# ---------------------------------------------------------------------------
# Built-in action kinds
# ---------------------------------------------------------------------------

def _describe_text(act: Action) -> str:
    text = str(act.params.get("text", ""))
    return f"type {text!r}" + (" + Return" if act.params.get("submit") else "")


@action(
    "text",
    summary="Type literal text; set submit=true to press Return afterwards.",
    required=("text",),
    optional=("submit",),
    describe=_describe_text,
)
def _run_text(act: Action, backend: Backend) -> None:
    backend.type_text(str(act.params["text"]))
    if act.params.get("submit"):
        backend.press_key("return")


def _check_key_combo(params: Mapping[str, Any]) -> None:
    """The ``key`` field names a keystroke we can actually deliver."""
    parse_combo(str(params["key"]))  # raises KeyNameError (a ValueError)


@action(
    "key",
    summary="Press a keystroke, e.g. escape / shift-tab / ctrl-r / cmd+shift+k.",
    required=("key",),
    describe=lambda act: f"press {act.params.get('key')}",
    check=_check_key_combo,
)
def _run_key(act: Action, backend: Backend) -> None:
    backend.press_key(str(act.params["key"]))


@action(
    "shell",
    summary="Run a shell command (fire-and-forget unless wait=true).",
    required=("command",),
    optional=("cwd", "wait"),
    describe=lambda act: f"run $ {act.params.get('command')}",
    modifier_safe=True,
)
def _run_shell(act: Action, backend: Backend) -> None:
    cwd = act.params.get("cwd")
    backend.run_shell(
        str(act.params["command"]),
        cwd=str(cwd) if cwd else None,
        wait=bool(act.params.get("wait", False)),
    )


@action(
    "applescript",
    summary="Run an arbitrary AppleScript (macOS automation escape hatch).",
    required=("script",),
    describe=lambda act: "run AppleScript",
)
def _run_applescript(act: Action, backend: Backend) -> None:
    backend.run_applescript(str(act.params["script"]))


@action(
    "app",
    summary="Bring an app to the front; cycle=true cycles its windows if it "
            "is already frontmost.",
    required=("name",),
    optional=("cycle",),
    describe=lambda act: (
        f"focus {act.params.get('name')}"
        + (" (cycle windows)" if act.params.get("cycle") else "")
    ),
    modifier_safe=True,
)
def _run_app(act: Action, backend: Backend) -> None:
    backend.activate_app(
        str(act.params["name"]), cycle=bool(act.params.get("cycle", False))
    )


#: The action an Agent Key gets by default. Named here because
#: :mod:`freemicro.padconfig` fills in the ``slot`` for it from the input id.
FOCUS_SESSION = "focus_session"


def _focus_plan(act: Action) -> Any:
    """Work out what this binding would focus. Never raises, never blocks long.

    Imported lazily: :mod:`freemicro.focus` reads the pad config, which imports
    *this* module.
    """
    from freemicro import focus

    slot = act.params.get("slot")
    try:
        return focus.plan_for_slot(
            -1 if slot is None else int(slot),  # `or -1` would eat slot 0
            project=str(act.params.get("project", "") or ""),
            fallback=bool(act.params.get("fallback", True)),
        )
    except Exception:  # noqa: BLE001 - a key press must never raise here
        return focus.FocusPlan(reason="could not read the session store")


def _describe_focus_session(act: Action) -> str:
    from freemicro import focus

    plan = _focus_plan(act)
    if isinstance(plan, focus.FocusPlan):
        return plan.describe()
    return "focus this key's session"  # pragma: no cover - defensive


@action(
    FOCUS_SESSION,
    summary="Raise the terminal running this Agent Key's project. Does nothing "
            "if that session is not live or its tab cannot be identified.",
    optional=("slot", "project", "fallback"),
    describe=_describe_focus_session,
    modifier_safe=True,
)
def _run_focus_session(act: Action, backend: Backend) -> None:
    """Bring a project's terminal tab to the front.

    Doing nothing is a correct outcome here, and the common one when a project
    is not running. Raising the *wrong* window would be worse than silence: the
    next thing typed would land somewhere unintended.
    """
    from freemicro import focus

    focus.perform(_focus_plan(act), backend)


#: Answer the permission prompt of whichever session is actually asking.
#:
#: Named here because :mod:`freemicro.padconfig` and
#: :mod:`freemicro.webui.keycaps` both refer to it, and because it is the one
#: kind whose whole point is that the binding says *what* to answer and never
#: *whether* - see :mod:`freemicro.permission_prompt`.
ANSWER_PERMISSION = "answer_permission"

#: How long the key must be held for ``long_press`` to win, in milliseconds.
#: 500 ms is the vendor firmware's own long-press threshold (FACTORY-DEFAULTS
#: §5), so a hold feels the same length on this pad as it does on a stock one.
DEFAULT_LONG_PRESS_MS = 500.0

#: Monotonic, never the wall clock: an NTP step must not turn a tap into a hold.
_clock = time.monotonic

#: Keys currently held down that are waiting to find out how long they were
#: held: ``id(action) -> (action, pressed_at)``.
#:
#: Keyed by identity, and the action is kept alongside so that identity stays
#: valid - the bridge holds the same :class:`Action` object for press and
#: release, and a config reload replaces it wholesale rather than mutating it.
_HELD: Dict[int, Tuple[Action, float]] = {}

#: A press whose release never arrived (the pad dropped, the config reloaded
#: mid-hold) is forgotten rather than kept forever. It must be far longer than
#: any deliberate hold and far shorter than "the rest of the session".
_HELD_TTL_SECONDS = 60.0


def _answer_of(act: Action, field_name: str = "answer") -> str:
    from freemicro import permission_prompt

    raw = act.params.get(field_name)
    if field_name == "answer" and raw in (None, ""):
        return permission_prompt.APPROVE
    return permission_prompt.normalise_answer(raw)


def _number(act: Action, field_name: str, fallback: float) -> float:
    try:
        value = float(act.params[field_name])
    except (KeyError, TypeError, ValueError):
        return fallback
    return value if value >= 0 else fallback


def _deliver_answer(act: Action, answer: str, backend: Backend) -> None:
    """Plan and (maybe) send one answer. Silence is a correct outcome."""
    from freemicro import permission_prompt

    if not answer:
        return
    plan = permission_prompt.plan(
        answer,
        project=str(act.params.get("project", "") or ""),
        max_age=_number(act, "max_age", permission_prompt.MAX_AGE_SECONDS),
    )
    permission_prompt.perform(plan, backend)


def _forget_stale_holds(now: float) -> None:
    for token, (_, pressed_at) in list(_HELD.items()):
        if now - pressed_at > _HELD_TTL_SECONDS:
            _HELD.pop(token, None)


def _press_answer_permission(act: Action, backend: Backend) -> None:
    """A press. Delivers immediately unless this key also has a long press.

    A binding with ``long_press`` cannot decide anything yet - the difference
    between "yes" and "yes, always" is how long you go on holding it - so the
    press only starts the clock and :func:`_release_answer_permission` does the
    work. That is what makes the wait invisible for a tap and deliberate for a
    hold, and it costs nothing: only bindings that ask for a long press pay it.
    """
    if _answer_of(act, "long_press"):
        now = _clock()
        _forget_stale_holds(now)
        _HELD[id(act)] = (act, now)
        return
    _deliver_answer(act, _answer_of(act), backend)


def _release_answer_permission(act: Action, backend: Backend) -> None:
    held = _HELD.pop(id(act), None)
    if held is None:
        return  # no long press configured, or the press was already delivered
    elapsed_ms = max(0.0, _clock() - held[1]) * 1000.0
    threshold = _number(act, "long_press_ms", DEFAULT_LONG_PRESS_MS)
    long_answer = _answer_of(act, "long_press")
    _deliver_answer(
        act,
        long_answer if elapsed_ms >= threshold else _answer_of(act),
        backend,
    )


def _check_answer_permission(params: Mapping[str, Any]) -> None:
    from freemicro import permission_prompt

    for name in ("answer", "long_press"):
        value = params.get(name)
        if value in (None, ""):
            continue
        if not permission_prompt.normalise_answer(value):
            raise ValueError(
                f"'{name}' must be one of "
                f"{', '.join(permission_prompt.ANSWERS)}, got {value!r}"
            )
    for name in ("long_press_ms", "max_age"):
        if name not in params:
            continue
        try:
            number = float(params[name])
        except (TypeError, ValueError):
            raise ValueError(f"'{name}' must be a number") from None
        if number < 0:
            raise ValueError(f"'{name}' must be 0 or more")


def _describe_answer_permission(act: Action) -> str:
    from freemicro import permission_prompt

    try:
        plan = permission_prompt.plan(
            _answer_of(act),
            project=str(act.params.get("project", "") or ""),
            max_age=_number(act, "max_age", permission_prompt.MAX_AGE_SECONDS),
        )
        line = plan.describe()
    except Exception:  # noqa: BLE001 - --list must never fail over one binding
        line = "answer a permission prompt"
    long_answer = _answer_of(act, "long_press")
    if long_answer:
        held = _number(act, "long_press_ms", DEFAULT_LONG_PRESS_MS)
        line += f"; hold {held:g}ms for {long_answer}"
    return line


@action(
    ANSWER_PERMISSION,
    summary="Answer the Claude Code permission prompt of whichever session is "
            "asking: raise its exact tab, then send the answer there. Does "
            "nothing at all when nothing is waiting.",
    optional=("answer", "long_press", "long_press_ms", "project", "max_age"),
    describe=_describe_answer_permission,
    check=_check_answer_permission,
    on_release=_release_answer_permission,
)
def _run_answer_permission(act: Action, backend: Backend) -> None:
    _press_answer_permission(act, backend)


def _describe_mouse(act: Action) -> str:
    if act.params.get("click"):
        count = int(act.params.get("count", 1) or 1)
        suffix = f" x{count}" if count > 1 else ""
        return f"{act.params['click']} click{suffix}"
    return f"move mouse ({act.params.get('x', 0)}, {act.params.get('y', 0)})"


@action(
    "mouse",
    summary="Move the pointer and/or click. Needs the CGEvent backend.",
    optional=("x", "y", "absolute", "click", "count"),
    describe=_describe_mouse,
    modifier_safe=True,
)
def _run_mouse(act: Action, backend: Backend) -> None:
    x = float(act.params.get("x", 0) or 0)
    y = float(act.params.get("y", 0) or 0)
    if x or y or act.params.get("absolute"):
        backend.move_mouse(x, y, relative=not act.params.get("absolute", False))
    button = act.params.get("click")
    if button:
        backend.click_mouse(str(button), int(act.params.get("count", 1) or 1))


def _release_hold(act: Action, backend: Backend) -> None:
    backend.hold_key(str(act.params["key"]), False)


@action(
    "hold",
    summary="Hold a key down while the pad key is held (true push-to-talk).",
    required=("key",),
    describe=lambda act: f"hold {act.params.get('key')} while pressed",
    check=_check_key_combo,
    on_release=_release_hold,
    holds_keys=True,
)
def _run_hold(act: Action, backend: Backend) -> None:
    # Pressing alone would leave the key stuck, so the bridge routes this kind
    # specially: down on press, up on release. Reaching here means a press.
    backend.hold_key(str(act.params["key"]), True)


@action(
    "none",
    summary="Do nothing - explicitly leave an input unbound.",
    describe=lambda act: "(unbound)",
    modifier_safe=True,
)
def _run_none(act: Action, backend: Backend) -> None:
    return None


#: Action kinds whose meaning depends on key *release* as well as press. The
#: bridge consults this rather than hard-coding a name.
#:
#: Derived from the registry rather than written out, so a kind that grows a
#: release half gets routed one automatically. Computed once, after every
#: built-in kind has registered; a kind added later by a plugin should add its
#: name here as well as registering, which is the price of the bridge reading a
#: constant instead of calling a function on every key event.
HOLD_KINDS: Tuple[str, ...] = tuple(
    sorted(kind for kind, spec in REGISTRY.items() if spec.on_release is not None)
)

#: Action kinds that still mean what they say while a ``hold`` binding has
#: **real** modifier keys physically down.
#:
#: An **allowlist**, and that is the whole point. A ``hold`` binding presses
#: Ctrl and Cmd as real keys and leaves them there, so anything that
#: synthesises a keystroke while it is down does not send what it says: with
#: ``ctrl+cmd+o`` held, ``{"action": "text", "text": "continue"}`` sends
#: ``ctrl+cmd+c``, ``ctrl+cmd+o``, ``ctrl+cmd+n``, live system and app
#: shortcuts, one of which fullscreens a window. The MIC keycap is double-width
#: and sits next to the other action keys, so brushing a second key mid-sentence
#: is an ordinary accident, not an edge case.
#:
#: Members are the kinds that reach the outside world by some route *other*
#: than the keyboard: focusing a window, moving the mouse, running a command.
#: Modifier state cannot change what any of those mean, and suppressing them
#: would be over-suppression.
#:
#: Everything else is suppressed, **including kinds this build has never heard
#: of**: ``ActionSpec.modifier_safe`` defaults to ``False``, and a kind
#: registered after import is not in this snapshot at all. "Assume it might
#: type" is the only safe guess - the cost of being wrong that way is one
#: skipped press, printed in the log; the cost of being wrong the other way is
#: an arbitrary system shortcut.
#:
#: ``applescript`` is deliberately absent. It is the documented escape hatch,
#: and ``tell application "System Events" to keystroke`` is the first thing
#: people put in one.
MODIFIER_SAFE_KINDS = frozenset(
    kind for kind, spec in REGISTRY.items() if spec.modifier_safe
)

#: Action kinds that leave a *real* key physically down between press and
#: release, and can therefore modify what any other key types.
#:
#: Not the same question as :data:`HOLD_KINDS`, which only asks whether a kind
#: cares about the release at all. ``answer_permission`` does - it times a long
#: press - but it holds nothing down while it waits, and treating it as a
#: modifier would suppress every other key for the length of an ordinary press.
#: The question here is narrower and concrete: does this kind call
#: :meth:`Backend.hold_key`?
#:
#: ``test_only_the_hold_kind_leaves_real_keys_down`` measures this against the
#: live registry by running every kind and watching the backend, so a new kind
#: that holds keys cannot be added without either declaring ``holds_keys`` or
#: failing the suite.
MODIFIER_HOLDING_KINDS = frozenset(
    kind for kind, spec in REGISTRY.items() if spec.holds_keys
)


def release(act: Action, backend: Backend) -> None:
    """Run the *release* half of a two-part action.

    Translates backend failures the same way :func:`perform` does: letting go
    of a key must not be able to kill the bridge either.
    """
    spec = REGISTRY.get(act.kind)
    if spec is None or spec.on_release is None:
        return
    try:
        spec.on_release(act, backend)
    except ActionError:
        raise
    except NotImplementedError as exc:
        raise ActionError(f"backend cannot perform '{act.kind}'") from exc
    except Exception as exc:  # noqa: BLE001 - one bad key must not kill the bridge
        raise ActionError(str(exc)) from exc


def action_help() -> List[str]:
    """One help line per registered action kind, for docs and ``--list``."""
    lines = []
    for kind in sorted(REGISTRY):
        spec = REGISTRY[kind]
        fields = ", ".join(spec.required + tuple(f"{o}?" for o in spec.optional))
        lines.append(f"{kind:12} {spec.summary}" + (f"  [{fields}]" if fields else ""))
    return lines


__all__ = [
    "ANSWER_PERMISSION",
    "DEFAULT_LONG_PRESS_MS",
    "FOCUS_SESSION",
    "Action",
    "ActionError",
    "ActionSpec",
    "AppleScriptBackend",
    "Backend",
    "CGEventBackend",
    "HOLD_KINDS",
    "MODIFIER_HOLDING_KINDS",
    "MODIFIER_SAFE_KINDS",
    "REGISTRY",
    "RecordingBackend",
    "action",
    "action_help",
    "best_backend",
    "perform",
    "release",
    "validate_params",
]
