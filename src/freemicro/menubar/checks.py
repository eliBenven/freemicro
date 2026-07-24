"""``Run Doctor`` for the menu bar: the same questions, as structured results.

``freemicro doctor`` prints. A menu bar cannot show a print, and dumping a
terminal transcript into an alert is the sort of thing that makes a native app
feel like a debug panel - so the checks are gathered here as data and rendered
by whoever asked for them.

Two ways this deliberately differs from the CLI's doctor:

* **It will not take the pad.** The CLI's ``device.status`` round trip needs the
  device open, and the daemon normally has it. When the lock is held, that check
  reports *skipped, and why* rather than fighting for the handle or lying about
  the result.
* **It never blocks the UI.** Everything here is called from a worker thread;
  the caller marshals the finished list back to the main thread.

The duplication with ``cli.cmd_doctor`` is real and is called out under "Wiring
required" in ``docs/MENUBAR.md``: the right fix is for the CLI's doctor to be
built on a structured checks function that both surfaces render.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

#: An advisory result - neither pass nor fail. Used for things that are a
#: *choice* (LED control is off by default) or genuinely unknown.
ADVISORY = None


@dataclass(frozen=True)
class Check:
    """One question, its answer, and what to do if the answer is bad."""

    label: str
    #: ``True`` pass, ``False`` fail, ``None`` advisory.
    ok: Optional[bool]
    detail: str = ""
    fix: str = ""

    @property
    def mark(self) -> str:
        if self.ok is True:
            return "✓"
        if self.ok is False:
            return "✕"
        return "•"

    def line(self) -> str:
        text = f"{self.mark}  {self.label}"
        if self.detail:
            text += f"\n     {self.detail}"
        if self.ok is False and self.fix:
            for part in self.fix.splitlines():
                text += f"\n     → {part}"
        return text


def run_checks() -> List[Check]:
    """Every check, in the order a human would ask them. Never raises."""
    checks: List[Check] = []
    checks.extend(_platform_checks())
    checks.extend(_permission_checks())
    checks.extend(_config_checks())
    checks.extend(_device_checks())
    return checks


def _platform_checks() -> List[Check]:
    from freemicro.device import is_supported, unsupported_reason

    supported = is_supported()
    return [
        Check(
            "macOS IOKit available",
            supported,
            "" if supported else unsupported_reason(),
            "Pad support is macOS-only today.",
        )
    ]


def _permission_checks() -> List[Check]:
    from freemicro import permissions

    checks: List[Check] = []
    granted, detail = permissions.input_monitoring()
    checks.append(
        Check(
            "Input Monitoring (lets FreeMicro read the pad)",
            granted,
            detail,
            permissions.fix_text("input_monitoring"),
        )
    )
    access, access_detail = permissions.accessibility()
    checks.append(
        Check(
            "Accessibility (lets FreeMicro type for you)",
            access,
            access_detail,
            permissions.fix_text("accessibility"),
        )
    )
    return checks


def _config_checks() -> List[Check]:
    from freemicro import padconfig

    checks: List[Check] = []
    try:
        pad = padconfig.load()
    except padconfig.PadConfigError as exc:
        return [
            Check(
                "Pad config loads",
                False,
                str(exc),
                "freemicro keys --init --force   (start again from the default)",
            )
        ]
    checks.append(Check("Pad config loads", True, pad.origin))
    for warning in pad.warnings:
        checks.append(Check("Config warning", ADVISORY, warning))
    if pad.lighting.enabled:
        checks.append(
            Check("LED control is on", True, ", ".join(pad.lighting.zones))
        )
    else:
        checks.append(
            Check(
                "LED control is off",
                ADVISORY,
                "The shipped default - FreeMicro does not take over your pad "
                "uninvited. Turn it on from this menu.",
            )
        )
    return checks


def _device_checks() -> List[Check]:
    from freemicro import permissions
    from freemicro.device import TRANSPORT_BLE, device_transport
    from freemicro.menubar import status

    checks: List[Check] = []
    transport = device_transport()
    if transport is None:
        checks.append(
            Check(
                "Codex Micro found",
                False,
                "",
                "Plug in the USB cable, or pair the pad over Bluetooth - both "
                "work.",
            )
        )
        return checks
    detail = transport
    if transport == TRANSPORT_BLE:
        detail += " - wireless is fully supported: input, LEDs and RPC"
    checks.append(Check("Codex Micro found", True, detail))

    if permissions.chatgpt_running():
        checks.append(
            Check(
                "The ChatGPT desktop app is running",
                ADVISORY,
                "A LIGHTING warning only - keys, dial and joystick still work, "
                "because macOS lets both apps read the pad at once. Only LED "
                "writes collide, and FreeMicro repaints as soon as ChatGPT "
                "quits. `freemicro lights --coexist` avoids it entirely by "
                "driving only the key backlight, which ChatGPT leaves dark.",
            )
        )

    owner = status.pad_owner()
    if owner:
        checks.append(
            Check(
                "Pad ownership",
                ADVISORY,
                f"{owner} has the pad. Only one process can usefully hold it, "
                "so the write test is skipped rather than fought for - the "
                "pad already has an owner, which is the working case.",
            )
        )
        return checks

    reply = status.probe_device_status()
    if reply:
        bits = []
        if reply.get("version"):
            bits.append(f"firmware {reply['version']}")
        if reply.get("battery") is not None:
            bits.append(
                f"battery {reply['battery']}%"
                + (" (charging)" if reply.get("is_charging") else "")
            )
        checks.append(
            Check("device.status round trip", True, " · ".join(bits))
        )
    else:
        checks.append(
            Check(
                "device.status round trip",
                False,
                "No reply.",
                "Success return codes prove nothing on this device - a wrongly\n"
                "framed write is discarded silently. Quit the ChatGPT app and\n"
                "try again; if it still fails, unplug and replug the pad.",
            )
        )
    return checks


def summary(checks: List[Check]) -> str:
    """One line: how many passed, how many did not."""
    failed = [c for c in checks if c.ok is False]
    passed = [c for c in checks if c.ok is True]
    if not failed:
        return f"All {len(passed)} checks passed."
    word = "check" if len(failed) == 1 else "checks"
    return f"{len(failed)} {word} failed."


def report(checks: List[Check]) -> str:
    """The whole thing as readable text, for the alert and the clipboard."""
    return "\n".join(check.line() for check in checks)


__all__ = ["ADVISORY", "Check", "report", "run_checks", "summary"]
