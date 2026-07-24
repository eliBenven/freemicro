"""Complete, opinionated pad layouts you can apply in one click.

Nobody should have to fill in sixteen inputs by hand to get a useful pad. Each
starter here is a **whole** ``bindings`` map - not a patch - so applying one has
a meaning a person can hold in their head: *the pad now does this.* The browser
diffs it against what you have, shows exactly what would change, and only then
writes it into the in-memory document; nothing reaches disk until you press
Save, and Save keeps the previous file as ``.bak``.

Three rules these definitions must keep, because breaking any of them puts the
UI back to "it accepted my change and then nothing happened":

1. **Everything here must validate.** Only registered action kinds, only fields
   those kinds take, only key names :mod:`freemicro.input.keys` can parse. The
   test suite applies every starter through the real config parser.
2. **The wide cap is settled on both ids.** It fires ``ACT10`` *and* ``ACT11``
   on one press, so :func:`_mic` puts the action on ``ACT10`` and silences
   ``ACT11`` - binding both fires the key twice.
3. **Nothing lies about what is wired.** A starter that would depend on a
   feature the runtime does not read yet declares it in ``unwired`` and the UI
   says so on the card, rather than writing config that silently does nothing.

Dictation is deliberately a *choice* rather than a hardcoded shortcut. The
default the project shipped with - a magic combo plus "now go and configure the
same one inside some other app" - is a bad first run, and it hides the one
detail that actually breaks dictation: hold versus toggle. See
:data:`DICTATION_CHOICES`.
"""

from __future__ import annotations

import copy
from typing import Any, Dict, List

from freemicro.padconfig import FACTORY_RECORDING
from freemicro.webui.layout import KEY_GROUPS

#: The two halves of the wide MIC cap. Named once, used everywhere.
MIC_IDS = KEY_GROUPS[0]

#: How to make the MIC key talk to a dictation app.
#:
#: The hold/toggle distinction is the whole game and it is why this is a choice
#: and not a constant: with ``action: "hold"`` the combo stays **down** for as
#: long as the pad key is held, so the dictation app must be set to
#: push-to-talk. Point that at a *toggle* shortcut instead and dictation starts
#: on the key-down and stops again on the key-up - it looks broken, and the
#: cause is invisible.
DICTATION_CHOICES: List[Dict[str, Any]] = [
    {
        "id": "wispr",
        "label": "Wispr Flow",
        "action": "hold",
        "key": "ctrl+cmd+o",
        # Wispr Flow will not register a shortcut longer than three keys. The
        # four-key combo this project shipped with could therefore *never*
        # fire, and nothing anywhere said so - the mic key simply did nothing.
        "max_keys": 3,
        "summary": "Hold MIC to talk, release to stop.",
        "setup": (
            "In Wispr Flow → Settings → Shortcuts, set the push-to-talk "
            "(hold) shortcut to ctrl+cmd+o - verified working. Flow's own "
            "default is holding fn, which FreeMicro cannot send on its own: "
            "macOS treats fn as a modifier, so there is no fn keystroke to "
            "synthesise."
        ),
        "warning": (
            "It must be Flow's HOLD shortcut, not its toggle: FreeMicro keeps "
            "the combo down while you hold MIC, so a toggle shortcut would "
            "start dictation and immediately stop it again."
        ),
    },
    {
        "id": "macos",
        "label": "macOS Dictation",
        "action": "key",
        "key": "f5",
        # macOS offers its own short list of dictation shortcuts; F5 is one of
        # them, so there is no combo length to police here.
        "max_keys": 0,
        "summary": "Tap MIC to start, tap again to stop.",
        "setup": (
            "In System Settings → Keyboard → Dictation, set the shortcut to "
            "F5. macOS Dictation is a toggle, so MIC taps it once rather than "
            "holding it down."
        ),
        "warning": "",
    },
    {
        "id": "custom",
        "label": "Something else",
        "action": "hold",
        "key": "ctrl+cmd+o",
        "max_keys": 0,
        "summary": "Your own shortcut - pick hold or tap to match the app.",
        "setup": (
            "Type the shortcut your dictation app listens for. Choose Hold if "
            "that app has a push-to-talk shortcut, Tap if it has a toggle."
        ),
        "warning": "",
    },
]

def combo_length(combo: str) -> int:
    """How many keys a combo asks the user's other app to register."""
    return len([p for p in str(combo or "").replace("-", "+").split("+") if p])


def combo_problem(choice_id: str, combo: str) -> str:
    """Why this shortcut will not work with that dictation app, or ``""``.

    A limit that is only discovered months later, when the mic key has quietly
    never worked, is not a limit the software should let you cross.
    """
    for choice in DICTATION_CHOICES:
        if choice["id"] != choice_id:
            continue
        limit = int(choice.get("max_keys") or 0)
        if limit and combo_length(combo) > limit:
            return (
                f"{choice['label']} shortcuts are limited to {limit} keys, and "
                f"“{combo}” is {combo_length(combo)}. It cannot be registered "
                "there, so the key would do nothing."
            )
    return ""


#: The dictation choice a starter uses unless it says otherwise.
DEFAULT_DICTATION = "wispr"


def mic_light() -> Dict[str, Any]:
    """What the pad shows while the mic key is held: the vendor's own look.

    ``docs/FACTORY-DEFAULTS.md`` §1b - the ChatGPT app drives the underglow
    ``#2E8B57``, snake, speed 0.4 while its voice state is ``recording``. Same
    principle as the five state colours: the opt-in should look like the pad you
    bought. The underglow, not the Agent Keys, because those are carrying one
    project each and that is exactly what you still want to see while you talk.
    """
    from freemicro.padconfig import factory_recording_light

    light = factory_recording_light()
    return {
        "color": FACTORY_RECORDING,
        "effect": "snake",
        "brightness": light.brightness,
        "speed": light.speed,
        "zones": list(light.zones),
    }


def dictation_binding(choice_id: str = DEFAULT_DICTATION) -> Dict[str, Any]:
    """The MIC binding for one dictation choice, ready to write to both ids.

    A ``hold`` choice gets the recording light; a toggle choice does **not**,
    and that omission is the feature. FreeMicro sees a toggle's key-down and
    never learns that dictation stopped, so a light there would go out while the
    mic was still live - a pad that has quietly started lying. Better nothing
    than something wrong about a microphone.
    """
    for choice in DICTATION_CHOICES:
        if choice["id"] == choice_id:
            binding: Dict[str, Any] = {
                "action": choice["action"],
                "key": choice["key"],
                "label": "mic - push to talk"
                if choice["action"] == "hold"
                else "mic - dictation",
            }
            if choice["action"] == "hold":
                binding["light"] = mic_light()
            return binding
    return dictation_binding(DEFAULT_DICTATION)


def _mic(choice_id: str = DEFAULT_DICTATION) -> Dict[str, Dict[str, Any]]:
    """The wide cap: the action on ``ACT10``, and ``ACT11`` explicitly silent.

    The cap spans two switches and the pad reports **both** ids on every press.
    The factory addresses the pair as ``ACT10_ACT11`` and discards the ``ACT11``
    half - and so must we: binding both makes one press fire the action twice,
    which for a push-to-talk hold means the combo goes down and up again under
    your finger. So the second half is bound to ``none``, deliberately and
    visibly, rather than left to whatever it happened to hold.
    """
    binding = dictation_binding(choice_id)
    binding["comment"] = (
        "The wide MIC cap spans two switches (ACT10 + ACT11). The pad reports "
        "both on every press, so only this half acts and ACT11 is silenced."
    )
    silent = {
        "action": "none",
        "label": "mic (second half)",
        "comment": "The other half of the wide MIC cap. Silenced on purpose: "
                   "acting on both would fire the key twice per press.",
    }
    return {MIC_IDS[0]: binding, MIC_IDS[1]: copy.deepcopy(silent)}


def _text(text: str, label: str, submit: bool = True) -> Dict[str, Any]:
    return {"action": "text", "text": text, "submit": submit, "label": label}


def _key(combo: str, label: str) -> Dict[str, Any]:
    return {"action": "key", "key": combo, "label": label}


def _app(name: str, label: str) -> Dict[str, Any]:
    return {"action": "app", "name": name, "cycle": True, "label": label}


def _agent_keys() -> Dict[str, Dict[str, Any]]:
    """Six keys, six projects. This is the whole point of the pad.

    Each Agent Key follows one project directory: its colour is that project's
    state - blue thinking, amber waiting on you, green just finished, dark for
    an empty slot - and pressing it brings that project's terminal to the
    front. Glance to see what needs you, press to go there.

    Not slash commands. Six keys that all type into whatever happens to be
    focused is six copies of one keyboard shortcut; six keys that each *stand
    for a project* is a status display you can press. It is also the thing the
    vendor app cannot do for Claude Code.
    """
    return {
        f"AG0{index}": {"action": "focus_session", "label": f"agent {index + 1}"}
        for index in range(6)
    }


def _pointer() -> Dict[str, Dict[str, Any]]:
    """Dial and stick as they are actually used: effort, and the mouse.

    The stick is an analogue thumbstick under your left hand - moving the
    pointer with it is worth far more than four more keyboard shortcuts, and
    the dial press is the click that completes it.
    """
    return {
        "ENC_CLK": {"action": "mouse", "click": "left", "label": "dial press - click"},
        "ENC_CW": _text("/effort up", "effort +"),
        "ENC_CC": _text("/effort down", "effort -"),
        "JOY_UP": {"action": "mouse", "y": -40, "label": "pointer up"},
        "JOY_DOWN": {"action": "mouse", "y": 40, "label": "pointer down"},
        "JOY_LEFT": {"action": "mouse", "x": -40, "label": "pointer left"},
        "JOY_RIGHT": {"action": "mouse", "x": 40, "label": "pointer right"},
    }


def _essentials() -> Dict[str, Dict[str, Any]]:
    bindings: Dict[str, Dict[str, Any]] = {
        "ACT06": _text(
            "/review this session's work in depth: what changed, what is "
            "risky, what is untested",
            "lab - deep review",
        ),
        "ACT07": _text(
            "Open a pull request for this branch. Review the diff first and "
            "tell me anything that should block it.",
            "pr - open a pull request",
        ),
        "ACT08": _app("Google Chrome", "nav - browser"),
        "ACT09": _text("continue", "play - continue"),
        "ACT12": _app("Terminal", "term - focus Terminal"),
    }
    bindings.update(_agent_keys())
    bindings.update(_mic())
    bindings.update(_pointer())
    return bindings


def _dictation_first() -> Dict[str, Dict[str, Any]]:
    bindings: Dict[str, Dict[str, Any]] = {
        "ACT09": _key("return", "play - send"),
        "ACT12": _app("Terminal", "term - focus Terminal"),
    }
    bindings.update(_agent_keys())
    bindings.update(_mic())
    bindings.update(_pointer())
    return bindings


def _git() -> Dict[str, Dict[str, Any]]:
    bindings: Dict[str, Dict[str, Any]] = {
        "ACT06": _text(
            "Review the staged diff and tell me anything that should block "
            "this commit.",
            "lab - review the diff",
        ),
        "ACT07": {
            "action": "shell",
            "command": "gh pr create --fill",
            "label": "pr - open a pull request",
            "comment": "Needs the GitHub CLI (`gh`) on your PATH.",
        },
        "ACT08": {
            "action": "shell",
            "command": "gh pr view --web",
            "label": "nav - this branch's PR",
            "comment": "Opens the current branch's pull request in a browser.",
        },
        "ACT09": _text("continue", "play - continue"),
        "ACT12": _app("Terminal", "term - focus Terminal"),
    }
    bindings.update(_agent_keys())
    bindings.update(_mic())
    bindings.update(_pointer())
    return bindings


def _minimal() -> Dict[str, Dict[str, Any]]:
    bindings: Dict[str, Dict[str, Any]] = {
        "ACT09": _text("continue", "play - continue"),
        "ACT12": _app("Terminal", "term - focus Terminal"),
    }
    bindings.update(_agent_keys())
    return bindings


#: The starters themselves. ``bindings`` is the complete map the pad ends up
#: with; anything not listed becomes unbound.
STARTERS: List[Dict[str, Any]] = [
    {
        "id": "essentials",
        "name": "Claude Code essentials",
        "tagline": "The sensible default. Start here if you are not sure.",
        "who": (
            "The six Agent Keys each follow one project: the colour is that "
            "project's state, and pressing the key jumps to its terminal. "
            "PLAY types “continue” to unstick a stalled agent - the most-used "
            "key on the pad. LAB and PR send prompts you would otherwise type "
            "out. NAV and TERM switch application and cycle their windows. MIC "
            "is hold-to-talk. The dial changes reasoning effort and clicks; "
            "the stick moves the mouse pointer."
        ),
        "bindings": _essentials(),
        "dictation": DEFAULT_DICTATION,
        "unwired": [],
    },
    {
        "id": "dictation",
        "name": "Dictation-first",
        "tagline": "Talk, don't type. The wide MIC cap does the work.",
        "who": (
            "MIC is hold-to-talk, the stick moves the mouse, the Agent Keys "
            "still follow your six projects, and almost nothing types on your "
            "behalf."
        ),
        "bindings": _dictation_first(),
        "dictation": DEFAULT_DICTATION,
        "unwired": [],
    },
    {
        "id": "git",
        "name": "Git workflow",
        "tagline": "Status, diff, branch, PR - without leaving the keyboard.",
        "who": (
            "LAB reviews the staged diff, PR and NAV shell out to the GitHub "
            "CLI, and the Agent Keys still do what they are for: one project "
            "each, pressed to jump to its terminal."
        ),
        "bindings": _git(),
        "dictation": DEFAULT_DICTATION,
        "unwired": [],
        "requires": ["The GitHub CLI (`gh`) for the PR and NAV keys."],
    },
    {
        "id": "minimal",
        "name": "Minimal",
        "tagline": "Almost everything unbound. Build it up yourself.",
        "who": (
            "The Agent Keys still follow your projects - that is the hardware "
            "working, not a binding - plus PLAY and TERM. Nothing else on the "
            "pad will type anything you did not ask it to."
        ),
        "bindings": _minimal(),
        "dictation": "",
        "unwired": [],
    },
]


def starters() -> List[Dict[str, Any]]:
    """The starter list, deep-copied so a caller cannot mutate the table."""
    return copy.deepcopy(STARTERS)


def find(starter_id: str) -> Dict[str, Any]:
    """One starter by id. Raises :class:`KeyError` if there is no such thing."""
    for starter in STARTERS:
        if starter["id"] == starter_id:
            return copy.deepcopy(starter)
    raise KeyError(starter_id)


def apply_to(document: Dict[str, Any], starter_id: str) -> Dict[str, Any]:
    """Return a copy of ``document`` with ``starter_id``'s bindings in it.

    Only ``bindings`` is replaced. Lighting, joystick geometry, comments and
    every key this build has never heard of are left exactly as they were - 
    picking a starter layout must never quietly reset someone's colours.
    """
    starter = find(starter_id)
    updated = copy.deepcopy(document)
    updated["bindings"] = starter["bindings"]
    return updated


__all__ = [
    "DEFAULT_DICTATION",
    "combo_length",
    "combo_problem",
    "DICTATION_CHOICES",
    "MIC_IDS",
    "STARTERS",
    "apply_to",
    "dictation_binding",
    "find",
    "starters",
]
