"""The menu *model*: what the menu says, given a snapshot of the world.

This module is pure. It imports no Cocoa, opens no device, reads no file and
talks to no daemon - it turns a :class:`Snapshot` into a list of
:class:`MenuItem` and nothing else. That separation is the only reason the menu
bar is testable at all: a GUI test needs a window server, a logged-in session
and a menu bar, and CI has none of those.

Two rules the layout obeys:

* **Colour carries exactly one meaning.** The state dot, and nothing else. A
  menu bar full of coloured glyphs is a debug panel, not a status item.
* **Rows appear only when they say something.** No "Battery: unknown", no
  "Firmware: -". A row that is empty half the time trains people to stop
  reading it, which costs more than the row is worth.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Tuple

from freemicro.renderers.base import GLYPH, PALETTE
from freemicro.state.engine import AgentState

RGB = Tuple[int, int, int]

#: Every action string the Cocoa layer knows how to dispatch. Keeping them as
#: plain strings is what lets the model be asserted without importing AppKit.
ACTIONS = (
    "toggle_lighting",
    "open_config",
    "run_doctor",
    "open_input_monitoring",
    "open_accessibility",
    "explain_chatgpt",
    "start_bridge",
    "restart_daemon",
    "restart_menubar",
    "quit",
)

#: How the pad's transport is written for humans. IOKit's own string
#: ("Bluetooth Low Energy") is accurate and too long for a menu.
_TRANSPORT_LABELS = {
    "USB": "USB",
    "Bluetooth Low Energy": "Bluetooth",
}

#: Past this, a cached battery reading is labelled with its age rather than
#: presented as current. The pad reports battery only while something holds it
#: open, so a stale number is the normal case, not a failure.
STALE_AFTER_SECONDS = 300.0


@dataclass(frozen=True)
class StaleNote:
    """One process that is running something other than what is installed.

    ``action`` is empty when the menu bar cannot honestly fix it from here - a
    ``freemicro run`` in somebody's terminal is theirs to restart, and a button
    that quietly killed it would be worse than a sentence that names it.
    """

    summary: str
    fix: str = ""
    action: str = ""


@dataclass(frozen=True)
class Snapshot:
    """Everything the menu needs to know, gathered elsewhere.

    Every field has a default so a test can build the one situation it cares
    about in a single line.
    """

    #: Resolved agent state across all live sessions.
    state: AgentState = AgentState.IDLE
    #: How many sessions are alive right now.
    sessions: int = 0

    #: False when this platform cannot talk to the pad at all.
    supported: bool = True
    unsupported_reason: str = ""

    #: Is a pad on the bus? Needs no permission and no open, so this is honest
    #: even when Input Monitoring is missing.
    connected: bool = False
    transport: str = ""

    battery: Optional[int] = None
    charging: bool = False
    firmware: str = ""
    #: Age of the battery/firmware reading, in seconds. ``None`` when we have
    #: no reading at all.
    reading_age: Optional[float] = None

    lighting_enabled: bool = False

    #: Human description of whichever process holds the pad, "" when it is free
    #: or when we hold it ourselves.
    owner: str = ""

    #: Is *anything* driving the pad? A pad nobody is listening to emits no
    #: scancodes at all, and reporting that as fine is how three separate
    #: evenings were lost to "the product is broken". Defaults to ``True``:
    #: never warn out of ignorance, only out of knowledge.
    pad_listening: bool = True
    #: Processes running something other than what is installed.
    stale: Tuple["StaleNote", ...] = ()

    #: ``None`` means macOS has not been asked yet - which is not a failure.
    input_monitoring: Optional[bool] = None
    accessibility: bool = True
    chatgpt_running: bool = False

    #: Whether the bundled web config UI can be started.
    web_ui: bool = False
    config_path: str = ""


@dataclass(frozen=True)
class MenuItem:
    """One row. ``separator`` items carry nothing but their key."""

    key: str
    label: str = ""
    action: Optional[str] = None
    enabled: bool = False
    checked: Optional[bool] = None
    color: Optional[RGB] = None
    #: Longer text: a tooltip, or the body of the alert the row opens.
    detail: str = ""
    separator: bool = False


@dataclass(frozen=True)
class BarTitle:
    """What the status item itself shows in the menu bar."""

    text: str
    #: ``None`` means "draw it in the system's quiet-text colour" - see
    #: :func:`state_color`.
    color: Optional[RGB]
    #: Spoken by VoiceOver, and the item's accessibility label.
    description: str = ""


# ---------------------------------------------------------------------------
# Phrasing
# ---------------------------------------------------------------------------

def transport_label(transport: str) -> str:
    """"USB" / "Bluetooth", or the raw IOKit string if it is something new."""
    return _TRANSPORT_LABELS.get(transport, transport or "unknown transport")


def state_color(state: AgentState) -> Optional[RGB]:
    """The dot's colour, or ``None`` for "use the system's quiet-text colour".

    Idle is the exception, and on purpose. The palette's idle is a near-black
    slate that reads as "on but quiet" on an LED and is *invisible* on a dark
    menu bar. Idle is also the state we least want to draw the eye, so it is
    drawn in whatever colour macOS itself uses for secondary text: legible in
    both appearances, and quiet in both.
    """
    if state == AgentState.IDLE:
        return None
    return PALETTE[state]


def state_label(state: AgentState) -> str:
    """The state, capitalised for a menu ("Waiting for you")."""
    return {
        AgentState.IDLE: "Idle",
        AgentState.WORKING: "Working",
        AgentState.WAITING: "Waiting for you",
        AgentState.DONE: "Done",
        AgentState.ERROR: "Error",
    }[state]


def connection_label(snap: Snapshot) -> str:
    """One line for the pad's link.

    A disconnected wireless pad is **normal** - it drops on sleep, on range, on
    a nudged cable. So this never says "error" and never says "failed"; it says
    what is true right now and lets the pad come back.
    """
    if not snap.supported:
        return "Pad support needs macOS"
    if not snap.connected:
        return "Pad not connected"
    if snap.input_monitoring is False:
        return f"Connected over {transport_label(snap.transport)} - no permission"
    return f"Connected over {transport_label(snap.transport)}"


def battery_label(snap: Snapshot) -> str:
    """"Battery 62% - charging", or "" when we have no reading."""
    if snap.battery is None:
        return ""
    text = f"Battery {int(snap.battery)}%"
    if snap.charging:
        return text + " - charging"
    age = snap.reading_age
    if age is not None and age > STALE_AFTER_SECONDS:
        return text + f" - {_age(age)} ago"
    return text


def _age(seconds: float) -> str:
    if seconds < 90:
        return f"{int(seconds)}s"
    if seconds < 5400:
        return f"{int(seconds / 60)}m"
    return f"{int(seconds / 3600)}h"


def config_label(snap: Snapshot) -> str:
    return "Open Config…" if snap.web_ui else "Show Config in Finder"


# ---------------------------------------------------------------------------
# The menu
# ---------------------------------------------------------------------------

def _info(key: str, label: str, detail: str = "") -> MenuItem:
    """A row that states a fact. Never clickable, so never disappointing."""
    return MenuItem(key=key, label=label, enabled=False, detail=detail)


def _command(
    key: str,
    label: str,
    action: str,
    *,
    enabled: bool = True,
    checked: Optional[bool] = None,
    detail: str = "",
) -> MenuItem:
    return MenuItem(
        key=key,
        label=label,
        action=action,
        enabled=enabled,
        checked=checked,
        detail=detail,
    )


def _separator(key: str) -> MenuItem:
    return MenuItem(key=key, separator=True)


def warning_items(snap: Snapshot) -> List[MenuItem]:
    """The rows that only exist when something is wrong.

    Each one is clickable and lands the user where the fix is - a warning you
    cannot act on from the place it is shown is just nagging.
    """
    items: List[MenuItem] = []
    # First, because it outranks everything else here: a pad nobody is driving
    # is dead hardware, and every other warning is about a pad that at least
    # has an owner.
    if not snap.pad_listening:
        items.append(
            _command(
                "warn.inert",
                "Nothing is driving the pad",
                "start_bridge",
                detail=(
                    "The pad emits no ordinary scancodes, so until a FreeMicro "
                    "bridge is listening it does nothing at all - this is not a "
                    "broken pad. Click to install the background daemon, which "
                    "starts at login and restarts if it dies. Or run "
                    "`freemicro run` in a terminal."
                ),
            )
        )
    for index, note in enumerate(snap.stale):
        key = f"warn.stale.{index}"
        if note.action:
            items.append(_command(key, note.summary, note.action, detail=note.fix))
        else:
            items.append(_info(key, note.summary, note.fix))
    # `None` means macOS has not been asked yet, which is not a denial. Warning
    # about it would send people to a settings pane where the app they need to
    # tick is not even listed.
    if snap.input_monitoring is False:
        items.append(
            _command(
                "warn.input_monitoring",
                "Input Monitoring is off",
                "open_input_monitoring",
                detail=(
                    "Without Input Monitoring the pad cannot be opened at all - "
                    "no keys, no LEDs. Add your terminal app (or whatever runs "
                    "FreeMicro), then quit and reopen it: macOS only re-reads "
                    "the grant at launch."
                ),
            )
        )
    if not snap.accessibility:
        items.append(
            _command(
                "warn.accessibility",
                "Accessibility is off",
                "open_accessibility",
                detail=(
                    "Without Accessibility macOS throws FreeMicro's keystrokes "
                    "away silently, so pad keys appear to do nothing. Add your "
                    "terminal app, then quit and reopen it."
                ),
            )
        )
    if snap.chatgpt_running:
        items.append(
            _command(
                "warn.chatgpt",
                "ChatGPT app is running",
                "explain_chatgpt",
                detail=(
                    "A lighting warning only - your keys, dial and joystick are "
                    "unaffected, because macOS lets both apps read the pad at "
                    "once. Only LED writes collide, and FreeMicro repaints as "
                    "soon as ChatGPT quits. To avoid it entirely, run "
                    "`freemicro lights --coexist`: FreeMicro then drives just "
                    "the key backlight, which ChatGPT leaves dark."
                ),
            )
        )
    return items


def build_menu(snap: Snapshot) -> List[MenuItem]:
    """The whole menu, top to bottom. Pure function of ``snap``."""
    items: List[MenuItem] = [
        MenuItem(
            key="state",
            label=state_label(snap.state),
            enabled=False,
            color=state_color(snap.state),
            detail=f"{snap.sessions} live session(s)",
        )
    ]
    if snap.sessions > 1:
        items.append(_info("sessions", f"{snap.sessions} sessions"))

    items.append(_separator("sep.device"))
    items.append(_info("connection", connection_label(snap), snap.unsupported_reason))

    battery = battery_label(snap)
    if battery:
        items.append(_info("battery", battery))
    if snap.firmware:
        items.append(_info("firmware", f"Firmware {snap.firmware}"))
    if snap.owner:
        items.append(
            _info(
                "owner",
                f"Pad driven by {snap.owner}",
                "Only one process can usefully hold the pad, so the menu bar "
                "reads status instead of taking it.",
            )
        )

    warnings = warning_items(snap)
    if warnings:
        items.append(_separator("sep.warnings"))
        items.extend(warnings)

    items.append(_separator("sep.controls"))
    items.append(
        _command(
            "toggle.lighting",
            "LED Control",
            "toggle_lighting",
            enabled=snap.supported,
            checked=snap.lighting_enabled,
            detail=(
                "Let FreeMicro drive the pad's LEDs from agent state. Off by "
                "default - we don't take over your pad uninvited."
            ),
        )
    )
    items.append(
        _command("config", config_label(snap), "open_config", detail=snap.config_path)
    )
    items.append(_command("doctor", "Run Doctor…", "run_doctor"))
    items.append(_separator("sep.quit"))
    items.append(_command("quit", "Quit FreeMicro", "quit"))
    return items


def bar_title(snap: Snapshot) -> BarTitle:
    """The glyph in the menu bar.

    Shape *and* colour both carry the state, so the item still reads correctly
    for someone who cannot tell the amber from the green. The pad's connection
    is deliberately not encoded here: agent state comes from the hook store, not
    the hardware, so it stays true while the pad is asleep - and a badge that
    blinks on every normal Bluetooth drop would be noise.
    """
    return BarTitle(
        text=GLYPH[snap.state],
        color=state_color(snap.state),
        description=f"FreeMicro - {state_label(snap.state).lower()}",
    )


def menu_item(items: List[MenuItem], key: str) -> Optional[MenuItem]:
    """Find one row by key. A convenience for tests and for the Cocoa layer."""
    for item in items:
        if item.key == key:
            return item
    return None


__all__ = [
    "ACTIONS",
    "STALE_AFTER_SECONDS",
    "BarTitle",
    "MenuItem",
    "Snapshot",
    "StaleNote",
    "bar_title",
    "battery_label",
    "build_menu",
    "config_label",
    "connection_label",
    "menu_item",
    "state_color",
    "state_label",
    "transport_label",
    "warning_items",
]
