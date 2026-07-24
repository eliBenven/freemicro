"""A diagnostic report a stranger can safely paste into a public bug tracker.

When somebody files "the lights don't work", the answer is almost always in a
handful of facts nobody thinks to include: which macOS permission is missing,
which transport the pad is on, which firmware it runs, whether the ChatGPT app
is contending for the same channel, and whether their config even parsed. This
module gathers all of that in one call so an issue can start with evidence
instead of a guess.

Which makes redaction the load-bearing feature, not a nicety. A FreeMicro
config can contain **arbitrary shell commands and AppleScript** - API tokens in
a `curl`, a client's name in a path, an internal hostname. Pasting a raw config
into a public issue would be a genuine leak, so this module never emits one:

* ``shell`` and ``applescript`` bindings are reported as *kind and size only*.
  Their contents never reach the report, in any output format.
* Absolute paths are reduced to ``<config>/…`` when they live in the FreeMicro
  config directory, ``<freemicro>/…`` when they ship with the package, and
  ``<path>`` otherwise. Nothing else is trusted to be non-personal.
* Free text (labels, error strings, warnings) has the home directory and the
  account name scrubbed out of it.

The rule is: **structure is diagnostic, contents are personal.** We report how
many shell bindings exist and which inputs they sit on, never what they run.

Nothing here writes to the pad. The device section is a read-only
``device.status`` round trip and honours ``FREEMICRO_NO_DEVICE``.
"""

from __future__ import annotations

import json
import os
import platform
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from freemicro import __version__, compat
from freemicro.config import config_home

#: Bump when the JSON shape changes in a way a reader would care about.
SCHEMA = 1

#: What replaces anything we will not print.
REDACTED = "<redacted>"

#: Action kinds whose parameters are, by design, arbitrary code. Their contents
#: are never included - see the module docstring. Kept as a frozenset rather
#: than derived from the registry so that adding an action kind cannot silently
#: widen what gets published; a new dangerous kind must be added here on
#: purpose. ``tests/test_diagnostics.py`` asserts this stays in sync.
UNSAFE_KINDS = frozenset({"shell", "applescript"})

#: Longest free-text value we will echo back (labels, bindings' typed text).
MAX_TEXT = 60

_PACKAGE_ROOT = Path(__file__).resolve().parent


# ---------------------------------------------------------------------------
# Redaction primitives
# ---------------------------------------------------------------------------

def _account_name() -> str:
    for key in ("USER", "LOGNAME", "USERNAME"):
        value = os.environ.get(key)
        if value:
            return value
    try:
        return Path.home().name
    except (OSError, RuntimeError):  # pragma: no cover - no home on this host
        return ""


def redact_text(value: Any) -> str:
    """Scrub the things that identify a machine's owner out of free text.

    Two substitutions, in order: the home directory becomes ``~``, then the
    account name becomes ``<user>``. Blunt on purpose - an over-eager
    substitution costs a slightly odd-looking word in an issue, while a missed
    one costs somebody their username or a client's directory name.
    """
    text = "" if value is None else str(value)
    if not text:
        return text
    try:
        home = str(Path.home())
    except (OSError, RuntimeError):  # pragma: no cover
        home = ""
    if home and home not in ("/", ""):
        text = text.replace(home, "~")
    user = _account_name()
    if len(user) > 2:
        text = text.replace(user, "<user>")
    return text


def truncate(value: Any, limit: int = MAX_TEXT) -> str:
    """Shorten a value for display, marking that it was shortened."""
    text = "" if value is None else str(value)
    if len(text) <= limit:
        return text
    return text[:limit] + f"… (+{len(text) - limit} chars)"


def redact_path(value: Any) -> str:
    """Reduce a filesystem path to something safe to publish.

    Paths inside the FreeMicro config directory keep their tail (``keymap.json``
    is useful and not personal). Paths inside the installed package keep theirs
    for the same reason. Everything else becomes ``<path>`` - a directory name
    on somebody's disk is exactly the sort of thing that turns out to be a
    client's name.
    """
    if value in (None, ""):
        return ""
    raw = Path(str(value)).expanduser()
    for root, marker in ((config_home(), "<config>"), (_PACKAGE_ROOT, "<freemicro>")):
        try:
            relative = _resolve(raw).relative_to(_resolve(root))
        except (ValueError, OSError):
            continue
        tail = relative.as_posix()
        return marker if tail == "." else f"{marker}/{tail}"
    return "<path>"


def _resolve(path: Path) -> Path:
    try:
        return path.resolve()
    except (OSError, RuntimeError):  # pragma: no cover - exotic filesystems
        return path


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------

def _system_section() -> Dict[str, Any]:
    mac = ""
    if sys.platform == "darwin":
        try:
            mac = platform.mac_ver()[0]
        except Exception:  # noqa: BLE001 - a version string is never worth a crash
            mac = ""
    return {
        "platform": sys.platform,
        "system": platform.system(),
        "release": platform.release(),
        "macos_version": mac,
        "machine": platform.machine(),
        "python_version": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        # The path would identify the user; whether it is a venv is what
        # actually explains "command not found" and import problems.
        "in_virtualenv": sys.prefix != getattr(sys, "base_prefix", sys.prefix),
    }


def _freemicro_section() -> Dict[str, Any]:
    return {
        "version": __version__,
        "install": (
            "source checkout (editable)"
            if _PACKAGE_ROOT.parent.name == "src"
            else "installed package"
        ),
        "package_path": redact_path(_PACKAGE_ROOT),
    }


def _environment_section() -> Dict[str, Any]:
    """Which FreeMicro environment variables are set - never their values.

    The values are paths. Whether they are set is what changes behaviour.
    """
    names = (
        "FREEMICRO_NO_DEVICE",
        "FREEMICRO_HOME",
        "FREEMICRO_KEYMAP",
        "XDG_CONFIG_HOME",
    )
    return {name: bool(os.environ.get(name)) for name in names}


def _permissions_section() -> Dict[str, Any]:
    from freemicro import permissions

    section: Dict[str, Any] = {}
    try:
        granted, detail = permissions.input_monitoring()
        section["input_monitoring"] = granted
        section["input_monitoring_detail"] = redact_text(detail)
    except Exception as exc:  # noqa: BLE001 - a probe must never fail the report
        section["input_monitoring"] = None
        section["input_monitoring_detail"] = redact_text(exc)
    try:
        granted, detail = permissions.accessibility()
        section["accessibility"] = granted
        section["accessibility_detail"] = redact_text(detail)
    except Exception as exc:  # noqa: BLE001
        section["accessibility"] = None
        section["accessibility_detail"] = redact_text(exc)
    try:
        section["host_app"] = permissions.host_app()
    except Exception:  # noqa: BLE001
        section["host_app"] = ""
    try:
        section["chatgpt_running"] = permissions.chatgpt_running()
    except Exception:  # noqa: BLE001
        section["chatgpt_running"] = None
    return section


def _device_section(probe: bool) -> Dict[str, Any]:
    """Transport, firmware and battery - via a read-only status round trip."""
    from freemicro import device as device_module

    section: Dict[str, Any] = {
        "vendor_id": f"0x{device_module.VENDOR_ID:04X}",
        "product_id": f"0x{device_module.PRODUCT_ID:04X}",
        "supported_platform": False,
        "unsupported_reason": "",
        "present": False,
        "transport": None,
        "opened": None,
        "status_roundtrip": None,
        "battery": None,
        "charging": None,
        "firmware": compat.check(None).to_dict(),
    }
    try:
        section["supported_platform"] = device_module.is_supported()
        if not section["supported_platform"]:
            section["unsupported_reason"] = redact_text(
                device_module.unsupported_reason()
            )
        transport = device_module.device_transport()
        section["transport"] = transport
        section["present"] = transport is not None
    except Exception as exc:  # noqa: BLE001
        section["unsupported_reason"] = redact_text(exc)
        return section

    if not section["present"] or not probe:
        return section

    try:
        handle = device_module.shared_device()
        section["opened"] = handle is not None
        if handle is not None:
            status = handle.self_test()
            section["status_roundtrip"] = status is not None
            if isinstance(status, Mapping):
                section["battery"] = status.get("battery")
                section["charging"] = status.get("is_charging")
                section["firmware"] = compat.check_status(status).to_dict()
    except Exception as exc:  # noqa: BLE001 - a flaky pad must not lose the report
        section["status_roundtrip"] = False
        section["unsupported_reason"] = redact_text(exc)
    finally:
        try:
            device_module.close_shared()
        except Exception:  # noqa: BLE001
            pass
    return section


def _renderers_section() -> Dict[str, Any]:
    from freemicro.renderers.base import REGISTRY

    renderers: Dict[str, Any] = {}
    for name, cls in sorted(REGISTRY.items()):
        entry: Dict[str, Any] = {
            "priority": getattr(cls, "priority", 0),
            "experimental": bool(getattr(cls, "experimental", False)),
            "available": None,
            "error": "",
        }
        instance = None
        try:
            instance = cls()
            entry["available"] = bool(instance.available())
        except Exception as exc:  # noqa: BLE001 - probing must not be fatal
            entry["error"] = redact_text(exc)
        finally:
            if instance is not None:
                try:
                    instance.close()
                except Exception:  # noqa: BLE001
                    pass
        renderers[name] = entry
    return {"renderers": renderers}


def _hooks_section() -> Dict[str, Any]:
    """Whether Claude Code will actually call us. Paths redacted."""
    from freemicro import hooks_install

    try:
        status = hooks_install.status()
    except Exception as exc:  # noqa: BLE001
        return {"error": redact_text(exc)}
    return {
        "settings_path": redact_path(status.get("path")),
        "settings_exists": bool(status.get("exists")),
        "installed": bool(status.get("installed")),
        "partial": bool(status.get("partial")),
        "events": list(status.get("events") or []),
        "missing_events": list(status.get("missing_events") or []),
        "stale_commands": len(status.get("stale_commands") or []),
        # The command is an absolute path to somebody's venv. Whether the
        # binary it names exists is the whole diagnostic value.
        "binary_exists": bool(status.get("binary_exists")),
    }


def _daemon_section() -> Dict[str, Any]:
    from freemicro import daemon

    section: Dict[str, Any] = {"installed": None, "running": None}
    try:
        section["installed"] = daemon.is_installed()
    except Exception as exc:  # noqa: BLE001
        section["error"] = redact_text(exc)
    try:
        section["running"] = daemon.is_running()
    except Exception as exc:  # noqa: BLE001
        section["error"] = redact_text(exc)
    return section


# ---------------------------------------------------------------------------
# Config - the part that needs the most care
# ---------------------------------------------------------------------------

def _binding_entry(input_id: str, action: Any) -> Dict[str, Any]:
    """One binding, described without publishing anything it could contain."""
    kind = getattr(action, "kind", "?")
    params = dict(getattr(action, "params", {}) or {})
    entry: Dict[str, Any] = {"input": input_id, "action": kind}
    if kind in UNSAFE_KINDS:
        # Structure only. Length is a useful "is this the 3-line one or the
        # 200-line one?" signal and leaks nothing.
        size = sum(len(str(value)) for value in params.values())
        entry["detail"] = f"{REDACTED} ({size} chars)"
        entry["redacted"] = True
        return entry
    detail = {}
    for key, value in params.items():
        detail[key] = (
            truncate(redact_text(value)) if isinstance(value, str) else value
        )
    entry["detail"] = detail
    entry["redacted"] = False
    label = getattr(action, "label", "")
    if label and label != input_id:
        entry["label"] = truncate(redact_text(label), 40)
    return entry


def _lighting_entry(lighting: Any) -> Dict[str, Any]:
    from freemicro.device.lighting import color_to_hex, effect_name

    states = {}
    for state, light in (getattr(lighting, "states", {}) or {}).items():
        states[getattr(state, "value", str(state))] = {
            "color": color_to_hex(light.color),
            "effect": effect_name(light.effect),
            "brightness": light.brightness,
            "speed": light.speed,
        }
    return {
        "enabled": bool(lighting.enabled),
        "method": lighting.method,
        "zones": list(lighting.zones),
        "on_exit": lighting.on_exit,
        "states": states,
    }


def _config_section(config_path: Optional[Path] = None) -> Dict[str, Any]:
    from freemicro import padconfig

    section: Dict[str, Any] = {
        "valid": False,
        "path": "",
        "origin": "",
        "error": "",
        "warnings": [],
        "binding_count": 0,
        "action_kinds": {},
        "unsafe_binding_count": 0,
        "bindings": [],
        "lighting": {},
        "joystick": {},
    }
    try:
        pad = padconfig.load(config_path)
    except Exception as exc:  # noqa: BLE001 - PadConfigError, or a broken file
        section["error"] = redact_text(exc)
        try:
            section["path"] = redact_path(padconfig.resolve_path(config_path))
        except Exception:  # noqa: BLE001
            pass
        return section

    kinds: Dict[str, int] = {}
    bindings: List[Dict[str, Any]] = []
    for input_id, action in sorted(pad.bindings.items()):
        kinds[action.kind] = kinds.get(action.kind, 0) + 1
        bindings.append(_binding_entry(input_id, action))

    section.update({
        "valid": True,
        "path": redact_path(pad.source),
        "origin": (
            "built-in default" if pad.origin == "built-in default"
            else redact_path(pad.source)
        ),
        "warnings": [redact_text(w) for w in pad.warnings],
        "binding_count": len(pad.bindings),
        "action_kinds": dict(sorted(kinds.items())),
        "unsafe_binding_count": sum(kinds.get(k, 0) for k in UNSAFE_KINDS),
        "bindings": bindings,
        "lighting": _lighting_entry(pad.lighting),
        "joystick": {
            "deadzone": pad.joystick.deadzone,
            "origin": pad.joystick.origin,
            "directions": list(pad.joystick.directions),
        },
    })
    return section


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------

def collect(
    probe_device: bool = True, config_path: Optional[Path] = None
) -> Dict[str, Any]:
    """Gather the whole redacted report.

    Never raises: every section is individually guarded, because a diagnostic
    that dies on the machine it is diagnosing is worse than useless. Set
    ``probe_device=False`` to skip opening the pad (the round trip takes a
    couple of seconds and needs Input Monitoring).
    """
    sections = (
        ("freemicro", _freemicro_section),
        ("system", _system_section),
        ("environment", _environment_section),
        ("permissions", _permissions_section),
        ("device", lambda: _device_section(probe_device)),
        ("renderers", _renderers_section),
        ("hooks", _hooks_section),
        ("daemon", _daemon_section),
        ("config", lambda: _config_section(config_path)),
    )
    report: Dict[str, Any] = {
        "schema": SCHEMA,
        "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "redaction": {
            "unsafe_kinds": sorted(UNSAFE_KINDS),
            "note": (
                "Contents of shell and applescript bindings are never included. "
                "Paths outside the FreeMicro config directory are replaced with "
                "<path>. Home directory and account name are scrubbed from all "
                "free text."
            ),
        },
    }
    for name, builder in sections:
        try:
            report[name] = builder()
        except Exception as exc:  # noqa: BLE001 - one bad section, not no report
            report[name] = {"error": redact_text(exc)}
    return report


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def render_json(report: Optional[Dict[str, Any]] = None, indent: int = 2) -> str:
    """The machine-readable form, for attaching to an issue verbatim."""
    return json.dumps(report if report is not None else collect(), indent=indent)


def _yes(value: Any) -> str:
    if value is None:
        return "unknown"
    return "yes" if value else "no"


def render_text(report: Optional[Dict[str, Any]] = None) -> str:
    """The human-readable form. Same facts, same redaction, easier to read."""
    data = report if report is not None else collect()
    out: List[str] = []

    def line(text: str = "") -> None:
        out.append(text)

    def section(title: str) -> None:
        line()
        line(title)
        line("-" * len(title))

    fm = data.get("freemicro", {})
    sysinfo = data.get("system", {})
    line(f"freemicro {fm.get('version', '?')} diagnostic report")
    line(f"generated {data.get('generated', '')}  ·  schema {data.get('schema')}")
    line("shell/applescript contents and personal paths are redacted")

    section("System")
    line(f"  platform            {sysinfo.get('system')} {sysinfo.get('release')}"
         + (f" (macOS {sysinfo['macos_version']})"
            if sysinfo.get("macos_version") else ""))
    line(f"  machine             {sysinfo.get('machine')}")
    line(f"  python              {sysinfo.get('python_version')} "
         f"({sysinfo.get('python_implementation')}, "
         f"venv={_yes(sysinfo.get('in_virtualenv'))})")
    line(f"  install             {fm.get('install')}")

    perms = data.get("permissions", {})
    section("Permissions")
    line(f"  input monitoring    {_yes(perms.get('input_monitoring'))}"
         f"  - {perms.get('input_monitoring_detail', '')}")
    line(f"  accessibility       {_yes(perms.get('accessibility'))}"
         f"  - {perms.get('accessibility_detail', '')}")
    line(f"  host app            {perms.get('host_app', '')}")
    line(f"  ChatGPT app running {_yes(perms.get('chatgpt_running'))}")

    dev = data.get("device", {})
    firmware = dev.get("firmware", {}) or {}
    section("Device")
    line(f"  vid:pid             {dev.get('vendor_id')}:{dev.get('product_id')}")
    line(f"  present             {_yes(dev.get('present'))}")
    line(f"  transport           {dev.get('transport') or '-'}")
    line(f"  opened              {_yes(dev.get('opened'))}")
    line(f"  device.status reply {_yes(dev.get('status_roundtrip'))}")
    line(f"  firmware            {firmware.get('reported') or '-'}"
         f"  [{firmware.get('status', 'unknown')}]")
    line(f"  battery             {dev.get('battery')}"
         f"  charging={_yes(dev.get('charging'))}")
    if dev.get("unsupported_reason"):
        line(f"  note                {dev['unsupported_reason']}")

    rend = data.get("renderers", {})
    section("Renderers")
    for name, entry in sorted((rend.get("renderers") or {}).items()):
        flag = "experimental" if entry.get("experimental") else ""
        line(f"  {name:<12} available={_yes(entry.get('available')):<7} {flag}"
             + (f"  {entry['error']}" if entry.get("error") else ""))

    hooks = data.get("hooks", {})
    daemon_info = data.get("daemon", {})
    section("Wiring")
    line(f"  hooks installed     {_yes(hooks.get('installed'))}"
         + ("  (partial)" if hooks.get("partial") else ""))
    line(f"  hook events         {', '.join(hooks.get('events') or []) or '-'}")
    line(f"  hook binary exists  {_yes(hooks.get('binary_exists'))}")
    line(f"  daemon installed    {_yes(daemon_info.get('installed'))}")
    line(f"  daemon running      {_yes(daemon_info.get('running'))}")

    cfg = data.get("config", {})
    section("Config")
    line(f"  valid               {_yes(cfg.get('valid'))}")
    line(f"  source              {cfg.get('origin') or cfg.get('path') or '-'}")
    if cfg.get("error"):
        line(f"  error               {cfg['error']}")
    for warning in cfg.get("warnings") or []:
        line(f"  warning             {warning}")
    kinds = cfg.get("action_kinds") or {}
    line(f"  bindings            {cfg.get('binding_count', 0)}"
         f"  ({', '.join(f'{k}={v}' for k, v in kinds.items()) or 'none'})")
    line(f"  shell/applescript   {cfg.get('unsafe_binding_count', 0)}"
         "  (contents not included)")
    lighting = cfg.get("lighting") or {}
    if lighting:
        line(f"  lighting            enabled={_yes(lighting.get('enabled'))}"
             f"  method={lighting.get('method')}"
             f"  zones={','.join(lighting.get('zones') or [])}")
    if cfg.get("bindings"):
        line("  bound inputs:")
        for entry in cfg["bindings"]:
            detail = entry.get("detail")
            if isinstance(detail, dict):
                detail = " ".join(f"{k}={v!r}" for k, v in detail.items())
            line(f"    {entry['input']:<10} {entry['action']:<12} {detail}")

    if firmware.get("message"):
        section("Firmware compatibility")
        for part in str(firmware["message"]).splitlines():
            line(f"  {part}")

    line()
    return "\n".join(out)


def bundle(
    probe_device: bool = True, config_path: Optional[Path] = None
) -> Dict[str, Any]:
    """Collect once and render both forms - what ``doctor --report`` wants."""
    report = collect(probe_device=probe_device, config_path=config_path)
    return {
        "report": report,
        "text": render_text(report),
        "json": render_json(report),
    }


__all__ = [
    "MAX_TEXT",
    "REDACTED",
    "SCHEMA",
    "UNSAFE_KINDS",
    "bundle",
    "collect",
    "redact_path",
    "redact_text",
    "render_json",
    "render_text",
    "truncate",
]
