"""The user-editable pad configuration: every binding, and every LED colour.

Why this file exists at all: a macro pad is only useful if *you* decide what its
keys do. Hard-coding a keymap in Python means every user has to fork the project
to change one key. So the whole layout - all thirteen keys, the dial press, the
joystick flicks, and the five state colours - lives in one JSON document that
``freemicro keys --init`` drops in your home directory.

Why JSON and not TOML
---------------------
FreeMicro's core is **dependency-free on Python 3.9**, and ``tomllib`` only
arrived in 3.11 - a TOML config would force a ``tomli`` dependency on exactly
the users least likely to want one. JSON is in the standard library everywhere,
it is already the format of ``config.json``, ``capabilities.json`` and
``presets/``, and it round-trips losslessly for ``--init``. To buy back the
readability TOML would have given us, the shipped default is heavily annotated
(``_readme`` and per-binding ``comment`` fields, both ignored by the loader) so
nobody ever starts from a blank file.

Validation is strict about *actions* and forgiving about *input ids*: a
misspelled action field is a hard error (you want to know now), while an
unrecognised input id is only a warning, so a future firmware revision that adds
a key can be bound today without a FreeMicro release.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from freemicro.agentkeys import (
    POLICY_MANUAL,
    POLICY_PINNED,
    AgentKeysConfig,
    AgentKeysError,
    parse_agent_keys,
)
from freemicro.config import config_home
from freemicro.device.lighting import (
    ZONE_AGENT_KEYS,
    ZONE_BACKLIGHT,
    ZONE_UNDERGLOW,
    LightingError,
    color_to_hex,
    effect_name,
    parse_color,
    parse_effect,
    parse_zone,
    preview_zone,
)
from freemicro.input.actions import (
    FOCUS_SESSION,
    HOLD_KINDS,
    Action,
    validate_params,
)
from freemicro.state.engine import AgentState

#: Environment variable that overrides where the config is read from.
ENV_PATH = "FREEMICRO_KEYMAP"

#: Filename used in every search location.
FILENAME = "keymap.json"

#: The shipped default, and the file ``--init`` copies.
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent / "default_keymap.json"

#: Inputs the hardware is known to report, in physical order.
AGENT_KEYS: Tuple[str, ...] = tuple(f"AG{i:02d}" for i in range(6))
ACTION_KEYS: Tuple[str, ...] = tuple(f"ACT{i:02d}" for i in range(6, 13))
#: Joystick flicks, listed in *angular* order: index 0 sits at angle 0 and each
#: step is a quarter turn. The order matters - it is the default wheel.
#:
#: Right, **down**, left, up - which is what the hardware actually reports.
#: ``docs/FACTORY-DEFAULTS.md`` §6 records the vendor firmware's sector table
#: as right at 0.0, down at 0.25, left at 0.5 and up at 0.75, confirmed from
#: the shipped binary. Taking ``(cos, sin)`` of ``2*pi*a`` straight as screen
#: coordinates gives exactly that, because macOS y grows *downward*.
#:
#: This list used to read right/up/left/down, an unchecked assumption of
#: maths-convention (y-up) axes, so a stick pushed down fired ``JOY_UP``. That
#: was survivable while flicks were the only mode; it is not survivable next to
#: :mod:`freemicro.input.pointer`, which moves the cursor the way the stick
#: actually went. One stick cannot mean two opposite things depending on
#: ``joystick.mode``.
JOYSTICK_INPUTS: Tuple[str, ...] = ("JOY_RIGHT", "JOY_DOWN", "JOY_LEFT", "JOY_UP")

#: The order this project shipped before the correction above. A config
#: written against it still parses and still works - it just has up and down
#: swapped - so it earns a warning rather than an error. Silence would be
#: worse: the failure mode is a stick that does the opposite of what it says.
_LEGACY_JOYSTICK_INPUTS: Tuple[str, ...] = (
    "JOY_RIGHT", "JOY_UP", "JOY_LEFT", "JOY_DOWN"
)
#: The dial: press, plus one event per detent of rotation. All three arrive
#: through ``v.oai.hid`` exactly like the keys.
ENCODER_INPUTS: Tuple[str, ...] = ("ENC_CLK", "ENC_CW", "ENC_CC")
#: The rotation ticks specifically. They are momentary - one event per detent,
#: with no matching release - so the bridge fires them on any ``act`` value.
ENCODER_TICKS: Tuple[str, ...] = ("ENC_CW", "ENC_CC")
KNOWN_INPUTS: Tuple[str, ...] = (
    AGENT_KEYS + ACTION_KEYS + ENCODER_INPUTS + JOYSTICK_INPUTS
)

#: Per-binding keys that describe the binding rather than parameterise it.
#:
#: ``light`` is here rather than in any action kind's ``optional`` list on
#: purpose: what the pad *shows* while a binding is live is a property of the
#: binding, not a parameter of what it does. Putting it here means every kind
#: gets it at once, including kinds added after this was written, and no kind
#: has to remember to declare it.
_META_FIELDS = ("action", "label", "comment", "light")

# ---------------------------------------------------------------------------
# Chords
# ---------------------------------------------------------------------------

#: What turns a binding key into a chord: ``"AG00+AG01"``.
CHORD_SEPARATOR = "+"

#: How many keys one chord may name.
#:
#: **Two, deliberately.** Not because three fingers is hard, but because a third
#: key would have to be *waited for*: on seeing AG00+AG01 the bridge could not
#: fire until it knew AG02 was not coming, which means a second settle window on
#: top of the first, paid by every two-key chord. Capping at two keeps the
#: resolution rule a single sentence and the latency a single window. Thirteen
#: keys already give seventy-eight pairs.
CHORD_MAX_KEYS = 2

#: Inputs that can never be part of a chord. A chord needs two keys held down at
#: the same time, and these report a single momentary event with no release, so
#: there is no "at the same time" for them to be in.
_MOMENTARY_INPUTS = frozenset(ENCODER_TICKS + JOYSTICK_INPUTS)

#: How long a chord-capable key's own binding waits to see whether a second key
#: is joining it, in milliseconds.
#:
#: Two fingers a human means as one press land within ~30 ms of each other; 45
#: gives that headroom without being felt. **This is only ever paid by a key
#: that both has a binding of its own and appears in a chord** - see
#: :class:`freemicro.input.bridge.Bridge`. 0 disables deferring entirely.
DEFAULT_CHORD_SETTLE_MS = 45.0

#: Above this a press stops feeling like a press. Guards a fat-fingered edit.
CHORD_SETTLE_MS_MAX = 500.0

_CHORD_FIELDS = ("settle_ms", "comment")


def is_chord_id(input_id: str) -> bool:
    """Whether a binding key names a chord rather than a single input."""
    return CHORD_SEPARATOR in input_id


def chord_key(members: Sequence[str]) -> Tuple[str, ...]:
    """The canonical form of a chord: its members, sorted.

    Sorting is what makes ``"AG00+AG01"`` and ``"AG01+AG00"`` the same chord.
    Two keys pressed together have no order the hardware can be trusted to
    report consistently, so the config must not pretend they do.
    """
    return tuple(sorted(members))


def chord_label(members: Sequence[str]) -> str:
    """The canonical chord written back out, for logs and dispatch ids."""
    return CHORD_SEPARATOR.join(chord_key(members))


def parse_chord_id(input_id: str) -> Tuple[str, ...]:
    """Validate a chord binding key and return its canonical member tuple."""
    members = [part.strip() for part in input_id.split(CHORD_SEPARATOR)]
    if any(not part for part in members):
        raise PadConfigError(
            f"binding {input_id!r} names an empty key; write a chord as "
            "\"AG00+AG01\""
        )
    if len(members) > CHORD_MAX_KEYS:
        raise PadConfigError(
            f"binding {input_id!r} names {len(members)} keys; FreeMicro binds "
            f"chords of {CHORD_MAX_KEYS} keys only, so that a two-key chord "
            "never has to wait to find out whether a third is coming"
        )
    if len(set(members)) != len(members):
        raise PadConfigError(
            f"binding {input_id!r} names the same key twice; a chord needs two "
            "different keys"
        )
    momentary = [m for m in members if m in _MOMENTARY_INPUTS]
    if momentary:
        raise PadConfigError(
            f"binding {input_id!r} cannot use {', '.join(momentary)}: dial "
            "detents and joystick flicks are momentary - they report one event "
            "and no release - so they can never be held alongside another key"
        )
    return chord_key(members)


def _parse_chord_settle(raw: Any) -> float:
    """Parse the ``chords`` section. Absent means :data:`DEFAULT_CHORD_SETTLE_MS`."""
    if raw is None:
        return DEFAULT_CHORD_SETTLE_MS
    if not isinstance(raw, dict):
        raise PadConfigError("\"chords\" must be an object")
    unknown = {k for k in raw if not k.startswith("_")} - set(_CHORD_FIELDS)
    if unknown:
        raise PadConfigError(
            f"\"chords\" has unknown field(s): {', '.join(sorted(unknown))}; "
            f"it takes: {', '.join(_CHORD_FIELDS)}"
        )
    if "settle_ms" not in raw:
        return DEFAULT_CHORD_SETTLE_MS
    try:
        settle = float(raw["settle_ms"])
    except (TypeError, ValueError):
        raise PadConfigError(
            f"\"chords.settle_ms\" must be a number of milliseconds, got "
            f"{raw['settle_ms']!r}"
        ) from None
    if not 0.0 <= settle <= CHORD_SETTLE_MS_MAX:
        raise PadConfigError(
            f"\"chords.settle_ms\" must be between 0 (never wait) and "
            f"{CHORD_SETTLE_MS_MAX:g}; anything longer stops feeling like a "
            "key press"
        )
    return settle

_EXIT_MODES = ("leave", "off", "breath")

#: Which protocol method the LED renderer uses for the backlight/underglow.
#:
#: ``rgbcfg`` is the default and the only one that works. Verified on hardware:
#: ``v.oai.rgbcfg`` visibly drives the underglow and the key backlight, which is
#: also what the vendor app uses for all lighting. ``lights.preview`` is in the
#: firmware's method table and replies ``{"result": null}``, but produces **no
#: visible change** on firmware v0.4.1 over USB or Bluetooth - an earlier
#: conclusion to the contrary predated the BLE framing fix and was wrong. It is
#: kept selectable for debugging and for a firmware that may one day honour it;
#: do not expect it to light anything today. See ``docs/PROTOCOL.md``.
LIGHTING_METHODS = ("rgbcfg", "preview")

#: Joystick modes. ``pointer`` is analogue velocity control - deflection sets
#: the cursor's *speed*, the way a ThinkPad TrackPoint does. ``directions``
#: is the older behaviour: one discrete, bindable flick per deflection.
MODE_POINTER = "pointer"
MODE_DIRECTIONS = "directions"
JOYSTICK_MODES = (MODE_POINTER, MODE_DIRECTIONS)

#: Sane bounds for ``joystick.tick_hz``. Below the floor the cursor visibly
#: steps; above the ceiling you are only burning wakeups, since no display
#: shows more frames than that and the pad samples far slower.
TICK_HZ_MIN = 20.0
TICK_HZ_MAX = 480.0

#: Every field ``joystick`` accepts. Anything else is a typo, and a typo in a
#: tuning value is exactly the kind of thing that otherwise gets silently
#: ignored while the user wonders why their edit did nothing.
_JOYSTICK_FIELDS = (
    "mode", "deadzone", "origin", "directions", "pointer_deadzone",
    "max_speed", "gamma", "tick_hz", "precision_key", "precision_scale",
    "invert_y", "comment",
)


class PadConfigError(ValueError):
    """Raised for a config file we refuse to run with. Always actionable."""


# ---------------------------------------------------------------------------
# Lighting
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class StateLight:
    """How the pad should look in one agent state."""

    color: int
    effect: int
    brightness: float
    speed: float
    magic: Optional[float] = None

    def to_zone(self) -> Dict[str, Any]:
        """The ``lights.preview`` zone object for this state."""
        return preview_zone(
            self.color, self.effect, self.brightness, self.speed, self.magic
        )

    def describe(self) -> str:
        return (
            f"{color_to_hex(self.color)} {effect_name(self.effect)} "
            f"b={self.brightness:g} s={self.speed:g}"
        )


#: The factory's state colours, and **the only definition of them in Python**.
#:
#: ``docs/FACTORY-DEFAULTS.md`` §1a records these as the exact values the vendor
#: app sends, and ``docs/CUSTOMIZING.md`` promises that a state you leave out of
#: ``lighting.states`` falls back to them. Anything that needs a colour for a
#: state nobody configured reads it from here, through :func:`factory_light`.
#:
#: The shipped ``default_keymap.json`` spells the same five colours, because a
#: config file people edit should show its values rather than imply them - and
#: ``tests/test_micro_leds.py`` asserts the two agree, so they cannot drift.
FACTORY_PALETTE: Mapping[AgentState, str] = {
    AgentState.IDLE: "#FFFFFF",     # white - idle
    AgentState.WORKING: "#304FFE",  # blue - thinking
    AgentState.WAITING: "#FF6D00",  # amber - needs you
    AgentState.DONE: "#00FF4C",     # green - unread, and meant to decay
    AgentState.ERROR: "#FF0033",    # red - something broke
}


def factory_light(state: AgentState) -> StateLight:
    """The factory look for one state: its colour, lit solid at full brightness.

    Solid, speed 0 and brightness 1.0 are the factory's own values for every
    lit state (``docs/FACTORY-DEFAULTS.md`` §1a and §3), so this is the whole
    look and not just the colour.
    """
    return StateLight(
        color=parse_color(FACTORY_PALETTE[state]),
        effect=parse_effect("solid"),
        brightness=1.0,
        speed=0.0,
    )


#: The colour the pad you bought already uses while it is listening to you.
#:
#: ``docs/FACTORY-DEFAULTS.md`` §1b: while the vendor app's voice state is
#: ``recording`` it drives the **underglow** ``#2E8B57`` with ``snake`` at speed
#: 0.4. So "the pad changes colour while the mic is live" is not a new idea we
#: are inventing a colour for - it is a factory behaviour with a factory value,
#: and copying it is the same promise the five state colours keep.
#:
#: Why not red, which is the recording idiom everywhere else: ``error`` is
#: ``#FF0033``. A pad that goes red when you talk *and* red when your agent
#: breaks has two meanings for one colour, and the one you would least want to
#: miss is the one that stops being believed.
#:
#: It is also clearly apart from the five state colours. The nearest is ``done``
#: ``#00FF4C``, and it is not close: ``#2E8B57`` is far darker, desaturated and
#: blue-shifted, it lands on a **different physical surface** (the underglow,
#: not the six Agent Keys), and it *animates* where every state colour is
#: solid. Three separations, not one.
FACTORY_RECORDING = "#2E8B57"

#: How long an activity light may stay on with nothing having ended it, before
#: FreeMicro takes it down from the clock alone.
#:
#: A release can be lost - a Bluetooth drop mid-hold, a sleep, a key-up eaten in
#: a burst - and a light that only a key-up can clear would then claim you are
#: still dictating until you restarted FreeMicro. This is the same shape as
#: :func:`freemicro.input.quartz.release_all` for stuck modifiers and the
#: pointer's stale-sample stop: an obligation the process that started it has to
#: discharge on its own, decided from the clock and nothing else.
#:
#: **120 seconds.** Long enough that no real push-to-talk hold reaches it - two
#: unbroken minutes of held dictation is already extraordinary - and short
#: enough that a stuck light clears itself while you are still at the desk
#: wondering about it. It is also under :attr:`LightingConfig.auto_dim_seconds`
#: (180), so a lost release can never outlive the pad's own dimming policy.
DEFAULT_ACTIVITY_TIMEOUT = 120.0

#: The longest timeout a config may ask for. Beyond ten minutes the backstop has
#: stopped being a backstop.
ACTIVITY_TIMEOUT_MAX = 600.0

_ACTIVITY_LIGHT_FIELDS = (
    "color", "effect", "brightness", "speed", "magic", "zones",
    "timeout_seconds", "comment",
)


@dataclass(frozen=True)
class ActivityLight(StateLight):
    """What the pad shows while one binding is live. A layer, not a state.

    The five :class:`StateLight` colours answer "what is my agent doing?" and
    they are the pad's real job. This answers a different question - "is this
    key doing its thing *right now*?" - and it must never destroy the answer to
    the first one. So it is composed **over** the state frame at render time
    rather than written to the pad on key-down and undone on key-up: the state
    renderer keeps ownership of every zone this does not name, and the moment
    the layer goes away the very next frame is the live state, whatever that
    state has become in the meantime. Nothing has to remember what to put back.

    :attr:`zones` defaults to the underglow for the reason the vendor puts its
    own recording colour there (``docs/FACTORY-DEFAULTS.md`` §1b): the six Agent
    Keys are carrying one project each, and that is exactly what you still want
    to be able to see while you are talking to one of them.

    :attr:`timeout_seconds` is not optional and cannot be switched off. See
    :data:`DEFAULT_ACTIVITY_TIMEOUT`.
    """

    zones: Sequence[str] = (ZONE_UNDERGLOW,)
    timeout_seconds: float = DEFAULT_ACTIVITY_TIMEOUT

    @property
    def drives_backlight(self) -> bool:
        return ZONE_BACKLIGHT in self.zones

    @property
    def drives_underglow(self) -> bool:
        return ZONE_UNDERGLOW in self.zones

    @property
    def drives_agent_keys(self) -> bool:
        return ZONE_AGENT_KEYS in self.zones

    def describe(self) -> str:
        return (
            f"{color_to_hex(self.color)} {effect_name(self.effect)} "
            f"b={self.brightness:g} s={self.speed:g} on "
            f"{', '.join(self.zones)}"
        )


def factory_recording_light() -> ActivityLight:
    """The vendor's dictation look, whole. See :data:`FACTORY_RECORDING`."""
    return ActivityLight(
        color=parse_color(FACTORY_RECORDING),
        effect=parse_effect("snake"),
        brightness=1.0,
        speed=0.4,
    )


@dataclass(frozen=True)
class ReassertConfig:
    """When FreeMicro re-sends the lighting it already sent.

    We are not the only process writing these LEDs - the ChatGPT desktop app
    drives the same zones over the same channel and the last write wins. It is
    event-driven, so once it stops writing our colours stay put; the damage is
    done at the *moments* it writes, and the repair is to send our current state
    again. See :mod:`freemicro.lighting_owner` for the triggers.

    ``heartbeat_seconds`` is **0 (off) by default and should usually stay
    there**: every lighting call replaces the last, so a periodic re-send
    restarts animated effects (``breath``, ``snake``) on every beat, and it puts
    avoidable traffic on the channel that also carries key events. The
    event-driven triggers cover every clobberer we can actually name.
    """

    enabled: bool = True
    #: 0 disables the heartbeat entirely. See the class docstring.
    heartbeat_seconds: float = 0.0
    #: How often the cheap "has ChatGPT quit / has the config changed?" probe
    #: may run. Never on the key path - see :meth:`LightingOwner.poll`.
    poll_seconds: float = 3.0

    @property
    def heartbeat_enabled(self) -> bool:
        return self.enabled and self.heartbeat_seconds > 0.0


@dataclass(frozen=True)
class LightingConfig:
    """The whole ``lighting`` section."""

    #: Off unless the user opts in. FreeMicro shares this HID device with
    #: anything else the user is running (the vendor app included), and two
    #: processes silently fighting over the LEDs is the worst possible first
    #: impression. See docs/FACTORY-DEFAULTS.md §12.
    enabled: bool = False
    zones: Sequence[str] = (ZONE_AGENT_KEYS,)
    on_exit: str = "off"
    #: ``rgbcfg`` - the one verified to light this hardware. See
    #: :data:`LIGHTING_METHODS`.
    method: str = "rgbcfg"
    states: Mapping[AgentState, StateLight] = field(default_factory=dict)
    #: How we defend the colours we set against the other app writing them.
    reassert: ReassertConfig = field(default_factory=ReassertConfig)
    #: Blank the pad after this long with nothing happening; 0 never dims.
    #: The factory's own default is "3-minutes" and dimming means *dark*, not
    #: dimmer (``docs/FACTORY-DEFAULTS.md`` §4). It matters more for us than
    #: for the vendor, because our ``idle`` is white at full brightness and
    #: idle is what a live project shows most of the time.
    auto_dim_seconds: float = 180.0
    #: Whether the two states that are *asking for you* - ``waiting`` and
    #: ``error`` - dim along with everything else.
    #:
    #: ``False`` on purpose, and the one place FreeMicro deliberately parts
    #: company with §4. An amber key means "your agent is blocked on you", and
    #: the moment that is most valuable is exactly the moment you are not at
    #: your desk to reset the inactivity timer. A notification that switches
    #: itself off after three minutes is not a notification. Set it to ``true``
    #: for exact factory behaviour.
    auto_dim_alerts: bool = False

    def for_state(self, state: AgentState) -> Optional[StateLight]:
        """The look this config sets for ``state``, or ``None`` if it sets none."""
        return self.states.get(state)

    def light_for(self, state: AgentState) -> StateLight:
        """The look to actually show: the configured one, or the factory's.

        Every state always has an answer here. ``docs/CUSTOMIZING.md`` tells
        people they can delete a state they are happy with and get the factory
        colour back, so the fallback has to *be* the factory colour.
        """
        return self.states.get(state) or factory_light(state)

    @property
    def auto_dim_enabled(self) -> bool:
        return self.auto_dim_seconds > 0.0

    @property
    def drives_backlight(self) -> bool:
        return ZONE_BACKLIGHT in self.zones

    @property
    def drives_underglow(self) -> bool:
        return ZONE_UNDERGLOW in self.zones

    @property
    def drives_agent_keys(self) -> bool:
        return ZONE_AGENT_KEYS in self.zones


@dataclass(frozen=True)
class JoystickConfig:
    """What the analogue stick does: point, or fire discrete flicks.

    Two deadzones live here and they are **not** the same number. The vendor
    firmware uses two as well (``docs/FACTORY-DEFAULTS.md`` §6): a large one at
    0.5 before a *command* fires, and a small one at 0.1 before the stick counts
    as touched at all. FreeMicro keeps the same split, because they answer
    different questions:

    * :attr:`deadzone` (0.6) guards an **action**. It is deliberately large:
      crossing it types something into your terminal, so a resting thumb must
      never reach it.
    * :attr:`pointer_deadzone` (0.1) guards **motion**. It only has to reject
      the stick's own slop, and every pixel above it is precision you can feel.
      Raising this to the action deadzone is the single easiest way to make
      pointing feel dead, so they are separate fields on purpose.
    """

    #: Action deadzone: how far the stick must go before a *flick* fires.
    #: Discrete mode only. See the class docstring.
    deadzone: float = 0.6
    origin: float = 0.0
    directions: Sequence[str] = JOYSTICK_INPUTS
    #: ``pointer`` (analogue cursor) or ``directions`` (bindable flicks).
    mode: str = MODE_POINTER
    #: Motion deadzone, pointer mode only. Small on purpose.
    pointer_deadzone: float = 0.1
    #: Pixels per second at full deflection.
    max_speed: float = 1200.0
    #: Response curve exponent. 1 is linear (twitchy); higher is gentler near
    #: centre and reaches ``max_speed`` only at the very edge.
    gamma: float = 2.0
    #: How often the cursor is moved, in Hz. Independent of the pad's sample
    #: rate, which is the whole point - see :mod:`freemicro.input.pointer`.
    tick_hz: float = 90.0
    #: Hold this pad input to slow the pointer down. Empty disables it. The
    #: key is consumed while pointing, so give it one that is otherwise spare.
    precision_key: str = ""
    #: Speed multiplier while ``precision_key`` is held.
    precision_scale: float = 0.25
    #: Flip the pointer's vertical axis. See :mod:`freemicro.input.pointer`
    #: for why up is angle 0.75 and when you might need this.
    invert_y: bool = False

    @property
    def pointing(self) -> bool:
        """Whether the stick drives the cursor rather than firing flicks."""
        return self.mode == MODE_POINTER

    def direction_for(self, angle: float) -> str:
        """Which input id an angle (0-1 of a turn) falls into."""
        steps = len(self.directions)
        shifted = (float(angle) - self.origin) % 1.0
        index = int(round(shifted * steps)) % steps
        return self.directions[index]


# ---------------------------------------------------------------------------
# The document
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PadConfig:
    """A parsed, validated pad configuration."""

    bindings: Mapping[str, Action]
    joystick: JoystickConfig = field(default_factory=JoystickConfig)
    lighting: LightingConfig = field(default_factory=LightingConfig)
    #: Which project each of the six Agent Keys follows. See
    #: :mod:`freemicro.agentkeys` for the policies and the stability rule.
    agent_keys: AgentKeysConfig = field(default_factory=AgentKeysConfig)
    #: Two-key bindings, keyed by :func:`chord_key` (the members, sorted).
    #: Kept out of :attr:`bindings` so that everything which walks single
    #: inputs - the LED renderer, the web editor, ``keys --list`` - keeps
    #: seeing exactly what it saw before chords existed.
    chords: Mapping[Tuple[str, ...], Action] = field(default_factory=dict)
    #: See :data:`DEFAULT_CHORD_SETTLE_MS`.
    chord_settle_ms: float = DEFAULT_CHORD_SETTLE_MS
    source: Optional[Path] = None
    warnings: Sequence[str] = ()

    def action_for(self, input_id: str) -> Optional[Action]:
        return self.bindings.get(input_id)

    def chord_for(self, members: Sequence[str]) -> Optional[Action]:
        """The binding for a set of keys pressed together, order-independent."""
        return self.chords.get(chord_key(members))

    def activity_lights(self) -> Tuple[Tuple[str, Action], ...]:
        """Every binding that lights the pad while it is live, chords included.

        Ordered for display: single inputs in physical order first, then
        anything unrecognised, then chords.
        """
        found: List[Tuple[str, Action]] = []
        ordered = [i for i in KNOWN_INPUTS if i in self.bindings]
        ordered += [i for i in self.bindings if i not in KNOWN_INPUTS]
        for input_id in ordered:
            action = self.bindings[input_id]
            if action.light is not None:
                found.append((input_id, action))
        for members in sorted(self.chords):
            action = self.chords[members]
            if action.light is not None:
                found.append((chord_label(members), action))
        return tuple(found)

    @property
    def activity_zones(self) -> Tuple[str, ...]:
        """Every zone some binding's light can claim, whether or not it is live.

        The renderer needs this to be a property of the *config* rather than of
        the moment: it is what makes "which zones does this process drive?" a
        fixed answer, so a zone the state lighting never touches is painted dark
        in every ordinary frame and lit only while the layer is up. Without it
        the renderer would have to remember what it had lit and undo it, which
        is exactly the design this feature is meant to avoid.
        """
        zones: List[str] = []
        for _, action in self.activity_lights():
            for zone in action.light.zones:
                if zone not in zones:
                    zones.append(zone)
        return tuple(zones)

    def chord_partners(self, input_id: str) -> Tuple[str, ...]:
        """Every key that forms a chord with ``input_id``. Empty for most keys.

        This is what decides whether a press pays the settle window, so it is
        deliberately a question about *one* key rather than a global flag.
        """
        partners = []
        for members in self.chords:
            if input_id in members:
                partners.extend(m for m in members if m != input_id)
        return tuple(dict.fromkeys(partners))

    @property
    def origin(self) -> str:
        """Where this config came from, for display."""
        if self.source is None or self.source == DEFAULT_CONFIG_PATH:
            return "built-in default"
        return str(self.source)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def _parse_activity_light(input_id: str, raw: Any) -> ActivityLight:
    """Parse one binding's ``light``. Strict, because a typo here is invisible.

    The one field with no "off" setting is ``timeout_seconds``: see
    :data:`DEFAULT_ACTIVITY_TIMEOUT` for why a light nothing can be relied on
    to clear is not a thing this config is allowed to describe.
    """
    if is_chord_id(input_id):
        members = parse_chord_id(input_id)
    else:
        members = (input_id,)
    momentary = [m for m in members if m in _MOMENTARY_INPUTS]
    if momentary:
        raise PadConfigError(
            f"binding {input_id!r} cannot take a \"light\": "
            f"{', '.join(momentary)} report one event and no release, so "
            "nothing would ever turn the light off again. A light belongs on "
            "an input you can hold."
        )
    if not isinstance(raw, dict):
        raise PadConfigError(
            f"binding {input_id!r}: \"light\" must be an object, e.g. "
            "{\"color\": \"" + FACTORY_RECORDING + "\", \"effect\": \"snake\"}"
        )
    if "color" not in raw:
        raise PadConfigError(f"binding {input_id!r}: \"light\" needs a \"color\"")
    unknown = {k for k in raw if not k.startswith("_")} - set(_ACTIVITY_LIGHT_FIELDS)
    if unknown:
        raise PadConfigError(
            f"binding {input_id!r}: \"light\" has unknown field(s): "
            f"{', '.join(sorted(unknown))}; it takes: "
            f"{', '.join(_ACTIVITY_LIGHT_FIELDS)}"
        )

    zones_raw = raw.get("zones", ActivityLight.zones)
    if isinstance(zones_raw, str):
        zones_raw = [zones_raw]
    if not isinstance(zones_raw, (list, tuple)) or not zones_raw:
        raise PadConfigError(
            f"binding {input_id!r}: \"light.zones\" must be a non-empty list"
        )
    try:
        zones = tuple(dict.fromkeys(parse_zone(z) for z in zones_raw))
        color = parse_color(raw["color"])
        effect = parse_effect(raw.get("effect", "solid"))
        brightness = float(raw.get("brightness", 1.0))
        speed = float(raw.get("speed", 0.0))
        magic = None if raw.get("magic") is None else float(raw["magic"])
        timeout = float(raw.get("timeout_seconds", DEFAULT_ACTIVITY_TIMEOUT))
    except (LightingError, TypeError, ValueError) as exc:
        raise PadConfigError(f"binding {input_id!r}: light: {exc}") from exc

    for name, value in (
        ("brightness", brightness), ("speed", speed), ("magic", magic),
    ):
        if value is not None and not 0.0 <= value <= 1.0:
            raise PadConfigError(
                f"binding {input_id!r}: light.{name} must be between 0 and 1"
            )
    if not 0.0 < timeout <= ACTIVITY_TIMEOUT_MAX:
        raise PadConfigError(
            f"binding {input_id!r}: \"light.timeout_seconds\" must be more "
            f"than 0 and at most {ACTIVITY_TIMEOUT_MAX:g}. There is no 'never' "
            "here on purpose: a Bluetooth drop mid-hold loses the key-up, and "
            "a light with nothing to end it would then claim the key was still "
            "down until you restarted FreeMicro."
        )
    return ActivityLight(
        color=color,
        effect=effect,
        brightness=brightness,
        speed=speed,
        magic=magic,
        zones=zones,
        timeout_seconds=timeout,
    )


def _parse_binding(input_id: str, raw: Any) -> Action:
    if isinstance(raw, str):
        # Shorthand: "AG00": "/resume"  ==  type that text.
        raw = {"action": "text", "text": raw}
    if not isinstance(raw, dict):
        raise PadConfigError(
            f"binding for {input_id!r} must be an object or a string, "
            f"got {type(raw).__name__}"
        )
    kind = raw.get("action")
    if not isinstance(kind, str) or not kind:
        raise PadConfigError(
            f"binding for {input_id!r} is missing its \"action\" field"
        )
    params = {k: v for k, v in raw.items() if k not in _META_FIELDS}
    if kind == FOCUS_SESSION and "slot" not in params and "project" not in params:
        # "AG02": {"action": "focus_session"} means *this* key's slot. Only the
        # config layer knows which input a binding belongs to, so it is filled
        # in here rather than making every user repeat the index they can see
        # printed on the key.
        if input_id in AGENT_KEYS:
            params["slot"] = AGENT_KEYS.index(input_id)
        else:
            raise PadConfigError(
                f"binding for {input_id!r}: '{FOCUS_SESSION}' needs a \"slot\" "
                "(0-5) or a \"project\" path unless it is on an Agent Key"
            )
    try:
        validate_params(kind, params)
    except ValueError as exc:
        raise PadConfigError(f"binding for {input_id!r}: {exc}") from exc
    light = (
        _parse_activity_light(input_id, raw["light"]) if "light" in raw else None
    )
    return Action(
        kind=kind,
        params=params,
        label=str(raw.get("label") or input_id),
        comment=_join_comment(raw.get("comment")),
        light=light,
    )


def _join_comment(raw: Any) -> str:
    """Comments may be a string or a list of lines - JSON has no block text."""
    if raw is None:
        return ""
    if isinstance(raw, (list, tuple)):
        return " ".join(str(line) for line in raw if str(line).strip())
    return str(raw)


def _joystick_number(raw: Mapping[str, Any], field_name: str, fallback: float) -> float:
    """One numeric joystick field, with the field named in every error.

    "could not convert string to float" tells the user nothing about which of
    six tuning values they fat-fingered, and these are values people edit
    repeatedly while tuning by feel.
    """
    if field_name not in raw:
        return fallback
    try:
        return float(raw[field_name])
    except (TypeError, ValueError):
        raise PadConfigError(
            f"\"joystick.{field_name}\" must be a number, got "
            f"{raw[field_name]!r}"
        ) from None


def _parse_joystick(raw: Any) -> JoystickConfig:
    if raw is None:
        return JoystickConfig()
    if not isinstance(raw, dict):
        raise PadConfigError("\"joystick\" must be an object")
    defaults = JoystickConfig()
    unknown = {k for k in raw if not k.startswith("_")} - set(_JOYSTICK_FIELDS)
    if unknown:
        raise PadConfigError(
            f"\"joystick\" has unknown field(s): {', '.join(sorted(unknown))}; "
            f"it takes: {', '.join(_JOYSTICK_FIELDS)}"
        )

    mode = str(raw.get("mode", defaults.mode)).lower()
    if mode not in JOYSTICK_MODES:
        raise PadConfigError(
            f"\"joystick.mode\" must be one of {', '.join(JOYSTICK_MODES)}, "
            f"got {raw.get('mode')!r}"
        )

    directions = raw.get("directions", defaults.directions)
    if not isinstance(directions, (list, tuple)) or len(directions) < 2:
        raise PadConfigError(
            "\"joystick.directions\" must list at least two input ids"
        )
    if not all(isinstance(d, str) and d for d in directions):
        raise PadConfigError("\"joystick.directions\" entries must be input ids")

    deadzone = _joystick_number(raw, "deadzone", defaults.deadzone)
    origin = _joystick_number(raw, "origin", defaults.origin)
    pointer_deadzone = _joystick_number(
        raw, "pointer_deadzone", defaults.pointer_deadzone
    )
    max_speed = _joystick_number(raw, "max_speed", defaults.max_speed)
    gamma = _joystick_number(raw, "gamma", defaults.gamma)
    tick_hz = _joystick_number(raw, "tick_hz", defaults.tick_hz)
    precision_scale = _joystick_number(
        raw, "precision_scale", defaults.precision_scale
    )

    if not 0.0 <= deadzone <= 1.0:
        raise PadConfigError("\"joystick.deadzone\" must be between 0 and 1")
    if not 0.0 <= pointer_deadzone < 1.0:
        raise PadConfigError(
            "\"joystick.pointer_deadzone\" must be at least 0 and less than 1 "
            "(1 would leave no travel to point with)"
        )
    if max_speed <= 0.0:
        raise PadConfigError(
            "\"joystick.max_speed\" must be more than 0 pixels per second"
        )
    if gamma <= 0.0:
        raise PadConfigError(
            "\"joystick.gamma\" must be more than 0 (1 is linear, ~2 is a "
            "TrackPoint-like curve)"
        )
    if not TICK_HZ_MIN <= tick_hz <= TICK_HZ_MAX:
        raise PadConfigError(
            f"\"joystick.tick_hz\" must be between {TICK_HZ_MIN:g} and "
            f"{TICK_HZ_MAX:g}"
        )
    if not 0.0 < precision_scale <= 1.0:
        raise PadConfigError(
            "\"joystick.precision_scale\" must be more than 0 and at most 1"
        )

    precision_key = raw.get("precision_key", defaults.precision_key)
    if precision_key is None:
        precision_key = ""
    if not isinstance(precision_key, str):
        raise PadConfigError("\"joystick.precision_key\" must be an input id")
    if precision_key in ENCODER_TICKS:
        raise PadConfigError(
            f"\"joystick.precision_key\" cannot be {precision_key!r}: dial "
            "detents are momentary and have no release, so precision mode "
            "would latch on forever"
        )

    return JoystickConfig(
        deadzone=deadzone,
        origin=origin,
        directions=tuple(directions),
        mode=mode,
        pointer_deadzone=pointer_deadzone,
        max_speed=max_speed,
        gamma=gamma,
        tick_hz=tick_hz,
        precision_key=precision_key,
        precision_scale=precision_scale,
        invert_y=bool(raw.get("invert_y", defaults.invert_y)),
    )


def _parse_state_light(state: str, raw: Any) -> StateLight:
    if not isinstance(raw, dict):
        raise PadConfigError(f"lighting.states.{state} must be an object")
    if "color" not in raw:
        raise PadConfigError(f"lighting.states.{state} needs a \"color\"")
    unknown = set(raw) - {
        "color", "effect", "brightness", "speed", "magic", "comment",
    }
    if unknown:
        raise PadConfigError(
            f"lighting.states.{state} has unknown field(s): "
            f"{', '.join(sorted(unknown))}"
        )
    try:
        color = parse_color(raw["color"])
        effect = parse_effect(raw.get("effect", "solid"))
        brightness = float(raw.get("brightness", 1.0))
        speed = float(raw.get("speed", 0.0))
        magic = None if raw.get("magic") is None else float(raw["magic"])
    except (LightingError, TypeError, ValueError) as exc:
        raise PadConfigError(f"lighting.states.{state}: {exc}") from exc
    for name, value in (
        ("brightness", brightness), ("speed", speed), ("magic", magic),
    ):
        if value is not None and not 0.0 <= value <= 1.0:
            raise PadConfigError(
                f"lighting.states.{state}.{name} must be between 0 and 1"
            )
    return StateLight(
        color=color, effect=effect, brightness=brightness, speed=speed, magic=magic
    )


def _parse_reassert(raw: Any) -> ReassertConfig:
    """Parse ``lighting.reassert``. Absent means the defaults, which are sane."""
    defaults = ReassertConfig()
    if raw is None:
        return defaults
    if not isinstance(raw, dict):
        raise PadConfigError("\"lighting.reassert\" must be an object")
    unknown = set(raw) - {
        "enabled", "heartbeat_seconds", "poll_seconds", "comment",
    }
    if unknown:
        raise PadConfigError(
            "lighting.reassert has unknown field(s): "
            f"{', '.join(sorted(unknown))}"
        )
    try:
        heartbeat = float(raw.get("heartbeat_seconds", defaults.heartbeat_seconds))
        poll = float(raw.get("poll_seconds", defaults.poll_seconds))
    except (TypeError, ValueError) as exc:
        raise PadConfigError(f"lighting.reassert: {exc}") from exc
    if heartbeat < 0.0:
        raise PadConfigError(
            "lighting.reassert.heartbeat_seconds must be 0 (off) or more"
        )
    if poll <= 0.0:
        raise PadConfigError("lighting.reassert.poll_seconds must be more than 0")
    return ReassertConfig(
        enabled=bool(raw.get("enabled", defaults.enabled)),
        heartbeat_seconds=heartbeat,
        poll_seconds=poll,
    )


def _parse_auto_dim(raw: Any) -> float:
    """Seconds of inactivity before the pad blanks. ``0``/``false``/``null`` is off.

    Written to be forgiving about *how* "never dim" is spelled, because a
    setting whose only two interesting values are "the default" and "off" is
    one people turn off in a hurry, from a config file, a browser form or a
    shell - and each of those hands us a different falsy thing.
    """
    if raw is None or raw is False:
        return 0.0
    if isinstance(raw, str) and raw.strip().lower() in ("off", "never", "none", ""):
        return 0.0
    try:
        seconds = float(raw)
    except (TypeError, ValueError):
        raise PadConfigError(
            "\"lighting.auto_dim_seconds\" must be a number of seconds "
            f"(0 or 'off' never dims), got {raw!r}"
        ) from None
    if seconds < 0.0:
        raise PadConfigError(
            "\"lighting.auto_dim_seconds\" must be 0 (never dim) or more"
        )
    return seconds


def _parse_lighting(raw: Any) -> LightingConfig:
    if raw is None:
        return LightingConfig()
    if not isinstance(raw, dict):
        raise PadConfigError("\"lighting\" must be an object")
    zones_raw = raw.get("zones", LightingConfig().zones)
    if isinstance(zones_raw, str):
        zones_raw = [zones_raw]
    if not isinstance(zones_raw, (list, tuple)) or not zones_raw:
        raise PadConfigError("\"lighting.zones\" must be a non-empty list")
    try:
        zones = tuple(dict.fromkeys(parse_zone(z) for z in zones_raw))
    except LightingError as exc:
        raise PadConfigError(f"lighting.zones: {exc}") from exc
    defaults = LightingConfig()
    on_exit = str(raw.get("on_exit", defaults.on_exit)).lower()
    if on_exit not in _EXIT_MODES:
        raise PadConfigError(
            f"lighting.on_exit must be one of {', '.join(_EXIT_MODES)}"
        )
    method = str(raw.get("method", defaults.method)).lower()
    if method not in LIGHTING_METHODS:
        raise PadConfigError(
            f"lighting.method must be one of {', '.join(LIGHTING_METHODS)}"
        )
    auto_dim = _parse_auto_dim(raw.get("auto_dim_seconds", defaults.auto_dim_seconds))
    states_raw = raw.get("states") or {}
    if not isinstance(states_raw, dict):
        raise PadConfigError("\"lighting.states\" must be an object")
    valid = {s.value for s in AgentState}
    states: Dict[AgentState, StateLight] = {}
    for name, value in states_raw.items():
        if name not in valid:
            raise PadConfigError(
                f"lighting.states has unknown state {name!r}; "
                f"expected {', '.join(sorted(valid))}"
            )
        states[AgentState(name)] = _parse_state_light(name, value)
    return LightingConfig(
        enabled=bool(raw.get("enabled", defaults.enabled)),
        zones=zones,
        on_exit=on_exit,
        method=method,
        states=states,
        reassert=_parse_reassert(raw.get("reassert")),
        auto_dim_seconds=auto_dim,
        auto_dim_alerts=bool(raw.get("auto_dim_alerts", defaults.auto_dim_alerts)),
    )


def _parse_agent_keys(raw: Any, warnings: List[str]) -> AgentKeysConfig:
    """Parse the ``agent_keys`` section, warning (never failing) on dead pins.

    A pinned directory that does not exist is a *warning* on purpose: projects
    are archived, checkouts move, drives are unmounted. Refusing to start the
    pad because one of six pins is stale would be the wrong trade - the other
    five keys still work, and the user gets told.
    """
    try:
        config = parse_agent_keys(raw)
    except AgentKeysError as exc:
        raise PadConfigError(str(exc)) from exc

    if config.policy in (POLICY_PINNED, POLICY_MANUAL):
        for index, pin in enumerate(config.slots):
            if pin and not Path(pin).is_dir():
                warnings.append(
                    f"agent_keys.slots[{index}] pins AG{index:02d} to {pin!r}, "
                    "which is not a directory right now - that key will stay "
                    "dark until it is."
                )
    if config.policy == POLICY_MANUAL and not any(config.slots):
        warnings.append(
            "agent_keys.policy is 'manual' but no slot names a project, so all "
            "six Agent Keys will stay dark. Use 'recent' to fill them "
            "automatically."
        )
    return config


def parse(data: Any, source: Optional[Path] = None) -> PadConfig:
    """Turn a decoded JSON document into a validated :class:`PadConfig`."""
    if not isinstance(data, dict):
        raise PadConfigError("the config file must contain a JSON object")

    version = data.get("version", 1)
    if version != 1:
        raise PadConfigError(
            f"unsupported config version {version!r}; this build understands 1"
        )

    bindings_raw = data.get("bindings")
    if bindings_raw is None:
        raise PadConfigError("the config file needs a \"bindings\" object")
    if not isinstance(bindings_raw, dict):
        raise PadConfigError("\"bindings\" must be an object of input id -> binding")

    warnings: List[str] = []
    bindings: Dict[str, Action] = {}
    chords: Dict[Tuple[str, ...], Action] = {}
    chord_written_as: Dict[Tuple[str, ...], str] = {}
    for input_id, raw in bindings_raw.items():
        if input_id.startswith("_"):
            continue  # a comment key
        if is_chord_id(input_id):
            members = parse_chord_id(input_id)
            if members in chords:
                raise PadConfigError(
                    f"binding {input_id!r} is the same chord as "
                    f"{chord_written_as[members]!r} - two keys pressed together "
                    "have no order, so only one of them can be bound"
                )
            chord_written_as[members] = input_id
            chord_action = _parse_binding(input_id, raw)
            if chord_action.label == input_id:
                # No explicit label, so it fell back to the id as written. Logs
                # name the chord canonically, and one chord with two spellings
                # in one log line reads like two different things.
                chord_action = replace(chord_action, label=chord_label(members))
            chords[members] = chord_action
            unknown_members = [m for m in members if m not in KNOWN_INPUTS]
            if unknown_members:
                warnings.append(
                    f"chord {input_id!r} uses "
                    f"{', '.join(repr(m) for m in unknown_members)}, which this "
                    "build does not know about - it will only fire if your "
                    "firmware reports that id."
                )
            continue
        bindings[input_id] = _parse_binding(input_id, raw)
        if input_id not in KNOWN_INPUTS:
            warnings.append(
                f"{input_id!r} is not an input this build knows about - it will "
                "only fire if your firmware reports that id."
            )

    chord_settle_ms = _parse_chord_settle(data.get("chords"))
    if chords and chord_settle_ms <= 0.0:
        # settle_ms 0 means "never defer a press", so a chord can only ever be
        # completed by a key that had nothing of its own to fire. A chord whose
        # members are all bound individually is then dead config, and silently
        # dead config is the worst kind.
        dead = [
            written for members, written in chord_written_as.items()
            if all(
                m in bindings and bindings[m].kind != "none" for m in members
            )
        ]
        if dead:
            warnings.append(
                "chords.settle_ms is 0, so no press is ever held back and "
                f"{', '.join(sorted(dead))} can never fire: every key in them "
                "already runs its own binding the instant it goes down. Give "
                "one key of each chord {\"action\": \"none\"}, or raise "
                "chords.settle_ms."
            )

    joystick = _parse_joystick(data.get("joystick"))
    lighting = _parse_lighting(data.get("lighting"))
    agent_keys = _parse_agent_keys(data.get("agent_keys"), warnings)

    lit = [
        (input_id, action)
        for input_id, action in list(bindings.items())
        + [(chord_label(m), a) for m, a in chords.items()]
        if action.light is not None
    ]
    for input_id, action in lit:
        if action.kind not in HOLD_KINDS:
            # Not an error: "lit while the key is down" is a coherent thing for
            # any key to do, and a torch is a legitimate use of it. But the
            # reason people reach for this is dictation, and a *toggle* bound
            # here gives a flash of colour and then darkness while the mic is
            # still live - a pad that has stopped telling the truth. Say it at
            # load time, once, rather than letting it be discovered in use.
            warnings.append(
                f"{input_id!r} has a \"light\" but its action is "
                f"'{action.kind}', so the light lasts exactly as long as your "
                "finger is on the key. If this is dictation, FreeMicro cannot "
                "see a toggle stop - use {\"action\": \"hold\"} and set your "
                "dictation app's push-to-talk (hold) shortcut to match."
            )
    if lit and not lighting.enabled:
        warnings.append(
            "lighting.enabled is false, so the \"light\" on "
            f"{', '.join(repr(i) for i, _ in lit)} will not show. Turn the LEDs "
            "on once with: freemicro lights --enable"
        )

    if joystick.pointing:
        # The stick is driving the cursor, so its four flick ids never fire.
        # Saying so is the difference between "my binding is broken" and "ah,
        # that is the mode I am in" - and the shipped default binds all four.
        shadowed = [
            direction for direction in joystick.directions
            if direction in bindings and bindings[direction].kind not in
            ("none", "mouse")
        ]
        if shadowed:
            warnings.append(
                "joystick.mode is 'pointer', so the stick moves the cursor and "
                f"{', '.join(shadowed)} will not fire. Set "
                "\"joystick\": {\"mode\": \"directions\"} to get the flicks back."
            )
        if joystick.precision_key and joystick.precision_key in bindings:
            warnings.append(
                f"joystick.precision_key is {joystick.precision_key!r}, which "
                "also has a binding - while pointing, holding it slows the "
                "cursor instead of running that binding."
            )
    else:
        for direction in joystick.directions:
            if direction not in bindings:
                warnings.append(f"joystick direction {direction!r} has no binding.")
    if tuple(joystick.directions) == _LEGACY_JOYSTICK_INPUTS:
        warnings.append(
            "joystick.directions is in the old right/up/left/down order, which "
            "puts JOY_UP where the hardware reports DOWN. The correct order is "
            "[\"JOY_RIGHT\", \"JOY_DOWN\", \"JOY_LEFT\", \"JOY_UP\"] - swap the "
            "two, or delete the line to take the default."
        )

    return PadConfig(
        bindings=bindings,
        joystick=joystick,
        lighting=lighting,
        agent_keys=agent_keys,
        chords=chords,
        chord_settle_ms=chord_settle_ms,
        source=source,
        warnings=tuple(warnings),
    )


# ---------------------------------------------------------------------------
# Locating and loading
# ---------------------------------------------------------------------------

def user_path() -> Path:
    """The canonical location ``--init`` writes to."""
    return config_home() / FILENAME


def xdg_path() -> Path:
    """The XDG location, honoured because plenty of people look there first."""
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "freemicro" / FILENAME


def search_paths() -> List[Path]:
    """Every location consulted, in priority order."""
    paths: List[Path] = []
    env = os.environ.get(ENV_PATH)
    if env:
        paths.append(Path(env).expanduser())
    paths.append(user_path())
    paths.append(xdg_path())
    paths.append(DEFAULT_CONFIG_PATH)
    return paths


def resolve_path(explicit: Optional[Path] = None) -> Path:
    """Pick the config file to use, falling back to the shipped default."""
    if explicit is not None:
        path = Path(explicit).expanduser()
        if not path.exists():
            raise PadConfigError(f"no such config file: {path}")
        return path
    for candidate in search_paths():
        if candidate.exists():
            return candidate
    return DEFAULT_CONFIG_PATH


def load(path: Optional[Path] = None) -> PadConfig:
    """Load and validate the pad config.

    Unlike :class:`freemicro.config.Config`, a broken file here is **fatal**
    rather than silently ignored: if you edited your keymap and it did not
    parse, running on the old defaults while your changes vanish is far worse
    than an error message.
    """
    resolved = resolve_path(path)
    try:
        text = resolved.read_text(encoding="utf-8")
    except OSError as exc:
        raise PadConfigError(f"could not read {resolved}: {exc}") from exc
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise PadConfigError(f"{resolved} is not valid JSON: {exc}") from exc
    return parse(data, source=resolved)


def load_default() -> PadConfig:
    """Load the shipped default, ignoring anything the user has written."""
    return load(DEFAULT_CONFIG_PATH)


def write_starter(path: Optional[Path] = None, force: bool = False) -> Path:
    """Copy the annotated default config to ``path`` for the user to edit."""
    target = Path(path).expanduser() if path is not None else user_path()
    if target.exists() and not force:
        raise PadConfigError(
            f"{target} already exists; pass --force to overwrite it"
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"), encoding="utf-8"
    )
    return target


__all__ = [
    "ACTION_KEYS",
    "ACTIVITY_TIMEOUT_MAX",
    "AGENT_KEYS",
    "ActivityLight",
    "AgentKeysConfig",
    "DEFAULT_ACTIVITY_TIMEOUT",
    "FACTORY_RECORDING",
    "factory_recording_light",
    "CHORD_MAX_KEYS",
    "CHORD_SEPARATOR",
    "CHORD_SETTLE_MS_MAX",
    "DEFAULT_CHORD_SETTLE_MS",
    "ENCODER_INPUTS",
    "ENCODER_TICKS",
    "DEFAULT_CONFIG_PATH",
    "ENV_PATH",
    "FACTORY_PALETTE",
    "JOYSTICK_INPUTS",
    "JOYSTICK_MODES",
    "JoystickConfig",
    "KNOWN_INPUTS",
    "LIGHTING_METHODS",
    "MODE_DIRECTIONS",
    "MODE_POINTER",
    "TICK_HZ_MAX",
    "TICK_HZ_MIN",
    "LightingConfig",
    "PadConfig",
    "PadConfigError",
    "ReassertConfig",
    "StateLight",
    "chord_key",
    "chord_label",
    "factory_light",
    "is_chord_id",
    "load",
    "parse_chord_id",
    "load_default",
    "parse",
    "resolve_path",
    "search_paths",
    "user_path",
    "write_starter",
    "xdg_path",
]
