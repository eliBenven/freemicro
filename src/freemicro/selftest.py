"""Prove the hook → state → light loop works, without a live agent.

The headline claim of this project is "the pad's LEDs reflect Claude Code's
live state". That claim spans four pieces that are each easy to test alone and
were, for a long time, never tested *together*: the installed binary, the hook
handler, the state store, and the LED renderer. Every LED lit during
development was lit by hand.

This module closes that gap. It synthesises a full session lifecycle and pushes
each event through **exactly the path Claude Code uses** - the command string
recorded in ``~/.claude/settings.json``, executed as a subprocess with the
event JSON on stdin - then reads back what the renderer loop would have seen.
Nothing here is mocked except the agent itself.

What each phase catches
-----------------------
* **hook command** - that the recorded command exists and runs at all. This is
  the failure nobody can debug: a moved venv, a path with a space in it, or a
  ``freemicro`` that was never on ``PATH`` in the first place, all of which end
  with hooks that silently do nothing.
* **lifecycle** - that each event lands on the right state in a real store on
  disk, written by a separate process, exactly as concurrent hooks do.
* **renderer** - that each state produces the protocol messages the pad needs,
  and that the five states are actually *distinguishable* (a palette where two
  states share a colour is a bug the eye finds long after the tests don't).

Everything runs against a throwaway ``FREEMICRO_HOME``, so a self-test can
never disturb the state of a session you actually have running.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from freemicro.state.engine import AgentState, DecayPolicy, StateStore

#: A realistic Claude Code session, in order, with the state each event must
#: produce. Payloads are trimmed to the fields Claude Code actually sends that
#: we actually read - see docs at
#: https://docs.claude.com/en/docs/claude-code/hooks
LIFECYCLE: Sequence[Tuple[str, Dict[str, Any], AgentState]] = (
    (
        "you send a prompt",
        {"hook_event_name": "UserPromptSubmit", "prompt": "fix the build"},
        AgentState.WORKING,
    ),
    (
        "agent starts a tool",
        {"hook_event_name": "PreToolUse", "tool_name": "Bash"},
        AgentState.WORKING,
    ),
    (
        "agent asks permission",
        {
            "hook_event_name": "Notification",
            "message": "Claude needs your permission to use Bash",
        },
        AgentState.WAITING,
    ),
    (
        "you approve, tool finishes",
        {"hook_event_name": "PostToolUse", "tool_name": "Bash"},
        AgentState.WORKING,
    ),
    (
        "agent finishes the turn",
        {"hook_event_name": "Stop", "stop_hook_active": False},
        AgentState.DONE,
    ),
    (
        "a turn ends in failure",
        {"hook_event_name": "Stop", "is_error": True},
        AgentState.ERROR,
    ),
    (
        "session ends",
        {"hook_event_name": "SessionEnd", "reason": "exit"},
        AgentState.IDLE,
    ),
)

#: A hook that takes longer than this throttles the agent itself. Interpreter
#: start-up dominates, so this is generous - it is a smell test, not a budget.
SLOW_HOOK_SECONDS = 1.5


@dataclass
class Check:
    """One assertion, with enough context to act on a failure.

    ``warn`` marks a check about the *environment* rather than the loop -
    whether anything happens to be listening right now, say. Those are worth
    printing and worth acting on, but failing the self-test over them would
    make it environment-dependent, which is exactly the property that made the
    old "it works on my machine" story worthless.
    """

    name: str
    ok: bool
    detail: str = ""
    fix: str = ""
    warn: bool = False

    @property
    def fatal(self) -> bool:
        return not self.ok and not self.warn

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "ok": self.ok,
            "detail": self.detail,
            "fix": self.fix,
            "warn": self.warn,
        }


@dataclass
class SelfTestReport:
    command: str = ""
    source: str = ""
    checks: List[Check] = field(default_factory=list)
    elapsed: float = 0.0

    def add(
        self, name: str, ok: bool, detail: str = "", fix: str = "",
        warn: bool = False,
    ) -> Check:
        check = Check(name=name, ok=ok, detail=detail, fix=fix, warn=warn)
        self.checks.append(check)
        return check

    @property
    def ok(self) -> bool:
        return bool(self.checks) and not self.failures

    @property
    def failures(self) -> List[Check]:
        return [c for c in self.checks if c.fatal]

    @property
    def warnings(self) -> List[Check]:
        return [c for c in self.checks if not c.ok and c.warn]

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "command": self.command,
            "source": self.source,
            "elapsed": round(self.elapsed, 3),
            "checks": [c.to_dict() for c in self.checks],
        }


# ---------------------------------------------------------------------------
# Which command do we test?
# ---------------------------------------------------------------------------

def resolve_command(
    settings_path: Optional[Path] = None, override: Optional[str] = None
) -> Tuple[str, str]:
    """``(command, where_it_came_from)``.

    Prefers whatever is **actually installed**, because that is the string
    Claude Code will run. Testing the command we would install instead would
    pass on a machine whose real hooks are broken - the precise situation this
    exists to catch.
    """
    from freemicro import hooks_install

    if override:
        return override, "--command"
    _path, settings = hooks_install.read_settings(settings_path)
    installed = hooks_install.installed_commands(settings)
    for event in hooks_install.HOOK_EVENTS:
        if event in installed:
            return installed[event], "installed in Claude Code settings"
    return (
        hooks_install.hook_command(),
        "not installed yet - testing the command `freemicro install` would write",
    )


# ---------------------------------------------------------------------------
# Phases
# ---------------------------------------------------------------------------

def _check_installation(
    report: SelfTestReport, settings_path: Optional[Path]
) -> None:
    """Are the hooks actually registered, on every event, with one command?

    Testing the handler while the hooks are missing would prove the machinery
    works and the product still doesn't - so the registration is a check in its
    own right. A settings file that has drifted (some events updated, one left
    pointing at an old venv) is the sneakiest version: five states light and one
    never does, which reads as a firmware bug.
    """
    from freemicro import hooks_install

    state = hooks_install.status(settings_path)
    report.add(
        "hooks registered on every lifecycle event",
        state["installed"],
        ", ".join(state["events"]) if state["events"]
        else f"nothing in {state['path']}",
        "Claude Code will never call FreeMicro until these exist.\n"
        "freemicro install",
    )
    if not state["events"]:
        return
    distinct = sorted(set(state["commands"].values()))
    report.add(
        "every event uses the same hook command",
        len(distinct) == 1,
        distinct[0] if len(distinct) == 1 else " | ".join(distinct),
        "Some events point at a different binary than others, so some states\n"
        "will light and others silently won't.\n"
        "freemicro install     # rewrites them all to this binary",
    )


def _check_command(report: SelfTestReport, command: str) -> bool:
    """The binary in the hook command has to exist and be executable."""
    try:
        parts = shlex.split(command)
    except ValueError:
        report.add(
            "hook command parses",
            False,
            f"unbalanced quotes in {command!r}",
            "freemicro install        # rewrite it",
        )
        return False
    if not parts:
        report.add("hook command parses", False, "the command is empty",
                   "freemicro install")
        return False

    binary = Path(parts[0])
    exists = binary.is_file() and os.access(str(binary), os.X_OK)
    report.add(
        "hook binary exists and is executable",
        exists,
        str(binary),
        "The recorded command points at something that is not there - a moved\n"
        "or deleted virtualenv is the usual cause.\n"
        "freemicro install        # rewrite it with this binary's real path",
    )
    if not exists:
        return False
    if not binary.is_absolute():
        report.add(
            "hook binary path is absolute",
            False,
            str(binary),
            "Claude Code runs hooks with an environment you do not control, so\n"
            "a relative or PATH-dependent command will work in your shell and\n"
            "fail there.\nfreemicro install",
        )
        return False
    return True


def _run_hook(
    command: str, event: dict, home: Path, timeout: float
) -> Tuple[int, str, float]:
    """Invoke the hook command exactly as Claude Code does: shell + stdin JSON."""
    env = dict(os.environ)
    env["FREEMICRO_HOME"] = str(home)
    # Claude Code never sets this; we do, so a self-test can't repaint a pad.
    env["FREEMICRO_NO_DEVICE"] = "1"
    started = time.time()
    try:
        proc = subprocess.run(
            command,
            shell=True,
            input=json.dumps(event),
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return -1, f"timed out after {timeout:g}s", time.time() - started
    except OSError as exc:
        return -1, str(exc), time.time() - started
    noise = (proc.stderr or "").strip()
    return proc.returncode, noise, time.time() - started


def _run_hook_in_process(
    command: str, event: dict, home: Path, timeout: float
) -> Tuple[int, str, float]:
    """Same contract as :func:`_run_hook`, without paying for an interpreter.

    Used by ``--in-process``: it exercises the CLI handler and the store but
    *not* the installed binary, so it can never prove the hook command works.
    Only worth it when you are iterating on the classifier.
    """
    import io

    from freemicro import cli

    started = time.time()
    previous_home = os.environ.get("FREEMICRO_HOME")
    previous_stdin = sys.stdin
    os.environ["FREEMICRO_HOME"] = str(home)
    sys.stdin = io.StringIO(json.dumps(event))
    try:
        import argparse

        code = cli.cmd_hook(argparse.Namespace())
    except Exception as exc:  # noqa: BLE001 - the point is to report it
        return -1, f"{type(exc).__name__}: {exc}", time.time() - started
    finally:
        sys.stdin = previous_stdin
        if previous_home is None:
            os.environ.pop("FREEMICRO_HOME", None)
        else:
            os.environ["FREEMICRO_HOME"] = previous_home
    return code, "", time.time() - started


def _check_lifecycle(
    report: SelfTestReport, command: str, home: Path, timeout: float,
    in_process: bool,
) -> None:
    """Push a whole session through the hook and read the store back."""
    # Deliberately *not* the user's store: this reads a throwaway home, and
    # "done" must stay put long enough to be asserted on however the user has
    # configured their own pad.
    store = StateStore(
        directory=home / "state", decay=DecayPolicy(done_ttl_seconds=0)
    )
    session = "freemicro-selftest"
    runner = _run_hook_in_process if in_process else _run_hook
    slowest = 0.0

    for label, payload, expected in LIFECYCLE:
        event = dict(payload)
        event["session_id"] = session
        event["cwd"] = str(home)
        code, noise, took = runner(command, event, home, timeout)
        slowest = max(slowest, took)

        if code != 0:
            report.add(
                f"{payload['hook_event_name']}: {label}",
                False,
                f"the hook exited {code}" + (f" - {noise}" if noise else ""),
                "A hook that fails is a hook Claude Code will start warning\n"
                "about. Run the command by hand with the same JSON on stdin.",
            )
            continue

        resolved = store.resolved_state()
        report.add(
            f"{payload['hook_event_name']}: {label}",
            resolved == expected,
            f"state → {resolved.value}"
            + ("" if resolved == expected else f" (expected {expected.value})")
            + f"  [{took * 1000:.0f}ms]",
            "The hook ran but the state store did not change. Check that\n"
            "freemicro.state.hooks.classify handles this event.",
        )

    report.add(
        "hook latency is not throttling the agent",
        slowest < SLOW_HOOK_SECONDS,
        f"slowest event {slowest * 1000:.0f}ms",
        "Every hook blocks Claude Code while it runs. Something is making\n"
        "interpreter start-up slow - a heavy sitecustomize or a network\n"
        "filesystem are the usual suspects.",
    )


def _check_renderer(report: SelfTestReport) -> None:
    """Assert the LED renderer emits the right bytes for each state.

    Pure message building: no device is opened, nothing is lit. This is the
    half of the loop the store cannot see.
    """
    from freemicro import padconfig
    from freemicro.device.lighting import (
        METHOD_PREVIEW,
        METHOD_RGBCFG,
        METHOD_THREAD_STATUS,
        color_to_hex,
    )
    from freemicro.renderers.micro_leds import MicroLedsRenderer

    try:
        pad = padconfig.load()
    except padconfig.PadConfigError as exc:
        report.add(
            "pad config loads",
            False,
            str(exc),
            "freemicro keys --init --force     # start again from the default",
        )
        return
    report.add("pad config loads", True, pad.origin)

    renderer = MicroLedsRenderer(config=pad)
    colors: Dict[str, str] = {}
    for state in AgentState:
        light = renderer.light_for(state)
        try:
            messages = renderer.messages_for(state, light=light)
        except Exception as exc:  # noqa: BLE001 - a bad palette must be named
            report.add(
                f"LED message for {state.value}", False, f"{type(exc).__name__}: {exc}",
                "Check the lighting.states entry for this state in your config.",
            )
            continue

        methods = [m.get("m") for m in messages]
        known = {METHOD_PREVIEW, METHOD_THREAD_STATUS, METHOD_RGBCFG}
        ok = bool(messages) and all(m in known for m in methods)
        detail = (
            f"{color_to_hex(light.color)} → "
            + ", ".join(f"{m} ({_payload_size(msg)}B)"
                        for m, msg in zip(methods, messages))
        )
        report.add(f"LED message for {state.value}", ok, detail,
                   "lighting.zones is empty, so nothing would be sent.\n"
                   "freemicro config --edit    # set lighting.zones")
        colors[state.value] = color_to_hex(light.color)

    # Five states that light identically are five states you cannot tell apart.
    duplicates = [c for c in set(colors.values()) if list(colors.values()).count(c) > 1]
    report.add(
        "every state has a distinguishable colour",
        not duplicates,
        ", ".join(f"{k}={v}" for k, v in colors.items()),
        "Two states share a colour, so the pad cannot tell you them apart.\n"
        "freemicro config --edit    # under lighting.states",
    )

    if not pad.lighting.enabled:
        report.add(
            "LED control is enabled",
            True,
            "off - the shipped default; the messages above are correct but "
            "nothing will be sent until you opt in "
            "(freemicro lights --enable)",
        )


def _payload_size(message: dict) -> int:
    return len(json.dumps(message, separators=(",", ":")).encode("utf-8"))


def _check_loop_target(report: SelfTestReport) -> None:
    """Is anything *listening*? A warning, never a failure.

    Nothing here is about whether the loop works - it is about whether this
    machine happens to have a listener up this second. Failing on it would make
    the self-test's verdict depend on the environment, and an environment-
    dependent proof is not a proof.
    """
    from freemicro import daemon

    if daemon.is_running():
        report.add("a process is listening for state changes", True,
                   "the background daemon is running")
    elif daemon.is_installed():
        report.add(
            "a process is listening for state changes", False,
            "the daemon is installed but not running",
            "freemicro daemon status     # see why\n"
            "freemicro daemon logs       # read its log",
            warn=True,
        )
    else:
        report.add(
            "a process is listening for state changes", True,
            "no daemon installed - run `freemicro run` in a terminal, or "
            "`freemicro daemon install` to have it always on",
        )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run(
    *,
    command: Optional[str] = None,
    settings_path: Optional[Path] = None,
    timeout: float = 30.0,
    in_process: bool = False,
    check_renderer: bool = True,
) -> SelfTestReport:
    """Run the whole self-test and return a report. Never raises."""
    report = SelfTestReport()
    started = time.time()

    resolved, source = resolve_command(settings_path, command)
    report.command = resolved
    report.source = source

    if command is None:
        _check_installation(report, settings_path)

    with tempfile.TemporaryDirectory(prefix="freemicro-selftest-") as tmp:
        home = Path(tmp)
        if in_process:
            report.add("hook handler is importable", True, "--in-process")
            _check_lifecycle(report, resolved, home, timeout, True)
        elif _check_command(report, resolved):
            _check_lifecycle(report, resolved, home, timeout, False)

    if check_renderer:
        _check_renderer(report)
        _check_loop_target(report)

    report.elapsed = time.time() - started
    return report


__all__ = [
    "LIFECYCLE",
    "Check",
    "SelfTestReport",
    "resolve_command",
    "run",
]
