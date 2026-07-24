"""A very small Objective-C bridge, built on ctypes and nothing else.

Why not PyObjC
--------------
FreeMicro's core has **no dependencies**, and it earns real things by that: it
installs on a stock Python 3.9, it runs from a launchd job with no site-packages
to go stale, and nobody has to compile anything to light an LED. PyObjC is a
large binary dependency to add for what is, in the end, one status item and one
menu. The project already speaks to macOS this way - ``device/codex_micro.py``
drives IOKit through ctypes, and ``permissions.py`` calls ``IOHIDCheckAccess``
the same way - so this is the house style rather than a new idea.

What is here is deliberately the smallest useful subset: send a message, make a
string, make a colour, and create one Objective-C class at runtime so menu items
have something to target. No proxies, no bridging magic, no attempt at
generality.

Three ctypes rules this file obeys, because breaking any of them crashes the
process rather than raising:

1. **Every ``objc_msgSend`` call declares its signature.** ``objc_msgSend`` is
   variadic in C; calling it without argtypes passes floats in the wrong
   registers and reads garbage. :func:`msg` builds (and caches) a correctly
   typed function pointer per signature instead of mutating one shared object.
2. **Callback trampolines are kept alive forever.** An ``IMP`` collected while
   the Objective-C runtime still points at it is a hard crash, so every one we
   create is parked in a module-level list.
3. **Nothing raises across the boundary.** Exceptions escaping a callback into
   Objective-C's stack take the interpreter with them, so every trampoline
   swallows everything.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import sys
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

# -- AppKit / Foundation constants we use -----------------------------------

#: ``NSApplicationActivationPolicyAccessory`` - in the menu bar, no Dock icon,
#: no menu bar menus of its own. Exactly what a status item wants.
ACTIVATION_POLICY_ACCESSORY = 2

#: ``NSVariableStatusItemLength`` - size the item to its content.
VARIABLE_STATUS_ITEM_LENGTH = -1.0

#: ``NSControlStateValue``.
STATE_OFF = 0
STATE_ON = 1

#: ``NSEventMaskAny``.
EVENT_MASK_ANY = 0xFFFFFFFFFFFFFFFF

#: ``NSAlertFirstButtonReturn``.
ALERT_FIRST_BUTTON = 1000

_objc: Optional[ctypes.CDLL] = None
_appkit: Optional[ctypes.CDLL] = None
_foundation: Optional[ctypes.CDLL] = None
_unavailable = ""

_msg_send_addr: int = 0
_prototypes: Dict[Tuple[Any, Tuple[Any, ...]], Any] = {}
_selectors: Dict[str, Any] = {}
_classes: Dict[str, Any] = {}

#: See rule 2 in the module docstring. Never cleared.
_imps: List[Any] = []

#: ``void method(id self, SEL _cmd, id argument)`` - the shape of every method
#: we install (menu action, menu delegate).
_IMP_V_AT_ID = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)


class NSRange(ctypes.Structure):
    """Two ``NSUInteger``s, passed by value. Used to colour one character."""

    _fields_ = [("location", ctypes.c_ulong), ("length", ctypes.c_ulong)]


def load() -> bool:
    """Bind the Objective-C runtime. Returns whether this machine can do it."""
    global _objc, _appkit, _foundation, _unavailable, _msg_send_addr
    if _objc is not None:
        return True
    if _unavailable:
        return False
    if sys.platform != "darwin":
        _unavailable = (
            f"the FreeMicro menu bar is a macOS status item; this is "
            f"{sys.platform}"
        )
        return False
    try:
        objc_path = ctypes.util.find_library("objc")
        appkit_path = ctypes.util.find_library("AppKit")
        foundation_path = ctypes.util.find_library("Foundation")
        if not (objc_path and appkit_path and foundation_path):
            raise OSError("could not locate objc/AppKit/Foundation")
        _objc = ctypes.CDLL(objc_path)
        _foundation = ctypes.CDLL(foundation_path)
        _appkit = ctypes.CDLL(appkit_path)
    except (OSError, TypeError) as exc:
        _objc = _appkit = _foundation = None
        _unavailable = f"could not load the Objective-C runtime: {exc}"
        return False

    _objc.objc_getClass.restype = ctypes.c_void_p
    _objc.objc_getClass.argtypes = [ctypes.c_char_p]
    _objc.sel_registerName.restype = ctypes.c_void_p
    _objc.sel_registerName.argtypes = [ctypes.c_char_p]
    _objc.objc_allocateClassPair.restype = ctypes.c_void_p
    _objc.objc_allocateClassPair.argtypes = [
        ctypes.c_void_p, ctypes.c_char_p, ctypes.c_size_t,
    ]
    _objc.objc_registerClassPair.argtypes = [ctypes.c_void_p]
    _objc.class_addMethod.restype = ctypes.c_bool
    _objc.class_addMethod.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_char_p,
    ]
    _msg_send_addr = ctypes.cast(_objc.objc_msgSend, ctypes.c_void_p).value or 0
    if not _msg_send_addr:
        _unavailable = "objc_msgSend is not callable"
        _objc = _appkit = _foundation = None
        return False
    return True


def is_available() -> bool:
    return load()


def unavailable_reason() -> str:
    load()
    return _unavailable


# ---------------------------------------------------------------------------
# Messaging
# ---------------------------------------------------------------------------

class CocoaUnavailable(RuntimeError):
    """Raised when the Objective-C runtime cannot be reached at all."""


def _runtime() -> ctypes.CDLL:
    """The loaded ``libobjc``, loading it on first use.

    Every entry point goes through here so callers never have to remember to
    call :func:`load` first - forgetting would otherwise mean a bare
    ``AssertionError`` from deep inside a message send.
    """
    if _objc is None and not load():
        raise CocoaUnavailable(_unavailable)
    assert _objc is not None
    return _objc


def sel(name: str) -> Any:
    selector = _selectors.get(name)
    if selector is None:
        selector = _runtime().sel_registerName(name.encode("utf-8"))
        _selectors[name] = selector
    return selector


def cls(name: str) -> Any:
    klass = _classes.get(name)
    if klass is None:
        klass = _runtime().objc_getClass(name.encode("utf-8"))
        _classes[name] = klass
    return klass


def msg(
    receiver: Any,
    selector: str,
    *args: Any,
    restype: Any = ctypes.c_void_p,
    argtypes: Sequence[Any] = (),
) -> Any:
    """Send one Objective-C message with an explicitly declared signature.

    ``argtypes`` describes the *method's* arguments only; the receiver and
    selector are added here. Declaring them is not optional - see rule 1 in the
    module docstring.
    """
    _runtime()
    key = (restype, tuple(argtypes))
    prototype = _prototypes.get(key)
    if prototype is None:
        prototype = ctypes.CFUNCTYPE(
            restype, ctypes.c_void_p, ctypes.c_void_p, *argtypes
        )(_msg_send_addr)
        _prototypes[key] = prototype
    return prototype(receiver, sel(selector), *args)


def constant(library: str, name: str) -> Any:
    """Read an exported ``NSString *`` constant (``NSDefaultRunLoopMode``…)."""
    _runtime()
    lib = _foundation if library == "Foundation" else _appkit
    if lib is None:  # pragma: no cover - _runtime() would have raised
        raise CocoaUnavailable(_unavailable)
    return ctypes.c_void_p.in_dll(lib, name)


class Pool:
    """An autorelease pool as a context manager.

    Everything Cocoa hands back from a convenience constructor is autoreleased,
    so any burst of work that happens outside the run loop's own pool - building
    a menu, rendering a title - needs one of these or it leaks quietly.
    """

    def __init__(self) -> None:
        self._pool: Any = None

    def __enter__(self) -> "Pool":
        self._pool = msg(msg(cls("NSAutoreleasePool"), "alloc"), "init")
        return self

    def __exit__(self, *exc: Any) -> None:
        pool, self._pool = self._pool, None
        if pool:
            msg(pool, "drain")


# ---------------------------------------------------------------------------
# Value helpers
# ---------------------------------------------------------------------------

def nsstring(text: str) -> Any:
    """An autoreleased ``NSString`` from a Python string."""
    return msg(
        cls("NSString"),
        "stringWithUTF8String:",
        text.encode("utf-8"),
        argtypes=(ctypes.c_char_p,),
    )


def nscolor(rgb: Optional[Tuple[int, int, int]]) -> Any:
    """An ``NSColor``. ``None`` gives ``secondaryLabelColor``.

    The system colour matters: the idle slate in FreeMicro's palette is nearly
    black, which is invisible on a dark menu bar. Anything the palette does not
    positively want to shout about is drawn in the colour macOS itself uses for
    quiet text, so it stays legible in both appearances.
    """
    if rgb is None:
        return msg(cls("NSColor"), "secondaryLabelColor")
    red, green, blue = (max(0, min(255, int(c))) / 255.0 for c in rgb)
    return msg(
        cls("NSColor"),
        "colorWithSRGBRed:green:blue:alpha:",
        ctypes.c_double(red),
        ctypes.c_double(green),
        ctypes.c_double(blue),
        ctypes.c_double(1.0),
        argtypes=(ctypes.c_double,) * 4,
    )


def attributed(text: str, rgb: Optional[Tuple[int, int, int]], length: int = 0) -> Any:
    """``text`` with a colour applied to its first ``length`` characters.

    ``length=0`` colours the whole string. Returns an autoreleased
    ``NSAttributedString``, or ``None`` if anything went wrong - callers fall
    back to a plain title rather than showing nothing.
    """
    try:
        colour = nscolor(rgb)
        key = constant("AppKit", "NSForegroundColorAttributeName")
        string = msg(
            msg(cls("NSMutableAttributedString"), "alloc"),
            "initWithString:",
            nsstring(text),
            argtypes=(ctypes.c_void_p,),
        )
        if not string:
            return None
        msg(string, "autorelease")
        span = NSRange(0, len(text) if length <= 0 else min(length, len(text)))
        msg(
            string,
            "addAttribute:value:range:",
            key,
            colour,
            span,
            argtypes=(ctypes.c_void_p, ctypes.c_void_p, NSRange),
        )
        return string
    except Exception:  # noqa: BLE001 - a plain title is an acceptable outcome
        return None


def copy_to_clipboard(text: str) -> bool:
    try:
        board = msg(cls("NSPasteboard"), "generalPasteboard")
        msg(board, "clearContents", restype=ctypes.c_long)
        msg(
            board,
            "setString:forType:",
            nsstring(text),
            constant("AppKit", "NSPasteboardTypeString"),
            restype=ctypes.c_bool,
            argtypes=(ctypes.c_void_p, ctypes.c_void_p),
        )
        return True
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# Defining a class at runtime
# ---------------------------------------------------------------------------

def define_class(name: str, methods: Dict[str, Callable[[Any, Any, Any], None]]) -> Any:
    """Create an ``NSObject`` subclass whose selectors call Python functions.

    ``methods`` maps selector names (all of shape ``v@:@``) to callables taking
    ``(self, cmd, argument)``. Every callable is wrapped so nothing can raise
    into Objective-C's stack.
    """
    runtime = _runtime()
    existing = cls(name)
    if existing:
        return existing
    klass = runtime.objc_allocateClassPair(cls("NSObject"), name.encode("utf-8"), 0)
    if not klass:
        return None
    for selector, function in methods.items():
        trampoline = _IMP_V_AT_ID(_guard(function))
        _imps.append(trampoline)
        runtime.class_addMethod(
            klass,
            sel(selector),
            ctypes.cast(trampoline, ctypes.c_void_p),
            b"v@:@",
        )
    runtime.objc_registerClassPair(klass)
    _classes.pop(name, None)
    return klass


def _guard(function: Callable[[Any, Any, Any], None]):
    def wrapper(this: Any, cmd: Any, argument: Any) -> None:
        try:
            function(this, cmd, argument)
        except Exception:  # noqa: BLE001 - see rule 3 in the module docstring
            pass

    return wrapper


__all__ = [
    "ACTIVATION_POLICY_ACCESSORY",
    "ALERT_FIRST_BUTTON",
    "EVENT_MASK_ANY",
    "STATE_OFF",
    "STATE_ON",
    "VARIABLE_STATUS_ITEM_LENGTH",
    "CocoaUnavailable",
    "NSRange",
    "Pool",
    "attributed",
    "cls",
    "constant",
    "copy_to_clipboard",
    "define_class",
    "is_available",
    "load",
    "msg",
    "nscolor",
    "nsstring",
    "sel",
    "unavailable_reason",
]
