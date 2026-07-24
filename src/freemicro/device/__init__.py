"""Talking to the physical pad.

This package owns the *transport* - opening the Codex Micro's vendor HID
collection, framing JSON-RPC messages onto 63-byte reports, and pumping the
macOS run loop. It deliberately sits outside ``input/`` and ``renderers/``
because the same channel carries **both** directions: key events come up it and
LED commands go down it. See ``docs/PROTOCOL.md`` for the wire facts.

Two constraints shape everything here:

* **One owner at a time.** Only one process can usefully hold the device, so a
  single cached handle is opened on first use and closed once. Any future
  daemon/IPC layer inherits that model rather than fighting it.
* **Disconnects are normal, not exceptional.** The pad drops on sleep, on
  battery, on a nudged cable. :func:`run_with_reconnect` treats that as the
  expected case instead of an error to exit on.
"""

from __future__ import annotations

import os
import time
from typing import Any, Callable, Dict, Optional

from freemicro.device.codex_micro import (
    TRANSPORT_NOTE,
    PRODUCT_ID,
    REPORT_ID,
    TRANSPORT_BLE,
    TRANSPORT_USB,
    VENDOR_ID,
    Device,
    DeviceError,
    FrameDecoder,
    frame_message,
    is_supported,
    open_device,
    unsupported_reason,
)
from freemicro.device.codex_micro import device_transport as _device_transport

#: Set this to any non-empty value to make FreeMicro behave as if no pad were
#: attached. The test suite sets it so a developer's real pad is never driven by
#: a test run, and it is a handy way to exercise the screen fallback by hand.
ENV_NO_DEVICE = "FREEMICRO_NO_DEVICE"

_shared: Optional[Device] = None
_shared_tried = False


def shared_device() -> Optional[Device]:
    """The one open handle this process uses for the pad.

    macOS will happily hand out several handles to the same device, but every
    extra one is another thing to keep in sync and another way to leave the LEDs
    lit after a crash. ``freemicro run`` drives keys *and* lights at once, so a
    single cached handle - opened on first use, closed once at exit - is both
    simpler and safer. A missing pad caches as ``None`` so we don't re-probe on
    every render tick.
    """
    global _shared, _shared_tried
    if os.environ.get(ENV_NO_DEVICE):
        return None
    if _shared is None and not _shared_tried:
        _shared_tried = True
        _shared = open_device()
    return _shared


def device_transport() -> Optional[str]:
    """Transport of the attached pad, honouring :data:`ENV_NO_DEVICE`."""
    if os.environ.get(ENV_NO_DEVICE):
        return None
    return _device_transport()


def device_present() -> bool:
    """True if a pad is on the bus - and honours :data:`ENV_NO_DEVICE`."""
    return device_transport() is not None


def close_shared() -> None:
    """Close the shared handle, if one was ever opened."""
    global _shared, _shared_tried
    if _shared is not None:
        _shared.close()
    _shared = None
    _shared_tried = False


def run_with_reconnect(
    on_event: Callable[[Dict[str, Any]], None],
    on_tick: Optional[Callable[[], None]] = None,
    on_connect: Optional[Callable[[Device], None]] = None,
    on_disconnect: Optional[Callable[[], None]] = None,
    tick_interval: float = 0.25,
    seconds: float = 0.0,
    retry_interval: float = 2.0,
) -> None:
    """Stream events forever, surviving the pad coming and going.

    The Codex Micro disconnects constantly - sleep, range, a nudged cable, a
    flat battery - so a long-running command that exits on the first drop is
    unusable in practice. This loop reopens instead, and calls ``on_connect`` /
    ``on_disconnect`` so the caller can report the transition and re-light.

    Presence is re-checked on every tick because IOKit will happily keep pumping
    a run loop for a device that is no longer there.
    """
    deadline = None if seconds <= 0 else time.time() + seconds
    connected = False
    while deadline is None or time.time() < deadline:
        device = shared_device()
        if device is None:
            if connected:
                connected = False
                if on_disconnect is not None:
                    on_disconnect()
            close_shared()
            # Keep ticking while we wait: whatever the caller renders to
            # instead of the pad must stay alive and responsive.
            waited = 0.0
            while waited < retry_interval:
                if on_tick is not None:
                    on_tick()
                time.sleep(tick_interval)
                waited += tick_interval
                if deadline is not None and time.time() >= deadline:
                    return
            continue

        connected = True
        if on_connect is not None:
            on_connect(device)

        last_check = [time.time()]

        def _tick() -> None:
            if on_tick is not None:
                on_tick()
            now = time.time()
            if now - last_check[0] >= 1.0:
                last_check[0] = now
                if not device_present():
                    device.request_stop()
            if deadline is not None and now >= deadline:
                device.request_stop()

        try:
            device.stream(
                on_event, seconds=0.0, on_tick=_tick, tick_interval=tick_interval
            )
        except DeviceError:
            pass
        # We only get here if the stream stopped: the pad went away, or we hit
        # the deadline. Drop the handle so the next pass opens a fresh one.
        close_shared()
        if deadline is None or time.time() < deadline:
            if on_disconnect is not None:
                on_disconnect()
            connected = False
            time.sleep(retry_interval)


__all__ = [
    "TRANSPORT_NOTE",
    "ENV_NO_DEVICE",
    "Device",
    "DeviceError",
    "FrameDecoder",
    "PRODUCT_ID",
    "REPORT_ID",
    "TRANSPORT_BLE",
    "TRANSPORT_USB",
    "VENDOR_ID",
    "close_shared",
    "device_present",
    "device_transport",
    "frame_message",
    "is_supported",
    "open_device",
    "run_with_reconnect",
    "shared_device",
    "unsupported_reason",
]
