"""Synthetic keyboard and mouse events via Quartz ``CGEvent``.

``osascript``/System Events was FreeMicro's first delivery path and it has two
hard limits that matter for this pad:

* **It cannot express ``fn``.** macOS handles the fn modifier below the
  synthetic-event API that AppleScript reaches, so an ``fn``-based dictation
  shortcut is unreachable through it.
* **It cannot hold a key down.** ``keystroke`` is press-and-release. The pad
  reports both press *and* release (``v.oai.hid`` ``act`` 1/0), so real
  hold-to-talk is available to us - but only with an API that separates the two.

CGEvent fixes both, and skips spawning a subprocess per keystroke, which is the
difference between a pad that feels instant and one that feels laggy.

Everything is loaded lazily through ctypes and guarded, so importing this module
on Linux - or on a Mac without the framework - is harmless. Callers check
:func:`is_available` first.

⚠️ **UNVERIFIED:** whether a *synthetic* ``fn`` flag actually triggers
third-party dictation apps. Some listen below the event tap. Treat ``fn``
bindings as experimental until someone confirms them on hardware.
"""

from __future__ import annotations

import atexit
import ctypes
import ctypes.util
import os
import signal
import sys
import threading
from typing import Any, List, Optional, Tuple

# Event flags (CGEventFlags).
FLAG_SHIFT = 0x00020000
FLAG_CONTROL = 0x00040000
FLAG_OPTION = 0x00080000
FLAG_COMMAND = 0x00100000
FLAG_SECONDARY_FN = 0x00800000

MODIFIER_FLAGS = {
    "shift": FLAG_SHIFT,
    "control": FLAG_CONTROL,
    "option": FLAG_OPTION,
    "command": FLAG_COMMAND,
    "fn": FLAG_SECONDARY_FN,
}

# CGEventType
_MOUSE_MOVED = 5
_MOUSE_EVENTS = {
    "left": (1, 2, 0),    # down, up, button
    "right": (3, 4, 1),
    "middle": (25, 26, 2),
}

# CGEventField ids for the motion an event represents, as distinct from the
# position it lands on. See mouse_move.
_MOUSE_DELTA_X = 96  # kCGMouseEventDeltaX
_MOUSE_DELTA_Y = 97  # kCGMouseEventDeltaY

_TAP_HID = 0  # kCGHIDEventTap
_SOURCE_HID_SYSTEM = 1  # kCGEventSourceStateHIDSystemState

_cg: Optional[ctypes.CDLL] = None
_cf: Optional[ctypes.CDLL] = None
_UNAVAILABLE = "not macOS"


class CGPoint(ctypes.Structure):
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]


if sys.platform == "darwin":
    try:
        _path = (
            ctypes.util.find_library("ApplicationServices")
            or ctypes.util.find_library("CoreGraphics")
        )
        _cg = ctypes.CDLL(_path) if _path else None
        _cf = ctypes.CDLL(ctypes.util.find_library("CoreFoundation"))
        if _cg is None:
            _UNAVAILABLE = "CoreGraphics not found"
    except (OSError, TypeError) as exc:  # pragma: no cover - macOS only
        _cg = _cf = None
        _UNAVAILABLE = f"could not load CoreGraphics: {exc}"
else:
    _UNAVAILABLE = f"CGEvent needs macOS; this is {sys.platform}"


def _configure() -> None:
    assert _cg is not None and _cf is not None
    _cg.CGEventSourceCreate.restype = ctypes.c_void_p
    _cg.CGEventSourceCreate.argtypes = [ctypes.c_uint32]
    _cg.CGEventCreateKeyboardEvent.restype = ctypes.c_void_p
    _cg.CGEventCreateKeyboardEvent.argtypes = [
        ctypes.c_void_p, ctypes.c_uint16, ctypes.c_bool,
    ]
    _cg.CGEventSetFlags.argtypes = [ctypes.c_void_p, ctypes.c_uint64]
    _cg.CGEventKeyboardSetUnicodeString.argtypes = [
        ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(ctypes.c_uint16),
    ]
    _cg.CGEventPost.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
    _cg.CGEventCreate.restype = ctypes.c_void_p
    _cg.CGEventCreate.argtypes = [ctypes.c_void_p]
    _cg.CGEventGetLocation.restype = CGPoint
    _cg.CGEventGetLocation.argtypes = [ctypes.c_void_p]
    _cg.CGEventCreateMouseEvent.restype = ctypes.c_void_p
    _cg.CGEventCreateMouseEvent.argtypes = [
        ctypes.c_void_p, ctypes.c_uint32, CGPoint, ctypes.c_uint32,
    ]
    _cg.CGEventSetIntegerValueField.argtypes = [
        ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int64,
    ]
    _cf.CFRelease.argtypes = [ctypes.c_void_p]


if _cg is not None and _cf is not None:
    try:
        _configure()
    except AttributeError as exc:  # pragma: no cover - unexpected framework
        _cg = _cf = None
        _UNAVAILABLE = f"CoreGraphics is missing a symbol: {exc}"


def is_available() -> bool:
    """Whether CGEvent delivery can be used on this machine."""
    return _cg is not None and _cf is not None


def unavailable_reason() -> str:
    return _UNAVAILABLE


def _source() -> Any:
    assert _cg is not None
    return _cg.CGEventSourceCreate(_SOURCE_HID_SYSTEM)


def _post(event: Any) -> None:
    assert _cg is not None and _cf is not None
    _cg.CGEventPost(_TAP_HID, event)
    _cf.CFRelease(event)


#: Virtual keycodes for the modifier keys themselves.
#:
#: Setting a modifier *flag* on a key event tells an app "this keystroke was
#: modified". It does **not** tell it "Command is currently held down" - that
#: comes from the modifier key's own events. Apps that watch for a held chord
#: (push-to-talk being the obvious one) look at the latter, so a hold built
#: only from flags never engages them.
MODIFIER_KEY_CODES = {
    "command": 55,
    "shift": 56,
    "option": 58,
    "control": 59,
    "fn": 63,
}


# ---------------------------------------------------------------------------
# Held keys, and getting them back up no matter how we die
# ---------------------------------------------------------------------------
#
# A `hold` binding presses *real* modifier keys and leaves them down until the
# pad reports the release. That release can be lost - Ctrl-C, a Bluetooth drop
# mid-hold, a config reload that rebinds the key, a self re-exec - and when it
# is, macOS is left believing Ctrl and Cmd are physically held. Every app in
# the session then misbehaves, and quitting FreeMicro does *not* fix it,
# because nothing sends the key-ups: recovery needs another app or a logout.
#
# So the process that pressed them has to be the one that guarantees the
# key-ups, on every exit path there is. That means remembering what is down.

_held_lock = threading.RLock()
#: Keycodes currently pressed by :func:`hold_chord`, in press order.
_held: List[int] = []
_guard_installed = False


def held_keys() -> Tuple[int, ...]:
    """Which keycodes we are currently holding down. For diagnostics."""
    with _held_lock:
        return tuple(_held)


def release_all() -> int:
    """Send a key-up for everything we are still holding. Returns how many.

    Idempotent and unconditionally safe: releasing a key that is already up is
    a no-op to the OS, and the registry is cleared first so a second call does
    nothing. **This must never raise** - it runs from ``atexit`` and from a
    signal handler, where an exception would be worse than the stuck key.
    """
    with _held_lock:
        codes = list(_held)
        del _held[:]
    if not codes or not is_available():
        return len(codes)
    for code in reversed(codes):  # mirror the press order
        try:
            key_event(code, False, 0)
        except Exception:  # noqa: BLE001 - a stuck key is the worse outcome
            pass
    return len(codes)


def _release_and_chain(signum: int, previous: Any) -> None:
    """Get the keys up, then let the signal do what it was going to do."""
    release_all()
    if callable(previous):
        previous(signum, None)
        return
    if previous == signal.SIG_IGN:
        return
    # SIG_DFL: restore it and re-raise, so the process still dies the way the
    # sender asked. Swallowing a SIGTERM to save a keystroke would be worse.
    try:
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)
    except (OSError, ValueError):  # pragma: no cover - defensive
        pass


def install_release_guard() -> None:
    """Arm the safety net. Called the first time a key is actually held.

    Deliberately lazy: importing this module must not touch the process's
    signal handlers, and a run that never uses a ``hold`` binding has nothing
    to protect. ``atexit`` alone is **not** enough - Python does not unwind it
    on a default ``SIGTERM``, and ``os.execv`` does not run it at all - which
    is why there is a signal handler here *and* a ``Bridge.close()`` on the
    re-exec path.
    """
    global _guard_installed
    with _held_lock:
        if _guard_installed:
            return
        _guard_installed = True
    atexit.register(release_all)
    for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        try:
            previous = signal.getsignal(signum)
            signal.signal(
                signum,
                lambda num, frame, _prev=previous: _release_and_chain(num, _prev),
            )
        except (ValueError, OSError, AttributeError):
            # signal.signal only works on the main thread, and SIGHUP does not
            # exist everywhere. Losing the handler is survivable; the atexit
            # hook and Bridge.close() still cover the ordinary paths.
            pass


def hold_chord(keycode: int, modifiers, down: bool) -> None:
    """Press or release a whole chord the way a human hand would.

    Modifiers go down *before* the key and come up *after* it, as real key
    events rather than flags alone. This is what makes hold-to-talk work: an
    app asking "is Ctrl+Cmd held right now?" is asking about the modifier
    keys, and flags on a single ``o`` event do not answer that question.

    Every key pressed here is registered before it is posted, so that a
    failure halfway through a chord still leaves the rest recoverable by
    :func:`release_all`.
    """
    flags = 0
    codes = []
    for name in modifiers:
        code = MODIFIER_KEY_CODES.get(name)
        if code is not None:
            codes.append(code)
        flag = MODIFIER_FLAGS.get(name)
        if flag:
            flags |= flag

    if down:
        install_release_guard()
        with _held_lock:
            _held.extend(codes)
            _held.append(keycode)
        for code in codes:                      # modifiers first…
            key_event(code, True, flags)
        key_event(keycode, True, flags)         # …then the key
    else:
        key_event(keycode, False, flags)        # key first…
        for code in reversed(codes):            # …then modifiers, in reverse
            key_event(code, False, 0)
        with _held_lock:
            for code in codes + [keycode]:
                if code in _held:
                    _held.remove(code)


def key_event(keycode: int, down: bool, flags: int = 0) -> None:
    """Post one key-down or key-up, with modifier ``flags`` applied."""
    assert _cg is not None and _cf is not None
    source = _source()
    event = _cg.CGEventCreateKeyboardEvent(source, ctypes.c_uint16(keycode), down)
    if not event:
        if source:
            _cf.CFRelease(source)
        raise OSError("CGEventCreateKeyboardEvent failed")
    if flags:
        _cg.CGEventSetFlags(event, ctypes.c_uint64(flags))
    _post(event)
    if source:
        _cf.CFRelease(source)


def tap_key(keycode: int, flags: int = 0) -> None:
    """Press and release one key."""
    key_event(keycode, True, flags)
    key_event(keycode, False, flags)


def type_text(text: str) -> None:
    """Type a string as literal Unicode.

    Uses ``CGEventKeyboardSetUnicodeString`` rather than per-character keycodes,
    so it is layout independent and handles anything the user puts in a binding.
    """
    assert _cg is not None and _cf is not None
    source = _source()
    try:
        for chunk in (text[i:i + 16] for i in range(0, len(text), 16)) or [""]:
            if not chunk:
                continue
            units = chunk.encode("utf-16-le")
            count = len(units) // 2
            buffer = (ctypes.c_uint16 * count).from_buffer_copy(units)
            for down in (True, False):
                event = _cg.CGEventCreateKeyboardEvent(source, 0, down)
                if not event:
                    raise OSError("CGEventCreateKeyboardEvent failed")
                _cg.CGEventKeyboardSetUnicodeString(event, count, buffer)
                _post(event)
    finally:
        if source:
            _cf.CFRelease(source)


def cursor_position() -> Tuple[float, float]:
    """Where the mouse pointer is right now."""
    assert _cg is not None and _cf is not None
    event = _cg.CGEventCreate(None)
    try:
        point = _cg.CGEventGetLocation(event)
        return (point.x, point.y)
    finally:
        if event:
            _cf.CFRelease(event)


def mouse_move(x: float, y: float, relative: bool = False) -> None:
    """Move the pointer, absolutely or by an offset.

    A relative move also carries its own delta on the event. CGEvent's
    "mouse moved" is fundamentally an absolute position, and an app that reads
    motion rather than position - anything with a crosshair, a 3D viewport, or
    pointer-lock - sees nothing at all unless ``kCGMouseEventDeltaX/Y`` are set.
    Filling them in costs two field writes and is simply what the move means.
    """
    assert _cg is not None and _cf is not None
    dx, dy = x, y
    if relative:
        current = cursor_position()
        x, y = current[0] + x, current[1] + y
    source = _source()
    event = _cg.CGEventCreateMouseEvent(
        source, _MOUSE_MOVED, CGPoint(x, y), 0
    )
    if not event:
        if source:
            _cf.CFRelease(source)
        raise OSError("CGEventCreateMouseEvent failed")
    if relative:
        _cg.CGEventSetIntegerValueField(event, _MOUSE_DELTA_X, int(dx))
        _cg.CGEventSetIntegerValueField(event, _MOUSE_DELTA_Y, int(dy))
    _post(event)
    if source:
        _cf.CFRelease(source)


def mouse_click(button: str = "left", count: int = 1) -> None:
    """Click at the pointer's current position."""
    assert _cg is not None and _cf is not None
    if button not in _MOUSE_EVENTS:
        raise ValueError(
            f"unknown mouse button {button!r}; expected "
            f"{', '.join(sorted(_MOUSE_EVENTS))}"
        )
    down_type, up_type, index = _MOUSE_EVENTS[button]
    x, y = cursor_position()
    source = _source()
    try:
        for click in range(1, max(1, int(count)) + 1):
            for event_type in (down_type, up_type):
                event = _cg.CGEventCreateMouseEvent(
                    source, event_type, CGPoint(x, y), index
                )
                if not event:
                    raise OSError("CGEventCreateMouseEvent failed")
                # kCGMouseEventClickState = 1: lets the OS see a double click.
                _cg.CGEventSetIntegerValueField(event, 1, click)
                _post(event)
    finally:
        if source:
            _cf.CFRelease(source)


__all__ = [
    "FLAG_COMMAND",
    "FLAG_CONTROL",
    "FLAG_OPTION",
    "FLAG_SECONDARY_FN",
    "FLAG_SHIFT",
    "MODIFIER_FLAGS",
    "cursor_position",
    "held_keys",
    "hold_chord",
    "install_release_guard",
    "is_available",
    "key_event",
    "mouse_click",
    "mouse_move",
    "release_all",
    "tap_key",
    "type_text",
    "unavailable_reason",
]
