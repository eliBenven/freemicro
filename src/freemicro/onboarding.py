"""``freemicro start`` - one command from nothing to working.

Everything FreeMicro needs is individually simple and collectively awful to
discover: two macOS permissions in two different panes, both of which fail
*silently*; a pad that types nothing until something listens to it; a vendor app
that quietly contends for the same LEDs; hooks in another program's config
file; and a background agent if you would rather not keep a terminal open.
Someone meeting this project for the first time should not have to assemble
that from a README.

Design rules this file follows
------------------------------
* **Every prompt has a safe default**, and the default is what happens on
  ``--yes``, on a timeout, and on a non-interactive stdin. Nothing here can
  hang a script or a CI job forever.
* **The dangerous default stays no.** Taking over someone's LEDs is a decision
  they make on purpose, once - ``--yes`` will not make it for them.
* **Re-runnable.** Every step detects what is already true and says so instead
  of redoing it, so this is also the right command to run when something has
  drifted.
* **Verify, don't assert.** Installing hooks is followed by actually firing a
  synthetic session through them (``freemicro selftest``). "Installed" is not
  the same as "working", and this project's whole history is that gap.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Optional

from freemicro import permissions
from freemicro.state.engine import AgentState

#: Never block a human for longer than this on one question.
PROMPT_TIMEOUT = 120.0

#: How long ``start`` waits for a pad to appear before moving on.
DEVICE_WAIT_SECONDS = 60.0


class Answers:
    """Non-interactive overrides, so ``start`` is scriptable end to end."""

    def __init__(self, args) -> None:
        self.assume_yes = bool(getattr(args, "yes", False))
        self.lights = getattr(args, "lights", None)
        self.hooks = getattr(args, "hooks", None)
        self.daemon = getattr(args, "daemon", None)


def _interactive() -> bool:
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False


def ask(
    question: str,
    default: bool = True,
    *,
    assume_yes: bool = False,
    override: Optional[bool] = None,
    timeout: float = PROMPT_TIMEOUT,
) -> bool:
    """Ask a yes/no question that can never hang.

    Returns ``override`` if one was given, the default if we are not on a
    terminal or ``--yes`` was passed, and the default again if the human walks
    away mid-setup.
    """
    if override is not None:
        print(f"  {question} [{'yes' if override else 'no'} - from the command line]")
        return override
    suffix = "[Y/n]" if default else "[y/N]"
    if assume_yes or not _interactive():
        reason = "--yes" if assume_yes else "not a terminal"
        print(f"  {question} {suffix} → {'yes' if default else 'no'} ({reason})")
        return default
    print(f"  {question} {suffix} ", end="", flush=True)
    answer = _read_line(timeout)
    if answer is None:
        print(f"\n  (no answer in {timeout:g}s - taking the default: "
              f"{'yes' if default else 'no'})")
        return default
    text = answer.strip().lower()
    if not text:
        return default
    return text.startswith("y")


def _read_line(timeout: float) -> Optional[str]:
    """One line from stdin, or ``None`` if nothing arrives in time."""
    import select

    try:
        ready, _, _ = select.select([sys.stdin], [], [], timeout)
    except (OSError, ValueError):
        # Not a selectable stdin - fall back to a plain (blocking) read.
        try:
            return sys.stdin.readline()
        except (OSError, KeyboardInterrupt):
            return None
    if not ready:
        return None
    try:
        line = sys.stdin.readline()
    except (OSError, KeyboardInterrupt):
        return None
    return line if line else None


def _pause(message: str, timeout: float = PROMPT_TIMEOUT) -> None:
    if not _interactive():
        return
    print(f"  {message} ", end="", flush=True)
    _read_line(timeout)
    print()


def _rule(title: str) -> None:
    print(f"\n{title}\n{'─' * len(title)}")


def _ok(text: str) -> None:
    print(f"  ✓ {text}")


def _no(text: str) -> None:
    print(f"  ✗ {text}")


def _note(text: str) -> None:
    for line in text.splitlines():
        print(f"    {line}")


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------

def step_intro() -> None:
    print("freemicro start - setup, in order, with nothing hidden\n")
    print(
        "This walks through the five things FreeMicro needs and stops to ask\n"
        "before changing anything outside its own config. It grants nothing\n"
        "itself: macOS permissions can only be given by you, in System\n"
        "Settings, so this checks them, opens the right pane, and waits. Then\n"
        "it finds your pad, offers to wire up Claude Code's hooks, and proves\n"
        "the whole loop works by firing a fake session through it. Safe to\n"
        "re-run any time - every step reports what is already done."
    )


def step_permissions(answers: Answers) -> bool:
    """Check both macOS permissions, guide, re-check. Returns all-granted."""
    _rule("1. macOS permissions")
    if not permissions.is_macos():
        _no("not macOS - pad support is macOS-only")
        _note("FreeMicro cannot show you agent state here: the pad is the "
              "display, and it is reached through IOKit.")
        return False

    app = permissions.host_app()
    print(f"  Both grants attach to {app}, not to freemicro itself.\n")

    checks = (
        ("input_monitoring", "Input Monitoring", "read the pad's keys and "
         "drive its LEDs", permissions.PANE_INPUT_MONITORING),
        ("accessibility", "Accessibility", "type for you",
         permissions.PANE_ACCESSIBILITY),
    )
    outcomes = []
    for key, label, why, pane in checks:
        outcomes.append(_guide_permission(answers, app, key, label, why, pane))
    return all(outcomes)


def _guide_permission(answers, app, key, label, why, pane) -> bool:
    """Check one permission, offer to open its pane, and re-check afterwards."""
    granted = _check_permission(key)
    if granted is True:
        _ok(f"{label} - granted")
        return True

    if granted is None:
        _no(f"{label} - macOS has not decided yet")
    else:
        _no(f"{label} - not granted (FreeMicro needs it to {why})")
    _note(permissions.fix_text(key))

    if not ask("Open that System Settings pane now?", True,
               assume_yes=answers.assume_yes):
        return False
    if permissions.open_pane(pane):
        _note(f"Opened. Add and enable {app}, then come back here.")
    else:
        _note(f"Could not open it. The pane is:\n{pane}")
    _pause("Press Return when you've done it (or to skip)…")

    if _check_permission(key) is True:
        _ok(f"{label} - granted")
        return True
    _no(f"{label} - still not granted")
    _note(
        f"If you just ticked the box, quit and reopen {app} and run\n"
        "`freemicro start` again. macOS only re-reads a grant when the app\n"
        "launches - this catches almost everyone once."
    )
    return False


def _check_permission(key: str) -> Optional[bool]:
    if key == "input_monitoring":
        granted, _detail = permissions.input_monitoring()
        return granted
    granted, _detail = permissions.accessibility()
    return granted


def step_device(answers: Answers) -> bool:
    """Find the pad, waiting a while if it isn't there yet."""
    from freemicro.device import device_transport

    _rule("2. Your pad")
    transport = device_transport()
    if transport:
        _ok(f"Codex Micro found over {transport}")
        return True

    _no("no Codex Micro on this machine")
    _note(
        "Either:\n"
        "  • plug in the USB-C cable, or\n"
        "  • pair it over Bluetooth (System Settings → Bluetooth) - everything\n"
        "    works wirelessly, keys, dial, thumbstick and LEDs alike."
    )
    if not ask("Wait for it to appear?", True, assume_yes=answers.assume_yes):
        _note("Carrying on without it. FreeMicro runs fine with no pad "
              "attached and\npicks it up the moment it shows up.")
        return False

    deadline = time.time() + DEVICE_WAIT_SECONDS
    print("  Waiting", end="", flush=True)
    while time.time() < deadline:
        transport = device_transport()
        if transport:
            print()
            _ok(f"Codex Micro found over {transport}")
            return True
        print(".", end="", flush=True)
        time.sleep(1.0)
    print()
    _no(f"still nothing after {DEVICE_WAIT_SECONDS:g}s")
    _note("`freemicro detect` lists every HID device macOS can see - the pad\n"
          "should appear as 303a:8360.")
    return False


def step_contention(answers: Answers) -> bool:
    """Warn about the vendor app. Returns whether to keep going."""
    _rule("3. Anything else driving the pad?")
    if not permissions.chatgpt_running():
        _ok("the ChatGPT desktop app is not running")
        return True
    _no("the ChatGPT desktop app is running")
    _note(
        "It drives the same LEDs down the same channel. Neither program can\n"
        "see the other, so the lights will flicker between the two and there\n"
        "is no way to tell from the outside which one is at fault. Quitting it\n"
        "is the fix; nothing else has to change."
    )
    return ask("Continue anyway?", True, assume_yes=answers.assume_yes)


def step_config() -> "Path":
    from freemicro import padconfig

    _rule("4. Your config")
    path = padconfig.user_path()
    if path.exists():
        _ok(f"already have one - {path}")
    else:
        padconfig.write_starter(path)
        _ok(f"created {path}")
    _note("Every key, the dial, the thumbstick and all five state colours live\n"
          "in that one file. `freemicro config --edit` opens it.")
    return path


def step_lights(answers: Answers, config_path: Path) -> bool:
    """Offer LED control. The default is, and stays, no."""
    import json

    from freemicro import padconfig

    _rule("5. LED control")
    try:
        pad = padconfig.load(config_path)
        already = pad.lighting.enabled
    except padconfig.PadConfigError:
        already = False
    if already:
        _ok("already enabled")
        return True

    print("  FreeMicro does not take over your pad's lights uninvited: macOS\n"
          "  shares this device, so if anything else is driving it you would\n"
          "  get a fight you cannot see. Turning this on is one command either\n"
          "  way (`freemicro lights --enable` / `--disable`).\n")
    if not ask("Let FreeMicro drive the LEDs from agent state?", False,
               assume_yes=answers.assume_yes, override=answers.lights):
        _note("Left off. `freemicro lights --enable` when you want it.")
        return False

    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        _no(f"could not read {config_path}: {exc}")
        return False
    data.setdefault("lighting", {})["enabled"] = True
    config_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    _ok("enabled - colours match the factory palette out of the box")
    return True


def step_hooks(answers: Answers) -> bool:
    """Install Claude Code's hooks, then prove they work."""
    from freemicro import hooks_install, selftest

    _rule("6. Claude Code hooks")
    state = hooks_install.status()
    if state["installed"] and not state["stale_commands"]:
        _ok(f"already installed in {state['path']}")
    else:
        if state["partial"]:
            _no(f"partially installed - missing "
                f"{', '.join(state['missing_events'])}")
        elif state["stale_commands"]:
            _no("installed, but calling a different FreeMicro than this one")
            _note(f"hooks call: {state['stale_commands'][0]}\n"
                  f"you ran:    {state['expected_command']}\n"
                  "Re-installing points them at this one, which is normally "
                  "what you want.")
        else:
            print("  Claude Code fires lifecycle events; FreeMicro turns them "
                  "into\n  colours. This adds six entries to "
                  f"{state['path']} and\n  touches nothing else in it.\n")
        if not ask("Install them?", True, assume_yes=answers.assume_yes,
                   override=answers.hooks):
            _note("Skipped. `freemicro install` when you want it.")
            return False
        written = hooks_install.install_hooks()
        _ok(f"written to {written}")
        _note(f"command: {hooks_install.hook_command()}")

    print("\n  Now proving it - firing a whole synthetic session through the\n"
          "  command Claude Code will actually run:\n")
    report = selftest.run()
    for check in report.checks:
        mark = "✓" if check.ok else ("!" if check.warn else "✗")
        print(f"    [{mark}] {check.name}"
              + (f" - {check.detail}" if check.detail else ""))
    if report.ok:
        _ok("the hook → state → light loop works")
        return True
    _no("the loop is not working - details above")
    _note("`freemicro selftest` re-runs this on its own.")
    return False


def step_daemon(answers: Answers) -> bool:
    from freemicro import daemon

    _rule("7. Background daemon")
    if daemon.is_installed():
        state = daemon.status()
        if state["pid"]:
            _ok(f"already installed and running (pid {state['pid']})")
        else:
            _ok(f"already installed - {state['plist']}")
            _note("It is not running right now; `freemicro daemon status` says "
                  "why.")
        return True

    print("  Without something listening, the pad is inert - its keys emit no\n"
          "  ordinary scancodes, so nothing types and nothing lights. A\n"
          "  LaunchAgent starts FreeMicro at login and restarts it if it dies,\n"
          "  so you never have to keep a terminal open. `freemicro daemon\n"
          "  uninstall` removes it completely.\n")
    if not ask("Install the background daemon?", True,
               assume_yes=answers.assume_yes, override=answers.daemon):
        _note("Skipped. Run `freemicro run` in a terminal when you want the "
              "pad live.")
        return False
    result = daemon.install()
    if not result["ok"]:
        _no(f"not installing: {result['error']}")
        return False
    pid = daemon.wait_until_running()
    if pid:
        _ok(f"installed and running (pid {pid}) - {result['path']}")
        _note(f"logs: {daemon.log_path()}")
        return True
    _no("installed, but it did not come up")
    reason = daemon.diagnose()
    _note(reason or f"`freemicro daemon logs` - {daemon.log_path()}")
    return False


def step_proof(lighting_on: bool, has_pad: bool) -> None:
    """Finish by showing the thing actually working."""
    _rule("8. Live proof")
    if not (has_pad and lighting_on):
        reason = "no pad attached" if not has_pad else "LED control is off"
        _note(f"Skipping the light show ({reason}).")
        print()
        _show_next_steps()
        return

    from freemicro import padconfig
    from freemicro.daemon import PadLock, describe_holder, lock_holder
    from freemicro.device import close_shared, shared_device
    from freemicro.renderers.micro_leds import MicroLedsRenderer

    holder = lock_holder()
    if holder:
        _note(f"{describe_holder(holder)} owns the pad right now, so this\n"
              "would fight it. Watch the pad instead - it is already live.")
        print()
        _show_next_steps()
        return

    lock = PadLock(role="start")
    if not lock.acquire():
        _note("Something else took the pad. Skipping the light show.")
        print()
        _show_next_steps()
        return
    try:
        device = shared_device()
        if device is None:
            _no("could not open the pad (Input Monitoring, then restart the "
                "terminal)")
            print()
            _show_next_steps()
            return
        pad = padconfig.load()
        renderer = MicroLedsRenderer(device=device, config=pad)
        print("  Watch the pad:\n")
        for state in AgentState:
            light = renderer.light_for(state)
            for message in renderer.messages_for(state, light=light):
                device.send(message)
            print(f"    {state.value:9} {light.describe()}")
            # Each lighting call replaces the last, so a fast sequence shows
            # only its final frame. Give a human time to actually see one.
            time.sleep(1.2)
        renderer.close()
        _ok("that is what Claude Code will drive")
    except Exception as exc:  # noqa: BLE001 - a demo must never end in a stack
        _no(f"the light show failed: {exc}")
    finally:
        close_shared()
        lock.release()
    print()
    _show_next_steps()


def _show_next_steps() -> None:
    from freemicro import daemon

    print("What to run next")
    print("────────────────")
    if daemon.is_installed():
        print("  Nothing - the daemon has the pad and starts at login.")
        print("  freemicro daemon status     is it alive?")
        print("  freemicro daemon logs       what has it been doing?")
    else:
        print("  freemicro run               keys in, agent state out "
              "(leave it running)")
    print("  freemicro status            what state is the engine in?")
    print("  freemicro selftest          re-prove the whole loop")
    print("  freemicro doctor            when anything looks wrong")
    print("  freemicro keys --list       every binding, colour and effect")


def run(args) -> int:
    """The whole guided setup. Returns a process exit code."""
    answers = Answers(args)
    step_intro()
    try:
        step_permissions(answers)
        has_pad = step_device(answers)
        if not step_contention(answers):
            print("\nStopped. Quit the ChatGPT app and run `freemicro start` "
                  "again.")
            return 1
        config_path = step_config()
        lighting_on = step_lights(answers, config_path)
        hooks_ok = step_hooks(answers)
        step_daemon(answers)
        step_proof(lighting_on, has_pad)
    except KeyboardInterrupt:
        print("\n\nStopped. Nothing is half-applied - re-run `freemicro start` "
              "any time.")
        return 130
    return 0 if hooks_ok else 1


__all__ = ["Answers", "ask", "run"]
