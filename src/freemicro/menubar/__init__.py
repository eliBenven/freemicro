"""The macOS menu bar item.

The pad is an *ambient* device: you want its state in the corner of your eye,
not in a window competing with the terminal you are actually working in. So the
visible surface of FreeMicro on macOS is a status item - connection, battery,
agent state and the LED kill switch, all at a glance and one click away.

Layout of this package, smallest dependency first:

``model``    Pure. Snapshot in, menu rows out. No Cocoa, no I/O, no device.
``status``   Gathers a snapshot by *reading* - the state store, IOKit's registry,
             the permission APIs, the daemon's lock. Never fights for the pad.
``checks``   ``Run Doctor`` as structured results instead of printed lines.
``cocoa``    A minimal Objective-C bridge over ctypes. No PyObjC, no rumps.
``app``      The status item, its menu, and the run loop.

Importing this package is cheap and safe everywhere - nothing here loads AppKit
until :func:`main` runs, so ``freemicro`` still imports fine on Linux, in CI, and
inside a launchd job with no GUI session.
"""

from __future__ import annotations

from typing import List, Optional


def main(argv: Optional[List[str]] = None) -> int:
    """Run the menu bar item. Imported lazily so AppKit is never loaded early."""
    from freemicro.menubar.app import main as _main

    return _main(argv)


__all__ = ["main"]
