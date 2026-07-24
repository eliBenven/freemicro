"""Read and write the Codex Micro's vendor HID channel.

The pad's Agent/action keys do **not** emit ordinary keyboard scancodes, and its
LEDs are not standard HID lighting. Both live on a private vendor collection
(usage page ``0xFF00``, Report ID 6) that speaks JSON-RPC. That is *why* the pad
looks inert without a host app listening - and why owning this one channel is
enough for FreeMicro to own the whole device. The wire facts are written up in
``docs/PROTOCOL.md``; this module is an independent implementation of them.

Two things here are worth knowing before you edit:

**macOS needs IOKit, not hidapi.** ``hidapi``'s ``open_path()`` always fails on
this device: hidapi models each HID top-level collection as its own openable
path, but macOS vends a *single* ``IOHIDDevice`` whose primary usage is the
keyboard collection and which merely *contains* ``0xFF00``. So we go straight to
``IOServiceGetMatchingServices`` → ``IOHIDDeviceCreate`` → ``IOHIDDeviceOpen``.
That works from plain userland once the terminal has **Input Monitoring**.

**Framing is separate from transport.** :func:`frame_message` and
:class:`FrameDecoder` are pure functions over bytes, so the protocol can be
tested exhaustively on any platform with no hardware attached.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import json
import sys
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

VENDOR_ID = 0x303A
PRODUCT_ID = 0x8360

#: The vendor collection's report id (input *and* output).
REPORT_ID = 6

#: Output report size per transport. USB takes the payload bare; Bluetooth
#: expects the report id repeated as the first byte of the buffer.
REPORT_BYTES = 63
REPORT_BYTES_BLE = 64
OPCODE_DATA = 0x02

#: Data bytes per report. Identical either way: BLE spends its extra byte on
#: the report-id prefix.
MAX_CHUNK = REPORT_BYTES - 2

#: Device -> host notification methods.
EVENT_KEY = "v.oai.hid"
EVENT_JOYSTICK = "v.oai.rad"

_REPORT_TYPE_OUTPUT = 1  # kIOHIDReportTypeOutput

#: Values of the IOHIDDevice ``Transport`` property we care about.
TRANSPORT_USB = "USB"
TRANSPORT_BLE = "Bluetooth Low Energy"

#: Writes are framed differently per transport, and getting it wrong fails
#: *silently* - see :func:`frame_message`.
TRANSPORT_NOTE = (
    "USB and Bluetooth frame writes differently: USB takes a bare 63-byte "
    "payload, Bluetooth wants 64 bytes with the report id repeated as byte 0. "
    "A wrongly framed write still returns success and is silently discarded, "
    "so the only trustworthy health check is a device.status round trip."
)


# ---------------------------------------------------------------------------
# Framing - pure, testable, platform independent
# ---------------------------------------------------------------------------

def frame_message(payload: str, transport: str = TRANSPORT_USB) -> List[bytes]:
    """Split a JSON string into vendor-channel output reports.

    The framing **differs by transport**, and this is the single nastiest trap
    in the whole protocol:

    ============ ============================= ==============
    Transport    Buffer                        Length
    ============ ============================= ==============
    USB          ``[0x02][len][json…]``        63
    Bluetooth LE ``[0x06][0x02][len][json…]``  64
    ============ ============================= ==============

    Both are passed to ``IOHIDDeviceSetReport`` with ``reportID=6`` as an
    *Output* report (Feature is rejected over BLE with ``0xE00002F0``). Get the
    framing wrong over Bluetooth and the call still returns
    ``kIOReturnSuccess`` - the device simply discards it. **Return codes prove
    nothing here.** The only reliable check is a ``device.status`` round trip;
    see :meth:`Device.self_test`.

    Messages are CRLF terminated so the firmware knows where they end, and
    anything longer than :data:`MAX_CHUNK` bytes spans several reports.
    """
    ble = transport == TRANSPORT_BLE
    size = REPORT_BYTES_BLE if ble else REPORT_BYTES
    prefix = 1 if ble else 0
    data = payload.encode("utf-8") + b"\r\n"
    reports: List[bytes] = []
    for offset in range(0, len(data), MAX_CHUNK):
        chunk = data[offset:offset + MAX_CHUNK]
        report = bytearray(size)
        if ble:
            report[0] = REPORT_ID
        report[prefix] = OPCODE_DATA
        report[prefix + 1] = len(chunk)
        report[prefix + 2:prefix + 2 + len(chunk)] = chunk
        reports.append(bytes(report))
    return reports


class FrameDecoder:
    """Reassemble JSON messages from a stream of vendor HID reports.

    A single logical message can span several reports, and several messages can
    arrive inside one report, so the decoder keeps a byte buffer and yields
    whatever complete CRLF-terminated lines it can parse.

    macOS hands the input-report callback a buffer that begins with the report
    id, while other paths hand over the payload alone. Rather than guess, we
    accept both layouts - the framing opcode tells us which one we got.
    """

    def __init__(self) -> None:
        self._buffer = bytearray()

    def feed(self, raw: bytes) -> List[Dict[str, Any]]:
        """Consume one report; return the messages it completed."""
        if len(raw) >= 3 and raw[0] == REPORT_ID and raw[1] == OPCODE_DATA:
            start = 1
        elif len(raw) >= 2 and raw[0] == OPCODE_DATA:
            start = 0
        else:
            return []
        length = raw[start + 1]
        body = raw[start + 2:start + 2 + length]
        self._buffer.extend(body)

        messages: List[Dict[str, Any]] = []
        while b"\r\n" in self._buffer:
            line, _, _ = bytes(self._buffer).partition(b"\r\n")
            del self._buffer[:len(line) + 2]
            text = line.decode("utf-8", "replace").strip()
            if not text:
                continue
            try:
                parsed = json.loads(text)
            except ValueError:
                continue  # a truncated or non-JSON frame is not fatal
            if isinstance(parsed, dict):
                messages.append(parsed)
        return messages

    def reset(self) -> None:
        self._buffer.clear()


def notification(method: str, params: Any) -> Dict[str, Any]:
    """Build a fire-and-forget request.

    The ``v.oai.*`` methods are **notifications**: sending one with an ``id``
    gets a ``404 Method not found`` back, so we never add one.
    """
    return {"m": method, "p": params}


# ---------------------------------------------------------------------------
# IOKit binding - macOS only, but importing this module elsewhere must not fail
# ---------------------------------------------------------------------------

_iokit: Optional[ctypes.CDLL] = None
_cf: Optional[ctypes.CDLL] = None
_UNSUPPORTED_REASON = ""

if sys.platform == "darwin":
    try:
        _iokit = ctypes.CDLL(ctypes.util.find_library("IOKit"))
        _cf = ctypes.CDLL(ctypes.util.find_library("CoreFoundation"))
    except (OSError, TypeError) as exc:  # pragma: no cover - macOS only
        _iokit = _cf = None
        _UNSUPPORTED_REASON = f"could not load IOKit/CoreFoundation: {exc}"
else:
    _UNSUPPORTED_REASON = (
        f"the Codex Micro vendor channel needs macOS IOKit; this is "
        f"{sys.platform}. Linux/Windows support is not implemented yet."
    )


def is_supported() -> bool:
    """True when this platform can talk to the pad at all."""
    return _iokit is not None and _cf is not None


def unsupported_reason() -> str:
    """Human-readable explanation for why :func:`is_supported` is False."""
    return _UNSUPPORTED_REASON


def _configure() -> None:
    """Declare ctypes signatures. Wrong signatures crash, so be explicit."""
    assert _cf is not None and _iokit is not None
    _cf.CFStringCreateWithCString.restype = ctypes.c_void_p
    _cf.CFStringCreateWithCString.argtypes = [
        ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32,
    ]
    _cf.CFRelease.argtypes = [ctypes.c_void_p]
    _cf.CFNumberGetValue.restype = ctypes.c_bool
    _cf.CFNumberGetValue.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
    _cf.CFStringGetCString.restype = ctypes.c_bool
    _cf.CFStringGetCString.argtypes = [
        ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32,
    ]
    _cf.CFRunLoopGetCurrent.restype = ctypes.c_void_p
    _cf.CFRunLoopRunInMode.restype = ctypes.c_int32
    _cf.CFRunLoopRunInMode.argtypes = [ctypes.c_void_p, ctypes.c_double, ctypes.c_bool]
    _iokit.IOServiceMatching.restype = ctypes.c_void_p
    _iokit.IOServiceMatching.argtypes = [ctypes.c_char_p]
    _iokit.IOServiceGetMatchingServices.restype = ctypes.c_int
    _iokit.IOServiceGetMatchingServices.argtypes = [
        ctypes.c_uint, ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint),
    ]
    _iokit.IOIteratorNext.restype = ctypes.c_uint
    _iokit.IOIteratorNext.argtypes = [ctypes.c_uint]
    _iokit.IORegistryEntryCreateCFProperty.restype = ctypes.c_void_p
    _iokit.IORegistryEntryCreateCFProperty.argtypes = [
        ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
    ]
    _iokit.IOHIDDeviceCreate.restype = ctypes.c_void_p
    _iokit.IOHIDDeviceCreate.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    _iokit.IOHIDDeviceOpen.restype = ctypes.c_int
    _iokit.IOHIDDeviceOpen.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    _iokit.IOHIDDeviceClose.restype = ctypes.c_int
    _iokit.IOHIDDeviceClose.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    _iokit.IOHIDDeviceScheduleWithRunLoop.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
    ]
    _iokit.IOHIDDeviceUnscheduleFromRunLoop.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
    ]
    _iokit.IOHIDDeviceRegisterInputReportCallback.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(ctypes.c_ubyte), ctypes.c_long,
        ctypes.c_void_p, ctypes.c_void_p,
    ]
    _iokit.IOHIDDeviceSetReport.restype = ctypes.c_int
    _iokit.IOHIDDeviceSetReport.argtypes = [
        ctypes.c_void_p, ctypes.c_int, ctypes.c_long,
        ctypes.POINTER(ctypes.c_ubyte), ctypes.c_long,
    ]
    _iokit.IOObjectRelease.argtypes = [ctypes.c_uint]


if is_supported():
    _configure()


def _cfstr(text: str) -> Any:
    assert _cf is not None
    return _cf.CFStringCreateWithCString(None, text.encode(), 0x08000100)


def _prop_int(service: int, key: str) -> Optional[int]:
    assert _iokit is not None and _cf is not None
    name = _cfstr(key)
    try:
        ref = _iokit.IORegistryEntryCreateCFProperty(service, name, None, 0)
    finally:
        _cf.CFRelease(name)
    if not ref:
        return None
    out = ctypes.c_int32(0)
    try:
        if _cf.CFNumberGetValue(ref, 3, ctypes.byref(out)):  # kCFNumberSInt32Type
            return out.value
    finally:
        _cf.CFRelease(ref)
    return None


def _prop_str(service: int, key: str) -> Optional[str]:
    """Read a CFString property (``Transport``, ``Product``) off the service."""
    assert _iokit is not None and _cf is not None
    name = _cfstr(key)
    try:
        ref = _iokit.IORegistryEntryCreateCFProperty(service, name, None, 0)
    finally:
        _cf.CFRelease(name)
    if not ref:
        return None
    buffer = ctypes.create_string_buffer(256)
    try:
        if _cf.CFStringGetCString(ref, buffer, len(buffer), 0x08000100):
            return buffer.value.decode("utf-8", "replace")
    finally:
        _cf.CFRelease(ref)
    return None


_CALLBACK = ctypes.CFUNCTYPE(
    None, ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_int,
    ctypes.c_uint32, ctypes.POINTER(ctypes.c_ubyte), ctypes.c_long,
)

#: Strong references to every ctypes callback that is (or has been) scheduled on
#: a run loop. Letting one be garbage collected while IOKit still holds the
#: pointer is a hard segfault - and the pad *does* drop mid-callback, often.
#: These objects are tiny and bounded by the number of streams a process opens,
#: so we simply never release them.
_LIVE_CALLBACKS: List[Any] = []


class DeviceError(RuntimeError):
    """Raised when the pad is present but refuses a command."""


class Device:
    """An open handle on the pad's vendor channel.

    Wraps the ``IOHIDDeviceRef`` so callers never touch ctypes. One handle
    serves both directions, which is what lets ``freemicro run`` drive the LEDs
    and read the keys in a single process.
    """

    def __init__(self, ref: Any, transport: str = TRANSPORT_USB) -> None:
        self._ref = ref
        self._closed = False
        self._decoder = FrameDecoder()
        self._callback: Any = None
        self._inbuf: Any = None
        self._mode: Any = None
        self._stop = False
        #: ``"USB"`` or ``"Bluetooth Low Energy"``, straight from IOKit.
        self.transport = transport

    def request_stop(self) -> None:
        """Ask a running :meth:`stream` to return at its next tick."""
        self._stop = True

    # -- writing ----------------------------------------------------------

    def send_report(self, report: bytes) -> None:
        """Push one raw 63-byte output report."""
        assert _iokit is not None
        if self._closed:
            raise DeviceError("device is closed")
        buf = (ctypes.c_ubyte * len(report)).from_buffer_copy(report)
        result = _iokit.IOHIDDeviceSetReport(
            self._ref, _REPORT_TYPE_OUTPUT, REPORT_ID, buf, len(report)
        )
        if result != 0:
            raise DeviceError(f"IOHIDDeviceSetReport failed: 0x{result & 0xFFFFFFFF:08X}")

    def send(self, message: Dict[str, Any]) -> None:
        """Serialise and send one JSON-RPC message.

        Fire and forget: no callback is registered and no run loop is pumped.
        That send-only path has proven markedly more stable than the callback
        path, so lighting never touches the run loop.
        """
        payload = json.dumps(message, separators=(",", ":"))
        for report in frame_message(payload, self.transport):
            self.send_report(report)

    def notify(self, method: str, params: Any) -> None:
        """Send a fire-and-forget ``v.oai.*`` notification."""
        self.send(notification(method, params))

    def request(
        self, method: str, params: Any = None, request_id: int = 1,
        timeout: float = 2.0,
    ) -> Optional[Dict[str, Any]]:
        """Send a request and wait briefly for its reply.

        Only for methods that *answer* - the ``v.oai.*`` notifications 404 if
        given an id. Returns ``None`` on timeout.
        """
        message: Dict[str, Any] = {"m": method, "id": int(request_id)}
        if params is not None:
            message["p"] = params
        reply: List[Dict[str, Any]] = []
        sent = []

        def _on_event(msg: Dict[str, Any]) -> None:
            if msg.get("id") == request_id or "result" in msg or "error" in msg:
                reply.append(msg)
                self.request_stop()

        def _tick() -> None:
            # Send from inside the loop, *after* the input callback is
            # registered. Sending first races the reply: the pad can answer in
            # under a millisecond and we would never see it.
            if not sent:
                sent.append(True)
                self.send(message)

        self.stream(_on_event, seconds=timeout, on_tick=_tick, tick_interval=0.05)
        return reply[0] if reply else None

    def self_test(self, timeout: float = 2.0) -> Optional[Dict[str, Any]]:
        """Prove the channel actually works, and return ``device.status``.

        This exists because **write return codes are meaningless here**: a
        wrongly framed write reports success and is silently discarded. A
        ``device.status`` round trip is the only evidence that the framing, the
        transport and the permissions are all correct.
        """
        reply = self.request("device.status", timeout=timeout)
        if reply is None:
            return None
        result = reply.get("result")
        return result if isinstance(result, dict) else reply

    # -- reading ----------------------------------------------------------

    def stream(
        self,
        on_event: Callable[[Dict[str, Any]], None],
        seconds: float = 0.0,
        on_tick: Optional[Callable[[], None]] = None,
        tick_interval: float = 0.1,
    ) -> None:
        """Pump the run loop, calling ``on_event`` for each decoded message.

        ``seconds <= 0`` runs until interrupted. ``on_tick`` fires roughly every
        ``tick_interval`` seconds between reports, which is how the combined
        ``run`` command polls agent state without a second thread.
        """
        assert _iokit is not None and _cf is not None
        if self._closed:
            raise DeviceError("device is closed")

        def _on_report(ctx, result, sender, rtype, rid, report, length):
            # This runs on IOKit's callback stack. An exception escaping here
            # crosses the ctypes boundary and takes the interpreter with it, so
            # *nothing* in this function is allowed to raise.
            try:
                count = max(0, min(int(length), REPORT_BYTES + 1))
                raw = bytes(bytearray(report[i] for i in range(count)))
                messages = self._decoder.feed(raw)
            except Exception:  # noqa: BLE001
                return
            for message in messages:
                try:
                    on_event(message)
                except Exception:  # noqa: BLE001 - a bad handler must not kill the loop
                    continue

        # Keep strong references: ctypes will not, and a callback collected
        # while IOKit still holds the pointer is a hard segfault - which this
        # device provokes regularly by dropping off the bus mid-callback.
        self._callback = _CALLBACK(_on_report)
        _LIVE_CALLBACKS.append(self._callback)
        self._inbuf = (ctypes.c_ubyte * (REPORT_BYTES + 1))()
        self._mode = _cfstr("kCFRunLoopDefaultMode")
        self._stop = False

        _iokit.IOHIDDeviceRegisterInputReportCallback(
            self._ref, self._inbuf, REPORT_BYTES + 1, self._callback, None
        )
        _iokit.IOHIDDeviceScheduleWithRunLoop(
            self._ref, _cf.CFRunLoopGetCurrent(), self._mode
        )
        started = time.time()
        try:
            while not self._stop and (
                seconds <= 0 or (time.time() - started) < seconds
            ):
                _cf.CFRunLoopRunInMode(
                    self._mode, ctypes.c_double(tick_interval), False
                )
                if on_tick is not None:
                    on_tick()
        finally:
            self._unschedule()

    def _unschedule(self) -> None:
        """Detach from the run loop before anything can be released.

        Order matters: unregister the callback first, then unschedule. Dropping
        the device reference while IOKit still has our function pointer is the
        crash we are avoiding, so we never clear ``_LIVE_CALLBACKS``.
        """
        if _iokit is None or _cf is None or self._closed or self._callback is None:
            return
        try:
            _iokit.IOHIDDeviceRegisterInputReportCallback(
                self._ref, self._inbuf, REPORT_BYTES + 1, None, None
            )
            _iokit.IOHIDDeviceUnscheduleFromRunLoop(
                self._ref, _cf.CFRunLoopGetCurrent(), self._mode
            )
        except Exception:  # pragma: no cover - teardown must never raise
            pass
        self._callback = None

    # -- lifecycle --------------------------------------------------------

    def close(self) -> None:
        if self._closed:
            return
        self._unschedule()
        self._closed = True
        if _iokit is not None:
            try:
                _iokit.IOHIDDeviceClose(self._ref, 0)
            except Exception:  # pragma: no cover
                pass
        if _cf is not None and self._mode:
            _cf.CFRelease(self._mode)
            self._mode = None
        if _cf is not None and self._ref:
            _cf.CFRelease(self._ref)
        self._ref = None

    def __enter__(self) -> "Device":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


def _matching_service() -> Optional[Tuple[int, str]]:
    """Find the pad's IOHIDDevice service, preferring USB over Bluetooth.

    The same pad can be present twice - cable in *and* paired over BLE. Both
    work; USB is preferred simply because a wired link does not drop.

    Matching is on **VendorID/ProductID only**. Do not match on the product
    string or collection count: over BLE the pad calls itself ``Codex Micro #1``
    and exposes 4 collections with a 216-byte descriptor, against ``Codex
    Micro`` with 6 collections and 275 bytes over USB.
    """
    assert _iokit is not None
    iterator = ctypes.c_uint(0)
    _iokit.IOServiceGetMatchingServices(
        0, _iokit.IOServiceMatching(b"IOHIDDevice"), ctypes.byref(iterator)
    )
    best: Optional[Tuple[int, str]] = None
    while True:
        candidate = _iokit.IOIteratorNext(iterator.value)
        if not candidate:
            break
        matched = (
            _prop_int(candidate, "VendorID") == VENDOR_ID
            and _prop_int(candidate, "ProductID") == PRODUCT_ID
        )
        if not matched:
            _iokit.IOObjectRelease(candidate)
            continue
        transport = _prop_str(candidate, "Transport") or "unknown"
        if best is None:
            best = (candidate, transport)
        elif transport == TRANSPORT_USB and best[1] != TRANSPORT_USB:
            _iokit.IOObjectRelease(best[0])
            best = (candidate, transport)
        else:
            _iokit.IOObjectRelease(candidate)
    return best


def open_device() -> Optional[Device]:
    """Open the pad, or return ``None`` if it is absent or unreachable.

    Returning ``None`` rather than raising is deliberate: every caller has a
    graceful degradation path (the screen renderer, or a "plug it in" message),
    and no part of FreeMicro should die because a USB device is missing.

    Bluetooth works as well as USB - it just frames writes differently, which
    :func:`frame_message` handles from :attr:`Device.transport`.
    """
    if not is_supported():
        return None
    assert _iokit is not None
    found = _matching_service()
    if found is None:
        return None
    service, transport = found
    try:
        ref = _iokit.IOHIDDeviceCreate(None, service)
    finally:
        _iokit.IOObjectRelease(service)
    if not ref:
        return None
    if _iokit.IOHIDDeviceOpen(ref, 0) != 0:
        # Almost always a missing Input Monitoring grant.
        if _cf is not None:
            _cf.CFRelease(ref)
        return None
    return Device(ref, transport=transport)


def device_transport() -> Optional[str]:
    """The transport of the pad we would open, or ``None`` if absent.

    Needs no permission and no open, so it is safe to poll - which is how the
    reconnect loop notices the pad coming and going.
    """
    if not is_supported():
        return None
    found = _matching_service()
    if found is None:
        return None
    assert _iokit is not None
    service, transport = found
    _iokit.IOObjectRelease(service)
    return transport


def device_present() -> bool:
    """True if a Codex Micro is on the bus (no open, no permission needed)."""
    return device_transport() is not None


__all__ = [
    "Device",
    "DeviceError",
    "EVENT_JOYSTICK",
    "EVENT_KEY",
    "FrameDecoder",
    "MAX_CHUNK",
    "OPCODE_DATA",
    "PRODUCT_ID",
    "REPORT_BYTES",
    "REPORT_ID",
    "REPORT_BYTES_BLE",
    "TRANSPORT_BLE",
    "TRANSPORT_NOTE",
    "TRANSPORT_USB",
    "VENDOR_ID",
    "device_present",
    "device_transport",
    "frame_message",
    "is_supported",
    "notification",
    "open_device",
    "unsupported_reason",
]
