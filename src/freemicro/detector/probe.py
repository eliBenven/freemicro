"""Read-only USB-HID probe that answers the Milestone 0 questions.

This is deliberately harmless: it only *enumerates* HID devices and reports
what it sees. It never writes to a device. Running it and pasting the output
into a GitHub issue is, by itself, a useful community artifact — nobody has
published a capability report for the shipping Codex Micro yet.

It answers:

* Which HID interfaces does the pad expose? (``hid.enumerate()``)
* Is there a raw interface on ``usage_page 0xFF60`` (VIA/QMK writable channel)?
* What are the VID/PID and product strings?

The remaining M0 questions — does the VIA lighting command actually move the
Agent Keys, is the bootloader open, does the ChatGPT app contend for the LEDs
— require a *write* and a human in the loop, so the probe only flags the
raw channel as "present" and leaves the write test to ``freemicro render``.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field

RAW_USAGE_PAGE = 0xFF60

# Loose hints only used to add a human-readable label. Never a gate — the
# probe reports everything it finds regardless.
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
        return self.usage_page == RAW_USAGE_PAGE

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

    def to_json(self, indent: int = 2) -> str:
        data = asdict(self)
        data["has_raw_channel"] = self.has_raw_channel
        return json.dumps(data, indent=indent)


def probe() -> CapabilityReport:
    """Enumerate HID devices and return a :class:`CapabilityReport`.

    Never raises for a missing dependency or permissions issue — those become
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
            "Found a raw-HID interface on usage_page 0xFF60 — a VIA/QMK "
            "writable channel is likely present. Try `freemicro render done`."
        )
    else:
        report.notes.append(
            "No 0xFF60 raw-HID interface found. The pad may not expose a "
            "writable VIA channel; fall back to busylight or screen renderers."
        )
    return report
