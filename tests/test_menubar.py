"""Tests for the menu bar.

These run with no hardware, no GUI session and no menu bar - which is the whole
reason the menu *model* is a pure function of a snapshot. Nothing here imports
AppKit; ``freemicro.menubar.cocoa`` is only ever asked whether it *could* load,
which is a question it answers without loading anything.

The conftest sets ``FREEMICRO_NO_DEVICE=1`` and relocates ``FREEMICRO_HOME``, so
the probes below run against an empty temp config rather than the developer's
real one.
"""

from __future__ import annotations

import json
import time

from freemicro.menubar import checks, cocoa, model, status
from freemicro.menubar.model import Snapshot, build_menu, menu_item
from freemicro.state.engine import AgentState


# ---------------------------------------------------------------------------
# The menu model
# ---------------------------------------------------------------------------

def test_menu_always_offers_the_essentials():
    items = build_menu(Snapshot())
    for required in ("state", "connection", "toggle.lighting", "config",
                     "doctor", "quit"):
        assert menu_item(items, required) is not None, required


def test_every_action_is_one_the_app_knows_how_to_dispatch():
    items = build_menu(Snapshot(input_monitoring=False, accessibility=False,
                                chatgpt_running=True))
    for item in items:
        if item.action is not None:
            assert item.action in model.ACTIONS


def test_only_actionable_rows_are_enabled():
    for item in build_menu(Snapshot(connected=True, battery=50, firmware="1.0")):
        assert item.enabled == (item.action is not None)


def test_state_row_carries_the_state_colour():
    row = menu_item(build_menu(Snapshot(state=AgentState.WAITING)), "state")
    assert row.label == "Waiting for you"
    assert row.color == (255, 149, 0)


def test_idle_has_no_colour_so_it_survives_a_dark_menu_bar():
    # The palette's idle is near-black; drawn literally it would vanish.
    assert model.state_color(AgentState.IDLE) is None
    assert model.bar_title(Snapshot()).color is None


def test_bar_glyph_differs_per_state_so_shape_carries_meaning_too():
    glyphs = {model.bar_title(Snapshot(state=s)).text for s in AgentState}
    assert len(glyphs) == len(list(AgentState))


# ---------------------------------------------------------------------------
# Connection: a wireless pad dropping is normal, never an error
# ---------------------------------------------------------------------------

def test_disconnected_reads_as_a_fact_not_a_failure():
    label = model.connection_label(Snapshot(connected=False))
    assert label == "Pad not connected"
    assert "error" not in label.lower()
    assert "fail" not in label.lower()


def test_transport_is_named_in_the_connection_row():
    assert "USB" in model.connection_label(Snapshot(connected=True, transport="USB"))
    assert "Bluetooth" in model.connection_label(
        Snapshot(connected=True, transport="Bluetooth Low Energy")
    )


def test_missing_permission_is_visible_on_the_connection_row():
    label = model.connection_label(
        Snapshot(connected=True, transport="USB", input_monitoring=False)
    )
    assert "no permission" in label


def test_unsupported_platform_says_so():
    assert model.connection_label(Snapshot(supported=False)) == (
        "Pad support needs macOS"
    )


# ---------------------------------------------------------------------------
# Battery
# ---------------------------------------------------------------------------

def test_battery_row_is_absent_when_there_is_no_reading():
    assert menu_item(build_menu(Snapshot(connected=True)), "battery") is None


def test_battery_row_shows_charge_and_charging():
    snap = Snapshot(connected=True, battery=62, charging=True, reading_age=1.0)
    assert model.battery_label(snap) == "Battery 62% - charging"


def test_a_stale_reading_is_labelled_with_its_age_not_passed_off_as_current():
    snap = Snapshot(connected=True, battery=62, reading_age=1800.0)
    assert model.battery_label(snap) == "Battery 62% - 30m ago"


def test_firmware_row_appears_only_when_known():
    assert menu_item(build_menu(Snapshot(connected=True)), "firmware") is None
    items = build_menu(Snapshot(connected=True, firmware="1.4.2"))
    assert menu_item(items, "firmware").label == "Firmware 1.4.2"


# ---------------------------------------------------------------------------
# Warnings
# ---------------------------------------------------------------------------

def test_no_warnings_when_nothing_is_wrong():
    assert model.warning_items(Snapshot(input_monitoring=True)) == []


def test_unknown_input_monitoring_is_not_treated_as_denied():
    # `None` means macOS has not been asked yet. Warning would send people to a
    # settings pane where the app they need to tick is not listed.
    assert model.warning_items(Snapshot(input_monitoring=None)) == []


def test_each_permission_warning_links_to_its_settings_pane():
    items = model.warning_items(
        Snapshot(input_monitoring=False, accessibility=False)
    )
    actions = {item.action for item in items}
    assert actions == {"open_input_monitoring", "open_accessibility"}
    for item in items:
        assert item.detail  # every warning explains itself


def test_an_inert_pad_is_the_first_thing_the_menu_says():
    """Nothing driving the pad outranks every other warning there is.

    The pad emits no ordinary scancodes, so a pad nobody is listening to is
    not degraded - it is dead - and every other warning here is about a pad
    that at least has an owner.
    """
    items = model.warning_items(
        Snapshot(pad_listening=False, input_monitoring=False, accessibility=False)
    )
    assert items[0].key == "warn.inert"
    assert items[0].action == "start_bridge"
    assert "freemicro run" in items[0].detail


def test_a_listening_pad_produces_no_inert_warning():
    assert model.menu_item(build_menu(Snapshot()), "warn.inert") is None


def test_a_stale_process_is_named_with_the_fix_and_a_button_where_we_have_one():
    note = model.StaleNote(
        summary="Out of date: the background daemon",
        fix="Fix: freemicro daemon install",
        action="restart_daemon",
    )
    row = model.menu_item(build_menu(Snapshot(stale=(note,))), "warn.stale.0")
    assert row.label == note.summary
    assert row.action == "restart_daemon"
    assert row.enabled is True


def test_a_process_we_cannot_honestly_restart_is_stated_not_offered():
    # Somebody's `freemicro run` lives in their terminal. A button that quietly
    # killed it would be worse than a sentence that names it.
    note = model.StaleNote(summary="Out of date: `freemicro run`", fix="Ctrl-C it")
    row = model.menu_item(build_menu(Snapshot(stale=(note,))), "warn.stale.0")
    assert row.action is None
    assert row.enabled is False
    assert row.detail == "Ctrl-C it"


def test_chatgpt_warning_appears_and_explains_the_conflict():
    """The warning must scope itself to lighting.

    macOS lets both apps read the pad at once, so keys, dial and joystick are
    unaffected - only LED writes collide. Telling someone to quit ChatGPT to
    get their keys working would send them chasing a problem they do not have.
    """
    items = build_menu(Snapshot(chatgpt_running=True))
    warning = menu_item(items, "warn.chatgpt")
    assert warning is not None
    detail = warning.detail.lower()
    assert "lighting" in detail          # scoped to the thing that conflicts
    assert "keys" in detail              # says input is fine
    assert "--coexist" in detail         # names the permanent fix


# ---------------------------------------------------------------------------
# LED control, config, ownership
# ---------------------------------------------------------------------------

def test_led_toggle_mirrors_the_config_setting():
    off = menu_item(build_menu(Snapshot()), "toggle.lighting")
    on = menu_item(build_menu(Snapshot(lighting_enabled=True)), "toggle.lighting")
    assert off.checked is False
    assert on.checked is True


def test_led_toggle_is_disabled_where_the_pad_cannot_work():
    item = menu_item(build_menu(Snapshot(supported=False)), "toggle.lighting")
    assert item.enabled is False


def test_config_row_prefers_the_web_ui_when_there_is_one():
    assert menu_item(build_menu(Snapshot(web_ui=True)), "config").label.endswith("…")
    assert "Finder" in menu_item(build_menu(Snapshot()), "config").label


def test_pad_owner_is_shown_rather_than_fought_for():
    items = build_menu(Snapshot(owner="the background daemon (pid 42)"))
    row = menu_item(items, "owner")
    assert "the background daemon" in row.label
    assert row.enabled is False


def test_session_count_only_appears_when_there_is_more_than_one():
    assert menu_item(build_menu(Snapshot(sessions=1)), "sessions") is None
    assert menu_item(build_menu(Snapshot(sessions=3)), "sessions").label == (
        "3 sessions"
    )


def test_separators_never_bookend_the_menu():
    items = build_menu(Snapshot(chatgpt_running=True))
    assert not items[0].separator
    assert not items[-1].separator


# ---------------------------------------------------------------------------
# Snapshot gathering - reads only, and never raises
# ---------------------------------------------------------------------------

def test_snapshot_reports_no_pad_when_the_device_layer_is_disabled():
    # conftest sets FREEMICRO_NO_DEVICE=1, so this is the "unplugged" path.
    snap = status.snapshot()
    assert snap.connected is False
    assert snap.battery is None


def test_snapshot_survives_every_underlying_probe_blowing_up(monkeypatch):
    """A pad that drops mid-poll must degrade a field, not kill the menu bar."""

    def boom(*_args, **_kwargs):
        raise RuntimeError("the pad went away")

    monkeypatch.setattr("freemicro.device.device_transport", boom)
    monkeypatch.setattr("freemicro.device.is_supported", boom)
    monkeypatch.setattr("freemicro.permissions.input_monitoring", boom)
    monkeypatch.setattr("freemicro.permissions.accessibility", boom)
    monkeypatch.setattr("freemicro.permissions.chatgpt_running", boom)
    monkeypatch.setattr("freemicro.daemon.lock_holder", boom)
    monkeypatch.setattr("freemicro.padconfig.load", boom)

    snap = status.snapshot()
    assert isinstance(snap, Snapshot)
    assert snap.connected is False
    assert snap.owner == ""
    assert snap.lighting_enabled is False
    # And the menu still builds from it.
    assert menu_item(build_menu(snap), "quit") is not None


def test_each_probe_swallows_its_own_failures(monkeypatch):
    import freemicro.permissions as permissions

    monkeypatch.setattr(
        permissions, "input_monitoring", lambda: (_ for _ in ()).throw(OSError("x"))
    )
    assert status.permissions_state() == (None, True)


def test_status_cache_round_trips(tmp_path):
    path = tmp_path / "status.json"
    status.write_status({"battery": 71, "version": "1.2.3"}, path)
    data = status.read_status(path)
    assert data["battery"] == 71
    assert data["version"] == "1.2.3"
    assert isinstance(data["updated_at"], float)


def test_unreadable_status_cache_is_not_fatal(tmp_path):
    path = tmp_path / "status.json"
    path.write_text("{ not json", encoding="utf-8")
    assert status.read_status(path) == {}


def test_a_reading_from_a_pad_that_is_gone_is_not_shown_as_current(tmp_path):
    # Written while the pad was attached; the pad is now absent (NO_DEVICE).
    status.write_status(
        {"battery": 90, "version": "1.0", "updated_at": time.time()},
        status.status_path(),
    )
    snap = status.snapshot()
    assert snap.connected is False
    assert snap.battery is None
    assert snap.firmware == ""


def test_probe_never_opens_the_device_while_someone_else_owns_it(monkeypatch):
    monkeypatch.setattr(status, "pad_owner", lambda: "the background daemon")

    def fail(*_args, **_kwargs):
        raise AssertionError("the menu bar must not open a pad someone else has")

    import freemicro.device as device

    monkeypatch.setattr(device, "shared_device", fail)
    assert status.probe_device_status() == {}


# ---------------------------------------------------------------------------
# The poller
# ---------------------------------------------------------------------------

def test_poller_has_a_usable_snapshot_the_instant_it_starts(monkeypatch):
    monkeypatch.setattr(status, "probe_device_status", lambda *a, **k: {})
    poller = status.Poller(interval=0.01)
    poller.start()
    try:
        assert isinstance(poller.current, Snapshot)
        # And the menu builds from it without any further waiting.
        assert menu_item(build_menu(poller.current), "quit") is not None
    finally:
        poller.stop()


def test_poller_does_not_hit_the_device_on_every_tick(monkeypatch):
    hits = []

    def probe(*_args, **_kwargs):
        hits.append(1)
        return {}

    monkeypatch.setattr(status, "probe_device_status", probe)
    poller = status.Poller(device_interval=10_000)
    poller.refresh()
    poller.refresh()
    poller.refresh()
    assert len(hits) == 1


def test_poller_thread_survives_a_probe_that_throws(monkeypatch):
    """A pad dropping mid-poll is routine; it must not end the poll loop."""

    def boom(*_args, **_kwargs):
        raise RuntimeError("pad vanished")

    monkeypatch.setattr(status, "probe_device_status", boom)
    poller = status.Poller(interval=0.01)
    poller.start()
    try:
        time.sleep(0.1)  # several ticks, every one of them exploding
        assert poller._thread is not None and poller._thread.is_alive()
        assert isinstance(poller.current, Snapshot)
    finally:
        poller.stop()


# ---------------------------------------------------------------------------
# One menu bar item, not two
# ---------------------------------------------------------------------------

def test_a_second_menu_bar_item_refuses_to_start(tmp_path):
    from freemicro.menubar.app import SingleInstance

    path = tmp_path / "menubar.lock"
    first = SingleInstance(path)
    assert first.acquire() is True
    second = SingleInstance(path)
    assert second.acquire() is False
    first.release()
    assert second.acquire() is True
    second.release()


def test_the_menu_bar_never_takes_the_pad_lock(tmp_path):
    """It uses its own lock file, so no other command thinks the pad is busy."""
    from freemicro import daemon
    from freemicro.menubar.app import SingleInstance

    instance = SingleInstance()
    assert instance.path != daemon.lock_path()
    instance.release()


# ---------------------------------------------------------------------------
# The LED kill switch
# ---------------------------------------------------------------------------

def test_toggling_led_control_writes_the_same_key_the_cli_uses(tmp_path):
    target = tmp_path / "keymap.json"
    status.set_lighting_enabled(True, target)
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data["lighting"]["enabled"] is True

    status.set_lighting_enabled(False, target)
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data["lighting"]["enabled"] is False


def test_toggling_led_control_creates_a_config_if_there_is_none(tmp_path):
    target = tmp_path / "nested" / "keymap.json"
    status.set_lighting_enabled(True, target)
    assert target.exists()
    # And what it wrote still parses as a real pad config.
    from freemicro import padconfig

    assert padconfig.load(target).lighting.enabled is True


def test_toggling_preserves_the_rest_of_the_config(tmp_path):
    target = tmp_path / "keymap.json"
    status.set_lighting_enabled(True, target)
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data["bindings"]  # the starter bindings survived the edit


# ---------------------------------------------------------------------------
# Knowing what is running, and whether it is current
# ---------------------------------------------------------------------------

def test_the_bridge_probe_never_invents_warnings_it_cannot_support(monkeypatch):
    from freemicro import staleness

    def boom(*args, **kwargs):
        raise OSError("no process table today")

    monkeypatch.setattr(staleness, "report", boom)
    listening, notes = status.bridge_state()
    # Ignorance is silence: a probe failure must not paint a warning the user
    # cannot act on.
    assert listening is True and notes == ()


def test_the_menu_bar_notices_its_own_drift(monkeypatch):
    from freemicro import staleness

    monkeypatch.setattr(
        staleness, "report",
        lambda *a, **k: staleness.Report(listener=staleness.PadListener("run", 1)),
    )
    monkeypatch.setattr(
        staleness, "package_mtime", lambda *a, **k: time.time() + 3600
    )
    _listening, notes = status.bridge_state()
    # A menu bar from before an update once rejected a perfectly valid config.
    # The one process that could have said so was the one not looking.
    assert notes[0].action == "restart_menubar"
    assert "out of date" in notes[0].summary


def test_a_stale_daemon_gets_a_restart_button_a_terminal_run_does_not(monkeypatch):
    from freemicro import staleness

    def report(*args, **kwargs):
        return staleness.Report(
            listener=staleness.PadListener("daemon", 5),
            stale=(
                staleness.Staleness(
                    staleness.ProcessInfo(5, staleness.ROLE_DAEMON, 0.0),
                    code_stale=True,
                ),
                staleness.Staleness(
                    staleness.ProcessInfo(6, staleness.ROLE_RUN, 0.0),
                    code_stale=True,
                ),
            ),
        )

    monkeypatch.setattr(staleness, "report", report)
    monkeypatch.setattr(staleness, "self_staleness", lambda *a, **k: None)
    _listening, notes = status.bridge_state()
    assert [note.action for note in notes] == ["restart_daemon", ""]


def test_the_snapshot_carries_liveness_through_to_the_menu(monkeypatch):
    note = model.StaleNote(summary="Out of date: the background daemon")
    monkeypatch.setattr(status, "bridge_state", lambda: (False, (note,)))
    snap = status.snapshot(cached={}, chatgpt=False)
    assert snap.pad_listening is False and snap.stale == (note,)
    assert menu_item(build_menu(snap), "warn.inert") is not None


def test_the_expensive_probes_share_the_slow_clock(monkeypatch):
    """A menu bar that shells out every two seconds is one people uninstall."""
    calls = []
    monkeypatch.setattr(status, "bridge_state", lambda: (calls.append(1), (True, ()))[1])
    monkeypatch.setattr(status, "chatgpt_running", lambda: False)
    monkeypatch.setattr(status, "probe_device_status", lambda *a, **k: {})
    poller = status.Poller(process_interval=3600.0, device_interval=3600.0)
    poller.refresh()
    poller.refresh()
    poller.refresh()
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# Doctor
# ---------------------------------------------------------------------------

def test_doctor_returns_structured_results_not_a_transcript():
    results = checks.run_checks()
    assert results
    assert all(isinstance(check, checks.Check) for check in results)
    assert all(check.label for check in results)


def test_doctor_renders_readably():
    results = [
        checks.Check("Pad config loads", True, "built-in default"),
        checks.Check("Codex Micro found", False, "", "Plug in the cable."),
        checks.Check("LED control is off", None, "The shipped default."),
    ]
    text = checks.report(results)
    assert "✓  Pad config loads" in text
    assert "✕  Codex Micro found" in text
    assert "→ Plug in the cable." in text
    assert checks.summary(results) == "1 check failed."


def test_doctor_summary_counts_only_real_failures():
    results = [checks.Check("a", True), checks.Check("b", None)]
    assert checks.summary(results) == "All 1 checks passed."


def test_doctor_skips_the_write_test_rather_than_fighting_for_the_pad(monkeypatch):
    monkeypatch.setattr(status, "pad_owner", lambda: "`freemicro run` (pid 9)")
    monkeypatch.setattr(
        "freemicro.device.device_transport", lambda: "USB", raising=False
    )
    labels = [check.label for check in checks._device_checks()]
    assert "Pad ownership" in labels
    assert "device.status round trip" not in labels


# ---------------------------------------------------------------------------
# The Cocoa layer, asked only what it can answer without a GUI
# ---------------------------------------------------------------------------

def test_cocoa_reports_availability_without_being_loaded_first():
    # Answering must never require a window server, a session, or AppKit on a
    # non-macOS box - this is what keeps the import of `freemicro.menubar` free.
    assert isinstance(cocoa.is_available(), bool)
    if not cocoa.is_available():
        assert cocoa.unavailable_reason()


def test_importing_the_package_does_not_pull_in_appkit():
    """``import freemicro.menubar`` must stay free on Linux, in CI, under launchd.

    The subprocess inherits this interpreter's ``sys.path`` as ``PYTHONPATH``.
    Without it the child gets a bare interpreter, ``import freemicro`` raises
    ``ModuleNotFoundError``, and the test fails for a reason that has nothing to
    do with AppKit - the real assertion below is never reached. A source
    checkout run as ``PYTHONPATH=src pytest`` hits that every time, so the
    failure looks like a menu bar regression to anyone who has just cloned us.

    Handing the path down deliberately tests *this* checkout rather than
    whatever ``freemicro`` happens to be installed for ``sys.executable``, which
    is the version we actually want to make a claim about.
    """
    import os
    import subprocess
    import sys

    script = (
        "import sys, freemicro.menubar;"
        "assert callable(freemicro.menubar.main);"
        "assert 'freemicro.menubar.app' not in sys.modules, 'app was imported';"
        "print('ok')"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(p for p in sys.path if p)
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    assert "ok" in proc.stdout
