"""What firmware FreeMicro has actually been verified against - and nothing more.

Every protocol fact in ``docs/PROTOCOL.md`` was established on **one physical
unit running firmware v0.4.1**. That is a real limitation, not a formality: the
write framing, the LED methods and the key ids are all things a firmware update
could change, and we would have no way of knowing until somebody's pad went
quiet.

So this module does exactly one thing: compare the version string the device
reports in ``device.status`` against what we have tested, and say something
honest about the difference. It deliberately does **not** gate anything.

    Blocking a newer firmware would be the wrong trade. The protocol may well be
    unchanged, and a tool that refuses to run because it has not heard of your
    version is worse than one that works and mentions the uncertainty. Warn,
    never gate - :attr:`FirmwareReport.blocks` is hard-coded ``False`` so that
    stays true no matter who edits the caller.

Usage::

    from freemicro import compat

    status = device.self_test()            # the device.status round trip
    report = compat.check_status(status)
    if not report.ok:
        print(report.message)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional, Tuple

#: Firmware version(s) the documented protocol was verified on, oldest first.
#: Add to this list only when someone has actually re-run the checks in
#: :data:`VERIFIED_BEHAVIOUR` on that firmware and said so in an issue.
KNOWN_GOOD: Tuple[str, ...] = ("0.4.1",)

#: What "verified" means for the versions above - each line was confirmed by
#: hand on hardware, most of them by eye. Printed alongside the classification
#: so a user can tell what we are actually claiming.
VERIFIED_BEHAVIOUR: Tuple[str, ...] = (
    "key, dial and thumbstick events arrive on the 0xFF00 vendor channel",
    "lights.preview drives the backlight and the underglow",
    "v.oai.thstatus sets the six Agent Keys individually",
    "a device.status round trip works over USB and over Bluetooth LE",
    "the transport-dependent write framing (63-byte USB, 64-byte BLE)",
)

#: Classification results.
KNOWN_GOOD_STATUS = "known-good"
NEWER = "newer-than-tested"
OLDER = "older-than-tested"
UNPARSEABLE = "unparseable"
MISSING = "missing"

#: Where to tell us about a difference. Kept here so every message can point at
#: the same place without the caller having to know the URL.
REPORT_URL = (
    "https://github.com/eliBenven/freemicro/issues/new"
    "?template=hardware_report.yml"
)

_ASK = (
    "If lighting or keys misbehave, please open a Hardware Report - a firmware "
    "version we have not seen is genuinely useful data:\n" + REPORT_URL
)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def parse_version(text: Any) -> Optional[Tuple[int, ...]]:
    """Turn a reported version string into a comparable tuple, or ``None``.

    Deliberately forgiving about presentation and strict about content. It
    accepts ``"0.4.1"``, ``"v0.4.1"``, ``"0.4.1-beta.2"``, ``"0.4.1+cafe"`` and
    ``"0.4.1 (build 7)"``; it returns ``None`` for anything whose numeric part
    is not purely numeric, because a wrong comparison is worse than no
    comparison. Pre-release and build metadata are dropped rather than ranked -
    FreeMicro has no evidence about how this vendor orders them.
    """
    if text is None:
        return None
    raw = str(text).strip()
    if not raw:
        return None
    raw = raw.split()[0]            # "0.4.1 (build 7)" -> "0.4.1"
    if raw[:1] in ("v", "V"):
        raw = raw[1:]
    for separator in ("-", "+", "_", "/"):
        raw = raw.split(separator)[0]
    if not raw:
        return None
    numbers = []
    for part in raw.split("."):
        if not part.isdigit():
            return None
        numbers.append(int(part))
    return tuple(numbers) if numbers else None


def format_version(version: Optional[Tuple[int, ...]]) -> str:
    """``(0, 4, 1)`` -> ``"0.4.1"``. Empty string for ``None``."""
    if not version:
        return ""
    return ".".join(str(part) for part in version)


def _pad(a: Tuple[int, ...], b: Tuple[int, ...]) -> Tuple[tuple, tuple]:
    """Zero-extend two version tuples so ``0.5`` and ``0.5.0`` compare equal."""
    width = max(len(a), len(b))
    return a + (0,) * (width - len(a)), b + (0,) * (width - len(b))


def newest_known_good() -> Tuple[int, ...]:
    """The highest version in :data:`KNOWN_GOOD`, as a tuple."""
    parsed = [parse_version(v) for v in KNOWN_GOOD]
    return max(v for v in parsed if v)


def classify(text: Any) -> str:
    """Classify a reported version string. Never raises."""
    if text is None or not str(text).strip():
        return MISSING
    version = parse_version(text)
    if version is None:
        return UNPARSEABLE
    for known in KNOWN_GOOD:
        parsed = parse_version(known)
        if parsed is None:  # pragma: no cover - KNOWN_GOOD is ours to keep sane
            continue
        left, right = _pad(version, parsed)
        if left == right:
            return KNOWN_GOOD_STATUS
    left, right = _pad(version, newest_known_good())
    return NEWER if left > right else OLDER


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FirmwareReport:
    """One firmware version, classified, with something honest to say about it."""

    raw: str
    version: Optional[Tuple[int, ...]]
    status: str
    message: str

    @property
    def ok(self) -> bool:
        """True only for a version we have actually tested on."""
        return self.status == KNOWN_GOOD_STATUS

    @property
    def blocks(self) -> bool:
        """Always ``False``.

        A firmware mismatch is information, not a fault. Nothing in FreeMicro
        may refuse to run because of one, and this property exists so that is a
        statement in code rather than a promise in a comment.
        """
        return False

    @property
    def tested(self) -> str:
        """The version(s) we tested on, for display."""
        return ", ".join(f"v{v}" for v in KNOWN_GOOD)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "reported": self.raw,
            "parsed": format_version(self.version),
            "status": self.status,
            "verified_on": list(KNOWN_GOOD),
            "message": self.message,
        }


def _message(status: str, raw: str, version: Optional[Tuple[int, ...]]) -> str:
    tested = ", ".join(f"v{v}" for v in KNOWN_GOOD)
    shown = f"v{format_version(version)}" if version else repr(raw)
    if status == KNOWN_GOOD_STATUS:
        return (
            f"Firmware {shown} - this is the version FreeMicro was verified on. "
            "Everything in docs/PROTOCOL.md was confirmed here."
        )
    if status == NEWER:
        return (
            f"FreeMicro was verified on {tested}; yours is {shown}. That is "
            "newer than anything we have tested, so some details may differ. "
            "Nothing is disabled - go ahead and use it.\n" + _ASK
        )
    if status == OLDER:
        return (
            f"FreeMicro was verified on {tested}; yours is {shown}, which is "
            "older. It will very likely behave the same, but we have not "
            "checked. Nothing is disabled.\n" + _ASK
        )
    if status == UNPARSEABLE:
        return (
            f"FreeMicro was verified on {tested}. Your pad reports {shown}, "
            "which is not a version number this build knows how to compare - "
            "so no claim either way. Nothing is disabled.\n" + _ASK
        )
    return (
        f"FreeMicro was verified on {tested}. Your pad did not report a "
        "firmware version, so there is nothing to compare. That is normal when "
        "the device.status round trip did not complete; run `freemicro doctor` "
        "to see whether the write path is working."
    )


def check(version: Any) -> FirmwareReport:
    """Classify a firmware version string and return a :class:`FirmwareReport`."""
    raw = "" if version is None else str(version).strip()
    status = classify(version)
    parsed = parse_version(version)
    return FirmwareReport(
        raw=raw, version=parsed, status=status,
        message=_message(status, raw, parsed),
    )


def check_status(status: Optional[Mapping[str, Any]]) -> FirmwareReport:
    """Classify the ``version`` field of a ``device.status`` reply.

    Takes the reply dict rather than the string so callers do not each have to
    remember which key it lives under, and so ``None`` (no round trip) lands on
    the "missing" message instead of crashing.
    """
    if not isinstance(status, Mapping):
        return check(None)
    return check(status.get("version"))


def summary_line(report: FirmwareReport) -> str:
    """A single line for ``doctor`` and other dense output."""
    shown = f"v{format_version(report.version)}" if report.version else "unknown"
    if report.status == KNOWN_GOOD_STATUS:
        return f"firmware {shown} - verified"
    if report.status == MISSING:
        return f"firmware not reported (verified on v{KNOWN_GOOD[-1]})"
    if report.status == UNPARSEABLE:
        return (
            f"firmware {report.raw!r} - unrecognised format "
            f"(verified on v{KNOWN_GOOD[-1]})"
        )
    return f"firmware {shown} - {report.status} (verified on v{KNOWN_GOOD[-1]})"


__all__ = [
    "KNOWN_GOOD",
    "KNOWN_GOOD_STATUS",
    "MISSING",
    "NEWER",
    "OLDER",
    "REPORT_URL",
    "UNPARSEABLE",
    "VERIFIED_BEHAVIOUR",
    "FirmwareReport",
    "check",
    "check_status",
    "classify",
    "format_version",
    "newest_known_good",
    "parse_version",
    "summary_line",
]
