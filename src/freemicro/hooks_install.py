"""Install FreeMicro's hook into Claude Code's ``settings.json``.

We register a single command - ``freemicro hook`` - on the lifecycle events we
care about. Each hook invocation reads the event JSON from stdin, classifies
it, and updates the per-session state store. The renderer loop (``freemicro
run`` / ``watch`` / the daemon) picks the change up on its next poll.

Three things this file has to get right, all of them invisible when wrong:

1. **An absolute, quoted command.** Claude Code runs hooks through a shell with
   an environment we do not control, so a bare ``freemicro`` on ``PATH`` is not
   guaranteed to resolve - and a path containing a space silently becomes two
   arguments. Both failures look identical from the outside: the LEDs just
   never change. We pin an absolute path and quote it.
2. **Repair, not just insert.** Re-running the installer after moving the venv
   (or switching from a clone to ``pipx``) must *update* our entry, not leave a
   stale one pointing at a binary that no longer exists.
3. **Never touching anyone else's hooks.** We merge, we only ever rewrite
   entries we can prove are ours, and uninstall removes exactly those.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

# Events we hook. A single handler classifies the payload, so the same command
# is registered on all of them.
HOOK_EVENTS = (
    # Fires when a session opens, before you type anything. Without it a fresh
    # Claude Code window is invisible to FreeMicro - its Agent Key stays dark
    # until the first prompt, which reads as "the pad is broken" rather than
    # "nothing has happened yet".
    "SessionStart",
    "UserPromptSubmit",
    "PreToolUse",
    "PostToolUse",
    "Notification",
    "Stop",
    "SessionEnd",
)

#: Seconds Claude Code will wait for our hook before giving up on it. The hook
#: writes one small file and exits; anything near this means something is very
#: wrong, and we would much rather be killed than hold up the agent.
HOOK_TIMEOUT_SECONDS = 10

_MARKER = "freemicro"  # how we recognize our own entries for idempotency.


def _executable_at(path: Path) -> Optional[Path]:
    try:
        if path.is_file() and os.access(str(path), os.X_OK):
            return path
    except OSError:
        pass
    return None


def console_script() -> Optional[Path]:
    """Absolute path to the installed ``freemicro`` binary, if there is one.

    Ordered by how much we trust the answer:

    1. ``sys.argv[0]`` when it *is* our console script - this is the binary the
       user just typed, which is unambiguously the one they mean.
    2. The script beside the running interpreter (``…/venv/bin/freemicro``).
       Correct for venvs and pipx even when neither is on ``PATH``.
    3. ``PATH``. Last, because a stale entry earlier in ``PATH`` would win.
    """
    argv0 = sys.argv[0] if sys.argv else ""
    if argv0:
        candidate = Path(argv0)
        if candidate.name == "freemicro":
            try:
                resolved = candidate.resolve()
            except OSError:
                resolved = candidate
            found = _executable_at(resolved)
            if found is not None:
                return found

    found = _executable_at(Path(sys.executable).parent / "freemicro")
    if found is not None:
        return found

    which = shutil.which("freemicro")
    if which:
        return Path(which)
    return None


def hook_command() -> str:
    """The exact command string Claude Code should run for each hook event.

    Falls back to ``<python> -m freemicro hook`` using the interpreter that ran
    the install - always correct, just uglier. Both forms contain the
    ``freemicro`` marker, so idempotency detection works either way.
    """
    script = console_script()
    if script is not None:
        return f"{shlex.quote(str(script))} hook"
    return f"{shlex.quote(sys.executable)} -m freemicro hook"


# Kept for backwards compatibility / display; the installer uses hook_command().
HOOK_COMMAND = "freemicro hook"


def default_settings_path() -> Path:
    return Path.home() / ".claude" / "settings.json"


def _entry(command: str) -> Dict[str, Any]:
    return {
        "hooks": [
            {
                "type": "command",
                "command": command,
                "timeout": HOOK_TIMEOUT_SECONDS,
            }
        ]
    }


def _is_ours(command: Any) -> bool:
    """Is this hook entry one we wrote?

    Deliberately narrow: it must mention ``freemicro`` **and** end in the
    ``hook`` subcommand. Someone who bound ``freemicro emit done`` to their own
    Stop hook gets to keep it.
    """
    text = str(command or "").strip()
    if _MARKER not in text:
        return False
    try:
        return shlex.split(text)[-1] == "hook"
    except ValueError:  # unbalanced quotes - not something we wrote
        return text.split()[-1:] == ["hook"]


def _groups(hooks: Any) -> List[dict]:
    return [g for g in hooks if isinstance(g, dict)] if isinstance(hooks, list) else []


def _our_hooks(event_hooks: Any):
    """Yield ``(group, hook)`` for every entry of ours under one event."""
    for group in _groups(event_hooks):
        for hook in group.get("hooks", []) or []:
            if isinstance(hook, dict) and _is_ours(hook.get("command")):
                yield group, hook


def build_settings(existing: dict, command: Optional[str] = None) -> dict:
    """Return ``existing`` with FreeMicro hooks merged in (pure function).

    Idempotent *and* self-repairing: an entry of ours whose command no longer
    matches (moved venv, switched to pipx, an old unquoted path) is rewritten
    in place rather than duplicated.
    """
    command = command or hook_command()
    settings = json.loads(json.dumps(existing))  # deep copy
    hooks = settings.setdefault("hooks", {})
    if not isinstance(hooks, dict):  # someone's settings are not our problem
        return settings
    for event in HOOK_EVENTS:
        event_hooks = hooks.setdefault(event, [])
        if not isinstance(event_hooks, list):
            continue
        found = False
        for _group, hook in _our_hooks(event_hooks):
            hook["command"] = command
            hook["type"] = "command"
            hook["timeout"] = HOOK_TIMEOUT_SECONDS
            found = True
        if not found:
            event_hooks.append(_entry(command))
    return settings


def strip_settings(existing: dict) -> dict:
    """Return ``existing`` with every FreeMicro hook entry removed.

    Prunes the containers we emptied so uninstalling leaves the file looking
    the way it did before we arrived, rather than littered with ``[]``.
    """
    settings = json.loads(json.dumps(existing))
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return settings
    for event in list(hooks):
        event_hooks = hooks.get(event)
        if not isinstance(event_hooks, list):
            continue
        kept_groups = []
        for group in event_hooks:
            if not isinstance(group, dict):
                kept_groups.append(group)
                continue
            inner = group.get("hooks")
            if isinstance(inner, list):
                group["hooks"] = [
                    h for h in inner
                    if not (isinstance(h, dict) and _is_ours(h.get("command")))
                ]
                if not group["hooks"]:
                    continue  # the group existed only for us
            kept_groups.append(group)
        if kept_groups:
            hooks[event] = kept_groups
        else:
            del hooks[event]
    if not hooks:
        del settings["hooks"]
    return settings


def read_settings(settings_path: Union[str, Path, None] = None) -> Tuple[Path, dict]:
    """Load Claude Code's settings, tolerating a missing or broken file."""
    path = Path(settings_path) if settings_path else default_settings_path()
    if not path.exists():
        return path, {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return path, {}
    return path, data if isinstance(data, dict) else {}


def installed_commands(settings: dict) -> Dict[str, str]:
    """Map of event -> the FreeMicro command currently registered on it."""
    found: Dict[str, str] = {}
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return found
    for event, event_hooks in hooks.items():
        for _group, hook in _our_hooks(event_hooks):
            found[str(event)] = str(hook.get("command", ""))
    return found


def status(settings_path: Union[str, Path, None] = None) -> dict:
    """Everything the CLI needs to say about the hook installation."""
    path, settings = read_settings(settings_path)
    found = installed_commands(settings)
    expected = hook_command()
    missing = [e for e in HOOK_EVENTS if e not in found]
    stale = sorted({c for c in found.values() if c != expected})
    binary = None
    for command in found.values():
        try:
            first = shlex.split(command)[0]
        except (ValueError, IndexError):
            continue
        binary = first
        break
    return {
        "path": str(path),
        "exists": path.exists(),
        "installed": bool(found) and not missing,
        "partial": bool(found) and bool(missing),
        "events": sorted(found),
        "missing_events": missing,
        "commands": found,
        "expected_command": expected,
        "stale_commands": stale,
        "binary_exists": bool(binary) and Path(binary).exists(),
        "binary": binary,
    }


def install_hooks(
    settings_path: Union[str, Path, None] = None,
    dry_run: bool = False,
    command: Optional[str] = None,
):
    """Merge FreeMicro hooks into the settings file.

    Returns the path written, or - for a dry run - the JSON that *would* be
    written.
    """
    path, existing = read_settings(settings_path)
    merged = build_settings(existing, command=command)
    rendered = json.dumps(merged, indent=2)

    if dry_run:
        return rendered

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(rendered + "\n", encoding="utf-8")
    return str(path)


def uninstall_hooks(
    settings_path: Union[str, Path, None] = None, dry_run: bool = False
):
    """Remove every FreeMicro hook entry. Returns ``(path_or_json, removed)``."""
    path, existing = read_settings(settings_path)
    before = len(installed_commands(existing))
    stripped = strip_settings(existing)
    rendered = json.dumps(stripped, indent=2)
    if dry_run:
        return rendered, before
    if before:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered + "\n", encoding="utf-8")
    return str(path), before
