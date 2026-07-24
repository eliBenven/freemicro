"""Read and write ``keymap.json`` without ever producing a file the CLI rejects.

This module is the only thing in the web UI that touches the user's config, and
it has one rule: **the document is validated by the same code path the CLI uses
before a single byte is written.** :func:`freemicro.padconfig.parse` is the
authority on what is legal. If it raises, the save is refused and the error text
goes straight to the browser - no second, drifting copy of the schema lives here.

Three practical consequences:

* The *whole document* round-trips. Comments (``_readme``, per-binding
  ``comment``) and any key a future FreeMicro adds survive an edit, because we
  load the raw JSON and mutate it rather than re-serialising a parsed
  :class:`~freemicro.padconfig.PadConfig`.
* Writes are atomic and backed up. A temp file in the same directory plus
  ``os.replace`` means the config is never half-written, and the previous
  version is kept as ``keymap.json.bak`` so a bad edit is one ``mv`` from
  recovery.
* The packaged default is never written to. If the resolved config *is* the
  shipped default (i.e. the user has none yet), the save target becomes
  ``~/.freemicro/keymap.json`` - the same file ``freemicro keys --init`` writes.

Saving writes changes, not documents
------------------------------------
The web UI used to ``PUT`` its entire in-memory document on every save. That is
a full-file overwrite dressed up as an edit, and it destroyed a user's working
dictation shortcut: the page held a value that had drifted from the file (a
stray keystroke landed in a combo field), the user later changed something
completely unrelated, and the save rewrote all sixteen bindings from memory.
Nothing in the UI said a word, because from its point of view it had simply
written what it had.

So a save now carries three documents, not one:

``base``
    what the page loaded - the state the user's edits are relative to.
``edited``
    what the page holds now.
``disk``
    what the file says at the instant of writing, which may be neither.

:func:`delta` extracts only the leaves that actually changed between ``base``
and ``edited``, and :func:`merge_onto` applies exactly those onto ``disk``.
Anything the user did not touch keeps the value on disk, whoever put it there.
If some *other* writer changed the same leaf, that is a conflict and it is
reported rather than silently resolved - see :class:`ConflictError`.

A content :func:`fingerprint` taken at load and checked at save turns "the file
moved under us" from an invisible race into a question the user gets to answer.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple

from freemicro import padconfig
from freemicro.padconfig import DEFAULT_CONFIG_PATH, PadConfig, PadConfigError

#: Suffix appended to the config filename for the pre-save copy.
BACKUP_SUFFIX = ".bak"

#: Binding fields that must end up as JSON booleans, whatever the browser sent.
_BOOL_FIELDS = frozenset({"submit", "wait", "cycle", "absolute"})

#: Binding fields that must end up as JSON numbers.
_NUMBER_FIELDS = frozenset({"x", "y", "count"})

#: Lighting fields that must end up as JSON numbers.
_LIGHT_NUMBER_FIELDS = ("brightness", "speed", "magic")

#: The same, for a binding's own ``light``. One extra field, and it matters:
#: ``timeout_seconds`` arriving as the string ``"120"`` would fail the range
#: check in the config layer, which is the right place to fail but the wrong
#: message to show somebody who typed a perfectly good number into a form.
_ACTIVITY_LIGHT_NUMBER_FIELDS = ("brightness", "speed", "magic", "timeout_seconds")


# ---------------------------------------------------------------------------
# Locating
# ---------------------------------------------------------------------------

def resolve_paths(explicit: Optional[Path] = None) -> Tuple[Path, Path]:
    """Return ``(load_from, save_to)``.

    They differ in exactly one case - a user with no config of their own, who is
    editing the shipped default. Their first save must create
    ``~/.freemicro/keymap.json`` rather than scribble inside the installed
    package, which would be both surprising and lost on the next upgrade.
    """
    load_from = padconfig.resolve_path(explicit)
    if explicit is not None:
        return load_from, Path(explicit).expanduser()
    if load_from == DEFAULT_CONFIG_PATH:
        return load_from, padconfig.user_path()
    return load_from, load_from


def backup_path(path: Path) -> Path:
    """Where the previous version of ``path`` is kept."""
    return path.with_name(path.name + BACKUP_SUFFIX)


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

def read_document(path: Path) -> Dict[str, Any]:
    """Load the raw JSON document, comments and all."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise PadConfigError(f"could not read {path}: {exc}") from exc
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise PadConfigError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise PadConfigError(f"{path} must contain a JSON object")
    return data


def fingerprint(path: Path) -> str:
    """A content hash of the file, or ``""`` when it does not exist.

    Content rather than mtime on purpose: mtime granularity is coarse enough to
    miss two writes in the same instant, and a file rewritten with identical
    content is not a conflict worth bothering anyone about.
    """
    try:
        data = Path(path).expanduser().read_bytes()
    except OSError:
        return ""
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# Merging - the difference between "save my edit" and "overwrite the file"
# ---------------------------------------------------------------------------

class ConflictError(PadConfigError):
    """The file changed underneath the editor in a way we must not resolve.

    Carries the detail the browser needs to explain itself: which leaves clash,
    and what the file currently says. Never raised for a change that merges
    cleanly - an unrelated edit by another process is normal and is merged.
    """

    def __init__(
        self,
        message: str,
        conflicts: Sequence[str] = (),
        disk: Optional[Mapping[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.conflicts = list(conflicts)
        self.disk = dict(disk or {})


class _Missing:
    """Sentinel for "this key is not present", distinct from ``None``."""

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "<missing>"


MISSING = _Missing()


def _walk(
    base: Mapping[str, Any],
    edited: Mapping[str, Any],
    prefix: Tuple[str, ...] = (),
) -> Iterator[Tuple[Tuple[str, ...], Any]]:
    for key in sorted(set(base) | set(edited)):
        was = base.get(key, MISSING)
        now = edited.get(key, MISSING)
        if was is not MISSING and now is not MISSING and was == now:
            continue
        path = prefix + (key,)
        if isinstance(was, dict) and isinstance(now, dict):
            # Recurse so that editing one binding's `text` cannot rewrite the
            # binding next to it. This granularity *is* the fix.
            yield from _walk(was, now, path)
        else:
            yield path, now


def delta(
    base: Mapping[str, Any], edited: Mapping[str, Any]
) -> List[Tuple[Tuple[str, ...], Any]]:
    """Every leaf that differs, as ``(path, new_value)``.

    ``new_value`` is :data:`MISSING` for a deletion. Order is deterministic so
    a report of "what changed" reads the same twice.
    """
    return list(_walk(base, edited))


def describe_path(path: Sequence[str]) -> str:
    """``('bindings', 'ACT06', 'text')`` -> ``bindings.ACT06.text``."""
    return ".".join(path)


def _read_at(document: Mapping[str, Any], path: Sequence[str]) -> Any:
    node: Any = document
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return MISSING
        node = node[key]
    return node


def _write_at(document: Dict[str, Any], path: Sequence[str], value: Any) -> None:
    node: Dict[str, Any] = document
    for key in path[:-1]:
        child = node.get(key)
        if not isinstance(child, dict):
            child = {}
            node[key] = child
        node = child
    last = path[-1]
    if value is MISSING:
        node.pop(last, None)
    else:
        node[last] = json.loads(json.dumps(value))


def merge_onto(
    disk: Mapping[str, Any],
    base: Mapping[str, Any],
    edited: Mapping[str, Any],
) -> Tuple[Dict[str, Any], List[str], List[str]]:
    """Apply only ``base -> edited`` changes onto ``disk``.

    Returns ``(merged, changed, conflicts)``. A conflict is a leaf that both
    the editor and somebody else moved, to *different* values - the one case
    where guessing would lose somebody's work. Everything else merges: a key
    the user never touched keeps whatever the file says, which is precisely
    what stops one edit from rewriting a config.
    """
    merged: Dict[str, Any] = json.loads(json.dumps(disk))
    changed: List[str] = []
    conflicts: List[str] = []
    for path, value in delta(base, edited):
        was = _read_at(base, path)
        on_disk = _read_at(disk, path)
        if on_disk != was and on_disk != value:
            conflicts.append(describe_path(path))
            continue
        _write_at(merged, path, value)
        changed.append(describe_path(path))
    return merged, changed, conflicts


# ---------------------------------------------------------------------------
# Normalising
# ---------------------------------------------------------------------------

def _as_bool(value: Any) -> Any:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("true", "yes", "on", "1"):
            return True
        if lowered in ("false", "no", "off", "0", ""):
            return False
    if isinstance(value, (int, float)):
        return bool(value)
    return value


def _as_number(value: Any) -> Any:
    """Coerce a numeric-looking string to a number, leaving junk alone.

    Leaving junk alone matters: :func:`normalise` runs over configs people wrote
    by hand, and silently turning an unparseable value into ``0`` would hide a
    typo instead of reporting it. Validation catches it a moment later with a
    message naming the field.
    """
    if isinstance(value, bool) or isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        try:
            number = float(value.strip())
        except ValueError:
            return value
        return int(number) if number.is_integer() else number
    return value


def normalise(document: Mapping[str, Any]) -> Dict[str, Any]:
    """Return a copy with browser-supplied fields coerced to JSON types.

    An HTML form yields strings for everything. Writing ``"x": "40"`` would pass
    validation (``validate_params`` checks *which* fields exist, not their
    types) and then fail at the moment the key is pressed - the worst possible
    place. Coercing here keeps that failure in the editor.
    """
    data = json.loads(json.dumps(document))  # deep copy through plain JSON types
    bindings = data.get("bindings")
    if isinstance(bindings, dict):
        for binding in bindings.values():
            if not isinstance(binding, dict):
                continue
            for field in list(binding):
                if field in _BOOL_FIELDS:
                    binding[field] = _as_bool(binding[field])
                elif field in _NUMBER_FIELDS:
                    binding[field] = _as_number(binding[field])
            light = binding.get("light")
            if isinstance(light, dict):
                for field in _ACTIVITY_LIGHT_NUMBER_FIELDS:
                    if field in light and light[field] is not None:
                        light[field] = _as_number(light[field])

    lighting = data.get("lighting")
    if isinstance(lighting, dict):
        if "enabled" in lighting:
            lighting["enabled"] = _as_bool(lighting["enabled"])
        states = lighting.get("states")
        if isinstance(states, dict):
            for light in states.values():
                if not isinstance(light, dict):
                    continue
                for field in _LIGHT_NUMBER_FIELDS:
                    if field in light and light[field] is not None:
                        light[field] = _as_number(light[field])

    joystick = data.get("joystick")
    if isinstance(joystick, dict):
        for field in ("deadzone", "origin"):
            if field in joystick:
                joystick[field] = _as_number(joystick[field])
    return data


# ---------------------------------------------------------------------------
# Validating
# ---------------------------------------------------------------------------

def validate(document: Mapping[str, Any]) -> PadConfig:
    """Parse a document exactly as the CLI would. Raises on anything illegal."""
    return padconfig.parse(normalise(document))


def describe(pad: PadConfig) -> Dict[str, Any]:
    """A compact summary of a parsed config, for the browser to display."""
    from freemicro.device.lighting import color_to_hex, effect_name
    from freemicro.state.engine import AgentState

    bindings: Dict[str, Any] = {}
    for input_id, action in pad.bindings.items():
        bindings[input_id] = {
            "label": action.label,
            "kind": action.kind,
            "summary": action.describe(),
            "comment": action.comment,
            # What the pad shows while this key is held, resolved the same way
            # the renderer resolves it, so the browser draws what will happen
            # rather than what the document happens to spell.
            "light": None if action.light is None else {
                "hex": color_to_hex(action.light.color),
                "effect": action.light.effect,
                "effect_name": effect_name(action.light.effect),
                "brightness": action.light.brightness,
                "speed": action.light.speed,
                "zones": list(action.light.zones),
                "timeout_seconds": action.light.timeout_seconds,
                "summary": action.light.describe(),
            },
        }
    states: Dict[str, Any] = {}
    for state in AgentState:
        light = pad.lighting.for_state(state)
        if light is None:
            continue
        states[state.value] = {
            "hex": color_to_hex(light.color),
            "effect": light.effect,
            "effect_name": effect_name(light.effect),
            "brightness": light.brightness,
            "speed": light.speed,
        }
    return {
        "bindings": bindings,
        "states": states,
        "warnings": list(pad.warnings),
    }


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

def save_document(
    path: Path,
    document: Mapping[str, Any],
    base: Optional[Mapping[str, Any]] = None,
    expect: Optional[str] = None,
) -> Dict[str, Any]:
    """Validate, back up, then atomically write to ``path``.

    With ``base`` - the document the editor loaded - only the leaves that
    changed between it and ``document`` are written, on top of whatever the
    file says now. Without it the whole document is written, which is what the
    CLI and the tests want and what the browser must never do.

    ``expect`` is the :func:`fingerprint` taken when the editor loaded the
    file. If the file has moved on, a clean merge proceeds (and says so), and a
    genuine clash raises :class:`ConflictError` rather than picking a winner.

    Returns a report the browser can show: where it went, what actually
    changed, whether a backup was made, the new fingerprint, and any warnings
    the config layer raised (an unknown input id is a warning, not an error - a
    future firmware key can be bound today).
    """
    target = Path(path).expanduser()
    current = fingerprint(target)
    changed: List[str] = []
    merged_from_disk = False

    if base is not None:
        try:
            disk = read_document(target) if target.exists() else {}
        except PadConfigError:
            # An unreadable or corrupt file cannot be merged onto. Writing the
            # editor's whole document is the honest recovery, and the .bak
            # below keeps whatever was there.
            disk = {}
        merged, changed, conflicts = merge_onto(disk, base, document)
        if conflicts:
            raise ConflictError(
                f"{target} was changed by something else while you were "
                "editing, and these settings were changed in both places: "
                + ", ".join(conflicts)
                + ". Nothing has been written.",
                conflicts=conflicts,
                disk=disk,
            )
        merged_from_disk = expect is not None and current != expect
        document = merged
    elif expect is not None and current != expect:
        raise ConflictError(
            f"{target} was changed by something else since this page loaded "
            "it. Nothing has been written - reload to see what it says now.",
            conflicts=[],
        )

    normalised = normalise(document)
    pad = padconfig.parse(normalised)  # raises PadConfigError; nothing written

    target.parent.mkdir(parents=True, exist_ok=True)
    backup: Optional[Path] = None
    if target.exists():
        backup = backup_path(target)
        try:
            backup.write_bytes(target.read_bytes())
        except OSError as exc:
            raise PadConfigError(f"could not write {backup}: {exc}") from exc

    text = json.dumps(normalised, indent=2, ensure_ascii=False) + "\n"
    handle = tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=str(target.parent),
        prefix=target.name + ".",
        suffix=".tmp",
        delete=False,
    )
    temp = Path(handle.name)
    try:
        with handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        # tempfile creates 0600. Inherit whatever the file already had so an
        # edit through the UI never silently changes the file's permissions;
        # a brand-new config keeps the private 0600, which is right for a file
        # that can name shell commands.
        if backup is not None:
            os.chmod(str(temp), os.stat(str(target)).st_mode & 0o7777)
        os.replace(str(temp), str(target))
    except OSError as exc:
        temp.unlink(missing_ok=True)
        raise PadConfigError(f"could not write {target}: {exc}") from exc

    return {
        "path": str(target),
        "backup": str(backup) if backup is not None else None,
        "warnings": list(pad.warnings),
        # Exactly which leaves were written. The browser shows this, so a save
        # that touched more than the user expected is visible instead of silent.
        "changed": changed,
        "merged_from_disk": merged_from_disk,
        "fingerprint": fingerprint(target),
    }


def input_ids(document: Mapping[str, Any]) -> List[str]:
    """Every input id the document binds, comment keys excluded."""
    bindings = document.get("bindings")
    if not isinstance(bindings, dict):
        return []
    return [key for key in bindings if not key.startswith("_")]


__all__ = [
    "BACKUP_SUFFIX",
    "MISSING",
    "ConflictError",
    "backup_path",
    "delta",
    "describe",
    "describe_path",
    "fingerprint",
    "merge_onto",
    "input_ids",
    "normalise",
    "read_document",
    "resolve_paths",
    "save_document",
    "validate",
]
