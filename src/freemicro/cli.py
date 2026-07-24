"""The ``freemicro`` command-line interface.

Subcommands
-----------
``start``     Guided setup: permissions, pad, config, hooks, daemon, proof.
``doctor``    Preflight: permissions, transport, and a real write test.
``selftest``  Prove the hook → state → LED loop works, with no live agent.
``daemon``    Install/remove the LaunchAgent that keeps FreeMicro running.
``config``    Show, locate or edit your pad config.
``run``       Everything at once: pad keys in, agent-state LEDs out.
``keys``      Bridge the pad's keys into your terminal (``--list``/``--init``).
``lights``    Send one lighting state straight to the pad (LED smoke test).
``detect``    Run the read-only HID probe and print a capability report.
``install``   Wire FreeMicro into Claude Code's hook settings.
``uninstall`` Take all of it back off: hooks, daemon, config, state, LEDs.
``hook``      Internal: consume one hook event from stdin (called by Claude).
``emit``      Manually set a state (for testing renderers/config).
``render``    One-shot: display a single state and exit.
``renderers`` List which renderers are available right now.
``status``    Is anything driving the pad, is it current, what state is showing.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from freemicro import __version__
from freemicro.config import Config
from freemicro.state.engine import AgentState, StateStore, default_store
from freemicro.state.hooks import classify, read_signals, session_id_of


def _store(cfg: Config) -> StateStore:
    """The one store construction, shared with every other reader.

    Deliberately a call to :func:`default_store` rather than a hand-built
    ``StateStore``: the CLI, the hooks, the menu bar and the renderers must
    decay claims under identical rules, and this call site is exactly where
    they used to drift apart.
    """
    return default_store(cfg)


def _hand_pad_back() -> None:
    """Apply ``lighting.on_exit`` to any pad this process is still driving.

    Passed to :meth:`CodeWatcher.restart` because ``os.execv`` runs neither
    ``finally`` nor ``atexit``, so the renderer's exit guard cannot fire on a
    self-update. Belt and braces rather than a fix: ``_run_pipeline`` already
    closes the renderer before the exec, and ``release_lighting`` is idempotent
    - it returns 0 when the pad was already handed back. Imported here so the
    CLI does not pull the renderer in at start-up.
    """
    from freemicro.renderers.micro_leds import release_lighting

    release_lighting()


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------

def cmd_detect(args: argparse.Namespace) -> int:
    from freemicro.detector import probe

    report = probe()
    if args.json:
        print(report.to_json())
        return 0

    print("FreeMicro - hardware capability probe (read-only)\n")
    if not report.hidapi_available:
        for note in report.notes:
            print(f"  ! {note}")
        return 1

    vendor_channels = report.candidate_vendor_channels
    vendor_summary = (
        ", ".join(sorted({i.usage_page_hex for i in vendor_channels}))
        if vendor_channels
        else "none"
    )
    print(f"  HID interfaces found: {len(report.interfaces)}")
    print(f"  Raw 0xFF60 channel:   {'yes' if report.has_raw_channel else 'no'}")
    print(f"  Vendor LED channel:   {vendor_summary}\n")

    candidates = report.candidate_pads
    if candidates:
        print("  Candidate pad interfaces:")
        for i in candidates:
            if i.is_raw_channel:
                tag = " [VIA raw 0xFF60]"
            elif i.is_vendor_channel:
                tag = f" [vendor {i.usage_page_hex} - LED-channel candidate]"
            else:
                tag = f" [{i.usage_page_hex}/{i.usage}]"
            name = i.product_string or "(unnamed)"
            print(f"    {i.vid_pid}  {name}{tag}")
        print()

    for note in report.notes:
        print(f"  • {note}")
    print(
        "\n  Help crowdsource the hardware DB: open a Hardware Report issue with"
        "\n  `freemicro detect --json` output. See hardware/capabilities.json."
    )
    return 0


def cmd_install(args: argparse.Namespace) -> int:
    from freemicro import hooks_install

    if args.uninstall:
        result, removed = hooks_install.uninstall_hooks(
            settings_path=args.settings, dry_run=args.dry_run
        )
        if args.dry_run:
            print(result)
            return 0
        if removed:
            print(f"Removed {removed} FreeMicro hook entr"
                  f"{'y' if removed == 1 else 'ies'} from {result}")
            print("Restart Claude Code for it to stop calling them.")
        else:
            print(f"No FreeMicro hooks to remove in {result}")
        return 0

    before = hooks_install.status(args.settings)
    changed = hooks_install.install_hooks(
        settings_path=args.settings, dry_run=args.dry_run
    )
    if args.dry_run:
        print(changed)
        return 0

    print(f"Installed FreeMicro hooks into {changed}")
    print(f"  command: {hooks_install.hook_command()}")
    print(f"  events:  {', '.join(hooks_install.HOOK_EVENTS)}")
    if before["stale_commands"]:
        print("  (repaired an entry that pointed at a different binary)")
    print("\nRestart Claude Code so it re-reads its settings.")

    if args.no_verify:
        print("Then prove it works:  freemicro selftest")
        return 0

    # "Installed" is not "working". Prove it before claiming anything.
    print("\nVerifying - firing a synthetic session through that exact "
          "command:\n")
    return _print_selftest(argparse.Namespace(json=False, in_process=False,
                                              command=None, settings=None,
                                              timeout=30.0))


def _print_plan(the_plan) -> None:
    """The preview. Nothing is deleted before this has been on the screen."""
    from freemicro import uninstall as uninstall_mod

    headings = {
        uninstall_mod.PROCESS: "Stop first",
        uninstall_mod.LEDS: "Your pad",
        uninstall_mod.LAUNCHAGENT: "Remove",
        uninstall_mod.HOOKS: "Remove",
        uninstall_mod.CONFIG: "Remove",
        uninstall_mod.STATE: "Remove",
        uninstall_mod.HOME: "Remove",
    }
    shown = ""
    for item in the_plan.actions:
        heading = headings.get(item.category, "Remove")
        if heading != shown:
            print(f"\n{heading}")
            shown = heading
        print(f"  {item.describe()}")

    kept = the_plan.kept
    if kept:
        print("\nKept (--keep-config)")
        for item in kept:
            print(f"  {item.describe()}")
        print("  Reinstall FreeMicro and it picks these up exactly as they are.")


def _print_cannot_remove() -> None:
    from freemicro import uninstall as uninstall_mod

    print("\nWhat this command cannot do")
    for block in uninstall_mod.cannot_remove():
        for line in block.splitlines():
            print(f"  {line}")


def cmd_uninstall(args: argparse.Namespace) -> int:
    """Take FreeMicro back off this machine, and say what is left.

    The posture is preview-first on purpose: this deletes a config someone may
    have spent an evening on, and there is no undo. ``--dry-run`` prints and
    stops, ``--yes`` is the non-interactive answer, and a pipe with neither
    refuses rather than guessing.
    """
    from freemicro import uninstall as uninstall_mod

    the_plan = uninstall_mod.plan(
        settings_path=Path(args.settings) if args.settings else None,
        keep_config=args.keep_config,
    )

    print("FreeMicro uninstall")
    if the_plan.lighting_note:
        print(f"  note: {the_plan.lighting_note}")

    if the_plan.empty:
        print("\nNothing to remove - FreeMicro is not installed on this machine.")
        if the_plan.kept:
            print("\nKept (--keep-config)")
            for item in the_plan.kept:
                print(f"  {item.describe()}")
        _print_cannot_remove()
        return 0

    _print_plan(the_plan)

    if args.dry_run:
        print("\nDry run - nothing was touched. Re-run without --dry-run to do it.")
        _print_cannot_remove()
        return 0

    if not args.yes:
        if not sys.stdin.isatty():
            print(
                "\nRefusing to remove any of that without a confirmation. "
                "Pass --yes to\nmean it, or --dry-run to just look.",
                file=sys.stderr,
            )
            return 1
        try:
            answer = input("\nRemove all of that? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            answer = ""
        if answer not in ("y", "yes"):
            print("Left everything alone.")
            return 1

    print()
    result = uninstall_mod.uninstall(the_plan)
    for outcome in result.outcomes:
        mark = "ok  " if outcome.ok else "FAIL"
        print(f"  [{mark}] {outcome.label}")
        print(f"          {outcome.message}")

    if result.failures:
        print(
            f"\n{len(result.failures)} of {len(result.outcomes)} steps did not "
            "finish. Everything else was removed;\nthe lines marked FAIL are "
            "still on this machine."
        )
    else:
        print(f"\nDone - {len(result.done)} steps, all of them.")
        if the_plan.kept:
            print("Kept, because you asked:")
            for item in the_plan.kept:
                print(f"  {item.describe()}")
    print("\nRestart Claude Code so it stops calling hooks that are gone.")
    _print_cannot_remove()
    return 1 if result.failures else 0


def _print_selftest(args: argparse.Namespace) -> int:
    from freemicro import selftest

    report = selftest.run(
        command=getattr(args, "command", None),
        settings_path=(
            Path(args.settings) if getattr(args, "settings", None) else None
        ),
        timeout=getattr(args, "timeout", 30.0),
        in_process=getattr(args, "in_process", False),
    )
    if getattr(args, "json", False):
        print(json.dumps(report.to_dict(), indent=2))
        return 0 if report.ok else 1

    print(f"hook command: {report.command}")
    print(f"  source:     {report.source}\n")
    for check in report.checks:
        if check.ok:
            mark = "PASS"
        elif check.warn:
            mark = "warn"
        else:
            mark = "FAIL"
        print(f"  [{mark}]  {check.name}")
        if check.detail:
            print(f"          {check.detail}")
        if not check.ok and check.fix:
            for line in check.fix.splitlines():
                print(f"          → {line}")
    print()
    if report.ok:
        warned = len(report.warnings)
        note = f" ({warned} warning{'' if warned == 1 else 's'})" if warned else ""
        print(f"All {len(report.checks)} checks passed in "
              f"{report.elapsed:.1f}s{note} - the hook → state → LED loop works.")
        return 0
    print(f"{len(report.failures)} of {len(report.checks)} checks failed. "
          "Fix the arrows above and re-run.")
    return 1


def cmd_selftest(args: argparse.Namespace) -> int:
    """Prove the whole loop without a live Claude session.

    This is the command that closes the project's oldest gap: every piece was
    unit-tested and the *assembly* never was. It runs the real hook command
    from Claude Code's settings, with real event JSON on stdin, against a
    throwaway state directory, and then checks the LED renderer's output for
    every state.
    """
    return _print_selftest(args)


def cmd_start(args: argparse.Namespace) -> int:
    from freemicro import onboarding

    return onboarding.run(args)


def _log_raw_event(event: dict, state) -> None:
    """Append one raw hook payload to ``$FREEMICRO_HOOK_LOG``, if set.

    We map hook events to states from documentation and assumption, and that
    has already been wrong once: pressing Escape to interrupt emits *no* event
    at all, so a session sat on ``working`` - a blue "thinking" light - for the
    full 30-minute TTL after being cancelled. There is no way to audit a
    mapping against events you have never looked at, so this makes the real
    traffic observable. Off unless the variable is set; never raises, because
    nothing here may break the user's agent.
    """
    path = os.environ.get("FREEMICRO_HOOK_LOG")
    if not path:
        return
    try:
        record = {
            "at": time.time(),
            "event": event.get("hook_event_name"),
            "classified_as": state.value if state is not None else None,
            "cwd": event.get("cwd", ""),
            "payload_keys": sorted(event)[:20],
            "payload": event,
        }
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")
    except Exception:  # noqa: BLE001 - diagnostics must never break the agent
        pass


def cmd_hook(args: argparse.Namespace) -> int:
    cfg = Config.load()
    try:
        event = json.load(sys.stdin)
    except (ValueError, OSError):
        return 0  # Never break Claude Code because of a hook parse error.

    state = classify(event)
    _log_raw_event(event, state)
    if state is None:
        return 0

    store = _store(cfg)
    session = session_id_of(event)
    signals = read_signals(event)

    # Three events classify as IDLE and only one of them means "gone":
    #
    # ``SessionStart``  a window just opened. Registering it here is what lets
    #                   a fresh Claude Code tab claim an Agent Key immediately;
    #                   clearing a session that does not exist yet would leave
    #                   the key dark until the first prompt, and a dark key is
    #                   indistinguishable from a broken one.
    # ``SessionEnd`` + ``reason: "clear"``   a ``/clear``. The project is still
    #                   open, so it keeps its slot and simply goes idle.
    # ``SessionEnd`` + anything else         the tab closed. That one clears.
    ends_session = (
        state == AgentState.IDLE
        and signals.event == "SessionEnd"
        and signals.end_reason != "clear"
    )
    if ends_session:
        store.clear(session)
    else:
        # ``signals`` carries prompt_id, the last event name, permission mode,
        # effort and running background tasks. Without them the store cannot
        # tell an interrupted turn from a long tool call, and a cancelled
        # session sits on a blue "thinking" light until its TTL expires.
        store.update(
            session,
            state,
            title=str(event.get("title", "")),
            cwd=str(event.get("cwd", "")),
            signals=signals,
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


def _hook_drift() -> "list[str]":
    """Lines about hooks this version expects but Claude Code is not firing.

    Every time FreeMicro learns a new lifecycle event, every machine that
    installed the hooks before that release silently keeps the old set - and
    the symptom is a state that never lights, which reads as a broken product.
    Same class of problem as a stale process, so it is reported in the same
    breath.
    """
    from freemicro import hooks_install

    try:
        hooks = hooks_install.status()
    except Exception:  # noqa: BLE001 - status must never fail over a settings file
        return []
    if not hooks["events"]:
        return [
            "Claude Code hooks are not installed - nothing will ever change the",
            "  state. Fix:  freemicro install",
        ]
    missing = hooks["missing_events"]
    if not missing:
        return []
    return [
        f"Claude Code is not firing {', '.join(missing)} - this FreeMicro "
        "expects it,",
        "  so some states will never light. Fix:  freemicro install",
    ]


def _print_liveness(report) -> None:
    """Say - first, and plainly - whether anything is driving the pad.

    This is the line that would have ended three of the five worst sessions
    this project has had. A ``status`` that lists live agent sessions while no
    bridge is running is describing a light nobody is switching on.
    """
    listener = report.listener
    print(listener.summary())
    for line in listener.fix().splitlines():
        print(f"  {line}")
    for entry in report.stale:
        print(f"\n! {entry.summary()}")
        print(f"  Fix: {entry.fix()}")
    drift = _hook_drift()
    if drift:
        print(f"\n! {drift[0]}")
        for line in drift[1:]:
            print(line)


def cmd_status(args: argparse.Namespace) -> int:
    from freemicro import staleness

    cfg = Config.load()
    store = _store(cfg)
    report = staleness.report()
    if args.json:
        winner = store.resolve()
        print(
            json.dumps(
                {
                    "resolved": store.resolved_state().value,
                    "sessions": [s.to_dict() for s in store.sessions()],
                    "winner": winner.to_dict() if winner else None,
                    "bridge": report.to_dict(),
                }
            )
        )
        return 0

    # Liveness first. Everything below it is a description of a light, and a
    # description of a light is worthless if nobody is switching it on.
    _print_liveness(report)
    print()
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


def _note_removed_renderers(cfg: Config) -> None:
    """Say so, plainly, if ``renderers.prefer`` names something we deleted.

    Silently ignoring a name is how a config nobody has touched in a year turns
    into an evening of wondering why the pad is dark. One line, once, naming the
    replacement.
    """
    from freemicro.renderers import REMOVED, removed_names

    for name in removed_names(cfg.prefer):
        print(f"Note: renderers.prefer lists '{name}', which no longer exists.",
              file=sys.stderr)
        advice = REMOVED[name.lower()]
        print(f"      {advice[0].upper()}{advice[1:]}.", file=sys.stderr)


def _print_state(state: AgentState) -> None:
    """The terminal half of every render command.

    This is what is left of the old screen renderer, and all it ever really
    was: one line saying what the pad is showing, so a command that renders is
    never silent on a machine with no pad attached.
    """
    print(f"  state: {state.value}", flush=True)


def cmd_render(args: argparse.Namespace) -> int:
    from freemicro.renderers import select

    cfg = Config.load()
    _note_removed_renderers(cfg)
    state = AgentState(args.state)
    renderers = select(prefer=cfg.prefer)
    targets = ", ".join(r.name for r in renderers) or "no hardware, this terminal only"
    print(f"Rendering '{state.value}' to: {targets}")
    for r in renderers:
        r.render(state)
    _print_state(state)
    if args.hold:
        try:
            time.sleep(args.hold)
        except KeyboardInterrupt:
            pass
    for r in renderers:
        r.close()
    return 0


def _parse_kv(pairs: list[str]) -> dict:
    out = {}
    for pair in pairs or []:
        if "=" not in pair:
            raise SystemExit(f"expected name=file, got: {pair!r}")
        name, _, path = pair.partition("=")
        out[name.strip()] = path.strip()
    return out


def cmd_learn(args: argparse.Namespace) -> int:
    """Build a replay profile from sniffed per-state captures (Path B)."""
    from freemicro.capture import learn
    from freemicro.protocol import default_profile_path

    state_captures = _parse_kv(args.state)
    color_captures = _parse_kv(args.color) if args.color else None
    if not state_captures:
        print("No captures given. Example:\n"
              "  freemicro learn thinking=thinking.json done=done.json "
              "awaiting=awaiting.json error=error.json idle=idle.json")
        print("\nSee docs/SNIFF-RUNBOOK.md for how to capture these.")
        return 2

    profile = learn(state_captures, color_captures=color_captures, vid_pid=args.vid_pid or "")
    if not profile.frames_by_state:
        print("Learned nothing — no HID report frames found in those captures.")
        print("Check the runbook: capture must contain the app→pad output reports.")
        return 1

    out = Path(args.out) if args.out else default_profile_path()
    profile.save(out)
    states = ", ".join(s.value for s in profile.known_states())
    print(f"Learned profile → {out}")
    print(f"  states captured: {states}")
    print(f"  report length:   {profile.report_length}")
    if profile.layout.is_usable():
        print(f"  inferred RGB offsets: r={profile.layout.r_offset} "
              f"g={profile.layout.g_offset} b={profile.layout.b_offset}")
    print("\nQuit the ChatGPT desktop app, then run `freemicro watch` — the pad "
          "now follows Claude Code.")
    return 0


def cmd_verify_leds(args: argparse.Namespace) -> int:
    """Active write-test: drive the pad's LEDs and record the verdict (Path A)."""
    from freemicro.verify import run_led_verify

    interactive = (not args.yes) and sys.stdin.isatty()
    print("FreeMicro — LED write-test (Path A). Watch the pad's top row.\n")
    result = run_led_verify(interactive=interactive, hold=args.hold)

    if result["renderer"]:
        print(f"  Writable LED channel: yes (via {result['renderer']})")
        verdict = result.get("verdict")
        if verdict:
            moved = verdict["agent_keys_moved"]
            print(f"  Agent Keys moved:     {'yes' if moved else 'no'}")
            if moved:
                print(f"  Granularity:          {verdict['granularity']}")
                print(f"  Needed app quit:      {verdict['chatgpt_app_quit_required']}")
        elif not interactive:
            print("  (non-interactive: writes attempted; re-run in a terminal for a verdict)")
    else:
        print("  Writable LED channel: no")
    print()
    for note in result["notes"]:
        print(f"  • {note}")
    print(f"\n  Report saved: {result['report_path']}")
    print("  Submit it via the Hardware Report issue to grow the capability DB.")
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    """Play the full state sequence on the real renderer(s).

    Useful for eyeballing the whole pipeline - and for recording a demo -
    without needing Claude Code or any hardware attached.
    """
    from freemicro.renderers import select

    cfg = Config.load()
    _note_removed_renderers(cfg)
    renderers = select(prefer=cfg.prefer)
    sequence = [
        AgentState.IDLE,
        AgentState.WORKING,
        AgentState.WAITING,
        AgentState.WORKING,
        AgentState.DONE,
        AgentState.ERROR,
        AgentState.IDLE,
    ]
    targets = ", ".join(r.name for r in renderers) or "no hardware, this terminal only"
    print(
        f"freemicro demo - targets: {targets} "
        f"({args.step}s per state). Ctrl-C to stop."
    )
    try:
        for _ in range(args.loops):
            for state in sequence:
                for r in renderers:
                    r.render(state)
                _print_state(state)
                time.sleep(args.step)
    except KeyboardInterrupt:
        pass
    finally:
        for r in renderers:
            r.close()
    return 0


def cmd_daemon(args: argparse.Namespace) -> int:
    """Manage the LaunchAgent that keeps FreeMicro alive without a terminal."""
    from freemicro import daemon

    action = args.action
    if action == "run":
        return _daemon_run(args)

    if action == "install":
        reinstall = daemon.is_installed()
        result = daemon.install(force=args.force)
        if not result["ok"]:
            print(f"Not installing: {result['error']}", file=sys.stderr)
            if result.get("warning"):
                print("  (--force installs it anyway.)", file=sys.stderr)
            return 1
        print(
            "Reinstalling the FreeMicro LaunchAgent (picking up the current "
            "binary)." if reinstall else "Installing the FreeMicro LaunchAgent."
        )
        print(f"  plist:   {result['path']}")
        print(f"  command: {daemon.status()['command']}")
        print(f"  log:     {daemon.log_path()}")

        # Writing a plist is not the same as having a running daemon. Wait for
        # a pid rather than declaring victory and letting it crash-loop unseen.
        pid = daemon.wait_until_running()
        if pid:
            print(f"\nRunning (pid {pid}). It starts at login and restarts if "
                  "it dies.")
            print("  freemicro daemon status     is it alive?")
            print("  freemicro daemon logs       what it's been doing")
            print("  freemicro daemon uninstall  remove it completely")
            return 0

        print("\nInstalled, but it did not come up.", file=sys.stderr)
        reason = daemon.diagnose()
        if reason:
            for line in reason.splitlines():
                print(f"  {line}", file=sys.stderr)
        else:
            print("  freemicro daemon logs       the last thing it printed",
                  file=sys.stderr)
        return 1

    if action == "uninstall":
        result = daemon.uninstall()
        if not result["ok"]:
            print(f"Uninstall failed: {result['error']}", file=sys.stderr)
            print("Try:  launchctl bootout " + daemon.service_target(),
                  file=sys.stderr)
            return 1
        if not result["existed"]:
            print("The daemon was not installed. Nothing to do.")
            return 0
        print(f"Stopped and removed {daemon.plist_path()}")
        print("The pad is free for `freemicro run` again.")
        return 0

    if action == "logs":
        text = daemon.read_log(lines=args.lines)
        if not text:
            print(f"Nothing logged yet ({daemon.log_path()})")
            return 0
        print(text)
        return 0

    # status
    state = daemon.status()
    if args.json:
        print(json.dumps(state, indent=2))
        return 0 if state["loaded"] else 1
    print(f"Label:     {state['label']}")
    print(f"Plist:     {state['plist']}"
          f"{'' if state['installed'] else '   (not installed)'}")
    print(f"Command:   {state['command']}")
    if state["pid"]:
        print(f"Running:   yes, pid {state['pid']}")
    elif state["loaded"]:
        print("Running:   no - registered with launchd but not up")
        if state["last_exit"]:
            print(f"           last exit code {state['last_exit']}")
    else:
        print("Running:   no - not registered with launchd")
    print(f"Log:       {state['log']}  ({state['log_size']} bytes)")
    if state["lock_role"]:
        print(f"Pad held by: {state['lock_role']} (pid {state['lock_pid']})")
    else:
        print("Pad held by: nobody")
    if not state["installed"]:
        if state["protected_location"]:
            print(f"\n  Note: this binary lives under ~/{state['protected_location']}"
                  ", which macOS\n  will not let a background agent read. See "
                  "`freemicro daemon install`.")
        print("\n  freemicro daemon install    start it at login")
        return 1
    if not state["pid"]:
        reason = daemon.diagnose()
        if reason:
            print()
            for line in reason.splitlines():
                print(f"  {line}")
        print("\n  freemicro daemon logs       the last thing it printed")
        return 1
    return 0


def _daemon_run(args: argparse.Namespace) -> int:
    """The daemon's own foreground loop. launchd runs exactly this.

    Headless on purpose: a LaunchAgent has no terminal, so the console
    indicator would only spam the log, and opening a Tk window from a launchd
    job is a good way to discover a Tcl that aborts the process. The pad *is*
    the display here.
    """
    from freemicro import daemon, staleness

    daemon.rotate_log()
    note = staleness.reclaim_stale_lock()
    lock = daemon.PadLock(role="daemon")
    if not lock.acquire():
        holder = daemon.lock_holder() or {}
        print(f"[freemicro] not starting: {daemon.describe_holder(holder)} "
              "already owns the pad.", flush=True)
        # Exit 0: this is a correct outcome, not a crash, and a non-zero exit
        # would have launchd restart us into the same conflict every 10s.
        return 0
    if note:
        print(f"[freemicro]{note}", flush=True)
    watcher = _code_watcher(args)
    try:
        print(f"[freemicro] daemon up (pid {os.getpid()}), "
              f"logging to {daemon.log_path()}", flush=True)
        result = _run_pipeline(args, headless=True, watcher=watcher)
    finally:
        lock.release()
    if watcher is not None and watcher.pending:
        watcher.restart(release=_hand_pad_back)
    return result


# ---------------------------------------------------------------------------
# pad commands (keys / lights / run)
# ---------------------------------------------------------------------------

def _load_pad(args: argparse.Namespace):
    """Load the pad config, or print an actionable error and return ``None``."""
    from freemicro import padconfig

    path = Path(args.config).expanduser() if getattr(args, "config", None) else None
    try:
        return padconfig.load(path)
    except padconfig.PadConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        print(
            "Fix the file, or run `freemicro keys --init --force` to start from "
            "the shipped default.",
            file=sys.stderr,
        )
        return None


def _reload_pad(override: "Path | None"):
    """Re-resolve the pad config the same way the process first loaded it.

    Deliberately goes through the normal search order rather than re-reading
    one remembered path: a config *created* while the bridge is running is
    exactly as much of a change as one edited, and only the search order knows
    which file now wins.
    """
    from freemicro import padconfig

    return padconfig.load(override)


def _open_pad(headless: bool = False):
    """Open the pad, printing the usual reasons it might not work."""
    from freemicro.device import (
        device_present,
        is_supported,
        shared_device,
        unsupported_reason,
    )

    if not is_supported():
        print(unsupported_reason(), file=sys.stderr)
        return None
    device = shared_device()
    if device is not None:
        return device
    if device_present() and headless:
        # A LaunchAgent has no terminal to grant anything to: macOS attaches
        # Input Monitoring to the *executable* launchd started, which appears
        # in the list under its own name and is off until switched on. Sending
        # someone to restart their terminal here would be plainly wrong advice.
        from freemicro import daemon

        binary = daemon.daemon_argv()[0]
        print(
            "Found a Codex Micro but macOS won't let this background agent "
            "open it.\n"
            "  → System Settings → Privacy & Security → Input Monitoring\n"
            f"    Switch on the entry for:  {binary}\n"
            "    (macOS adds it, switched off, the first time we try.)\n"
            "  → Then: freemicro daemon uninstall && freemicro daemon install",
            file=sys.stderr,
        )
    elif device_present():
        print(
            "Found a Codex Micro but macOS won't let this terminal open it.\n"
            "  → System Settings → Privacy & Security → Input Monitoring\n"
            "    Add your terminal app, then RESTART the terminal.\n"
            "  → Then run: freemicro doctor",
            file=sys.stderr,
        )
    else:
        print(
            "No Codex Micro found.\n"
            "  → Plug in the USB cable, or pair the pad over Bluetooth "
            "(both work).\n"
            "  → Then run: freemicro doctor",
            file=sys.stderr,
        )
    return None


def _print_keymap(pad) -> None:
    from freemicro.device.lighting import color_to_hex, effect_name
    from freemicro.input.actions import action_help
    from freemicro.padconfig import KNOWN_INPUTS

    print(f"Pad config: {pad.origin}\n")
    print("Bindings")
    ordered = [i for i in KNOWN_INPUTS if i in pad.bindings]
    ordered += [i for i in pad.bindings if i not in KNOWN_INPUTS]
    for input_id in ordered:
        action = pad.bindings[input_id]
        print(f"  {input_id:9} {action.label:16} {action.describe()}")
    unbound = [i for i in KNOWN_INPUTS if i not in pad.bindings]
    if unbound:
        print(f"\n  unbound: {', '.join(unbound)}")

    # Chords live in their own mapping, keyed by the sorted members, so a
    # listing that walks `bindings` alone shows nothing for a chord the user
    # just saved - and "it did not save" is the wrong conclusion to invite.
    if pad.chords:
        from freemicro.padconfig import chord_label

        print("\nChords (two keys pressed together)")
        for members in sorted(pad.chords):
            action = pad.chords[members]
            label = chord_label(members)
            print(f"  {label:9} {action.label:16} {action.describe()}")
        paying = sorted(
            m for members in pad.chords for m in members
            if m in pad.bindings and pad.bindings[m].kind != "none"
        )
        if pad.chord_settle_ms > 0 and paying:
            print(
                f"\n  {', '.join(dict.fromkeys(paying))} wait up to "
                f"{pad.chord_settle_ms:g}ms before firing alone, in case you "
                "are\n  reaching for the other half. A quick tap fires on "
                "release without waiting."
            )
        elif pad.chord_settle_ms > 0:
            print("\n  No key waits for anything: every chord member is "
                  "unbound on its own,\n  so these add no delay at all.")

    print("\nLighting")
    lighting = pad.lighting
    status = "on" if lighting.enabled else "off"
    print(f"  enabled: {status}   zones: {', '.join(lighting.zones)}"
          f"   on exit: {lighting.on_exit}")
    # Auto-dim is invisible until something is printed about it, and a pad that
    # has blanked itself after three minutes looks exactly like a pad that has
    # stopped working. Say the number.
    if lighting.auto_dim_enabled:
        dim = f"  auto-dim: {lighting.auto_dim_seconds:g}s"
        dim += (
            "   alerts dim too" if lighting.auto_dim_alerts
            else "   waiting and error stay lit"
        )
        print(dim)
    else:
        print("  auto-dim: off")
    for state in AgentState:
        # The *effective* look, never "(default palette)": the question this
        # listing answers is what the pad will show, and a state the user did
        # not configure still has a colour.
        print(f"  {state.value:9} {lighting.light_for(state).describe()}")

    print("\nJoystick")
    joystick = pad.joystick
    print(f"  mode: {joystick.mode}")
    if joystick.pointing:
        # Only the settings that are actually in force, so nobody tunes a
        # number that this mode does not read.
        print(f"  analogue cursor: max_speed={joystick.max_speed:g}px/s "
              f"gamma={joystick.gamma:g} deadzone={joystick.pointer_deadzone:g} "
              f"tick={joystick.tick_hz:g}Hz")
        if joystick.precision_key:
            print(f"  hold {joystick.precision_key} for "
                  f"{joystick.precision_scale:g}x speed")
        if joystick.invert_y:
            print("  vertical axis inverted")
        print("  the four JOY_* bindings below do not fire in this mode")
    else:
        print(f"  deadzone={joystick.deadzone:g} origin={joystick.origin:g}")
        print(f"  directions (from angle 0, by equal steps): "
              f"{', '.join(joystick.directions)}")

    print("\nAction kinds you can use")
    for line in action_help():
        print(f"  {line}")

    if pad.warnings:
        print("\nWarnings")
        for warning in pad.warnings:
            print(f"  ! {warning}")

    # Colour swatch line: handy when tuning a palette by eye. Effective looks,
    # for the same reason as the block above.
    swatches = "  ".join(
        f"{s.value}={color_to_hex(light.color)}/{effect_name(light.effect)}"
        for s in AgentState
        for light in [lighting.light_for(s)]
    )
    if swatches:
        print(f"\n  {swatches}")


def _accessibility_ok() -> "tuple[bool, str]":
    """Is Accessibility granted? Read-only, via a real System Events call.

    ``permissions.accessibility()`` answers instantly from ``AXIsProcessTrusted``;
    this goes the whole way through ``osascript``, which is what the key bridge
    actually uses. Doctor is the one place worth paying for the round trip.
    """
    from freemicro import permissions

    granted, detail = permissions.accessibility()
    if not granted:
        return False, detail
    return permissions.accessibility_roundtrip()


def _chatgpt_running() -> bool:
    """Is the vendor app up? It drives the same LEDs on the same channel."""
    from freemicro.lighting_owner import vendor_app_running

    return vendor_app_running()


def _doctor_liveness(check, live) -> None:
    """The "is anything listening?" section, given doctor's own ``check``.

    A check of its own, deliberately separate from "is the pad present": a pad
    that is plugged in, permitted and configured but that nobody is reading
    emits nothing at all, and until this existed that state read as all-green -
    which is the single most common way this product looks broken while
    working perfectly.
    """
    check(
        live.listener.listening,
        "a FreeMicro bridge is driving the pad",
        live.listener.summary() if live.listener.listening else "",
        live.listener.fix(),
    )
    for entry in live.stale:
        # A warning, not a failure: the software is correct, it is just not the
        # software on disk. What failed is visibility, which this repairs.
        print(f"  [warn]  {entry.summary()}")
        print(f"          → {entry.fix()}")
    if not live.stale and live.processes:
        print(f"  [info]  {len(live.processes)} FreeMicro process(es) running, "
              "all on the current code and config")


def cmd_doctor(args: argparse.Namespace) -> int:
    """Check everything that has to be true, and name the fix for each failure.

    Every failure mode on this device is invisible: macOS drops synthetic events
    silently, a wrongly framed HID write *returns success* and is discarded, and
    a contending app just quietly wins. One command that says which of those is
    happening is worth more than any feature.
    """
    from freemicro import padconfig
    from freemicro.device import (
        TRANSPORT_BLE,
        close_shared,
        device_transport,
        is_supported,
        shared_device,
        unsupported_reason,
    )
    from freemicro.input import quartz
    from freemicro.input.actions import best_backend

    failures = 0

    def check(good: bool, label: str, detail: str = "", fix: str = "") -> bool:
        nonlocal failures
        print(f"  [{'PASS' if good else 'FAIL'}]  {label}")
        if detail:
            print(f"          {detail}")
        if not good:
            failures += 1
            if fix:
                for line in fix.splitlines():
                    print(f"          → {line}")
        return good

    print("freemicro doctor - checking everything that has to be true\n")

    print("Platform")
    check(
        is_supported(),
        "macOS IOKit available (needed to talk to the pad at all)",
        "" if is_supported() else unsupported_reason(),
        "Pad support is macOS-only today. Nothing else in FreeMicro can show\n"
        "you agent state without it - the pad is the display.",
    )
    backend = best_backend()
    if quartz.is_available():
        check(True, "Quartz CGEvent available (fn keys, hold-to-talk, low latency)",
              f"using: {backend.description}")
    else:
        print("  [warn]  Quartz CGEvent unavailable - falling back to AppleScript")
        print(f"          {quartz.unavailable_reason()}")
        print("          → Everything works except fn bindings and hold-to-talk.")

    print("\nPermissions")
    from freemicro import permissions

    listening, listen_detail = permissions.input_monitoring()
    if listening is None:
        # "Undecided" is a real, common third state - usually because nothing
        # has tried to open the pad on this machine yet. Calling it a failure
        # would send people to a settings pane that has nothing to tick.
        print("  [ ?  ]  Input Monitoring - macOS hasn't decided yet")
        print(f"          {listen_detail}")
        print("          → The device check below is the real answer.")
    else:
        check(
            listening,
            "Input Monitoring (lets FreeMicro read the pad and light it)",
            listen_detail,
            permissions.fix_text("input_monitoring"),
        )
    granted, detail = _accessibility_ok()
    check(
        granted,
        "Accessibility (lets FreeMicro type for you)",
        f"frontmost app: {detail}" if granted else detail,
        permissions.fix_text("accessibility"),
    )

    print("\nClaude Code")
    from freemicro import hooks_install

    hooks = hooks_install.status()
    if hooks["installed"]:
        check(True, "lifecycle hooks installed", hooks["path"])
        # What matters is that the recorded binary *exists*, not that it is
        # this one: running doctor from a second checkout must not report a
        # perfectly good installation as broken.
        check(
            hooks["binary_exists"],
            "the hook command points at a binary that exists",
            hooks["binary"] or "",
            "The recorded path is gone - a moved or deleted virtualenv.\n"
            "freemicro install     # rewrite it with this binary's real path",
        )
        check(
            len(set(hooks["commands"].values())) == 1,
            "every event uses the same hook command",
            "" if len(set(hooks["commands"].values())) == 1
            else " | ".join(sorted(set(hooks["commands"].values()))),
            "Some events point somewhere else, so some states will light and\n"
            "others silently won't.\nfreemicro install",
        )
        if hooks["stale_commands"]:
            print("  [info]  the hooks call a different FreeMicro install than")
            print("          this one - fine, as long as that one still works")
            print(f"          hooks: {hooks['stale_commands'][0]}")
            print(f"          this:  {hooks['expected_command']}")
    elif hooks["partial"]:
        check(
            False, "lifecycle hooks installed",
            f"missing: {', '.join(hooks['missing_events'])}",
            "Claude Code is not firing every event this FreeMicro expects, so\n"
            "some states will never light. Usually means the hooks were\n"
            "installed before this version added an event.\n"
            "freemicro install     # then restart Claude Code",
        )
    else:
        check(
            False, "lifecycle hooks installed",
            f"nothing in {hooks['path']}",
            "freemicro install     # then `freemicro selftest` to prove it",
        )
    print("          Prove the whole loop end to end:  freemicro selftest")

    print("\nBackground daemon")
    from freemicro import daemon as _daemon

    dstate = _daemon.status()
    if not dstate["installed"]:
        print("  [ off]  not installed - the pad only works while a terminal")
        print("          is running `freemicro run`")
        print("          → freemicro daemon install")
    elif dstate["pid"]:
        check(True, "daemon running", f"pid {dstate['pid']}")
    else:
        check(
            False, "daemon running", "installed but not up",
            "freemicro daemon logs      # see why",
        )
    holder = _daemon.lock_holder()
    if holder:
        print(f"  [info]  the pad is held by {_daemon.describe_holder(holder)}")

    print("\nConfig")
    path = Path(args.config).expanduser() if args.config else None
    pad = None
    try:
        pad = padconfig.load(path)
        check(True, f"pad config loads - {pad.origin}")
        for warning in pad.warnings:
            print(f"          ! {warning}")
    except padconfig.PadConfigError as exc:
        check(
            False, "pad config loads", str(exc),
            "freemicro keys --init --force     # start again from the default",
        )

    if pad is not None:
        # Lighting-off is the shipped default, not a fault - report it as a
        # state with the command to change it, never as a failure.
        if pad.lighting.enabled:
            check(True, "LED control enabled")
        else:
            print("  [ off]  LED control is off (the default - we don't take")
            print("          over your pad uninvited)")
            print("          → freemicro lights --enable")

    print("\nDevice")
    transport = device_transport()
    if transport is None:
        check(
            False, "Codex Micro found", "",
            "Plug in the USB cable, or pair the pad over Bluetooth - both work.\n"
            "freemicro detect                  # list every HID device seen",
        )
        print(f"\n{failures} check(s) failed. Fix the arrows above and re-run.")
        return 1
    check(True, "Codex Micro found", f"transport: {transport}")
    if transport == TRANSPORT_BLE:
        print("          Wireless is fully supported - input, LEDs and RPC.")

    # Contention is reported per *capability*, because input and lighting do not
    # contend for the same reasons: two processes read this device happily, and
    # only writes fight. Doctor shows the lighting side - the peer-process case
    # is already reported under "Background daemon" above.
    from freemicro.lighting_owner import (
        CAPABILITY_LIGHTING,
        SOURCE_FREEMICRO,
        contention,
    )

    for conflict in contention().for_capability(CAPABILITY_LIGHTING):
        if conflict.source == SOURCE_FREEMICRO:
            continue  # already reported, by name, under "Background daemon"
        # A warning, never a failure: it costs you nothing but colour accuracy,
        # and it is not a state anyone has to fix before running FreeMicro.
        print(f"  [warn]  {conflict.summary} - a LIGHTING warning only")
        for line in conflict.detail.splitlines():
            print(f"          {line}")
        for line in conflict.mitigation.splitlines():
            print(f"          → {line}")

    device = shared_device()
    if device is None:
        check(
            False, "device opens (Input Monitoring)", "",
            "System Settings → Privacy & Security → Input Monitoring → add\n"
            "your terminal app, then RESTART the terminal.",
        )
        print(f"\n{failures} check(s) failed. Fix the arrows above and re-run.")
        return 1
    check(True, "device opens (Input Monitoring granted)")

    # The only honest write test. A malformed write returns success and is
    # silently discarded, so a returned status is the sole proof the whole
    # path - framing, transport, permission - actually works.
    status = device.self_test(timeout=args.timeout)
    detail = ""
    if status is not None:
        bits = []
        if status.get("version"):
            bits.append(f"firmware {status['version']}")
        if status.get("battery") is not None:
            bits.append(
                f"battery {status['battery']}%"
                + (" (charging)" if status.get("is_charging") else "")
            )
        detail = " · ".join(bits)
    check(
        status is not None,
        "device.status round trip - the write path really works",
        detail,
        "The pad accepted the write but never answered. Success return codes\n"
        "prove nothing on this device, so this is the only real health check.\n"
        "Unplug and replug the pad, then re-run. (The ChatGPT app being open\n"
        "is not the cause - it overwrites colours, it does not block writes.)",
    )

    close_shared()

    # Last, and only once the pad is proven present and writable: a pad that
    # works and that nobody is reading is inert, and that is a *different*
    # answer from "no pad". Asked here so it can never be the reason a
    # first-time user with no pad plugged in sees a confusing failure.
    print("\nIs anything listening?")
    from freemicro import staleness

    _doctor_liveness(check, staleness.report())

    if failures:
        print(f"\n{failures} check(s) failed. Fix the arrows above and re-run.")
        return 1
    print("\nAll good. Try:  freemicro run")
    return 0


def cmd_menubar(args: argparse.Namespace) -> int:
    """Run the macOS status-bar item.

    Deliberately a *reader*: it takes state from the store and the IOKit
    registry rather than opening the pad, so it can sit alongside a running
    daemon instead of fighting it for the device.
    """
    from freemicro.menubar import main as menubar_main

    return menubar_main([])


def cmd_config(args: argparse.Namespace) -> int:
    """Show, locate, or open the pad config - so nobody has to hunt for it."""
    import subprocess

    from freemicro import padconfig

    if getattr(args, "web", False):
        from freemicro.webui import serve

        serve(open_browser=not getattr(args, "no_browser", False),
              config_path=Path(args.config).expanduser() if args.config else None)
        return 0

    path = Path(args.config).expanduser() if args.config else padconfig.user_path()

    if args.path:
        print(path)
        return 0

    if args.edit or args.create:
        if not path.exists():
            padconfig.write_starter(path)
            print(f"Created {path}")
        if args.edit:
            editor = os.environ.get("EDITOR") or os.environ.get("VISUAL")
            command = [editor, str(path)] if editor else ["open", "-t", str(path)]
            try:
                subprocess.run(command, check=False)
            except (OSError, subprocess.SubprocessError) as exc:
                print(f"Could not open an editor: {exc}", file=sys.stderr)
                print(f"Edit it yourself: {path}")
                return 1
        else:
            print("Edit it with: freemicro config --edit")
        return 0

    # Default: show where everything lives and what is in effect.
    try:
        pad = padconfig.load(Path(args.config).expanduser() if args.config else None)
    except padconfig.PadConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 2
    print(f"In effect:  {pad.origin}")
    print(f"Your copy:  {path}" + ("" if path.exists() else "   (not created yet)"))
    print(f"Lighting:   {'on' if pad.lighting.enabled else 'off'}")
    print("\nSearch order (first match wins):")
    for candidate in padconfig.search_paths():
        mark = "*" if candidate.exists() else " "
        print(f"  {mark} {candidate}")
    print(
        "\nCommands:\n"
        "  freemicro config --edit      open it in $EDITOR (creates it first)\n"
        "  freemicro config --path      print the path (for scripting)\n"
        "  freemicro keys --list        show the resolved bindings and colours\n"
        "  freemicro lights --enable    let FreeMicro drive the LEDs"
    )
    return 0


def cmd_keys(args: argparse.Namespace) -> int:
    """Bridge pad key presses into whatever the user bound them to."""
    from freemicro import padconfig
    from freemicro.input.actions import RecordingBackend, best_backend
    from freemicro.input.bridge import Bridge, joystick_line

    if args.init:
        target = Path(args.config).expanduser() if args.config else None
        try:
            written = padconfig.write_starter(target, force=args.force)
        except padconfig.PadConfigError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        print(f"Wrote a starter config to {written}")
        print("Open it in an editor - every key, colour and effect is yours.")
        return 0

    pad = _load_pad(args)
    if pad is None:
        return 2

    if args.list:
        _print_keymap(pad)
        return 0

    from freemicro import staleness
    from freemicro.daemon import PadLock

    if not args.take_pad and _pad_is_taken("keys"):
        return 1
    lock = PadLock(role="keys")
    if not args.take_pad:
        note = staleness.reclaim_stale_lock()
        lock.acquire()
        if note:
            print(note)

    device = _open_pad()
    if device is None:
        lock.release()
        return 1

    backend = RecordingBackend() if args.dry_run else best_backend()
    # `on_dispatch` so a press held back inside the chord settle window
    # prints when it actually fires, not when the next event happens to
    # arrive. A readout that lags the key it is describing is worse than
    # none while somebody is tuning chords.
    bridge = Bridge(pad, backend, on_dispatch=_print_dispatch)

    print(f"FreeMicro key bridge - {backend.description}")
    print(f"Config: {pad.origin}   Transport: {device.transport}")
    for warning in pad.warnings:
        print(f"  ! {warning}")
    if args.dry_run:
        print("\nPress pad keys; nothing is delivered. Ctrl-C to stop.")
        if pad.joystick.pointing:
            print(
                "Push the joystick to tune it: the px/s printed at the "
                "deflection\nthat feels right is your joystick.max_speed. "
                "See docs/CUSTOMIZING.md.\n"
            )
        else:
            print()
    else:
        print(
            "\nKeystrokes go to the FRONTMOST window - focus your Claude Code "
            "terminal first.\nCtrl-C to stop.\n"
        )

    def handle(message: dict) -> None:
        if args.dry_run:
            # In pointer mode this prints the velocity the sample implies as
            # well as the raw numbers, because px/s is the thing you are
            # actually choosing when you tune max_speed and gamma.
            line = joystick_line(message, bridge)
            if line is not None:
                print(line)
        for result in bridge.handle(message):
            _print_dispatch(result)

    from freemicro.device import close_shared, run_with_reconnect

    try:
        run_with_reconnect(
            handle,
            on_connect=lambda dev: print(f"  [pad connected: {dev.transport}]"),
            on_disconnect=lambda: print("  [pad disconnected - waiting…]"),
            seconds=args.seconds,
        )
    except KeyboardInterrupt:
        pass
    finally:
        # Before anything else: a `hold` binding may have real modifier keys
        # physically down right now, and if we exit without sending the
        # key-ups the whole machine behaves as if Ctrl is stuck - which
        # quitting FreeMicro does not fix.
        bridge.close()
        close_shared()
        lock.release()
    return 0


def _print_lighting(event, verbose: bool = False) -> None:
    """Say that we re-sent the lighting, and why.

    Reasserting is the kind of thing that should never be invisible magic: if
    the pad repaints itself a second after you quit ChatGPT, the log should say
    so, or the next person to see it will file a bug about flickering. The
    heartbeat is the exception - it is periodic by definition, so it is only
    printed when asked for.
    """
    if event is None or (event.verbose_only and not verbose):
        return
    print(f"  [lighting] {event.message}", flush=True)


def _describe_config_changes(old, new) -> str:
    """What actually changed between two loaded configs, for one log line.

    "reloaded" on its own is not information: the whole point of reloading in
    place is that the user can see their edit take effect without restarting,
    and a line that does not say what landed leaves them exactly as unsure as
    before.
    """
    parts = []
    keys = set(old.bindings) | set(new.bindings)
    changed = sum(
        1 for key in keys if old.bindings.get(key) != new.bindings.get(key)
    )
    if changed:
        parts.append(f"{changed} binding{'' if changed == 1 else 's'} changed")
    chords = set(old.chords) | set(new.chords)
    chords_changed = sum(
        1 for key in chords if old.chords.get(key) != new.chords.get(key)
    )
    if chords_changed:
        parts.append(
            f"{chords_changed} chord{'' if chords_changed == 1 else 's'} changed"
        )
    if old.chord_settle_ms != new.chord_settle_ms:
        parts.append(f"chord settle {new.chord_settle_ms:g}ms")
    if old.lighting != new.lighting:
        if old.lighting.enabled != new.lighting.enabled:
            parts.append(f"LEDs {'on' if new.lighting.enabled else 'off'}")
        else:
            parts.append("lighting changed")
    if old.agent_keys != new.agent_keys:
        parts.append(f"agent keys: {new.agent_keys.policy}")
    if old.joystick != new.joystick:
        parts.append("joystick retuned")
    return ", ".join(parts) if parts else "no functional changes"


def _print_dispatch(result) -> None:
    # ``describe()`` for every case, including the unbound one. A key that is a
    # chord member with no solo binding is not unmapped, it is standing by, and
    # printing "unmapped" about a key the user has just bound is how a working
    # feature gets reported as broken.
    line = f"  {result.input_id:9} {result.describe()}"
    print(line if result.ok else f"{line}  FAILED: {result.error}")


def cmd_lights(args: argparse.Namespace) -> int:
    """Send lighting straight to the pad - the LED smoke test."""
    from freemicro.device import close_shared
    from freemicro.device.lighting import LightingError
    from freemicro.renderers.micro_leds import MicroLedsRenderer

    if args.coexist or args.zones:
        from freemicro.lighting_owner import VENDOR_QUIET_ZONES

        zones = (
            list(VENDOR_QUIET_ZONES)
            if args.coexist
            else [z.strip() for z in args.zones.split(",") if z.strip()]
        )
        return _set_lighting_zones(args, zones)

    if args.enable or args.disable:
        return _set_lighting_enabled(args, enabled=bool(args.enable))

    pad = _load_pad(args)
    if pad is None:
        return 2

    # A one-shot `lights` is short enough to share the pad with a running
    # owner, so this warns rather than refusing - but the owner will repaint
    # over it on its next state change, and that surprises people.
    from freemicro import daemon as _daemon

    holder = _daemon.lock_holder()
    if holder:
        print(f"Note: {_daemon.describe_holder(holder)} also has the pad - it "
              "will repaint\n  over this on its next state change.",
              file=sys.stderr)

    device = _open_pad()
    if device is None:
        return 1

    # An explicit `freemicro lights` *is* the user asking for light, so it runs
    # even when the persistent opt-in is off. Only the background renderer
    # respects lighting.enabled.
    renderer = MicroLedsRenderer(device=device, config=pad)
    states = list(AgentState) if args.cycle else [AgentState(args.state)]
    try:
        for state in states:
            try:
                light = _override_light(renderer.light_for(state), args)
            except LightingError as exc:
                print(f"Bad lighting value: {exc}", file=sys.stderr)
                return 2
            for message in renderer.messages_for(state, light=light):
                device.send(message)
            print(f"  {state.value:9} {light.describe()}")
            # Each call replaces the last, so a fast sequence looks like only
            # its final frame. Hold long enough for a human to actually see it.
            time.sleep(args.hold)
    except KeyboardInterrupt:
        pass
    finally:
        if args.restore:
            renderer.close()
        else:
            close_shared()
    return 0


def _edit_lighting(args: argparse.Namespace, **changes) -> int:
    """Write one or more ``lighting.*`` keys into the user's config.

    Creates the config from the shipped default first if it does not exist yet,
    so ``freemicro lights --enable`` works on a machine that has never been
    configured - which is exactly the machine it is usually run on.
    """
    from freemicro import padconfig

    target = Path(args.config).expanduser() if args.config else padconfig.user_path()
    if not target.exists():
        padconfig.write_starter(target)
        print(f"Created {target}")
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"Could not read {target}: {exc}", file=sys.stderr)
        return 2
    lighting = data.setdefault("lighting", {})
    lighting.update(changes)
    target.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(f"Updated {target}")
    return 0


def _set_lighting_enabled(args: argparse.Namespace, enabled: bool) -> int:
    """Turn the LED renderer on or off, persistently.

    FreeMicro ships with lighting **off**. macOS shares this HID device, so the
    vendor app may also be writing these LEDs, and taking over the hardware
    should be a decision someone makes once, on purpose. It is no longer a
    decision that requires quitting anything: see :mod:`freemicro.lighting_owner`
    for how the two coexist.
    """
    result = _edit_lighting(args, enabled=enabled)
    if result != 0:
        return result

    if not enabled:
        print("LED control disabled. The pad is yours again.")
        return 0

    print(
        "FreeMicro will now light the pad from agent state.\n"
        "  •  Colours match the factory palette out of the box; edit\n"
        "     lighting.states to change them.\n"
        "  •  `freemicro lights --disable` hands the pad back."
    )
    if _chatgpt_running():
        print(
            "\nThe ChatGPT desktop app is running. You do NOT have to quit it:\n"
            "  •  Your keys, dial and joystick are unaffected either way - both\n"
            "     apps read this device happily.\n"
            "  •  It only writes lighting when its own state changes, and\n"
            "     FreeMicro re-sends its colours the moment ChatGPT quits.\n"
            "  •  To never collide at all:  freemicro lights --coexist"
        )
    return 0


def _set_lighting_zones(args: argparse.Namespace, zones: "list[str]") -> int:
    """Point FreeMicro at a specific set of lighting zones, persistently.

    The reason this is a first-class command rather than a config edit: the
    ``backlight``-only setting is the one configuration in which FreeMicro and
    the ChatGPT app *cannot* fight, and a user who has just been told about the
    conflict should be able to act on it in one line.
    """
    from freemicro.device.lighting import LightingError, parse_zone
    from freemicro.lighting_owner import VENDOR_QUIET_ZONES

    try:
        parsed = list(dict.fromkeys(parse_zone(zone) for zone in zones))
    except LightingError as exc:
        print(f"Bad zone: {exc}", file=sys.stderr)
        return 2
    result = _edit_lighting(args, enabled=True, zones=parsed)
    if result != 0:
        return result
    print(f"FreeMicro now drives: {', '.join(parsed)}")
    if all(zone in VENDOR_QUIET_ZONES for zone in parsed):
        print(
            "\nThis is the coexistence setting. The ChatGPT app keeps the key\n"
            "backlight dark essentially always - it flashes it for ~4s when you\n"
            "change the selected thread and otherwise leaves it off - so the two\n"
            "of you never write the same zone.\n"
            "  Trade-off: the backlight sits UNDER the keycaps, so agent state\n"
            "  reads as one colour glowing through the whole pad rather than six\n"
            "  independent per-project lights. You lose the per-key detail; you\n"
            "  gain colours nothing ever overwrites.\n"
            "  Back to per-key status:  freemicro lights --zones agent_keys"
        )
    return 0


def _override_light(light, args: argparse.Namespace):
    """Apply any ``freemicro lights`` flags on top of the configured look."""
    from freemicro.device.lighting import parse_color, parse_effect
    from freemicro.padconfig import StateLight

    return StateLight(
        color=parse_color(args.color) if args.color else light.color,
        effect=parse_effect(args.effect) if args.effect else light.effect,
        brightness=(
            light.brightness if args.brightness is None else args.brightness
        ),
        speed=light.speed if args.speed is None else args.speed,
        magic=light.magic,
    )


def _first_run_greeting() -> None:
    """Explain the two macOS permissions once, in plain language.

    Shown when no config exists yet, i.e. the first time somebody runs this.
    Nobody should have to read a README to find out why their keys do nothing.
    """
    from freemicro import padconfig

    if padconfig.user_path().exists() or padconfig.xdg_path().exists():
        return
    print("Welcome to FreeMicro. Two things macOS needs from you, once:\n")
    print("  1. Input Monitoring - to READ the pad.")
    print("     System Settings → Privacy & Security → Input Monitoring")
    print("  2. Accessibility - to TYPE for you. Without it macOS silently")
    print("     throws your keystrokes away.")
    print("     System Settings → Privacy & Security → Accessibility\n")
    print("  Add THIS terminal app to both, then restart it - macOS only reads")
    print("  the grants at launch. `freemicro doctor` checks both for you.\n")
    print("  Running on the built-in defaults for now. To make them yours:")
    print("    freemicro keys --init      # writes ~/.freemicro/keymap.json")
    print("    freemicro config --edit    # opens it\n")


def _pad_is_taken(role: str) -> bool:
    """Refuse to fight another FreeMicro for the device, and say who has it.

    Two processes holding the pad is the failure mode with the worst symptoms:
    half your keys work, the lights flicker between two owners, and nothing in
    either program's output hints at the other. Naming the conflict costs one
    check and saves a support thread.
    """
    from freemicro import daemon

    holder = daemon.lock_holder()
    if holder is None:
        return False
    print(
        f"{daemon.describe_holder(holder)} already has the pad.\n"
        "  Only one process can usefully hold this device, so FreeMicro will\n"
        "  not fight it - your pad is already live.\n"
        "  → freemicro daemon status        what it's doing\n"
        "  → freemicro daemon uninstall     stop it and take the pad back\n"
        f"  → freemicro {role} --take-pad{' ' * (10 - len(role))}take it anyway "
        "(expect a mess)",
        file=sys.stderr,
    )
    return True


def _code_watcher(args: argparse.Namespace):
    """The self-restart watcher, unless the user asked for none.

    A seam as much as a factory: tests replace this to drive the restart path
    without a real ``execv``, and ``--no-restart`` exists because anyone
    debugging FreeMicro *from* a working tree edits the package constantly and
    would otherwise be restarted out from under themselves.
    """
    from freemicro import staleness

    if getattr(args, "no_restart", False) or os.environ.get("FREEMICRO_NO_RESTART"):
        return None
    return staleness.CodeWatcher()


def cmd_run(args: argparse.Namespace) -> int:
    """The everyday command: pad keys in, agent-state lights out, one process."""
    from freemicro import staleness
    from freemicro.daemon import PadLock

    if not args.take_pad and _pad_is_taken("run"):
        return 1
    _first_run_greeting()
    lock = PadLock(role="run")
    if not args.take_pad:
        note = staleness.reclaim_stale_lock()
        lock.acquire()
        if note:
            print(note)
    watcher = _code_watcher(args)
    try:
        result = _run_pipeline(args, headless=False, watcher=watcher)
    finally:
        # Release before re-execing: the new process takes this same lock, and
        # handing it over cleanly is the difference between a restart and a
        # process that refuses its own pad.
        lock.release()
    if watcher is not None and watcher.pending:
        watcher.restart(release=_hand_pad_back)
    return result


def _run_pipeline(
    args: argparse.Namespace, headless: bool = False, watcher=None
) -> int:
    """The shared keys-in/lights-out loop behind ``run`` and the daemon."""
    from freemicro import staleness
    from freemicro.device import close_shared, run_with_reconnect
    from freemicro.input.actions import RecordingBackend, best_backend
    from freemicro.input.bridge import Bridge, JoystickTracker
    from freemicro.lighting_owner import (
        REASON_CONFIG,
        REASON_CONFIG_BROKEN,
        LightingOwner,
        coexist_advice,
        vendor_app_running,
    )
    from freemicro.renderers.micro_leds import MicroLedsRenderer

    cfg = Config.load()
    pad = _load_pad(args)
    if pad is None:
        return 2
    store = _store(cfg)
    verbose = bool(getattr(args, "verbose", False))
    # Defends our colours against the other app writing them, and reloads the
    # config in place when it changes. Costs one clock read per key event.
    owner = LightingOwner(config=pad)

    device = _open_pad(headless=headless)
    if device is None:
        print("Starting anyway - FreeMicro will connect as soon as the pad "
              "appears.\n", flush=True)

    renderers: list = []
    backend = RecordingBackend() if args.dry_run else best_backend()
    # `on_dispatch` so a press held back inside the chord settle window
    # prints when it actually fires, not when the next event happens to
    # arrive. A readout that lags the key it is describing is worse than
    # none while somebody is tuning chords.
    bridge = Bridge(pad, backend, on_dispatch=_print_dispatch)
    last: AgentState | None = None
    override = Path(args.config).expanduser() if getattr(args, "config", None) else None
    config_watcher = staleness.ConfigWatcher(
        path=staleness.config_watch_path(pad, override),
        loader=lambda: _reload_pad(override),
    )

    def rebuild(dev) -> None:
        """(Re)attach the LED renderer to a freshly opened device."""
        nonlocal renderers, last
        renderers = []
        leds = MicroLedsRenderer(device=dev, config=pad)
        # Kept in the list even with lighting off: it renders nothing and
        # touches nothing in that state, but staying attached is what lets
        # `freemicro lights --enable` take effect without a restart.
        renderers.append(leds)
        last = None  # force a re-light so the pad shows state immediately
        if leds.available():
            note = "keys live, LEDs live"
        elif pad.lighting.enabled:
            note = "keys live, LEDs failed"
        else:
            note = "keys live, LEDs off"
        print(f"  [pad connected over {dev.transport} - {note}]", flush=True)
        # A reconnected pad may have been repainted while we were away, so the
        # renderer's cached frame no longer describes what is on the LEDs.
        _print_lighting(owner.attach(leds), verbose)

    def adopt(new_pad) -> None:
        """Swap a freshly loaded config into the *whole* running process.

        Bindings, joystick tuning and the LED renderer together, in one place.
        The lighting owner reloads the file too, but only for lighting and
        only while a pad is attached - and a bridge whose colours follow an
        edit while its keys keep doing the old thing is the incident this
        exists to end.
        """
        nonlocal pad
        summary = _describe_config_changes(pad, new_pad)
        pad = new_pad
        bridge.config = new_pad
        bridge.joystick = JoystickTracker(new_pad.joystick)
        for renderer in renderers:
            apply_config = getattr(renderer, "apply_config", None)
            if callable(apply_config):
                apply_config(new_pad)
        print(f"  [config] reloaded - {summary}", flush=True)
        for warning in new_pad.warnings:
            print(f"  ! {warning}", flush=True)

    def lighting_event(event) -> None:
        """One lighting-owner event, routed to whoever it is really about.

        Its config events are demoted to verbose: the config watcher below has
        already said what changed, and two descriptions of one edit is how a
        log stops being read.
        """
        if event.reason in (REASON_CONFIG, REASON_CONFIG_BROKEN):
            if verbose:
                print(f"  [lighting] {event.message}", flush=True)
            return
        _print_lighting(event, verbose)

    def reload_config() -> None:
        change = config_watcher.poll() if config_watcher is not None else None
        if change is None:
            return
        if change.ok:
            adopt(change.config)
        else:
            # Never half-apply: the running config stays exactly as it was.
            print(
                "  [config] changed but would not load - still running the one "
                f"it started with ({change.error})",
                flush=True,
            )

    def dropped() -> None:
        nonlocal renderers, last
        renderers = []
        last = None
        owner.attach(None)
        print("  [pad disconnected - waiting for it to come back. This is normal;"
              "\n   the pad drops on sleep, on range, on a nudged cable.]",
              flush=True)

    label = "freemicro daemon" if headless else "freemicro run"
    print(f"{label} - keys: {backend.description}")
    print(f"Config: {pad.origin}")
    if pad.lighting.enabled:
        print("LEDs:   driven from agent state (freemicro lights --disable to stop)")
        print(f"        zones: {', '.join(pad.lighting.zones)}")
    else:
        print("LEDs:   OFF - FreeMicro does not take over your pad uninvited.")
        print("        Turn them on once:  freemicro lights --enable")
    advice = coexist_advice(pad.lighting, vendor_running=vendor_app_running())
    for line in advice.splitlines():
        print(f"        {line}")
    for warning in pad.warnings:
        print(f"  ! {warning}")
    print("" if headless else "Ctrl-C to stop.\n", flush=True)

    housekeeping = [time.time()]

    def tick() -> None:
        nonlocal last
        # Before rendering, not after: a reassert clears the renderer's dedupe
        # cache, so acting on it here means the resend happens in *this* tick.
        for event in owner.poll():
            lighting_event(event)
        reload_config()
        if watcher is not None:
            decision = watcher.poll()
            if decision is not None:
                print(decision.message, flush=True)
                if decision.restart:
                    # Unwind the run loop first. Raising here - from the plain
                    # Python tick, never from the IOKit callback - is what lets
                    # `device.stream` unschedule its input-report callback and
                    # close the pad before anyone calls execv.
                    raise staleness.RestartRequested()
        state = store.resolved_state()
        for renderer in renderers:
            renderer.render(state)
        if state != last:
            # Unconditional. This one line is the whole of what the screen
            # renderer ever delivered, and it is the only thing that tells you
            # the loop is alive on a machine whose pad is unplugged.
            _print_state(state)
            last = state
        if headless and time.time() - housekeeping[0] > 300:
            # launchd holds this log open for the life of the job, so nothing
            # else is going to keep it from growing forever.
            housekeeping[0] = time.time()
            from freemicro import daemon as _daemon

            _daemon.rotate_log()

    def handle(message: dict) -> None:
        # One clock read. This channel carries key events *and* lighting, so
        # nothing may be written to it while a burst is arriving.
        owner.note_input()
        for result in bridge.handle(message):
            _print_dispatch(result)

    # run_with_reconnect covers "no pad yet" as well as "pad went away", so the
    # command is useful before you plug in and survives every drop afterwards.
    renderers = []
    try:
        run_with_reconnect(
            handle,
            on_tick=tick,
            on_connect=rebuild,
            on_disconnect=dropped,
            tick_interval=args.interval,
            seconds=args.seconds,
        )
    except (KeyboardInterrupt, staleness.RestartRequested):
        # A restart is not an error and not an exit: the caller checks the
        # watcher, releases the pad lock, and re-execs. Everything this
        # function owns is handed back in the `finally` first.
        pass
    finally:
        # First, because this is the one that can outlive the process: a
        # `hold` binding leaves real modifier keys down, and a restart
        # re-execs without ever running atexit. See Bridge.close().
        bridge.close()
        for renderer in renderers:
            renderer.close()
        close_shared()
    return 0


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    from freemicro import padconfig

    p = argparse.ArgumentParser(
        prog="freemicro",
        description="Turn a macro pad into a live status light for coding agents.",
    )
    p.add_argument("--version", action="version", version=f"freemicro {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    d = sub.add_parser("detect", help="read-only HID capability probe (Milestone 0)")
    d.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    d.set_defaults(func=cmd_detect)

    st = sub.add_parser(
        "start", help="guided setup: permissions, pad, hooks, daemon, proof"
    )
    st.add_argument("--yes", action="store_true",
                    help="accept every default without asking (non-interactive)")
    st.add_argument("--lights", dest="lights", action="store_true", default=None,
                    help="enable LED control without asking")
    st.add_argument("--no-lights", dest="lights", action="store_false",
                    help="leave LED control off without asking")
    st.add_argument("--hooks", dest="hooks", action="store_true", default=None,
                    help="install Claude Code hooks without asking")
    st.add_argument("--no-hooks", dest="hooks", action="store_false",
                    help="skip the Claude Code hooks without asking")
    st.add_argument("--daemon", dest="daemon", action="store_true", default=None,
                    help="install the background daemon without asking")
    st.add_argument("--no-daemon", dest="daemon", action="store_false",
                    help="skip the background daemon without asking")
    st.set_defaults(func=cmd_start)

    i = sub.add_parser("install", help="install Claude Code hooks")
    i.add_argument("--settings", help="path to Claude settings.json")
    i.add_argument("--dry-run", action="store_true", help="print, don't write")
    i.add_argument("--uninstall", action="store_true",
                   help="remove FreeMicro's hooks, leaving everyone else's")
    i.add_argument("--no-verify", action="store_true",
                   help="don't run the selftest afterwards")
    i.set_defaults(func=cmd_install)

    un = sub.add_parser(
        "uninstall",
        help="remove FreeMicro's hooks, daemon, config and state (shows the "
             "list first)",
    )
    un.add_argument("--dry-run", action="store_true",
                    help="print exactly what would be removed and stop")
    un.add_argument("-y", "--yes", action="store_true",
                    help="do not ask for confirmation (for scripts)")
    un.add_argument("--keep-config", action="store_true",
                    help="keep your keymap, engine settings and saved layouts "
                         "so a reinstall picks them up")
    un.add_argument("--settings", help="path to Claude settings.json")
    un.set_defaults(func=cmd_uninstall)

    sf = sub.add_parser(
        "selftest",
        help="prove the hook → state → LED loop works (no live agent needed)",
    )
    sf.add_argument("--json", action="store_true", help="machine-readable report")
    sf.add_argument("--settings", help="path to Claude settings.json")
    sf.add_argument("--command",
                    help="test this hook command instead of the installed one")
    sf.add_argument("--in-process", action="store_true",
                    help="skip the subprocess (fast, but proves nothing about "
                         "the installed binary)")
    sf.add_argument("--timeout", type=float, default=30.0,
                    help="seconds to allow each hook invocation")
    sf.set_defaults(func=cmd_selftest)

    dn = sub.add_parser(
        "daemon", help="the LaunchAgent that keeps FreeMicro running at login"
    )
    dn.add_argument("action", nargs="?", default="status",
                    choices=["install", "uninstall", "status", "logs", "run"],
                    help="what to do (default: status)")
    dn.add_argument("--json", action="store_true",
                    help="with status: machine-readable output")
    dn.add_argument("--lines", type=int, default=50,
                    help="with logs: how many lines to show")
    dn.add_argument("--config", help="path to a pad config")
    dn.add_argument("--dry-run", action="store_true",
                    help="with run: log key presses instead of delivering them")
    dn.add_argument("--interval", type=float, default=0.25,
                    help="with run: how often to poll agent state")
    dn.add_argument("--seconds", type=float, default=0.0,
                    help="with run: stop after N seconds (0 = forever)")
    dn.add_argument("--force", action="store_true",
                    help="with install: install even from a folder macOS blocks "
                         "background agents from reading")
    dn.add_argument("--no-restart", action="store_true",
                    help="with run: don't re-exec when FreeMicro is updated")
    dn.add_argument("-v", "--verbose", action="store_true",
                    help="with run: log routine lighting reasserts too")
    dn.set_defaults(func=cmd_daemon, take_pad=False)

    h = sub.add_parser("hook", help="internal: consume a hook event from stdin")
    h.set_defaults(func=cmd_hook)

    cf = sub.add_parser("config", help="show, locate or edit your pad config")
    cf.add_argument("--path", action="store_true", help="print the path and exit")
    cf.add_argument("--edit", action="store_true", help="open it in $EDITOR")
    cf.add_argument("--create", action="store_true",
                    help="create it from the default without opening an editor")
    cf.add_argument("--config", help="operate on a specific file")
    cf.add_argument("--web", action="store_true",
                    help="open the visual editor in your browser")
    cf.add_argument("--no-browser", action="store_true",
                    help="with --web: print the URL, don't open a browser")
    cf.set_defaults(func=cmd_config)

    mb = sub.add_parser("menubar", help="run the macOS status-bar item")
    mb.set_defaults(func=cmd_menubar)

    dr = sub.add_parser(
        "doctor", help="check permissions, transport and the write path"
    )
    dr.add_argument("--config", help="path to a pad config")
    dr.add_argument("--timeout", type=float, default=2.0,
                    help="seconds to wait for the device.status reply")
    dr.set_defaults(func=cmd_doctor)

    rn = sub.add_parser(
        "run", help="the everyday command: pad keys in, agent-state lights out"
    )
    rn.add_argument("--config", help=f"path to a pad config ({padconfig.FILENAME})")
    rn.add_argument("--dry-run", action="store_true",
                    help="log key presses instead of delivering them")
    rn.add_argument("--interval", type=float, default=0.25,
                    help="how often to poll agent state, in seconds")
    rn.add_argument("--seconds", type=float, default=0.0,
                    help="stop after N seconds (0 = run until Ctrl-C)")
    rn.add_argument("--take-pad", action="store_true",
                    help="start even if another FreeMicro process holds the pad")
    rn.add_argument("--no-restart", action="store_true",
                    help="don't re-exec when FreeMicro itself is updated on "
                         "disk (for hacking on FreeMicro)")
    rn.add_argument("-v", "--verbose", action="store_true",
                    help="report routine lighting reasserts too (heartbeat)")
    rn.set_defaults(func=cmd_run)

    k = sub.add_parser("keys", help="bridge pad keys into your terminal")
    k.add_argument("--config", help=f"path to a pad config ({padconfig.FILENAME})")
    k.add_argument("--list", action="store_true",
                   help="print the resolved bindings, lighting and action kinds")
    k.add_argument("--init", action="store_true",
                   help="write a starter config you can edit")
    k.add_argument("--force", action="store_true",
                   help="with --init, overwrite an existing config")
    k.add_argument("--dry-run", action="store_true",
                   help="print what each key would do, don't type it")
    k.add_argument("--seconds", type=float, default=0.0,
                   help="stop after N seconds (0 = run until Ctrl-C)")
    k.add_argument("--take-pad", action="store_true",
                   help="start even if another FreeMicro process holds the pad")
    k.set_defaults(func=cmd_keys)

    lt = sub.add_parser("lights", help="send lighting straight to the pad")
    lt.add_argument("state", nargs="?", default=AgentState.DONE.value,
                    choices=[s.value for s in AgentState],
                    help="which configured state's look to show")
    lt.add_argument("--config", help="path to a pad config")
    lt.add_argument("--enable", action="store_true",
                    help="opt in: let FreeMicro drive the pad's LEDs from now on")
    lt.add_argument("--disable", action="store_true",
                    help="stop driving the LEDs and hand the pad back")
    lt.add_argument("--coexist", action="store_true",
                    help="drive only the key backlight - the one zone the "
                         "ChatGPT app leaves dark, so neither app ever "
                         "overwrites the other")
    lt.add_argument("--zones",
                    help="which zones FreeMicro drives, comma separated "
                         "(agent_keys, backlight, underglow)")
    lt.add_argument("--cycle", action="store_true",
                    help="walk every state in turn")
    lt.add_argument("--color", help="override the colour (#RRGGBB)")
    lt.add_argument("--effect",
                    help="override the effect "
                         "(off/solid/snake/rainbow/breath/gradient/shallow-breath)")
    lt.add_argument("--brightness", type=float, help="override brightness (0-1)")
    lt.add_argument("--speed", type=float, help="override speed (0-1)")
    lt.add_argument("--hold", type=float, default=1.5,
                    help="seconds to hold each colour (each call replaces the "
                         "last, so give your eyes time)")
    lt.add_argument("--restore", action="store_true",
                    help="apply lighting.on_exit when finished")
    lt.set_defaults(func=cmd_lights)

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

    ln = sub.add_parser("learn", help="build a replay profile from sniffed captures (Path B)")
    ln.add_argument("state", nargs="*", metavar="CODEXSTATE=FILE",
                    help="e.g. thinking=thinking.json done=done.json")
    ln.add_argument("--color", nargs="*", metavar="COLOR=FILE",
                    help="optional solid-colour captures: red=red.json green=green.json blue=blue.json")
    ln.add_argument("--vid-pid", help="pin the device, e.g. 574c:1df9")
    ln.add_argument("--out", help="output profile path (default ~/.freemicro/protocol.json)")
    ln.set_defaults(func=cmd_learn)

    v = sub.add_parser("verify-leds", help="active write-test: light the pad and record a verdict")
    v.add_argument("--hold", type=float, default=1.5, help="seconds per state")
    v.add_argument("--yes", action="store_true", help="skip prompts (non-interactive)")
    v.set_defaults(func=cmd_verify_leds)

    dm = sub.add_parser("demo", help="play the full state sequence (no agent/hw needed)")
    dm.add_argument("--step", type=float, default=1.5, help="seconds per state")
    dm.add_argument("--loops", type=int, default=1, help="how many times to cycle")
    dm.set_defaults(func=cmd_demo)

    return p


#: Command-line spellings FreeMicro used to accept, and what to use instead.
#: argparse's own "invalid choice" is accurate and useless: it lists what
#: exists without ever saying that the thing you typed last month is gone or
#: why. Anything in here gets a sentence.
REMOVED_ARGS: "dict[str, str]" = {
    "watch": (
        "It lit non-pad targets, and there are none left.\n"
        "Use:  freemicro run       same lights, plus the key bridge, and it\n"
        "                          prints every state change to this terminal."
    ),
    "--no-screen": (
        "It suppressed the on-screen status chip, which no longer exists.\n"
        "`freemicro run` already drives only the pad, and prints each state\n"
        "change here as it happens."
    ),
}


def _removed_argument(argv: "list[str]") -> "tuple[str, str] | None":
    """The first removed subcommand or flag in ``argv``, with its explanation."""
    for index, token in enumerate(argv):
        name = token.split("=", 1)[0]
        if name.startswith("-"):
            if name in REMOVED_ARGS:
                return name, REMOVED_ARGS[name]
        elif index == 0 and name in REMOVED_ARGS:
            # Only in subcommand position: `--config watch` is a file path.
            return name, REMOVED_ARGS[name]
    return None


def main(argv: "list[str] | None" = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    removed = _removed_argument(argv)
    if removed is not None:
        token, advice = removed
        print(f"`{token}` was removed from FreeMicro.", file=sys.stderr)
        for line in advice.splitlines():
            print(f"  {line}", file=sys.stderr)
        return 2
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)
