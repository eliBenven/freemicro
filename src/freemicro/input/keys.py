"""Key names → macOS keystrokes, as pure data and pure functions.

Delivering a keystroke and *naming* one are different problems, and only the
naming half can be tested without a Mac. This module owns the naming half: it
turns a human-written combo like ``ctrl-r`` or ``cmd+shift+k`` into the
AppleScript that ``System Events`` understands, and it fails loudly on typos.

Keeping it pure matters for the user-facing keymap: :mod:`freemicro.input.keymap`
validates every ``key`` binding through :func:`parse_combo` at *load* time, so a
misspelled key name is reported when you edit the config - not silently ignored
hours later when you press the pad.
"""

from __future__ import annotations

from typing import Dict, List, Tuple


class KeyNameError(ValueError):
    """Raised for a combo FreeMicro cannot turn into a keystroke."""


#: Modifier spellings we accept, normalised to AppleScript's vocabulary.
MODIFIER_ALIASES: Dict[str, str] = {
    "cmd": "command",
    "command": "command",
    "meta": "command",
    "super": "command",
    "win": "command",
    "ctrl": "control",
    "control": "control",
    "alt": "option",
    "opt": "option",
    "option": "option",
    "shift": "shift",
    # fn is reachable only through CGEvent - see applescript_for().
    "fn": "fn",
    "function": "fn",
}

#: Emitted in a stable order so generated AppleScript is deterministic.
MODIFIER_ORDER: Tuple[str, ...] = ("command", "control", "option", "shift", "fn")

#: macOS virtual key codes for keys that have no printable character.
#: Printable characters (letters, digits, punctuation) go through
#: ``keystroke "x"`` instead and need no entry here.
KEY_CODES: Dict[str, int] = {
    "return": 36,
    "enter": 76,  # keypad enter - distinct from return on macOS
    "tab": 48,
    "space": 49,
    "delete": 51,  # backspace
    "backspace": 51,
    "forward-delete": 117,
    "escape": 53,
    "esc": 53,
    "help": 114,
    "home": 115,
    "end": 119,
    "page-up": 116,
    "page-down": 121,
    "left": 123,
    "right": 124,
    "down": 125,
    "up": 126,
    "f1": 122,
    "f2": 120,
    "f3": 99,
    "f4": 118,
    "f5": 96,
    "f6": 97,
    "f7": 98,
    "f8": 100,
    "f9": 101,
    "f10": 109,
    "f11": 103,
    "f12": 111,
}

#: Names for printable keys whose character would otherwise collide with the
#: ``-``/``+`` combo separators.
LITERAL_ALIASES: Dict[str, str] = {
    "minus": "-",
    "hyphen": "-",
    "plus": "+",
    "equals": "=",
    "comma": ",",
    "period": ".",
    "slash": "/",
    "backslash": "\\",
    "semicolon": ";",
    "quote": "'",
    "grave": "`",
    "backtick": "`",
}


#: US-layout virtual key codes for printable characters. Only needed by the
#: CGEvent backend, which addresses keys by code; the AppleScript backend types
#: characters directly and needs none of this.
CHAR_KEY_CODES: Dict[str, int] = {
    "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8,
    "v": 9, "b": 11, "q": 12, "w": 13, "e": 14, "r": 15, "y": 16, "t": 17,
    "1": 18, "2": 19, "3": 20, "4": 21, "6": 22, "5": 23, "=": 24, "9": 25,
    "7": 26, "-": 27, "8": 28, "0": 29, "]": 30, "o": 31, "u": 32, "[": 33,
    "i": 34, "p": 35, "l": 37, "j": 38, "'": 39, "k": 40, ";": 41, "\\": 42,
    ",": 43, "/": 44, "n": 45, "m": 46, ".": 47, "`": 50,
}


def known_key_names() -> List[str]:
    """Every named (non-printable) key, sorted - used in error messages."""
    return sorted(set(KEY_CODES) | set(LITERAL_ALIASES))


def _split(combo: str) -> List[str]:
    """Split ``cmd+shift-k`` into parts on either separator.

    Both ``+`` and ``-`` are accepted because both are common in the wild, and
    the pad's default map ships ``ctrl-r`` / ``shift-tab``. A literal ``-`` or
    ``+`` key is written by name (``minus`` / ``plus``) to stay unambiguous.

    A name that *contains* a hyphen wins over splitting it. Without that,
    ``page-up``, ``page-down`` and ``forward-delete`` are unwritable: they split
    into ``page`` + ``up``, ``page`` is not a modifier, and the rejection then
    lists those very names as the valid options.

    So the whole string is tried first, and then the longest hyphenated *tail*
    is rejoined - which is what makes ``cmd+page-up`` work as well as bare
    ``page-up``. Only the tail is rejoined, because the base key is always last
    and every leading part must be a modifier; no modifier name contains a
    hyphen, so this cannot swallow one by accident.
    """
    whole = combo.strip().lower()
    if _is_key_name(whole):
        return [whole]
    parts: List[str] = []
    token = ""
    for char in combo.strip():
        if char in "+-" and token:
            parts.append(token)
            token = ""
        else:
            token += char
    if token:
        parts.append(token)
    # Longest tail first: a two-part name must not win over a three-part one.
    for start in range(len(parts) - 2, -1, -1):
        tail = "-".join(parts[start:]).lower()
        if _is_key_name(tail):
            return parts[:start] + [tail]
    return parts


def _is_key_name(name: str) -> bool:
    """Is this the whole name of a key, rather than part of a combo?"""
    return name in KEY_CODES or name in LITERAL_ALIASES


def parse_combo(combo: str) -> Tuple[Tuple[str, ...], str]:
    """Split a combo into ``(modifiers, base_key)``.

    Modifiers are normalised and de-duplicated; the base key is lower-cased but
    otherwise untouched. Raises :class:`KeyNameError` for anything we cannot
    deliver, so bad names surface at config-load time.
    """
    if not isinstance(combo, str) or not combo.strip():
        raise KeyNameError("key must be a non-empty string, e.g. 'escape'")

    parts = _split(combo)
    if not parts:
        raise KeyNameError(f"could not parse key combo {combo!r}")

    *modifier_parts, base = parts
    modifiers: List[str] = []
    for raw in modifier_parts:
        canonical = MODIFIER_ALIASES.get(raw.lower())
        if canonical is None:
            raise KeyNameError(
                f"unknown modifier {raw!r} in {combo!r}; "
                f"expected one of {', '.join(sorted(set(MODIFIER_ALIASES)))}"
            )
        if canonical not in modifiers:
            modifiers.append(canonical)

    base = base.lower()
    if base in KEY_CODES or base in LITERAL_ALIASES:
        pass
    elif len(base) == 1:
        pass  # a printable character we can type directly
    else:
        raise KeyNameError(
            f"unknown key {base!r} in {combo!r}; use a single character or one "
            f"of: {', '.join(known_key_names())}"
        )

    ordered = tuple(m for m in MODIFIER_ORDER if m in modifiers)
    return ordered, base


def escape_applescript(text: str) -> str:
    """Escape a string for embedding in an AppleScript literal."""
    return text.replace("\\", "\\\\").replace('"', '\\"')


def applescript_for(combo: str) -> str:
    """Render the AppleScript that presses ``combo`` once.

    Named keys go through ``key code`` (the only way to reach Escape, arrows or
    Tab); printable characters go through ``keystroke``, which respects the
    user's actual keyboard layout.
    """
    modifiers, base = parse_combo(combo)
    if "fn" in modifiers:
        raise KeyNameError(
            f"{combo!r} uses fn, which AppleScript cannot express - macOS "
            "handles fn below the System Events API. Use the CGEvent backend "
            "(the default when available) for fn bindings."
        )
    if base in KEY_CODES:
        target = f"key code {KEY_CODES[base]}"
    else:
        char = LITERAL_ALIASES.get(base, base)
        target = f'keystroke "{escape_applescript(char)}"'
    if modifiers:
        using = ", ".join(f"{m} down" for m in modifiers)
        target = f"{target} using {{{using}}}"
    return f'tell application "System Events" to {target}'


def cgevent_spec(combo: str) -> Tuple[int, Tuple[str, ...]]:
    """Resolve a combo to ``(virtual_keycode, modifiers)`` for CGEvent.

    Unlike AppleScript this needs a *code* for the base key, so printable
    characters are looked up in :data:`CHAR_KEY_CODES` (US layout). A character
    outside that table can still be typed as text - it just cannot carry
    modifiers.
    """
    modifiers, base = parse_combo(combo)
    if base in KEY_CODES:
        return KEY_CODES[base], modifiers
    char = LITERAL_ALIASES.get(base, base)
    code = CHAR_KEY_CODES.get(char.lower())
    if code is None:
        raise KeyNameError(
            f"no virtual key code for {char!r} in {combo!r}; use a named key or "
            "type it with a 'text' action instead"
        )
    return code, modifiers


def applescript_for_text(text: str) -> str:
    """Render the AppleScript that types ``text`` verbatim."""
    return (
        'tell application "System Events" to keystroke '
        f'"{escape_applescript(text)}"'
    )


__all__ = [
    "CHAR_KEY_CODES",
    "KEY_CODES",
    "KeyNameError",
    "LITERAL_ALIASES",
    "MODIFIER_ALIASES",
    "MODIFIER_ORDER",
    "applescript_for",
    "applescript_for_text",
    "cgevent_spec",
    "escape_applescript",
    "known_key_names",
    "parse_combo",
]
