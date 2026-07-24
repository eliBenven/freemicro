"""The two macOS permissions FreeMicro needs, checked without side effects.

macOS gates the two things this project does in two different places, and both
fail *silently*: without Input Monitoring the pad simply cannot be opened, and
without Accessibility synthetic keystrokes are dropped with no error anywhere.
Guessing from a failure downstream ("the pad didn't open - maybe permission,
maybe it's unplugged") is exactly the ambiguity that makes this hard to set up,
so we ask the OS directly.

* **Input Monitoring** - ``IOHIDCheckAccess(kIOHIDRequestTypeListenEvent)``.
  Read-only, works with no pad attached, and never shows a prompt.
* **Accessibility** - ``AXIsProcessTrusted()``. Also read-only and prompt-free
  (``AXIsProcessTrustedWithOptions`` is the one that prompts; we never call it).

Both answers describe **the process asking** - i.e. your terminal app, since
that is what the grant is attached to. That is why every fix here ends with
"restart the terminal": macOS only re-reads a grant when the app launches.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import subprocess
import sys
from typing import Optional, Tuple

#: Deep links into the exact System Settings panes. ``open`` these.
PANE_INPUT_MONITORING = (
    "x-apple.systempreferences:com.apple.preference.security?Privacy_ListenEvent"
)
PANE_ACCESSIBILITY = (
    "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"
)

#: ``IOHIDAccessType`` values.
_ACCESS_GRANTED = 0
_ACCESS_DENIED = 1
_ACCESS_UNKNOWN = 2

#: ``IOHIDRequestType.kIOHIDRequestTypeListenEvent`` - "may I read input?"
_REQUEST_LISTEN_EVENT = 1

_UNKNOWN_NOTE = (
    "macOS has not been asked yet - the grant appears once something tries "
    "to open the pad"
)


def is_macos() -> bool:
    return sys.platform == "darwin"


def _iokit() -> Optional[ctypes.CDLL]:
    if not is_macos():
        return None
    try:
        path = ctypes.util.find_library("IOKit")
        return ctypes.CDLL(path) if path else None
    except OSError:
        return None


def input_monitoring() -> Tuple[Optional[bool], str]:
    """``(granted, detail)`` for Input Monitoring.

    ``granted`` is ``None`` when macOS genuinely does not know yet - a state
    worth distinguishing, because it means "nothing has tried to open the pad
    on this machine", not "you were denied".
    """
    lib = _iokit()
    if lib is None:
        return None, "IOKit unavailable (pad support is macOS-only)"
    try:
        lib.IOHIDCheckAccess.restype = ctypes.c_uint32
        lib.IOHIDCheckAccess.argtypes = [ctypes.c_uint32]
        result = int(lib.IOHIDCheckAccess(_REQUEST_LISTEN_EVENT))
    except (AttributeError, OSError) as exc:
        # Pre-10.15, or a stripped IOKit. Fall back to "we can't tell".
        return None, f"IOHIDCheckAccess unavailable: {exc}"
    if result == _ACCESS_GRANTED:
        return True, "granted"
    if result == _ACCESS_DENIED:
        return False, "denied"
    return None, _UNKNOWN_NOTE


def accessibility() -> Tuple[bool, str]:
    """``(granted, detail)`` for Accessibility, via ``AXIsProcessTrusted()``.

    This never prompts. ``AXIsProcessTrustedWithOptions`` would, which is why
    it is deliberately not used: a setup tool that fires an OS modal the user
    did not ask for is worse than one that prints a path.
    """
    if not is_macos():
        return False, "Accessibility is a macOS concept"
    try:
        path = ctypes.util.find_library("ApplicationServices")
        lib = ctypes.CDLL(path) if path else None
        if lib is None:
            raise OSError("ApplicationServices not found")
        lib.AXIsProcessTrusted.restype = ctypes.c_bool
        lib.AXIsProcessTrusted.argtypes = []
        granted = bool(lib.AXIsProcessTrusted())
    except (AttributeError, OSError) as exc:
        return False, f"could not query Accessibility: {exc}"
    return granted, "granted" if granted else "not granted"


def accessibility_roundtrip() -> Tuple[bool, str]:
    """Confirm Accessibility by actually using it (slow; ``doctor`` only).

    Asking System Events for the frontmost process needs the same grant that
    typing does, but changes nothing. :func:`accessibility` is the cheap
    answer; this is the one that proves osascript itself works.
    """
    script = (
        'tell application "System Events" to return name of '
        "first application process whose frontmost is true"
    )
    try:
        proc = subprocess.run(
            ["osascript", "-e", script], capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    if proc.returncode == 0:
        return True, (proc.stdout or "").strip()
    tail = (proc.stderr or "").strip().splitlines()
    return False, tail[-1] if tail else "osascript failed"


def host_app() -> str:
    """Best-effort name of the app the grant has to be given to.

    macOS attaches both permissions to the *application* that owns the
    process, not to ``freemicro``, so telling someone to "add FreeMicro" sends
    them looking for a checkbox that will never exist.
    """
    name = os.environ.get("__CFBundleIdentifier", "")
    friendly = {
        "com.apple.Terminal": "Terminal",
        "com.googlecode.iterm2": "iTerm",
        "com.mitchellh.ghostty": "Ghostty",
        "dev.warp.Warp-Stable": "Warp",
        "net.kovidgoyal.kitty": "kitty",
        "com.microsoft.VSCode": "Visual Studio Code",
        "com.todesktop.230313mzl4w4u92": "Cursor",
    }
    if name in friendly:
        return friendly[name]
    if os.environ.get("TERM_PROGRAM"):
        return str(os.environ["TERM_PROGRAM"])
    if name:
        return name
    return "your terminal app"


def open_pane(url: str) -> bool:
    """Open a System Settings pane. Returns whether ``open`` was launched."""
    if not is_macos():
        return False
    try:
        proc = subprocess.run(
            ["open", url], capture_output=True, text=True, timeout=15
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def chatgpt_running() -> bool:
    """Is the vendor app up? It drives the same LEDs on the same channel."""
    try:
        proc = subprocess.run(
            ["pgrep", "-x", "ChatGPT"], capture_output=True, text=True, timeout=5
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def fix_text(permission: str) -> str:
    """The exact words to fix one permission, ready to print."""
    app = host_app()
    if permission == "input_monitoring":
        where = "Input Monitoring"
        why = "Without it the pad cannot be opened at all - keys or LEDs."
    else:
        where = "Accessibility"
        why = "Without it macOS throws your keystrokes away silently."
    return (
        f"System Settings → Privacy & Security → {where}\n"
        f"Add {app} (the app you run freemicro from), switch it on,\n"
        f"then QUIT AND REOPEN {app} - macOS only re-reads the grant at\n"
        f"launch. {why}"
    )


__all__ = [
    "PANE_ACCESSIBILITY",
    "PANE_INPUT_MONITORING",
    "accessibility",
    "accessibility_roundtrip",
    "chatgpt_running",
    "fix_text",
    "host_app",
    "input_monitoring",
    "is_macos",
    "open_pane",
]
