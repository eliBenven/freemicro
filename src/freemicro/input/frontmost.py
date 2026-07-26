"""Cheap, cached lookup of the frontmost macOS app, for per-app pad profiles.

Per-app profiles (see :mod:`freemicro.padconfig`) let one pad key mean different
things in different applications, which means resolving a press has to know what
is frontmost. The hard constraint is that this must add **no** perceptible
latency to a key press: the OS must never be queried synchronously on the key
path.

Two pieces keep that promise:

* :func:`detect_frontmost` is the actual, in-process lookup. It reads
  ``NSWorkspace.sharedWorkspace.frontmostApplication.localizedName`` straight
  through the Objective-C runtime with :mod:`ctypes` - no PyObjC, no subprocess,
  no dependency on anything outside the standard library, exactly like the rest
  of FreeMicro's core. It never raises: every failure path (not on macOS, the
  runtime not loadable, nothing frontmost) degrades to ``None``, which the
  resolver reads as "no profile, use the base bindings".

* :class:`FrontmostWatcher` is what the run loop actually calls. It caches the
  last name and only lets the OS be re-read once every ``interval`` seconds, so
  the run loop can poll it every tick without turning a human-paced app switch
  into a stream of lookups. The bridge reads the *cached* value on the key path;
  the watcher does the OS work on the tick. That split is the whole point.

The provider is injectable so the entire test suite runs without touching the
real OS - the same discipline the backends and clocks use everywhere else.
"""

from __future__ import annotations

import sys
import time
from typing import Callable, Optional

#: Monotonic, never the wall clock: a throttle window must survive an NTP step.
_monotonic = time.monotonic


def _detect_via_appkit() -> Optional[str]:
    """Ask AppKit for the frontmost app's name through the objc runtime.

    Returns the localised name, or ``None`` if anything at all is not exactly
    right. Guarded to the last line: this runs inside a live key bridge and a
    background daemon, so a wrong ``ctypes`` signature must degrade to "no
    profile", never take the process down.
    """
    import ctypes
    import ctypes.util

    objc_path = ctypes.util.find_library("objc")
    if not objc_path:
        return None
    objc = ctypes.CDLL(objc_path)
    # Loading AppKit is what makes the NSWorkspace class exist in this process.
    appkit_path = ctypes.util.find_library("AppKit")
    if not appkit_path:
        return None
    ctypes.CDLL(appkit_path)

    objc.objc_getClass.restype = ctypes.c_void_p
    objc.objc_getClass.argtypes = [ctypes.c_char_p]
    objc.sel_registerName.restype = ctypes.c_void_p
    objc.sel_registerName.argtypes = [ctypes.c_char_p]

    def msg(receiver: Optional[int], selector: bytes, restype=ctypes.c_void_p):
        """One objc_msgSend call with an explicit result type.

        A fresh prototype per call is deliberate: ``objc_msgSend`` is variadic,
        so ctypes needs the exact signature every time, and reusing one function
        object with a mutated ``restype`` is how you get a silent misread.
        """
        if not receiver:
            return None
        send = ctypes.CDLL(objc_path)["objc_msgSend"]
        send.restype = restype
        send.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        return send(ctypes.c_void_p(receiver), objc.sel_registerName(selector))

    workspace_cls = objc.objc_getClass(b"NSWorkspace")
    if not workspace_cls:
        return None
    shared = msg(workspace_cls, b"sharedWorkspace")
    app = msg(shared, b"frontmostApplication")
    name_obj = msg(app, b"localizedName")
    utf8 = msg(name_obj, b"UTF8String", restype=ctypes.c_char_p)
    if not utf8:
        return None
    try:
        return utf8.decode("utf-8") or None
    except (UnicodeDecodeError, AttributeError):
        return None


def detect_frontmost() -> Optional[str]:
    """The frontmost app's localised name, or ``None`` if it cannot be read.

    macOS only, and best-effort by design: on any other platform, or if the
    Objective-C runtime cannot be reached, this returns ``None`` and the caller
    treats that as "no profile is active". It never raises.
    """
    if sys.platform != "darwin":
        return None
    try:
        return _detect_via_appkit()
    except Exception:  # noqa: BLE001 - a lookup must never break the key bridge
        return None


class FrontmostWatcher:
    """A throttled, cached view of the frontmost app for the run loop.

    :meth:`poll` is what the run loop calls on its tick. It re-reads the OS at
    most once every ``interval`` seconds and otherwise hands back the cached
    name, so polling every tick costs at most one lookup per window. The bridge
    never calls this on the key path - it reads the name the last poll cached.

    Both the clock and the provider are injectable, so tests drive the exact
    timing from data and never touch the real OS.
    """

    def __init__(
        self,
        provider: Optional[Callable[[], Optional[str]]] = None,
        *,
        clock: Optional[Callable[[], float]] = None,
        interval: float = 0.3,
    ) -> None:
        self.provider = provider or detect_frontmost
        self.clock = clock or _monotonic
        #: The throttle window in seconds. See
        #: :data:`freemicro.padconfig.DEFAULT_PROFILE_POLL_MS`.
        self.interval = max(0.0, interval)
        self._name: Optional[str] = None
        self._last_at: Optional[float] = None

    @property
    def current(self) -> Optional[str]:
        """The last cached name, without touching the OS."""
        return self._name

    def poll(self) -> Optional[str]:
        """Return the frontmost app name, re-reading the OS at most once/window.

        A provider that raises is swallowed and read as ``None`` - the same
        "no profile" degradation :func:`detect_frontmost` already promises, kept
        here too so an *injected* provider cannot break the loop either.
        """
        now = self.clock()
        if self._last_at is not None and now - self._last_at < self.interval:
            return self._name
        self._last_at = now
        try:
            name = self.provider()
        except Exception:  # noqa: BLE001 - see the class docstring
            name = None
        self._name = name or None
        return self._name


__all__ = ["FrontmostWatcher", "detect_frontmost"]
