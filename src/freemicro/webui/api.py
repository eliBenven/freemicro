"""The JSON API behind the editor, kept free of HTTP.

Every handler here takes plain Python and returns ``(status, payload)``, which
means the whole API is testable without a socket, a browser or a pad. The HTTP
layer in :mod:`freemicro.webui.server` does nothing but authenticate, route and
serialise.

The API's contract with the rest of FreeMicro is narrow on purpose:

* schema comes from :data:`freemicro.input.actions.REGISTRY` and
  :data:`freemicro.device.lighting.EFFECTS`, so a new action kind or effect
  shows up in the UI without anyone editing the UI,
* saving goes through :mod:`freemicro.webui.configio`, which validates with
  :func:`freemicro.padconfig.parse` before writing,
* hardware goes through :class:`freemicro.webui.padlink.PadLink`, which refuses
  to fight another process for the device.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from freemicro.device.lighting import ZONES
from freemicro.padconfig import (
    DEFAULT_CONFIG_PATH,
    JOYSTICK_INPUTS,
    KNOWN_INPUTS,
    LIGHTING_METHODS,
    PadConfigError,
)
from freemicro.state.engine import AgentState
from freemicro.webui import apps as appdir
from freemicro.webui import configio, keycaps, layouts, starters
from freemicro.webui.layout import (
    AGENT_SECTION,
    FACTORY_PRESETS,
    FIELD_HINTS,
    FIRMWARE_CONTROLS,
    LIT_INPUTS,
    PAIRED_INPUTS,
    agent_policies,
    effect_choices,
    pad_layout,
)
from freemicro.webui.padlink import PadLink

Response = Tuple[int, Dict[str, Any]]

#: ``lighting.on_exit`` values, mirrored from padconfig's private tuple so the
#: UI cannot offer one the parser rejects.
EXIT_MODES = ("leave", "off", "breath")


def _store():
    """The session store - the same one the pad reads, by construction.

    This page is the consumer that most obviously must not disagree with the
    LEDs, and it once did: it built its own store and passed two of the four
    TTLs, so a user who tuned the other two got a page that decayed differently
    from their own hardware. It no longer builds anything. There is one
    construction, :func:`freemicro.state.engine.default_store`, and every
    surface calls it, so "the web UI disagrees with the pad" cannot be
    reintroduced here one keyword at a time.

    The decay itself lives in :meth:`StateStore.sessions`, which is the single
    implementation the renderers, the slot resolver and ``freemicro status``
    all read. Nothing here re-derives it, and nothing here passes
    ``decay=False``: a view that shows a retired claim as the current state is
    a view that contradicts the hardware.
    """
    from freemicro.state.engine import default_store

    return default_store()


def _agent_slots_wired() -> bool:
    """Does the runtime actually read ``agent_keys`` yet?

    Asked of :class:`~freemicro.padconfig.PadConfig` rather than hardcoded, so
    this page can never claim a feature is live when it is not - or keep
    apologising for one that is.
    """
    import dataclasses

    from freemicro.padconfig import PadConfig

    return any(f.name == AGENT_SECTION for f in dataclasses.fields(PadConfig))


def _key_names() -> list:
    """Every base key name :mod:`freemicro.input.keys` can resolve.

    Named keys, printable characters and the punctuation aliases, sorted with
    the names people reach for first. A dropdown built from this cannot offer
    something the parser would then refuse.
    """
    from freemicro.input.keys import CHAR_KEY_CODES, KEY_CODES, LITERAL_ALIASES

    names = set(KEY_CODES) | set(LITERAL_ALIASES) | set(CHAR_KEY_CODES)
    return sorted(names, key=lambda n: (len(n) == 1, n))


def _package_stamp() -> float:
    """The newest modification time in the installed FreeMicro package.

    Python holds imported modules in memory, so a web UI left running while
    the code underneath it is updated goes on serving the old one - which
    produced a genuinely baffling "unknown action 'focus_session'" for an
    action that existed on disk. Cheap to notice, so notice it.
    """
    import freemicro

    root = Path(freemicro.__file__).resolve().parent
    newest = 0.0
    try:
        for path in root.rglob("*.py"):
            newest = max(newest, path.stat().st_mtime)
    except OSError:
        return 0.0
    return newest


class Api:
    """One instance per running server."""

    def __init__(self, config_path: Optional[Path] = None) -> None:
        self.load_path, self.save_path = configio.resolve_paths(config_path)
        self.pad = PadLink()
        #: What the code on disk looked like when this process imported it.
        self.stamp = _package_stamp()

    # -- read-only --------------------------------------------------------

    def schema(self) -> Response:
        """Everything the browser needs to render editors it did not hard-code."""
        from freemicro.input.actions import REGISTRY

        actions = []
        for kind in sorted(REGISTRY):
            spec = REGISTRY[kind]
            fields = []
            for name in spec.required + spec.optional:
                hint = dict(FIELD_HINTS.get(name, {"widget": "text"}))
                hint["name"] = name
                hint["required"] = name in spec.required
                fields.append(hint)
            actions.append(
                {
                    "kind": kind,
                    "summary": spec.summary,
                    "required": list(spec.required),
                    "optional": list(spec.optional),
                    "fields": fields,
                }
            )
        return 200, {
            "actions": actions,
            "effects": effect_choices(),
            "zones": list(ZONES),
            "methods": list(LIGHTING_METHODS),
            "exit_modes": list(EXIT_MODES),
            "states": [state.value for state in AgentState],
            "presets": FACTORY_PRESETS,
            "layout": pad_layout(),
            "known_inputs": list(KNOWN_INPUTS),
            "joystick_inputs": list(JOYSTICK_INPUTS),
            # Which inputs have an addressable LED. Everything else is lit only
            # by a global zone, and the UI must not pretend otherwise.
            "lit_inputs": list(LIT_INPUTS),
            "agent_policies": agent_policies(),
            "agent_section": AGENT_SECTION,
            # Asked, not assumed: the runtime grew a real parser for this
            # section, and a UI that keeps claiming "not wired" after it lands
            # is as misleading as one that claims the opposite.
            "agent_slots_wired": _agent_slots_wired(),
            # A slot holds a project *directory*, not a session id.
            "agent_slot_kind": "project",
            # Caps that sit over two switches: editing one must write both.
            "paired_inputs": PAIRED_INPUTS,
            # Drawn on the diagram, owned by the firmware, not bindable.
            "controls": FIRMWARE_CONTROLS,
            "dictation": starters.DICTATION_CHOICES,
            # Which physical cap is on each key. Presentation only, stored in a
            # top-level section - see freemicro.webui.keycaps.
            "keycaps": keycaps.catalogue(),
            "keycap_section": "keycaps",
            "keycap_rules": keycaps.SUGGESTIONS,
            "keycap_double": list(keycaps.DOUBLE_WIDTH),
            # "you fitted this cap, so shall the key do this?" - offered, never
            # applied on its own.
            "keycap_bindings": keycaps.BINDING_FOR_CAP,
            # Every base key the combo parser accepts, so the browser offers a
            # list instead of asking someone to guess the spelling.
            "key_names": _key_names(),
            "modifiers": ["cmd", "ctrl", "option", "shift"],
        }

    def projects(self) -> Response:
        """Live project directories, freshest first - what an Agent Key holds.

        An Agent Key stands for a *project*, not a session: terminal tabs come
        and go, the repository does not. This groups the same sessions the
        renderer sees, so the slot picker offers exactly what the pad would
        light up.
        """
        from freemicro.agentkeys import group_projects

        store = _store()
        try:
            live = store.sessions()          # decayed, exactly as the LEDs see it
        except OSError as exc:
            return 200, {"projects": [], "error": str(exc)}
        found = group_projects(live, now=time.time(), decay=store.decay)
        return 200, {
            "projects": [
                {
                    "path": project.path,
                    "label": project.label,
                    "state": project.state.value,
                    "sessions": project.session_count,
                    "last_active": project.last_active,
                }
                for project in found
            ],
            "error": "",
        }

    def starters(self) -> Response:
        """Complete one-click layouts, with what each one is for."""
        return 200, {"starters": starters.starters()}

    def layouts(self) -> Response:
        """Named whole pads: the shipped starters plus the user's own."""
        return 200, {
            "layouts": layouts.catalogue(),
            "directory": str(layouts.directory()),
        }

    def layout_save(self, body: Dict[str, Any]) -> Response:
        """Keep the current pad under a name you can switch back to."""
        document = body.get("document")
        if not isinstance(document, dict):
            return 400, {"ok": False, "error": "expected a 'document' object"}
        try:
            saved = layouts.save(str(body.get("name") or ""), document)
        except PadConfigError as exc:
            return 200, {"ok": False, "error": str(exc)}
        saved["ok"] = True
        saved["layouts"] = layouts.catalogue()
        return 200, saved

    def layout_delete(self, body: Dict[str, Any]) -> Response:
        try:
            gone = layouts.delete(str(body.get("id") or ""))
        except PadConfigError as exc:
            return 200, {"ok": False, "error": str(exc)}
        return 200, {"ok": gone, "layouts": layouts.catalogue(),
                     "error": "" if gone else "no such layout"}

    def apps(self) -> Response:
        """Applications installed on this Mac, for the ``app`` action's picker.

        Filesystem only - nothing is launched, opened or inspected beyond its
        name, and an unreadable directory yields a shorter list rather than an
        error.
        """
        return 200, {"apps": appdir.installed_apps()}

    def sessions(self) -> Response:
        """Live Claude Code sessions, freshest first - what a slot can hold.

        Read-only, and it uses the same store the renderer resolves state from,
        so the list in the slot picker is the list the pad would actually show.
        """
        store = _store()
        try:
            live = store.sessions()          # decayed, exactly as the LEDs see it
        except OSError as exc:
            return 200, {"sessions": [], "error": str(exc)}
        return 200, {
            "sessions": [
                {
                    "session_id": s.session_id,
                    # The *effective* state - what the pad is showing. Never
                    # the raw claim: a session that was interrupted emits no
                    # hook at all, so believing its last "working" would leave
                    # this page insisting on blue while the key sat dark.
                    "state": s.state.value,
                    # The retired claim, offered as detail rather than as the
                    # answer, so "why did it go idle?" is still inspectable.
                    "claim": s.claim.value,
                    "stale": s.stale,
                    "claim_text": s.describe_claim(),
                    "title": s.title,
                    "cwd": s.cwd,
                    "updated_at": s.updated_at,
                }
                for s in live
            ],
            "error": "",
        }

    def config(self) -> Response:
        """The raw document, plus a parsed summary and where it came from."""
        try:
            document = configio.read_document(self.load_path)
        except PadConfigError as exc:
            return 200, {
                "document": None,
                "error": str(exc),
                "load_path": str(self.load_path),
                "save_path": str(self.save_path),
                "is_default": self.load_path == DEFAULT_CONFIG_PATH,
            }
        payload: Dict[str, Any] = {
            "document": document,
            # Taken with the document, checked at save: this is how the editor
            # notices that something else moved the file underneath it.
            "fingerprint": configio.fingerprint(self.load_path),
            "load_path": str(self.load_path),
            "save_path": str(self.save_path),
            "is_default": self.load_path == DEFAULT_CONFIG_PATH,
            "backup_path": str(configio.backup_path(self.save_path)),
            "error": "",
        }
        try:
            payload["summary"] = configio.describe(configio.validate(document))
        except PadConfigError as exc:
            payload["error"] = str(exc)
        return 200, payload

    def device(self) -> Response:
        """Pad availability - and whether this process is running stale code."""
        status = self.pad.status()
        newest = _package_stamp()
        if newest > self.stamp + 1.0:
            # The page turns this into a blocking panel and supplies the
            # command itself, so this is the diagnosis and nothing else.
            status["restart"] = (
                "FreeMicro has been updated on disk since this page's server "
                "started, so the running process is still answering with the "
                "old code. That is how you get a 404 on a route that exists "
                "and an unknown action for one that exists."
            )
        else:
            status["restart"] = ""
        return 200, status

    # -- writes -----------------------------------------------------------

    def validate(self, body: Dict[str, Any]) -> Response:
        """Check a document without saving it. Drives the live error banner."""
        document = body.get("document")
        if not isinstance(document, dict):
            return 400, {"ok": False, "error": "expected a 'document' object"}
        try:
            pad = configio.validate(document)
        except PadConfigError as exc:
            return 200, {"ok": False, "error": str(exc)}
        return 200, {"ok": True, "error": "", "summary": configio.describe(pad)}

    def save(self, body: Dict[str, Any]) -> Response:
        """Write the editor's *changes*, not the editor's whole document.

        ``base`` is what the page loaded. Given it, only the leaves that
        differ are written, on top of whatever the file says at this instant - 
        so an edit to one colour cannot rewrite sixteen bindings, and an
        unrelated change made by the CLI in the meantime survives.

        Without ``base`` this is a full overwrite, which is what a script or a
        test may legitimately want and what the browser never sends.
        """
        document = body.get("document")
        if not isinstance(document, dict):
            return 400, {"ok": False, "error": "expected a 'document' object"}
        base = body.get("base")
        expect = body.get("fingerprint")
        try:
            report = configio.save_document(
                self.save_path,
                document,
                base=base if isinstance(base, dict) else None,
                expect=expect if isinstance(expect, str) and expect else None,
            )
        except configio.ConflictError as exc:
            # Not an error the user caused, and not one to resolve for them.
            return 200, {
                "ok": False,
                "conflict": True,
                "error": str(exc),
                "conflicts": exc.conflicts,
                "document": exc.disk or None,
                "fingerprint": configio.fingerprint(self.save_path),
            }
        except PadConfigError as exc:
            return 200, {"ok": False, "error": str(exc)}
        # After the first save the user has a config of their own, so subsequent
        # loads must read it rather than the shipped default.
        self.load_path = self.save_path
        report["ok"] = True
        report["error"] = ""
        return 200, report

    # -- hardware ---------------------------------------------------------

    def preview(self, body: Dict[str, Any]) -> Response:
        """Send the look being edited to the pad, right now."""
        from freemicro.device.lighting import LightingError

        zones = body.get("zones") or ["agent_keys"]
        if not isinstance(zones, list):
            return 400, {"ok": False, "error": "'zones' must be a list"}
        try:
            result = self.pad.preview(
                color=body.get("color", 0),
                effect=body.get("effect", 1),
                brightness=float(body.get("brightness", 1.0)),
                speed=float(body.get("speed", 0.0)),
                zones=zones,
                method=str(body.get("method", "rgbcfg")),
            )
        except (LightingError, ValueError, TypeError) as exc:
            return 200, {"ok": False, "error": str(exc)}
        except RuntimeError as exc:
            return 200, {"ok": False, "error": str(exc), "unavailable": True}
        result["ok"] = True
        return 200, result

    def blank(self, body: Dict[str, Any]) -> Response:
        zones = body.get("zones") or ["agent_keys"]
        try:
            result = self.pad.blank(zones=zones)
        except RuntimeError as exc:
            return 200, {"ok": False, "error": str(exc), "unavailable": True}
        result["ok"] = True
        return 200, result

    def capture_start(self, body: Dict[str, Any]) -> Response:
        try:
            result = self.pad.start_capture()
        except RuntimeError as exc:
            return 200, {"ok": False, "error": str(exc), "unavailable": True}
        result["ok"] = True
        return 200, result

    def capture_stop(self, body: Dict[str, Any]) -> Response:
        result = self.pad.stop_capture()
        result["ok"] = True
        return 200, result

    def capture_events(self, since: int = 0) -> Response:
        result = self.pad.events(since=since)
        result["ok"] = True
        return 200, result

    # -- lifecycle --------------------------------------------------------

    def close(self) -> None:
        self.pad.close()


__all__ = ["Api", "EXIT_MODES", "Response"]
