"""Install FreeMicro's hook into Claude Code's ``settings.json``.

We register a single command — ``freemicro hook`` — on the lifecycle events we
care about. Each hook invocation reads the event JSON from stdin, classifies
it, and updates the per-session state store. The renderer loop (``freemicro
watch``) picks the change up on its next poll.

The installer is conservative: it merges into existing settings, never removes
hooks it didn't add, and is idempotent (running it twice is a no-op).
"""

from __future__ import annotations

import json
from pathlib import Path

# Events we hook and the fact that our single handler figures out the state
# from the payload means we can register the same command everywhere.
HOOK_EVENTS = (
    "UserPromptSubmit",
    "PreToolUse",
    "PostToolUse",
    "Notification",
    "Stop",
    "SessionEnd",
)

HOOK_COMMAND = "freemicro hook"
_MARKER = "freemicro"  # how we recognize our own entries for idempotency.


def default_settings_path() -> Path:
    return Path.home() / ".claude" / "settings.json"


def _entry() -> dict:
    return {
        "hooks": [
            {"type": "command", "command": HOOK_COMMAND}
        ]
    }


def _already_installed(hook_list: list) -> bool:
    for group in hook_list:
        for hook in group.get("hooks", []):
            if _MARKER in str(hook.get("command", "")):
                return True
    return False


def build_settings(existing: dict) -> dict:
    """Return ``existing`` with FreeMicro hooks merged in (pure function)."""
    settings = json.loads(json.dumps(existing))  # deep copy
    hooks = settings.setdefault("hooks", {})
    for event in HOOK_EVENTS:
        event_hooks = hooks.setdefault(event, [])
        if not _already_installed(event_hooks):
            event_hooks.append(_entry())
    return settings


def install_hooks(settings_path: str | Path | None = None, dry_run: bool = False):
    """Merge FreeMicro hooks into the settings file. Returns the path or, for
    a dry run, the JSON that *would* be written."""
    path = Path(settings_path) if settings_path else default_settings_path()

    existing: dict = {}
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            existing = {}

    merged = build_settings(existing)
    rendered = json.dumps(merged, indent=2)

    if dry_run:
        return rendered

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(rendered + "\n", encoding="utf-8")
    return str(path)
