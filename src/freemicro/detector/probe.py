"""Read-only USB-HID probe that answers the Milestone 0 questions.

This is deliberately harmless: it only *enumerates* HID devices and reports
what it sees. It never writes to a device. Running it and pasting the output
into a GitHub issue is, by itself, a useful community artifact - nobody has
published a capability report for the shipping Codex Micro yet.

It answers:

* Which HID interfaces does the pad expose? (``hid.enumerate()``)
* Is there a raw interface on ``usage_page 0xFF60`` (VIA/QMK writable channel)?
* What are the VID/PID and product strings?

The remaining M0 questions - does the VIA lighting command actually move the
Agent Keys, is the bootloader open, does the ChatGPT app contend for the LEDs
- require a *write* and a human in the loop, so the probe only flags the
raw channel as "present" and leaves the write test to ``freemicro render``.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field

RAW_USAGE_PAGE = 0xFF60  # VIA/QMK raw-HID console channel

# USB-HID reserves 0xFF00-0xFFFF for vendor-defined usage pages. VIA's 0xFF60
# lives inside this range, but so does any *private* protocol a vendor invents -
# including, on the shipping Codex Micro, the 0xFF00 interface the ChatGPT app
# uses to push LED state. A pad that lacks 0xFF60 but exposes some other vendor
# page is not "undriveable"; it's a capture/replay (Path B) target.
VENDOR_PAGE_MIN = 0xFF00
VENDOR_PAGE_MAX = 0xFFFF

# Loose hints only used to add a human-readable label. Never a gate - the
# probe reports everything it finds regardless. Note: the shipping Codex Micro
# enumerates under Espressif's *shared* VID 0x303A (it is ESP32-based, not the
# RP2040 Work Louder CM2 originally assumed), so we deliberately do NOT gate on
# 0x303A - that would over-match every ESP32 device on the bus. The product
# string ("Codex Micro") is what identifies it.
VENDOR_HINTS = {
    0x574C: "Work Louder (suspected)",
    0x4C57: "Work Louder (suspected, byte-swapped)",
}
PRODUCT_HINTS = ("micro", "creator", "work louder", "codex")


@dataclass
class HidInterface:
    vendor_id: int
    product_id: int
    usage_page: int
    usage: int
    interface_number: int
    product_string: str
    manufacturer_string: str

    @property
    def is_raw_channel(self) -> bool:
        """True for the VIA/QMK raw-HID console channel specifically."""
        return self.usage_page == RAW_USAGE_PAGE

    @property
    def is_vendor_channel(self) -> bool:
        """True for any vendor-defined usage page (0xFF00-0xFFFF).

        This is a superset of :attr:`is_raw_channel`: it also matches private
        vendor protocols like the Codex Micro's 0xFF00 LED channel.
        """
        return VENDOR_PAGE_MIN <= self.usage_page <= VENDOR_PAGE_MAX

    @property
    def usage_page_hex(self) -> str:
        return f"0x{self.usage_page:04X}"

    @property
    def vid_pid(self) -> str:
        return f"{self.vendor_id:04x}:{self.product_id:04x}"


@dataclass
class CapabilityReport:
    """Everything the read-only probe could learn about attached HID devices."""

    hidapi_available: bool
    interfaces: list[HidInterface] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def has_raw_channel(self) -> bool:
        return any(i.is_raw_channel for i in self.interfaces)

    @property
    def candidate_pads(self) -> list[HidInterface]:
        """Interfaces that look like they might be the Codex Micro."""
        out = []
        for i in self.interfaces:
            label = f"{i.manufacturer_string} {i.product_string}".lower()
            if i.vendor_id in VENDOR_HINTS or any(h in label for h in PRODUCT_HINTS):
                out.append(i)
        return out

    @property
    def candidate_vendor_channels(self) -> list[HidInterface]:
        """Vendor-defined interfaces (0xFF00-0xFFFF) *on candidate pads*.

        Scoped to candidate pads on purpose: a typical host exposes many of its
        own vendor-defined interfaces (Apple keyboards alone show several on
        0xFF00), so an unscoped scan would be pure noise. Restricted to the pad,
        a vendor page that is not the VIA 0xFF60 channel is the strongest signal
        of a private LED-control protocol - the capture/replay (Path B) target.
        """
        return [i for i in self.candidate_pads if i.is_vendor_channel]

    def to_json(self, indent: int = 2) -> str:
        data = asdict(self)
        data["has_raw_channel"] = self.has_raw_channel
        data["candidate_vendor_channels"] = [
            {"vid_pid": i.vid_pid, "usage_page": i.usage_page_hex, "usage": i.usage}
            for i in self.candidate_vendor_channels
        ]
        return json.dumps(data, indent=indent)


def probe() -> CapabilityReport:
    """Enumerate HID devices and return a :class:`CapabilityReport`.

    Never raises for a missing dependency or permissions issue - those become
    ``notes`` on the report so the CLI can print actionable guidance.
    """
    try:
        import hid
    except Exception:
        return CapabilityReport(
            hidapi_available=False,
            notes=[
                "hidapi is not installed. Install with: "
                'pip install "freemicro[detect]"  (or: pip install hidapi)',
            ],
        )

    report = CapabilityReport(hidapi_available=True)
    try:
        devices = hid.enumerate()
    except Exception as exc:  # pragma: no cover - platform/permission specific
        report.notes.append(f"hid.enumerate() failed: {exc}")
        report.notes.append(
            "On Linux you may need udev rules or to run with elevated "
            "permissions to read HID device metadata."
        )
        return report

    for dev in devices:
        report.interfaces.append(
            HidInterface(
                vendor_id=int(dev.get("vendor_id", 0)),
                product_id=int(dev.get("product_id", 0)),
                usage_page=int(dev.get("usage_page", 0)),
                usage=int(dev.get("usage", 0)),
                interface_number=int(dev.get("interface_number", -1)),
                product_string=str(dev.get("product_string") or ""),
                manufacturer_string=str(dev.get("manufacturer_string") or ""),
            )
        )

    if not report.interfaces:
        report.notes.append("No HID devices enumerated.")
    if report.has_raw_channel:
        report.notes.append(
            "Found a raw-HID interface on usage_page 0xFF60 - a VIA/QMK "
            "writable channel. FreeMicro does not drive it: the Codex Micro "
            "speaks its own vendor protocol on 0xFF00 (docs/PROTOCOL.md)."
        )
    else:
        vendor = report.candidate_vendor_channels
        if vendor:
            pages = ", ".join(sorted({i.usage_page_hex for i in vendor}))
            report.notes.append(
                f"The candidate pad exposes a vendor-defined HID interface on "
                f"{pages}. On the Codex Micro that is 0xFF00, and it carries "
                "keys, the joystick and the LEDs alike - see docs/PROTOCOL.md. "
                "Try `freemicro doctor` for a real round-trip write test."
            )
        else:
            report.notes.append(
                "No vendor-defined HID interface found on the candidate pad. "
                "FreeMicro reaches the Codex Micro through 0xFF00, so there is "
                "nothing here for it to drive. Try `freemicro doctor`."
            )
    return report
