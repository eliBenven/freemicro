"""The ``freemicro`` command-line interface.

Subcommands
-----------
``detect``    Run the read-only HID probe and print a capability report.
``install``   Wire FreeMicro into Claude Code's hook settings.
``hook``      Internal: consume one hook event from stdin (called by Claude).
``watch``     Run the renderer loop, lighting your best available target.
``emit``      Manually set a state (for testing renderers/config).
``render``    One-shot: display a single state and exit.
``renderers`` List which renderers are available right now.
``status``    Print the current resolved state across all sessions.
"""

from __future__ import annotations

import argparse
import json
import sys
import time

from freemicro import __version__
from freemicro.config import Config, config_home
from freemicro.state.engine import AgentState, StateStore
from freemicro.state.hooks import classify, session_id_of


def _store(cfg: Config) -> StateStore:
    return StateStore(
        directory=config_home() / "state",
        ttl_seconds=cfg.ttl_seconds,
    )


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------

def cmd_detect(args: argparse.Namespace) -> int:
    from freemicro.detector import probe

    report = probe()
    if args.json:
        print(report.to_json())
        return 0

    print("FreeMicro — hardware capability probe (read-only)\n")
    if not report.hidapi_available:
        for note in report.notes:
            print(f"  ! {note}")
        return 1

    print(f"  HID interfaces found: {len(report.interfaces)}")
    print(f"  Raw 0xFF60 channel:   {'yes' if report.has_raw_channel else 'no'}\n")

    candidates = report.candidate_pads
    if candidates:
        print("  Candidate pad interfaces:")
        for i in candidates:
            raw = " [raw 0xFF60]" if i.is_raw_channel else ""
            name = i.product_string or "(unnamed)"
            print(f"    {i.vid_pid}  {name}{raw}")
        print()

    for note in report.notes:
        print(f"  • {note}")
    print(
        "\n  Help crowdsource the hardware DB: open a Hardware Report issue with"
        "\n  `freemicro detect --json` output. See hardware/capabilities.json."
    )
    return 0


def cmd_install(args: argparse.Namespace) -> int:
    from freemicro.hooks_install import install_hooks

    changed = install_hooks(settings_path=args.settings, dry_run=args.dry_run)
    if args.dry_run:
        print(changed)
    else:
        print(f"Installed FreeMicro hooks into {changed}")
        print("Restart Claude Code, then run `freemicro watch` in another pane.")
    return 0


def cmd_hook(args: argparse.Namespace) -> int:
    cfg = Config.load()
    try:
        event = json.load(sys.stdin)
    except (ValueError, OSError):
        return 0  # Never break Claude Code because of a hook parse error.

    state = classify(event)
    if state is None:
        return 0

    store = _store(cfg)
    session = session_id_of(event)
    if state == AgentState.IDLE:
        store.clear(session)
    else:
        store.update(
            session,
            state,
            title=str(event.get("title", "")),
            cwd=str(event.get("cwd", "")),
        )
    return 0


def cmd_emit(args: argparse.Namespace) -> int:
    cfg = Config.load()
    store = _store(cfg)
    state = AgentState(args.state)
    if state == AgentState.IDLE:
        store.clear(args.session)
    else:
        store.update(args.session, state)
    print(f"{args.session}: {state.value}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    cfg = Config.load()
    store = _store(cfg)
    if args.json:
        winner = store.resolve()
        print(
            json.dumps(
                {
                    "resolved": store.resolved_state().value,
                    "sessions": [s.to_dict() for s in store.sessions()],
                    "winner": winner.to_dict() if winner else None,
                }
            )
        )
        return 0
    sessions = store.sessions()
    print(f"Resolved state: {store.resolved_state().value}")
    if sessions:
        print("Live sessions:")
        for s in sessions:
            print(f"  {s.state.value:8} {s.session_id}  ({int(s.age())}s ago)")
    else:
        print("No live sessions.")
    return 0


def cmd_renderers(args: argparse.Namespace) -> int:
    from freemicro.renderers import REGISTRY, available_renderers

    live = {r.name for r in available_renderers()}
    print("Renderers:")
    for name, cls in sorted(REGISTRY.items(), key=lambda kv: -kv[1].priority):
        mark = "✓" if name in live else " "
        tags = " (experimental)" if cls.experimental else ""
        print(f"  [{mark}] {name:12} priority={cls.priority}{tags}")
    return 0


def cmd_render(args: argparse.Namespace) -> int:
    from freemicro.renderers import select

    cfg = Config.load()
    state = AgentState(args.state)
    renderers = select(prefer=cfg.prefer)
    print(f"Rendering '{state.value}' to: {', '.join(r.name for r in renderers)}")
    for r in renderers:
        r.render(state)
    if args.hold:
        try:
            time.sleep(args.hold)
        except KeyboardInterrupt:
            pass
    for r in renderers:
        r.close()
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    from freemicro.renderers import select

    cfg = Config.load()
    store = _store(cfg)
    renderers = select(prefer=cfg.prefer)
    print(
        f"freemicro watching — targets: {', '.join(r.name for r in renderers)} "
        f"(poll {args.interval}s). Ctrl-C to stop."
    )
    last: AgentState | None = None
    try:
        while True:
            state = store.resolved_state()
            if state != last:
                for r in renderers:
                    r.render(state)
                last = state
            else:
                # Still pump GUI renderers so windows stay responsive.
                for r in renderers:
                    r.render(state)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    finally:
        for r in renderers:
            r.close()
    return 0


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="freemicro",
        description="Turn a macro pad into a live status light for coding agents.",
    )
    p.add_argument("--version", action="version", version=f"freemicro {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    d = sub.add_parser("detect", help="read-only HID capability probe (Milestone 0)")
    d.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    d.set_defaults(func=cmd_detect)

    i = sub.add_parser("install", help="install Claude Code hooks")
    i.add_argument("--settings", help="path to Claude settings.json")
    i.add_argument("--dry-run", action="store_true", help="print, don't write")
    i.set_defaults(func=cmd_install)

    h = sub.add_parser("hook", help="internal: consume a hook event from stdin")
    h.set_defaults(func=cmd_hook)

    e = sub.add_parser("emit", help="manually set a state (testing)")
    e.add_argument("state", choices=[s.value for s in AgentState])
    e.add_argument("--session", default="manual")
    e.set_defaults(func=cmd_emit)

    s = sub.add_parser("status", help="show current resolved state")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_status)

    r = sub.add_parser("renderers", help="list available renderers")
    r.set_defaults(func=cmd_renderers)

    rd = sub.add_parser("render", help="one-shot: display a single state")
    rd.add_argument("state", choices=[s.value for s in AgentState])
    rd.add_argument("--hold", type=float, default=3.0, help="seconds to hold")
    rd.set_defaults(func=cmd_render)

    w = sub.add_parser("watch", help="run the renderer loop")
    w.add_argument("--interval", type=float, default=0.25, help="poll seconds")
    w.set_defaults(func=cmd_watch)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)
