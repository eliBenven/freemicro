"""The physical shape of the pad, as data the browser can draw.

The whole point of this UI is that you point at the key you want to change
instead of remembering that ``ACT09`` is the one with the play triangle on it.
That only works if the picture matches the object on your desk, so the layout
lives here as data - one place to correct when someone's unit differs - rather
than being hard-coded in the page.

The real arrangement is a **4x4 grid**, not rows of identical keycaps:

===========  ===========  ===========  ===========
dial (◯)     AG00         AG01         stick (●)
AG02         AG03         AG04         AG05
ACT06 LAB    ACT07 PR     ACT08 NAV    ACT09 PLAY
haptic (◯)   MIC - double width        ACT12 TERM
             ACT10 + ACT11
===========  ===========  ===========  ===========

Three details that are easy to get wrong and expensive to get wrong. All three
were wrong here until they were checked against the unit on the desk:

* **The two round controls sit in the top row's corners** - the rotary dial at
  top-left (three ids: ``ENC_CW``/``ENC_CLK``/``ENC_CC``) and the analogue
  thumbstick at top-right (``v.oai.rad``, resolved into four flicks). They look
  nothing like the keycaps and must not be drawn as squares.
* **``MIC`` is a double-width cap over two switches.** It fires **both**
  ``ACT10`` *and* ``ACT11`` on every press - not one or the other. So the only
  correct way to bind it is to write the *same* binding to both ids, which is
  what :data:`PAIRED_INPUTS` tells the editor to do. Bind only one half and the
  other half fires whatever it still held, which is how "pressing MIC also does
  something else" happens.
* **The bottom-left round control is not a key at all.** It is a haptic pad
  (tap + circle) that switches the pad's **Bluetooth host profile** between
  three slots - the pad advertises as ``Codex Micro #1/#2/#3`` and
  ``device.status.profile_index`` reports the active one, zero-indexed. Three
  small white LEDs beside it show which slot is live. It emits only standard
  HID (reportID 1 keyboard, 2 consumer) and **never** ``v.oai.hid``, so
  FreeMicro cannot see it, cannot bind it and must not offer to. It is drawn,
  labelled and explained - and nothing more. ``ACT12`` is the separate narrow
  TERM key to the *right* of MIC.

**Only the six Agent Keys have individual RGB.** They are frosted and lit
per key over ``v.oai.thstatus``. The action keys are tan and opaque with no LED
of their own; everything else you see glowing is the *global* backlight and
underglow, which take one colour each over ``lights.preview``. The UI therefore
offers per-key colour on the Agent Keys and global colour on the two zones, and
offers nothing at all on the action keys - a colour control that cannot change
anything is worse than no control.
"""

from __future__ import annotations

from typing import Any, Dict, List

from freemicro.device.lighting import AGENT_KEY_COUNT, EFFECTS
from freemicro.padconfig import ACTION_KEYS, AGENT_KEYS, JOYSTICK_INPUTS

#: The keycap that came on each switch position **in the box**
#: (``docs/FACTORY-DEFAULTS.md`` §7, shipped layout, Confirmed).
#:
#: This is a *default*, not a fact about anybody's pad. The caps are physically
#: interchangeable - the pad ships with a tray of them and people rearrange them
#: freely - so which glyph sits on which position is **user data**, stored in the
#: config's ``keycaps`` section and edited with the picker. All this table does
#: is make a pad nobody has configured yet look like the one they unboxed.
FACTORY_KEYCAPS: Dict[str, str] = {
    "ACT06": "FAST",
    "ACT07": "APPR",
    "ACT08": "REJ",
    "ACT09": "SPLIT",
    "ACT10": "MIC",
    "ACT11": "MIC",
    "ACT12": "CODEX",
}

#: Ids that occupy two key units of width. See the module docstring.
WIDE_KEYS = ("ACT10",)

#: Ids drawn as a round cap rather than a rectangle. None: the only round thing
#: on the bottom row is the haptic profile control, which is not an input.
ROUND_KEYS: tuple = ()

#: Caps that physically sit over more than one switch, so the pad reports every
#: id in the group on a single press. Editing any member must write the same
#: binding to all of them - see :func:`paired_ids`.
KEY_GROUPS: tuple = (("ACT10", "ACT11"),)

#: ``id -> every id its keycap fires``. Flattened once so both the API and the
#: tests read the same table rather than re-deriving the pairing.
PAIRED_INPUTS: Dict[str, List[str]] = {
    member: list(group) for group in KEY_GROUPS for member in group
}


def paired_ids(input_id: str) -> List[str]:
    """Every id that fires when the cap carrying ``input_id`` is pressed."""
    return list(PAIRED_INPUTS.get(input_id, [input_id]))


#: Controls that are drawn on the diagram but are **not** FreeMicro inputs: the
#: firmware owns them end to end and never reports them over ``v.oai.hid``.
#: Rendering them is not decoration - leaving them off the picture is how a user
#: concludes the UI is broken when the thing they pressed does nothing.
FIRMWARE_CONTROLS: List[Dict[str, Any]] = [
    {
        "cell": "control",
        "control": "profile",
        "keycap": "BT",
        "label": "Bluetooth host profile",
        "span": 1,
        "round": True,
        "bindable": False,
        "leds": 3,
        "note": (
            "Haptic pad - tap it, or circle it, to switch which of the pad's "
            "three Bluetooth host profiles is live. The pad advertises as "
            "Codex Micro #1, #2 and #3, and the three small white LEDs beside "
            "this control show which slot is active. It is firmware-owned: it "
            "emits only standard HID reports (keyboard and consumer), never "
            "v.oai.hid, so FreeMicro cannot see it and cannot bind it. There "
            "is nothing to configure here - that is the hardware, not a "
            "missing feature."
        ),
    }
]

#: How many inputs have their own addressable LED, and which. Everything else
#: is lit - if at all - by a global zone.
LIT_INPUTS = AGENT_KEYS

#: Human-readable notes shown under an input when it is selected.
NOTES: Dict[str, str] = {
    "ENC_CW": (
        "One event per detent, with no matching release. The bridge fires "
        "rotation on any act value, so a fast spin sends several in a row."
    ),
    "ENC_CC": (
        "One event per detent, with no matching release. The bridge fires "
        "rotation on any act value, so a fast spin sends several in a row."
    ),
    "ENC_CLK": "Press and release, like any other key.",
    "ACT10": (
        "The double-width position. One wide cap sits over two switches, so "
        "the pad fires ACT10 and ACT11 together on every press - FreeMicro "
        "writes what you set here to both, so the halves cannot disagree. "
        "Only the wide caps (MIC, EMPT5) physically fit here."
    ),
    "ACT12": (
        "The single-width position to the right of the wide cap. Its own "
        "switch; nothing else fires with it. The factory ships a CODEX cap "
        "here, but caps are swappable - tell FreeMicro which one you fitted."
    ),
}
NOTES["ACT11"] = NOTES["ACT10"]
for _joy in JOYSTICK_INPUTS:
    NOTES[_joy] = (
        "Synthesised from the analogue stick, not a physical switch. Which "
        "flick fires is decided by joystick.deadzone and joystick.origin."
    )

#: How the six Agent Keys pick which **project** they follow. A key stands for
#: a project *directory*, not a session id - terminal tabs come and go, the repo
#: does not (see :mod:`freemicro.agentkeys`, which owns this model). The help
#: text is the UI's; the policy names come from the runtime so the two cannot
#: drift apart.
AGENT_POLICY_HELP: Dict[str, Dict[str, str]] = {
    "recent": {
        "label": "Most recent",
        "help": "The six most recently active projects fill the keys "
                "automatically, newest first. Slots are sticky: a project "
                "keeps its key for as long as it is live. Nothing to set up.",
    },
    "pinned": {
        "label": "Pinned first",
        "help": "Projects you pin below keep their key permanently - a pinned "
                "key is never lent to anything else, and stays dark when that "
                "project is quiet. Unpinned keys fill from 'most recent'.",
    },
    "manual": {
        "label": "Manual",
        "help": "Only the projects you name light up. Any key you leave empty "
                "stays dark, like the factory's 'no assigned agent'.",
    },
    "mirror": {
        "label": "Mirror one state",
        "help": "The old behaviour: all six keys show the single winning "
                "state. Six copies of one status light.",
    },
}


def agent_policies() -> List[Dict[str, str]]:
    """The policies the runtime actually accepts, with UI prose attached."""
    from freemicro.agentkeys import POLICIES

    out: List[Dict[str, str]] = []
    for value in POLICIES:
        text = AGENT_POLICY_HELP.get(value, {})
        out.append(
            {
                "value": value,
                "label": text.get("label", value),
                "help": text.get("help", ""),
            }
        )
    return out

#: The config section this UI writes for the six slots. Documented here so the
#: shape has one definition rather than being implied by the JavaScript.
AGENT_SECTION = "agent_keys"


def _input(input_id: str, keycap: str, position: str = "") -> Dict[str, Any]:
    entry: Dict[str, Any] = {
        "id": input_id,
        "ids": paired_ids(input_id),
        "keycap": keycap,
        "note": NOTES.get(input_id, ""),
        "bindable": True,
    }
    if position:
        entry["position"] = position
    return entry


def _key_cell(input_id: str, kind: str, keycap: str, slot: int = -1) -> Dict[str, Any]:
    cell = _input(input_id, keycap)
    # The cap that shipped on this position. The browser draws the user's own
    # arrangement when they have one, and falls back to this.
    cell["factory_cap"] = FACTORY_KEYCAPS.get(input_id, "")
    cell["cell"] = kind
    cell["span"] = 2 if input_id in WIDE_KEYS else 1
    cell["round"] = input_id in ROUND_KEYS
    cell["lit"] = input_id in LIT_INPUTS
    cell["bindable"] = True
    # Every id this one cap fires. One entry for an ordinary key; two for MIC.
    cell["ids"] = paired_ids(input_id)
    if slot >= 0:
        cell["slot"] = slot
    return cell


def pad_layout() -> Dict[str, Any]:
    """The drawing instructions for the pad diagram, in grid order.

    Cells are emitted row by row and simply flow into a four-column grid, so a
    unit with a different arrangement is one edit to this function.
    """
    agent = list(AGENT_KEYS)
    action = {input_id: input_id for input_id in ACTION_KEYS}
    cells: List[Dict[str, Any]] = [
        {
            "cell": "dial",
            "label": "dial",
            "span": 1,
            "inputs": [
                _input("ENC_CW", "CW", "n"),
                _input("ENC_CLK", "push", "c"),
                _input("ENC_CC", "CCW", "s"),
            ],
        },
        _key_cell(agent[0], "agent", "AG1", slot=0),
        _key_cell(agent[1], "agent", "AG2", slot=1),
        {
            "cell": "stick",
            "label": "stick",
            "span": 1,
            "inputs": [
                _input("JOY_UP", "up", "n"),
                _input("JOY_RIGHT", "right", "e"),
                _input("JOY_DOWN", "down", "s"),
                _input("JOY_LEFT", "left", "w"),
            ],
        },
        _key_cell(agent[2], "agent", "AG3", slot=2),
        _key_cell(agent[3], "agent", "AG4", slot=3),
        _key_cell(agent[4], "agent", "AG5", slot=4),
        _key_cell(agent[5], "agent", "AG6", slot=5),
    ]
    for input_id in ("ACT06", "ACT07", "ACT08", "ACT09"):
        cells.append(_key_cell(action[input_id], "action", input_id))
    # Bottom row, confirmed on hardware: the firmware-owned haptic profile
    # control, the double-width MIC over ACT10+ACT11, then ACT12 = TERM.
    cells.append(dict(FIRMWARE_CONTROLS[0]))
    cells.append(_key_cell(action["ACT10"], "action", "ACT10+ACT11"))
    cells.append(_key_cell(action["ACT12"], "action", "ACT12"))
    return {
        "columns": 4,
        "cells": cells,
        "agent_slots": AGENT_KEY_COUNT,
        "paired": PAIRED_INPUTS,
    }


def effect_choices() -> List[Dict[str, Any]]:
    """Effect ids in firmware order, named the way the config spells them."""
    return [
        {"value": number, "name": name}
        for name, number in sorted(EFFECTS.items(), key=lambda item: item[1])
    ]


#: The factory palette, offered as one-click presets. Values are the exact
#: integers recorded in ``docs/FACTORY-DEFAULTS.md`` §1a - the point of the
#: preset is that picking it makes the pad look like the one you bought.
FACTORY_PRESETS: List[Dict[str, Any]] = [
    {"state": "idle", "label": "Idle", "hex": "#FFFFFF", "vendor": "White - Idle"},
    {
        "state": "working",
        "label": "Working",
        "hex": "#304FFE",
        "vendor": "Blue - Thinking",
    },
    {
        "state": "done",
        "label": "Done",
        "hex": "#00FF4C",
        "vendor": "Green - Complete (vendor 'unread')",
    },
    {
        "state": "waiting",
        "label": "Waiting",
        "hex": "#FF6D00",
        "vendor": "Amber - Requires input",
    },
    {"state": "error", "label": "Error", "hex": "#FF0033", "vendor": "Red - Error"},
]

#: Per-input-field widget hints. Derived from the registry's field names, never
#: a second source of truth for *which* fields exist - that stays in
#: ``freemicro.input.actions``.
FIELD_HINTS: Dict[str, Dict[str, Any]] = {
    "text": {"widget": "textarea", "placeholder": "/resume"},
    "submit": {"widget": "boolean", "help": "Press Return afterwards"},
    "key": {"widget": "combo", "placeholder": "cmd+shift+k"},
    "command": {"widget": "textarea", "placeholder": "gh pr create --fill"},
    "cwd": {"widget": "text", "placeholder": "~/code/project"},
    "wait": {"widget": "boolean", "help": "Block until the command exits"},
    "script": {"widget": "textarea", "placeholder": 'display notification "hi"'},
    # "name" belongs to the `app` action and must be an application's exact
    # name. Typing it by hand is the single easiest thing to get wrong in this
    # whole config, so the browser gets a picker of what is actually installed.
    "name": {
        "widget": "app",
        "placeholder": "Terminal",
        "help": "Pick an app that is actually installed - exact name, no “.app”.",
    },
    "cycle": {"widget": "boolean", "help": "Cycle windows if already frontmost"},
    "x": {"widget": "number", "step": 1},
    "y": {"widget": "number", "step": 1},
    "absolute": {"widget": "boolean", "help": "Treat x/y as screen coordinates"},
    "click": {"widget": "choice", "choices": ["", "left", "right", "middle"]},
    "count": {"widget": "number", "step": 1, "min": 1},
    # `answer_permission`. Free text here would be four boxes where three of
    # the four answers are a fixed vocabulary, and a typo in `answer` is a key
    # that silently does nothing on the one press you most need to work.
    "answer": {
        "widget": "choice",
        "choices": ["", "approve", "reject", "always"],
        "help": "What a press means. `always` also stops it asking again.",
    },
    "long_press": {
        "widget": "choice",
        "choices": ["", "approve", "reject", "always"],
        "help": "What a HOLD means instead. Leave blank for no long press.",
    },
    "long_press_ms": {"widget": "number", "step": 50, "min": 100},
    "max_age": {
        "widget": "number",
        "step": 30,
        "min": 0,
        "help": "Refuse to answer a prompt older than this. 0 disables.",
    },
    # Shared by `answer_permission` and `focus_session`: nail a key to one repo
    # instead of letting the slot policy decide.
    "project": {"widget": "text", "placeholder": "~/code/project"},
}

__all__ = [
    "AGENT_POLICY_HELP",
    "AGENT_SECTION",
    "FACTORY_PRESETS",
    "FIELD_HINTS",
    "FIRMWARE_CONTROLS",
    "FACTORY_KEYCAPS",
    "KEY_GROUPS",
    "LIT_INPUTS",
    "NOTES",
    "PAIRED_INPUTS",
    "ROUND_KEYS",
    "WIDE_KEYS",
    "agent_policies",
    "effect_choices",
    "pad_layout",
    "paired_ids",
]
