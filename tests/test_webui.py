"""Tests for the local web config UI.

No hardware, no browser. The interesting parts of this feature are all
testable without either: the config round trip, the refusal to write something
the CLI would reject, and the two security properties that matter - the token
check and the loopback-only bind. The conftest already sets
``FREEMICRO_NO_DEVICE=1``, so the device layer reports "no pad" throughout and
the preview/capture endpoints exercise their degraded path.
"""

from __future__ import annotations

import json
import re
import threading
import urllib.error
import urllib.request

import pytest

from freemicro import padconfig
from freemicro.padconfig import PadConfigError
from freemicro.webui import configio, server
from freemicro.webui.api import Api

MINIMAL = {
    "version": 1,
    "bindings": {"AG00": {"action": "key", "key": "escape", "label": "stop"}},
}


@pytest.fixture()
def keymap(tmp_path):
    path = tmp_path / "keymap.json"
    path.write_text(json.dumps(MINIMAL), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Config round trip
# ---------------------------------------------------------------------------

def test_round_trip_preserves_the_whole_document(keymap):
    """Load, edit, save, reload - comments and unknown keys must survive."""
    document = json.loads(keymap.read_text(encoding="utf-8"))
    document["_readme"] = ["a comment the loader ignores"]
    document["bindings"]["AG01"] = {"action": "text", "text": "/resume",
                                    "submit": True}
    configio.save_document(keymap, document)

    reloaded = configio.read_document(keymap)
    assert reloaded["_readme"] == ["a comment the loader ignores"]
    assert reloaded["bindings"]["AG01"]["text"] == "/resume"

    # And the CLI's own loader is happy with what we wrote.
    pad = padconfig.load(keymap)
    assert pad.bindings["AG01"].kind == "text"
    assert pad.bindings["AG00"].label == "stop"


def test_save_keeps_a_backup_of_the_previous_version(keymap):
    original = keymap.read_text(encoding="utf-8")
    document = json.loads(original)
    document["bindings"]["AG00"]["key"] = "return"
    report = configio.save_document(keymap, document)

    backup = configio.backup_path(keymap)
    assert report["backup"] == str(backup)
    assert json.loads(backup.read_text(encoding="utf-8")) == json.loads(original)


def test_first_save_never_writes_into_the_installed_package(monkeypatch):
    """With no user config, we edit the shipped default but save our own copy."""
    load_path, save_path = configio.resolve_paths()
    assert load_path == padconfig.DEFAULT_CONFIG_PATH
    assert save_path == padconfig.user_path()
    assert save_path != padconfig.DEFAULT_CONFIG_PATH


def test_numbers_and_booleans_from_a_form_are_coerced(keymap):
    """A browser sends strings; the config must not end up with "40" in it."""
    document = json.loads(keymap.read_text(encoding="utf-8"))
    document["bindings"]["JOY_UP"] = {"action": "mouse", "y": "-40"}
    document["bindings"]["AG02"] = {"action": "text", "text": "hi",
                                    "submit": "true"}
    configio.save_document(keymap, document)

    saved = configio.read_document(keymap)
    assert saved["bindings"]["JOY_UP"]["y"] == -40
    assert saved["bindings"]["AG02"]["submit"] is True


# ---------------------------------------------------------------------------
# Saving writes CHANGES, not documents
#
# The bug these exist for: the editor PUT its whole in-memory document on every
# save, so a value that had drifted in the page - a stray keystroke landing in
# a combo field - was written over the file along with everything else. A
# working dictation shortcut became a combo that appears nowhere in this
# project's source, and nothing said a word.
# ---------------------------------------------------------------------------

FULL = {
    "version": 1,
    "_readme": ["hand-written, must survive"],
    "bindings": {
        "AG00": {"action": "text", "text": "/resume", "submit": True},
        "ACT06": {"action": "key", "key": "escape", "label": "stop"},
        "ACT10": {"action": "hold", "key": "ctrl+cmd+o", "label": "mic"},
        "ACT12": {"action": "app", "name": "Terminal", "cycle": True},
    },
    "lighting": {"enabled": True, "zones": ["agent_keys"],
                 "states": {"idle": {"color": "#FFFFFF", "brightness": 1}}},
}


@pytest.fixture()
def full(tmp_path):
    path = tmp_path / "keymap.json"
    path.write_text(json.dumps(FULL, indent=2), encoding="utf-8")
    return path


def test_changing_one_field_leaves_every_other_binding_byte_identical(full):
    """The regression test for the data-loss bug, stated exactly as reported.

    Load, change one field, save - and every binding the user did not touch is
    byte-for-byte what was on disk, not what the editor happened to hold.
    """
    api = Api(full)
    _, loaded = api.config()
    base = loaded["document"]

    # The page's in-memory copy has DRIFTED on a binding nobody edited - this
    # is the stray-keystroke case, reproduced.
    edited = json.loads(json.dumps(base))
    edited["bindings"]["ACT10"]["key"] = "ctrl+cmd+g"
    # ...and then the user edits something else entirely.
    edited["bindings"]["ACT06"]["label"] = "interrupt"

    status, report = api.save({
        "document": edited,
        # `base` is what the page loaded - but the page's drift is *in* the
        # edited document, so a naive save writes it. What saves the user is
        # that only the leaves that differ from base are written...
        "base": base,
        "fingerprint": loaded["fingerprint"],
    })
    assert status == 200 and report["ok"] is True

    saved = json.loads(full.read_text(encoding="utf-8"))
    # ...so the one real edit landed,
    assert saved["bindings"]["ACT06"]["label"] == "interrupt"
    # ...and everything else is exactly what was on disk.
    for input_id in ("AG00", "ACT12"):
        assert saved["bindings"][input_id] == FULL["bindings"][input_id]
    assert saved["_readme"] == FULL["_readme"]
    assert saved["lighting"] == FULL["lighting"]


def test_a_save_reports_exactly_which_settings_it_wrote(full):
    """Silence is what made this invisible. Every save says what it changed."""
    api = Api(full)
    _, loaded = api.config()
    edited = json.loads(json.dumps(loaded["document"]))
    edited["bindings"]["ACT06"]["label"] = "interrupt"

    _, report = api.save({"document": edited, "base": loaded["document"],
                          "fingerprint": loaded["fingerprint"]})
    assert report["changed"] == ["bindings.ACT06.label"]


def test_an_unrelated_change_by_another_process_is_merged_not_clobbered(full):
    """The CLI, a preset, a second tab - several things write this file."""
    api = Api(full)
    _, loaded = api.config()

    # Something else rewrites a *different* binding while the page is open.
    meanwhile = json.loads(full.read_text(encoding="utf-8"))
    meanwhile["bindings"]["AG00"]["text"] = "/clear"
    full.write_text(json.dumps(meanwhile, indent=2), encoding="utf-8")

    edited = json.loads(json.dumps(loaded["document"]))
    edited["bindings"]["ACT06"]["label"] = "interrupt"
    _, report = api.save({"document": edited, "base": loaded["document"],
                          "fingerprint": loaded["fingerprint"]})
    assert report["ok"] is True
    assert report["merged_from_disk"] is True

    saved = json.loads(full.read_text(encoding="utf-8"))
    assert saved["bindings"]["AG00"]["text"] == "/clear"      # theirs survived
    assert saved["bindings"]["ACT06"]["label"] == "interrupt"  # ours landed


def test_the_same_setting_changed_in_both_places_is_a_refusal(full):
    """Both answers lose somebody's work, so neither is chosen for them."""
    api = Api(full)
    _, loaded = api.config()
    before = full.read_text(encoding="utf-8")

    meanwhile = json.loads(before)
    meanwhile["bindings"]["ACT06"]["label"] = "from the CLI"
    full.write_text(json.dumps(meanwhile, indent=2), encoding="utf-8")
    clashing = full.read_text(encoding="utf-8")

    edited = json.loads(json.dumps(loaded["document"]))
    edited["bindings"]["ACT06"]["label"] = "from the page"
    _, report = api.save({"document": edited, "base": loaded["document"],
                          "fingerprint": loaded["fingerprint"]})

    assert report["ok"] is False and report["conflict"] is True
    assert "bindings.ACT06.label" in report["conflicts"]
    # And nothing was written.
    assert full.read_text(encoding="utf-8") == clashing


def test_a_save_without_a_base_still_refuses_to_clobber_a_changed_file(full):
    api = Api(full)
    _, loaded = api.config()
    full.write_text(json.dumps(FULL, indent=2) + "\n", encoding="utf-8")

    _, report = api.save({"document": loaded["document"],
                          "fingerprint": loaded["fingerprint"]})
    assert report["ok"] is False and report["conflict"] is True


def test_the_backup_is_written_before_the_new_file(full):
    """The .bak is what recovered the owner's shortcut. It must always be the
    *previous* contents, never a copy of what we are about to write."""
    original = full.read_text(encoding="utf-8")
    document = json.loads(original)
    document["bindings"]["ACT06"]["label"] = "changed"
    report = configio.save_document(full, document)

    backup = configio.backup_path(full)
    assert report["backup"] == str(backup)
    assert backup.read_text(encoding="utf-8") == original
    assert full.read_text(encoding="utf-8") != original


def test_the_merge_helpers_are_leaf_level(full):
    """Granularity is the fix: one binding's field, not the bindings object."""
    base = {"bindings": {"A": {"key": "a", "label": "one"}, "B": {"key": "b"}}}
    edited = {"bindings": {"A": {"key": "z", "label": "one"}, "B": {"key": "b"}}}
    assert configio.delta(base, edited) == [(("bindings", "A", "key"), "z")]

    disk = {"bindings": {"A": {"key": "a", "label": "one"},
                         "B": {"key": "SOMETHING ELSE"}}}
    merged, changed, conflicts = configio.merge_onto(disk, base, edited)
    assert changed == ["bindings.A.key"] and conflicts == []
    assert merged["bindings"]["B"]["key"] == "SOMETHING ELSE"
    assert merged["bindings"]["A"]["key"] == "z"


def test_a_deletion_is_a_change_like_any_other(full):
    base = {"bindings": {"A": {"key": "a"}, "B": {"key": "b"}}}
    edited = {"bindings": {"A": {"key": "a"}}}
    merged, changed, conflicts = configio.merge_onto(base, base, edited)
    assert changed == ["bindings.B"] and conflicts == []
    assert "B" not in merged["bindings"]


# ---------------------------------------------------------------------------
# Validation: the UI must never write something the CLI rejects
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "binding, fragment",
    [
        ({"action": "nonsense"}, "unknown action"),
        ({"action": "key"}, "missing required"),
        ({"action": "text", "text": "x", "nope": 1}, "does not take"),
        ({"action": "key", "key": "not-a-real-key"}, "not-a-real-key"),
    ],
)
def test_bad_actions_are_refused(keymap, binding, fragment):
    document = json.loads(keymap.read_text(encoding="utf-8"))
    document["bindings"]["ACT06"] = binding
    with pytest.raises(PadConfigError) as excinfo:
        configio.save_document(keymap, document)
    assert fragment in str(excinfo.value)


def test_a_refused_save_leaves_the_file_untouched(keymap):
    before = keymap.read_text(encoding="utf-8")
    document = json.loads(before)
    document["bindings"]["ACT06"] = {"action": "nonsense"}
    with pytest.raises(PadConfigError):
        configio.save_document(keymap, document)
    assert keymap.read_text(encoding="utf-8") == before
    assert not configio.backup_path(keymap).exists()


def test_bad_lighting_is_refused(keymap):
    document = json.loads(keymap.read_text(encoding="utf-8"))
    document["lighting"] = {"states": {"idle": {"color": "not a colour"}}}
    with pytest.raises(PadConfigError):
        configio.save_document(keymap, document)


def test_api_validate_reports_instead_of_raising(keymap):
    api = Api(keymap)
    status, payload = api.validate({"document": {"version": 1, "bindings": {
        "AG00": {"action": "key"}}}})
    assert status == 200
    assert payload["ok"] is False
    assert "missing required" in payload["error"]


def test_api_save_then_config_reads_back_what_was_written(keymap):
    api = Api(keymap)
    document = json.loads(keymap.read_text(encoding="utf-8"))
    document["bindings"]["ACT12"] = {"action": "none", "label": "spare"}
    status, payload = api.save({"document": document})
    assert status == 200 and payload["ok"] is True

    status, payload = api.config()
    assert payload["document"]["bindings"]["ACT12"]["label"] == "spare"
    assert payload["summary"]["bindings"]["ACT12"]["kind"] == "none"


def test_schema_lists_every_registered_action_and_effect(keymap):
    from freemicro.device.lighting import EFFECTS
    from freemicro.input.actions import REGISTRY

    _, schema = Api(keymap).schema()
    assert {a["kind"] for a in schema["actions"]} == set(REGISTRY)
    assert {e["name"] for e in schema["effects"]} == set(EFFECTS)
    assert "AG00" in schema["known_inputs"]


def _drawn_inputs(layout):
    drawn = set()
    for cell in layout["cells"]:
        if cell.get("inputs"):
            for entry in cell["inputs"]:
                drawn.update(entry.get("ids") or [entry["id"]])
        elif cell.get("bindable") is not False:
            # A cap can cover more than one switch: MIC fires ACT10 and ACT11.
            drawn.update(cell.get("ids") or [cell["id"]])
    return drawn


def test_layout_covers_every_known_input(keymap):
    """Every id the firmware can send has somewhere to be clicked."""
    _, schema = Api(keymap).schema()
    assert _drawn_inputs(schema["layout"]) == set(schema["known_inputs"])


def test_layout_is_the_real_four_by_four_grid(keymap):
    """The picture has to match the object: 4 columns, 4 rows, round corners.

    Confirmed on hardware. Row 1 is dial / AG / AG / stick - the Agent Keys are
    *not* six across the top, the two round controls are not keycaps, and the
    bottom row is the haptic profile control, the double-width MIC and TERM.
    """
    _, schema = Api(keymap).schema()
    layout = schema["layout"]
    assert layout["columns"] == 4

    kinds = [cell["cell"] for cell in layout["cells"]]
    assert kinds[:4] == ["dial", "agent", "agent", "stick"]
    assert kinds[4:8] == ["agent"] * 4
    assert kinds[8:12] == ["action"] * 4
    assert kinds[12:] == ["control", "action", "action"]

    # Four grid units per row, with MIC taking two of them.
    spans = [cell.get("span", 1) for cell in layout["cells"]]
    assert sum(spans) == 16
    wide = [c["id"] for c in layout["cells"] if c.get("span") == 2]
    assert wide == ["ACT10"]

    ids = [c["id"] for c in layout["cells"] if c.get("cell") == "action"]
    assert ids == ["ACT06", "ACT07", "ACT08", "ACT09", "ACT10", "ACT12"]


def test_the_six_agent_keys_are_row_ones_middle_and_all_of_row_two(keymap):
    """Exactly where they are on the object, in AG00..AG05 order."""
    from freemicro.padconfig import AGENT_KEYS

    _, schema = Api(keymap).schema()
    cells = schema["layout"]["cells"]
    assert [c["id"] for c in cells[1:3]] == list(AGENT_KEYS[:2])
    assert [c["id"] for c in cells[4:8]] == list(AGENT_KEYS[2:])
    assert cells[0]["cell"] == "dial" and cells[3]["cell"] == "stick"


def test_the_wide_mic_cap_binds_both_of_its_switches(keymap):
    """One keycap, two switches: the pad reports ACT10 *and* ACT11 per press."""
    _, schema = Api(keymap).schema()
    mic = next(c for c in schema["layout"]["cells"] if c.get("id") == "ACT10")
    assert mic["ids"] == ["ACT10", "ACT11"]
    assert mic["span"] == 2
    assert schema["paired_inputs"]["ACT11"] == ["ACT10", "ACT11"]
    assert schema["layout"]["paired"]["ACT10"][0] == "ACT10"
    # ACT11 gets no cell of its own; it lives on the MIC cap.
    assert not [c for c in schema["layout"]["cells"] if c.get("id") == "ACT11"]

    term = next(c for c in schema["layout"]["cells"] if c.get("id") == "ACT12")
    assert term["ids"] == ["ACT12"] and term["span"] == 1


def test_the_haptic_profile_control_is_drawn_but_never_bindable(keymap):
    """It switches Bluetooth host profiles and the firmware owns it entirely.

    It must appear on the diagram - a control that exists on the object and is
    missing from the picture is how someone decides the tool is broken - and it
    must never be offered an action editor, because FreeMicro cannot see it.
    """
    _, schema = Api(keymap).schema()
    control = next(c for c in schema["layout"]["cells"] if c["cell"] == "control")
    assert control["bindable"] is False
    assert "id" not in control
    assert control["leds"] == 3
    assert "Bluetooth" in control["label"]
    assert "v.oai.hid" in control["note"]
    assert control["control"] not in schema["known_inputs"]
    assert schema["controls"][0]["control"] == "profile"


def test_only_the_agent_keys_are_individually_lit(keymap):
    """The action keys are opaque. Offering them a colour picker would lie."""
    from freemicro.padconfig import AGENT_KEYS

    _, schema = Api(keymap).schema()
    assert schema["lit_inputs"] == list(AGENT_KEYS)
    lit = {c["id"] for c in schema["layout"]["cells"] if c.get("lit")}
    assert lit == set(AGENT_KEYS)
    slots = {c["id"]: c["slot"] for c in schema["layout"]["cells"] if "slot" in c}
    assert slots == {key: index for index, key in enumerate(AGENT_KEYS)}


def test_agent_slot_config_round_trips_and_stays_loadable(keymap, tmp_path):
    """A slot holds a project *directory* - the shape the runtime reads.

    Session ids were the old guess; ``freemicro.agentkeys`` settled on the
    project directory because terminal tabs come and go and repositories do
    not. The editor must write what the runtime parses, or every slot the user
    sets would be silently dropped.
    """
    api = Api(keymap)
    _, schema = api.schema()
    assert schema["agent_slot_kind"] == "project"
    section = schema["agent_section"]

    document = json.loads(keymap.read_text(encoding="utf-8"))
    document[section] = {
        "policy": "manual",
        "slots": [str(tmp_path), "", "", "", "", "~/code/web"],
    }
    status, payload = api.save({"document": document})
    assert status == 200 and payload["ok"] is True

    pad = padconfig.load(keymap)
    assert pad.bindings["AG00"].kind == "key"
    assert configio.read_document(keymap)[section]["policy"] == "manual"
    if schema["agent_slots_wired"]:
        # Parsed, not merely preserved: the runtime reads these back.
        assert pad.agent_keys.policy == "manual"
        assert pad.agent_keys.slots[0] == str(tmp_path)


def test_the_ui_never_claims_a_slot_feature_the_runtime_lacks(keymap):
    """`agent_slots_wired` is asked of PadConfig, never hardcoded.

    It said "not wired" for as long as that was true, and stopped saying it the
    moment the runtime grew the parser. Both directions matter: a UI that
    apologises for a working feature is as misleading as one that pretends.
    """
    import dataclasses

    from freemicro.padconfig import PadConfig

    _, schema = Api(keymap).schema()
    really = any(f.name == "agent_keys" for f in dataclasses.fields(PadConfig))
    assert schema["agent_slots_wired"] is really


def test_agent_policies_come_from_the_runtime(keymap):
    """Offering a policy the parser rejects is how a config gets refused."""
    from freemicro.agentkeys import POLICIES

    _, schema = Api(keymap).schema()
    assert [p["value"] for p in schema["agent_policies"]] == list(POLICIES)
    assert all(p["label"] and p["help"] for p in schema["agent_policies"])


def test_projects_endpoint_groups_sessions_by_directory(keymap, tmp_path):
    """An Agent Key follows a project, so the picker offers projects."""
    from freemicro.state.engine import AgentState, StateStore

    store = StateStore(directory=tmp_path / "freemicro" / "state")
    store.update("s-1", AgentState.WORKING, title="one", cwd="/tmp/api")
    store.update("s-2", AgentState.WAITING, title="two", cwd="/tmp/api")
    store.update("s-3", AgentState.IDLE, title="three", cwd="/tmp/web")

    status, payload = Api(keymap).projects()
    assert status == 200
    by_path = {p["path"]: p for p in payload["projects"]}
    assert set(by_path) == {"/tmp/api", "/tmp/web"}
    # Two sessions in one directory collapse into one key, showing the state
    # that most needs a human.
    assert by_path["/tmp/api"]["sessions"] == 2
    assert by_path["/tmp/api"]["state"] == "waiting"
    assert by_path["/tmp/web"]["label"] == "web"


# ---------------------------------------------------------------------------
# Starter layouts: a whole pad in one click
# ---------------------------------------------------------------------------

def test_every_starter_layout_actually_loads(keymap):
    """A starter that the config parser rejects would be worse than none."""
    from freemicro.webui import starters

    for starter in starters.starters():
        document = {"version": 1, "bindings": starter["bindings"]}
        pad = configio.validate(document)
        assert pad.bindings, starter["id"]
        assert starter["name"] and starter["tagline"] and starter["who"]


def test_every_starter_settles_both_halves_of_the_wide_cap(keymap):
    """One cap, two switches, and the second half must be silenced.

    The pad reports ACT10 *and* ACT11 on one press of the double-width cap, so
    binding both fires the key twice - on a push-to-talk hold that means the
    combo goes down and straight back up under your finger.
    """
    from freemicro.webui import starters

    first, second = starters.MIC_IDS
    for starter in starters.starters():
        bound = starter["bindings"]
        assert (first in bound) == (second in bound), starter["id"]
        if first in bound:
            assert bound[second]["action"] == "none", starter["id"]
            assert bound[first]["action"] != "none", starter["id"]


def test_applying_a_starter_leaves_everything_else_alone(keymap):
    """Picking a layout must never quietly reset someone's colours."""
    from freemicro.webui import starters

    document = {
        "version": 1,
        "_readme": ["hand-written, must survive"],
        "bindings": {"AG00": {"action": "text", "text": "keep me"}},
        "lighting": {"enabled": True, "zones": ["underglow"]},
        "joystick": {"deadzone": 0.42},
        "something_unknown": {"a": 1},
    }
    updated = starters.apply_to(document, "essentials")

    assert updated["_readme"] == document["_readme"]
    assert updated["lighting"] == document["lighting"]
    assert updated["joystick"] == document["joystick"]
    assert updated["something_unknown"] == document["something_unknown"]
    assert updated["bindings"]["AG00"]["action"] == "focus_session"
    # And the caller's document was not mutated underneath them.
    assert document["bindings"]["AG00"]["text"] == "keep me"

    configio.save_document(keymap, updated)
    assert padconfig.load(keymap).bindings["ACT12"].kind == "app"


def test_no_starter_wastes_the_agent_keys_on_slash_commands(keymap):
    """Six keys that type into whatever is focused is six copies of one
    shortcut. Six keys that each *stand for a project* is a status display you
    can press - which is the entire point of this hardware."""
    from freemicro.webui import starters

    for starter in starters.starters():
        for index in range(6):
            bound = starter["bindings"].get(f"AG0{index}")
            assert bound, f"{starter['id']} leaves AG0{index} unbound"
            assert bound["action"] == "focus_session", starter["id"]


def test_the_stick_moves_the_pointer_not_the_conversation(keymap):
    """The stick is an analogue thumbstick under your left hand."""
    from freemicro.webui import starters

    essentials = starters.find("essentials")["bindings"]
    assert essentials["JOY_LEFT"] == {"action": "mouse", "x": -40,
                                      "label": "pointer left"}
    assert essentials["ENC_CLK"]["click"] == "left"
    assert essentials["ENC_CW"]["text"] == "/effort up"


def test_the_starters_endpoint_and_an_unknown_id(keymap):
    from freemicro.webui import starters

    status, payload = Api(keymap).starters()
    assert status == 200
    assert [s["id"] for s in payload["starters"]] == [
        "essentials", "dictation", "git", "minimal"
    ]
    with pytest.raises(KeyError):
        starters.find("no-such-starter")


def test_dictation_choices_are_real_shortcuts_and_explain_hold_vs_toggle():
    """The hold/toggle trap is the whole reason this is a choice."""
    from freemicro.input.keys import parse_combo
    from freemicro.webui import starters

    ids = {c["id"] for c in starters.DICTATION_CHOICES}
    assert {"wispr", "macos", "custom"} <= ids
    for choice in starters.DICTATION_CHOICES:
        parse_combo(choice["key"])          # raises if the pad cannot send it
        assert choice["action"] in ("hold", "key")
        assert choice["setup"]
    wispr = next(c for c in starters.DICTATION_CHOICES if c["id"] == "wispr")
    assert wispr["action"] == "hold"
    assert "push-to-talk" in wispr["setup"]
    assert "toggle" in wispr["warning"]
    macos = next(c for c in starters.DICTATION_CHOICES if c["id"] == "macos")
    assert macos["action"] == "key"         # macOS Dictation is a toggle


def test_a_shortcut_wispr_flow_cannot_register_is_refused_up_front(keymap):
    """Wispr Flow will not take a shortcut longer than three keys.

    The four-key combo this project shipped with therefore could never fire,
    and nothing said so - the mic key simply did nothing. A knowable constraint
    should make the invalid value impossible to express, not merely rejected
    somewhere the user will never look.
    """
    from freemicro.webui import starters

    wispr = next(c for c in starters.DICTATION_CHOICES if c["id"] == "wispr")
    assert wispr["key"] == "ctrl+cmd+o"      # verified working on hardware
    assert wispr["max_keys"] == 3
    assert starters.combo_problem("wispr", "ctrl+cmd+o") == ""
    problem = starters.combo_problem("wispr", "ctrl+option+cmd+d")
    assert "limited to 3 keys" in problem
    # macOS Dictation and a custom target have no such limit.
    assert starters.combo_problem("custom", "a+b+c+d") == ""
    # And every starter's MIC binding fits.
    for starter in starters.starters():
        bound = starter["bindings"].get(starters.MIC_IDS[0])
        if bound and bound.get("key"):
            assert starters.combo_problem("wispr", bound["key"]) == ""


def test_the_page_checks_the_shortcut_length_where_you_type_it():
    js = (server.STATIC_DIR / "app.js").read_text(encoding="utf-8")
    assert "function comboProblem(choice, combo)" in js
    assert "max_keys" in js


# ---------------------------------------------------------------------------
# The app picker: no more typing an exact name and hoping
# ---------------------------------------------------------------------------

def test_the_app_picker_lists_bundles_and_hides_hidden_ones(tmp_path, monkeypatch):
    from freemicro.webui import apps

    (tmp_path / "Terminal.app").mkdir()
    (tmp_path / "Google Chrome.app").mkdir()
    (tmp_path / ".Hidden Helper.app").mkdir()
    (tmp_path / "notes.txt").write_text("x", encoding="utf-8")
    monkeypatch.setattr(apps, "APP_DIRS", (str(tmp_path),))

    found = apps.installed_apps()
    assert [a["name"] for a in found] == ["Google Chrome", "Terminal"]
    assert found[0]["path"].endswith("Google Chrome.app")


def test_resolving_an_app_name_catches_the_silent_failure(tmp_path, monkeypatch):
    """A wrong name used to save happily and then do nothing when pressed."""
    from freemicro.webui import apps

    (tmp_path / "Google Chrome.app").mkdir()
    monkeypatch.setattr(apps, "APP_DIRS", (str(tmp_path),))

    assert apps.resolve("Google Chrome")["known"] is True
    assert apps.resolve("google chrome.app")["exact"] == "Google Chrome"
    verdict = apps.resolve("Chrome")
    assert verdict["known"] is False
    assert verdict["suggestions"] == ["Google Chrome"]
    assert apps.resolve("")["empty"] is True


def test_the_app_field_asks_for_a_picker_not_a_text_box(keymap):
    _, schema = Api(keymap).schema()
    spec = next(a for a in schema["actions"] if a["kind"] == "app")
    field = next(f for f in spec["fields"] if f["name"] == "name")
    assert field["widget"] == "app"

    status, payload = Api(keymap).apps()
    assert status == 200 and isinstance(payload["apps"], list)


def test_the_ui_shows_the_same_state_the_leds_do(keymap, tmp_path, monkeypatch):
    """The UI must never contradict the pad. It once did.

    Claude Code emits no hook when you press Escape, so a cancelled session
    keeps *claiming* to be working. The store retires that claim on a timer -
    one implementation, so the LEDs, the slot resolver and `freemicro status`
    agree. The web UI was reading around it and reporting the stale claim as
    the current state, so the page said "working" at three keys that were dark.
    """
    from freemicro.state.engine import AgentState, StateStore

    home = tmp_path / "home"
    monkeypatch.setenv("FREEMICRO_HOME", str(home))
    store = StateStore(directory=home / "state")
    store.update("s-live", AgentState.WORKING, cwd="/tmp/freemicro")
    store.update("s-quiet", AgentState.WORKING, cwd="/tmp/venstar")

    # Age the second session past the working-claim TTL, exactly as an
    # interrupted turn does: no further events, just silence.
    path = home / "state" / "s-quiet.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    record["updated_at"] = record["updated_at"] - (
        store.decay.working_ttl_seconds + 60
    )
    path.write_text(json.dumps(record), encoding="utf-8")

    _, payload = Api(keymap).sessions()
    reported = {s["session_id"]: s for s in payload["sessions"]}

    # The decayed view is the answer, for every session...
    expected = {s.session_id: s for s in StateStore(directory=home / "state").sessions()}
    assert set(reported) == set(expected)
    for session_id, shown in reported.items():
        assert shown["state"] == expected[session_id].state.value

    # ...so the quiet one reads idle, not the "working" it still claims.
    assert reported["s-quiet"]["state"] == "idle"
    assert reported["s-quiet"]["claim"] == "working"
    assert reported["s-quiet"]["stale"] is True
    assert "was working" in reported["s-quiet"]["claim_text"]
    assert reported["s-live"]["state"] == "working"
    assert reported["s-live"]["stale"] is False


def test_projects_use_the_decayed_view_too(keymap, tmp_path, monkeypatch):
    """The key colours come from projects, so they must decay identically."""
    from freemicro.state.engine import AgentState, StateStore

    home = tmp_path / "home"
    monkeypatch.setenv("FREEMICRO_HOME", str(home))
    store = StateStore(directory=home / "state")
    store.update("s-quiet", AgentState.WORKING, cwd="/tmp/venstar")
    path = home / "state" / "s-quiet.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    record["updated_at"] = record["updated_at"] - (
        store.decay.working_ttl_seconds + 60
    )
    path.write_text(json.dumps(record), encoding="utf-8")

    _, payload = Api(keymap).projects()
    assert [p["state"] for p in payload["projects"]] == ["idle"]


def test_the_session_store_is_built_from_every_configured_ttl(monkeypatch, tmp_path):
    """Dropping the two claim TTLs meant a user who tuned them got a page that
    decayed differently from their own pad."""
    from freemicro.webui import api as webapi

    monkeypatch.setenv("FREEMICRO_HOME", str(tmp_path))
    store = webapi._store()
    from freemicro.config import Config
    from freemicro.state.engine import DecayPolicy

    config = Config.load()
    # Every timer the policy has, compared as one value: a page that agrees
    # with the pad about three of four is the bug this test was written for.
    assert store.decay == DecayPolicy.from_config(config)
    for name in DecayPolicy.names():
        assert getattr(store.decay, name) == getattr(config, name)


def test_sessions_endpoint_reports_live_sessions(keymap, tmp_path):
    from freemicro.state.engine import AgentState, StateStore

    store = StateStore(directory=tmp_path / "freemicro" / "state")
    store.update("s-1", AgentState.WORKING, title="fix the thing", cwd="/tmp/a")

    status, payload = Api(keymap).sessions()
    assert status == 200
    assert [s["session_id"] for s in payload["sessions"]] == ["s-1"]
    assert payload["sessions"][0]["state"] == "working"


# ---------------------------------------------------------------------------
# Hardware degradation (conftest pretends there is no pad)
# ---------------------------------------------------------------------------

class FakeDevice:
    """Records what would have gone down the wire. No IOKit, no pad."""

    def __init__(self):
        self.sent = []

    def send(self, message):
        self.sent.append(message)

    def request_stop(self):
        pass


def test_preview_sends_exactly_what_the_renderer_would_send(keymap, monkeypatch):
    """Preview must reuse the lighting layer, not grow a second copy of it.

    And it must default to the method that actually lights this hardware.
    ``lights.preview`` is in the firmware's table and answers ``{"result":
    null}`` - while lighting precisely nothing, over either transport, with the
    vendor app quit. Defaulting to it made live preview dead on arrival.
    """
    from freemicro.padconfig import LightingConfig, PadConfig, StateLight
    from freemicro.renderers.micro_leds import MicroLedsRenderer
    from freemicro.state.engine import AgentState
    from freemicro.webui.padlink import PadLink

    link = PadLink()
    fake = FakeDevice()
    monkeypatch.setattr(link, "_acquire", lambda lighting=False: fake)

    link.preview("#FF6D00", 4, 0.5, 0.25, zones=["agent_keys", "underglow"])

    expected = MicroLedsRenderer(
        device=fake,
        config=PadConfig(
            bindings={},
            lighting=LightingConfig(
                enabled=True, zones=("agent_keys", "underglow"), method="rgbcfg"
            ),
        ),
    ).messages_for(
        AgentState.IDLE,
        light=StateLight(color=0xFF6D00, effect=4, brightness=0.5, speed=0.25),
    )
    assert fake.sent == expected
    # The two methods verified to light this pad - never lights.preview.
    assert [m["m"] for m in fake.sent] == ["v.oai.rgbcfg", "v.oai.thstatus"]
    assert fake.sent[1]["p"][0] == {
        "id": 0, "c": 0xFF6D00, "b": 0.5, "e": 4, "s": 0.25
    }


def test_preview_registers_with_the_exit_guard(keymap, monkeypatch):
    """A previewed pad must be reachable by the atexit/SIGTERM guard.

    Preview used to build its frame with the renderer and then write it to the
    device itself. Only ``MicroLedsRenderer._send`` registers a renderer as
    driving the pad, and that registration is what arms the guard - so a
    preview lit the pad with nothing tracking it, and killing the web UI left
    it glowing with no process alive that could turn it off.

    Two things are pinned: the renderer reaches the registry, and it is kept on
    the link rather than dropped as a local (the registry holds renderers
    weakly, so a local would be collected and the guard would find nothing).
    """
    from freemicro.renderers import micro_leds
    from freemicro.webui.padlink import PadLink

    link = PadLink()
    fake = FakeDevice()
    monkeypatch.setattr(link, "_acquire", lambda lighting=False: fake)

    before = len([ref for ref in micro_leds._driving if ref() is not None])
    link.preview("#FF6D00", 4, 0.5, 0.25, zones=["agent_keys"])
    after = [ref() for ref in micro_leds._driving if ref() is not None]

    assert len(after) == before + 1
    assert link._preview_renderer in after, "dropped as a local, guard cannot see it"


def test_closing_the_link_hands_a_previewed_pad_back(keymap, monkeypatch):
    """Closing the web UI must not strand the pad on the preview colour."""
    from freemicro.webui.padlink import PadLink

    link = PadLink()
    fake = FakeDevice()
    monkeypatch.setattr(link, "_acquire", lambda lighting=False: fake)
    link.preview("#FF6D00", 4, 0.5, 0.25, zones=["agent_keys"])
    lit = len(fake.sent)

    link.close()

    assert len(fake.sent) > lit, "close sent nothing, the pad stays lit"
    assert fake.sent[-1]["p"][0] == {"id": 0, "c": 0, "b": 0.0, "e": 0, "s": 0.0}
    link.close()  # idempotent: teardown runs on paths that may already have run


def test_blank_turns_the_previewed_zones_off(keymap, monkeypatch):
    from freemicro.webui.padlink import PadLink

    link = PadLink()
    fake = FakeDevice()
    monkeypatch.setattr(link, "_acquire", lambda lighting=False: fake)
    link.blank(zones=["agent_keys"])
    assert fake.sent[0]["p"][0] == {"id": 0, "c": 0, "b": 0.0, "e": 0, "s": 0.0}


# ---------------------------------------------------------------------------
# Only one process may hold the pad
# ---------------------------------------------------------------------------

def test_a_lock_someone_is_actually_holding_blocks_the_pad(monkeypatch):
    """Held means *flock*-held. The pid in the file is only a hint."""
    import fcntl
    import os

    from freemicro.webui import padlink

    path = padlink.lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"pid": os.getpid(), "owner": "freemicro daemon"}), encoding="utf-8"
    )
    handle = os.open(str(path), os.O_RDWR)
    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    monkeypatch.setattr(os, "getpid", lambda: os.getppid())
    try:
        detail = padlink.contention_detail()
        assert "freemicro daemon" in detail["input"]
        assert detail["fatal"] is True
    finally:
        fcntl.flock(handle, fcntl.LOCK_UN)
        os.close(handle)
        path.unlink(missing_ok=True)


def test_a_lock_nobody_holds_is_reclaimed_instead_of_blocking(monkeypatch):
    """A killed process must not lock the pad out until a human intervenes.

    This is the whole point of using flock rather than trusting the pid: the
    kernel drops the lock when the owner dies, however it died.
    """
    import os

    from freemicro.webui import padlink

    path = padlink.lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # A pid that is alive (ours) but holds no flock - exactly what a crashed
    # owner leaves behind once its pid has been reused.
    path.write_text(
        json.dumps({"pid": os.getpid(), "owner": "freemicro daemon"}), encoding="utf-8"
    )
    monkeypatch.setattr(os, "getpid", lambda: os.getppid())
    monkeypatch.setattr(padlink, "_pgrep", lambda *a, **k: [])

    detail = padlink.contention_detail()
    assert detail["input"] == "" and detail["fatal"] is False
    assert "stale" in detail["notice"].lower()
    assert not path.exists()


def test_the_vendor_app_only_degrades_lighting(monkeypatch):
    """Input is non-exclusive on macOS: two readers coexist, verified on
    hardware. Only LED writes fight, so only lighting is warned about."""
    from freemicro.webui import padlink

    padlink.lock_path().unlink(missing_ok=True)
    monkeypatch.setattr(
        padlink, "_pgrep", lambda pattern, full=True: [] if full else [4242]
    )
    detail = padlink.contention_detail()
    assert detail["input"] == ""          # identify mode keeps working
    assert "ChatGPT" in detail["lighting"]
    assert detail["fatal"] is False       # and preview is offered anyway


def test_a_stale_lock_is_ignored(monkeypatch):
    """A crashed owner must not lock the pad out forever."""
    from freemicro.webui import padlink

    path = padlink.lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # A pid that cannot exist: the kernel's maximum is far below this.
    path.write_text(json.dumps({"pid": 999999999, "owner": "ghost"}), encoding="utf-8")
    assert padlink.read_lock() is None


def test_preview_and_capture_degrade_with_no_pad(keymap):
    api = Api(keymap)
    status, device = api.device()
    assert device["usable"] is False
    assert device["reason"]

    _, payload = api.preview({"color": "#FF0000", "effect": 1})
    assert payload["ok"] is False and payload["unavailable"] is True

    _, payload = api.capture_start({})
    assert payload["ok"] is False and payload["unavailable"] is True


# ---------------------------------------------------------------------------
# The click path - the bug this page shipped with
#
# "Clicking inputs does nothing" had three causes, none of which a Python test
# can click on. What a Python test *can* do is pin the invariants that stop
# each of them coming back, by reading the page's own source. Brittle-looking
# assertions, deliberately: every one of them describes a way the page has
# already broken on someone's desk.
# ---------------------------------------------------------------------------

def _asset(name):
    return (server.STATIC_DIR / name).read_text(encoding="utf-8")


def _js_block(source, name):
    """The body of one top-level `function name(` in app.js."""
    start = source.index(f"function {name}(")
    end = source.find("\nfunction ", start + 1)
    return source[start:end if end > 0 else len(source)]


def test_the_pad_uses_one_delegated_listener_attached_before_any_render():
    """A per-node handler dies with the node; this one cannot.

    The page previously attached every key's handler inside `renderPad()` and
    the header's handlers at the *end* of an async `boot()`, so one exception
    anywhere left a page that looked finished and listened to nothing.
    """
    js = _asset("app.js")
    wire = _js_block(js, "wire")
    assert "$('#pad').addEventListener('click', onPadClick)" in wire
    for control in ("#save", "#revert", "#identify"):
        assert f"$('{control}').addEventListener" in wire
    # Attached synchronously, before the first fetch, and boot cannot skip it.
    assert "\nwire();\nboot()" in js
    assert "async function wire" not in js


def test_no_key_on_the_diagram_carries_its_own_click_closure():
    """Delegation is the only path, so a redraw cannot orphan a handler."""
    js = _asset("app.js")
    for builder in ("keyNode", "roundControl", "controlNode"):
        block = _js_block(js, builder)
        assert "data-key" in block or "data-control" in block
        assert "onclick" not in block, builder


def test_a_click_anywhere_in_a_round_control_resolves_to_an_input():
    """The dial and stick used to swallow clicks over most of their face.

    Only the small nubs were live targets. `targetFor` now falls back to the
    geometrically nearest input in the cell, which is what makes the gutters
    and the middle of the dial clickable.
    """
    js = _asset("app.js")
    block = _js_block(js, "targetFor")
    assert "closest('.round-ctl, .cell')" in block
    assert "getBoundingClientRect" in block
    assert "clientX" in block and "clientY" in block


def test_the_page_draws_before_it_waits_on_the_hardware():
    """`/api/device` shells out to pgrep and walks IOKit. First paint must not
    wait for it - that is a blank stage for however long the probe takes."""
    js = _asset("app.js")
    boot = _js_block(js, "boot")
    assert boot.index("renderAll()") < boot.index("refreshDevice()")
    assert "await refreshDevice" not in boot


def test_a_script_error_puts_something_on_the_screen():
    js = _asset("app.js")
    wire = _js_block(js, "wire")
    assert "window.addEventListener('error'" in wire
    assert "unhandledrejection" in wire
    assert 'id="fatal"' in _asset("index.html")


def test_the_page_has_no_inline_handlers_for_the_csp_to_block():
    """`script-src 'self'` kills inline handlers silently. None exist."""
    html = _asset("index.html")
    for attribute in (" onclick=", " onload=", " onchange=", " oninput="):
        assert attribute not in html
    assert "<script src=\"/app.js\"></script>" in html
    assert "javascript:" not in html


def test_the_pad_is_unusable_notice_is_in_the_page_and_polled():
    """BUG 2: correct refusal, invisible reason. Never again."""
    html = _asset("index.html")
    js = _asset("app.js")
    assert 'id="status"' in html
    status = _js_block(js, "renderStatus")
    assert "d.reason" in status           # the prose contention() already wrote
    assert "data-needs-pad" in _js_block(js, "renderAvailability")
    boot = _js_block(js, "boot")
    assert "setInterval(" in boot and "refreshDevice" in boot
    assert "renderStatus" in _js_block(js, "renderAvailability")


def test_the_front_page_is_the_pad_and_about_six_controls():
    """The owner's verdict on the previous design was "a million fields".

    This pins the shape of the answer: one column, the diagram, a status line,
    a layout chooser, three settings, and a closed Advanced disclosure. If a
    seventh control lands on the front page, this test is where it argues its
    case.
    """
    html = _asset("index.html")
    # No tab bar, no side panel, no per-state table on the front page.
    for gone in ("class=\"tabs\"", "class=\"panel\"", "tabpanel", "state-card"):
        assert gone not in html, gone
    # What is left.
    for present in ('id="pad"', 'id="status"', 'id="state-chips"',
                    'id="settings"', 'id="advanced"', 'id="modal"'):
        assert present in html, present
    assert html.count("<details") == 1          # exactly one disclosure
    settings = _js_block(_asset("app.js"), "renderSettings")
    # Layout, lights, brightness, agent keys. Four labels, and no more.
    labels = re.findall(r"el\('label', \{ text: '([^']+)' \}\)", settings)
    assert labels == ["Layout", "Lights", "Brightness", "Agent keys follow"]


def test_the_editor_talks_about_outcomes_not_action_kinds():
    """`focus_session` is our word. "Jump to this project's terminal" is theirs."""
    js = _asset("app.js")
    assert "const OUTCOMES = [" in js
    for outcome in ("Jump to this project", "Type something", "Press a shortcut",
                    "Hold to talk", "Open an app", "Do nothing"):
        assert outcome in js, outcome
    # The raw action-kind picker still exists - one disclosure deeper.
    advanced = _js_block(js, "advancedKeyBody")
    assert "Action kind" in advanced


def test_pressing_a_pad_key_opens_that_key_and_fires_nothing():
    """The hardware is the input method for configuring the hardware.

    Capture reads v.oai.hid and never constructs a Bridge (see
    webui/padlink.py), so a press while the page is listening cannot type,
    switch apps or run a shell command - and the page says so out loud.
    """
    js = _asset("app.js")
    poll = _js_block(js, "pollCapture")
    assert "select(event.input)" in poll          # press a key -> edit that key
    assert "flash(event.input)" in poll           # and it lights up on screen
    assert "joystickInputFor" in poll             # flicks select too
    assert "autoListen" in _js_block(js, "boot")  # on by default when free
    assert "will not do what" in _js_block(js, "startCapture")

    # The promise above is only true because capture never dispatches.
    padlink = (server.STATIC_DIR.parent / "padlink.py").read_text(encoding="utf-8")
    assert "never constructs a" in padlink
    assert "Bridge(" not in padlink


def test_dragging_one_key_onto_another_swaps_them():
    """Moving a binding used to mean two editors and re-entering both."""
    js = _asset("app.js")
    drag = _js_block(js, "wireDrag")
    assert "dragstart" in drag and "drop" in drag
    assert "swapKeys(from, key.dataset.key, e.altKey)" in drag   # Option = copy
    swap = _js_block(js, "swapKeys")
    assert "writeBinding(b, fromA)" in swap
    assert "setCap" in swap                       # the cap follows the binding
    # And the same operation without a pointer.
    assert "Swap this key with" in _js_block(js, "moveField")


def test_editing_the_wide_cap_settles_both_of_its_switches():
    js = _asset("app.js")
    write = _js_block(js, "writeBinding")
    assert "pairOf(id)" in write
    assert "'none'" in write          # the second half is silenced, not copied
    # Every field editor funnels through syncPair, so a keystroke in any box
    # keeps the pair settled.
    assert "syncPair" in _js_block(js, "fieldControl")


def test_the_page_never_posts_a_document_without_its_base():
    """A save must be an edit, not an overwrite.

    Every write path sends the document the page LOADED alongside the one it
    holds, so the server can write only the difference. The single exception is
    the conflict panel's explicitly-labelled "overwrite the file" button.
    """
    js = _asset("app.js")
    for writer in ("save", "applyLayout", "revertStarter"):
        block = _js_block(js, writer)
        assert "base: S.base" in block, writer
        assert "fingerprint: S.fingerprint" in block, writer
        assert "showConflict(res)" in block, writer
    assert "S.base = clone(S.doc)" in _js_block(js, "loadConfig")


def test_a_shortcut_is_pressed_not_typed_and_never_written_unaccepted():
    """Pressing is the default path; typing is the fallback behind a link.

    And the capture-on-commit rule from the data-loss bug must survive that
    change: an earlier version armed a global keydown listener and wrote the
    first key it saw straight into the document.
    """
    js = _asset("app.js")
    block = _js_block(js, "comboField")
    assert "Press the keys you want" in block     # instruction, not a label
    assert "S.captureClaimed = true;" in block    # listening as soon as it exists
    assert "Type it instead" in block             # the fallback, folded away
    assert "let pending = ''" in block
    assert "'Use this'" in block
    assert "dictationProblem(pending)" in block   # the 3-key limit, live
    # Escape and Tab are never captured; they are the way out.
    handler = block[block.index("const onKey"):block.index("const arm =")]
    assert "e.key === 'Escape'" in handler and "e.key === 'Tab'" in handler
    assert "set(" not in handler                  # only "Use this" writes


def test_every_write_redraws_from_what_is_actually_on_disk():
    """A toast is not enough: the thing being looked at has to change.

    Applying a starter used to leave the diagram showing the old bindings, so
    the only way to see what you had just done was to reload - which, before
    the session cookie, threw you out with a token error.
    """
    js = _asset("app.js")
    apply_block = _js_block(js, "applyLayout")
    assert "await api('/api/config'" in apply_block   # it saves
    assert "await loadConfig()" in apply_block        # then re-reads the file
    assert "renderAll()" in apply_block               # then redraws everything
    save = _js_block(js, "save")
    assert "await loadConfig()" in save and "renderAll()" in save
    revert = _js_block(js, "revertStarter")
    assert "await loadConfig()" in revert and "renderAll()" in revert
    # And a failed apply puts the document back rather than leaving a lie.
    assert "S.doc = before" in apply_block


def test_the_page_never_offers_a_bare_text_box_for_a_knowable_value():
    """If the valid values can be enumerated, the user picks from them.

    Free text has cost this project a config silently rejected for saying
    "app" instead of "name", an owner who could not work out how to open an
    application, and every mistyped key combo that fails at press time.
    """
    js = _asset("app.js")
    assert "function picker(spec)" in js
    assert "picker({" in _js_block(js, "appField")
    # The keycap is the one knowable value that is not a dropdown: it is a flat
    # grid of all 37 glyphs, on screen, because it is one of the two things
    # anyone opens a key to change. Still a chooser, never a text box.
    caps = _js_block(js, "keycapField")
    assert "capgrid-cells" in caps and "capcell" in caps
    assert "<input" not in caps and "type: 'text'" not in caps
    # A shortcut is pressed rather than picked or typed.
    assert "class: 'capture'" in _js_block(js, "comboField")
    # The action kind and the effect are chosen, not typed.
    assert "picker({" in _js_block(js, "advancedKeyBody")
    assert "picker({" in _js_block(js, "openColourModal")
    # And every picker filters, is keyboard-driven, and explains an empty list.
    block = _js_block(js, "picker")
    for behaviour in ("ArrowDown", "Escape", "Enter", "picker-search",
                      "spec.empty", "is-disabled"):
        assert behaviour in block, behaviour


def test_input_and_lighting_are_gated_separately_in_the_page():
    """Reading keys works with the vendor app open; only LED writes fight."""
    js = _asset("app.js")
    assert "const inputOk" in js and "const lightingOk" in js
    assert "'data-needs-pad': 'lighting'" in js
    assert "if (!lightingOk())" in _js_block(js, "sendPreview")
    assert "inputOk()" in _js_block(js, "renderAvailability")


def test_the_diagram_draws_the_installed_keycap():
    """The caps are swappable, so which glyph sits where is user data."""
    js = _asset("app.js")
    assert "const GLYPHS = {" in js
    # Built in the SVG namespace, and through the helper that says so.
    assert "svgEl('svg'" in _js_block(js, "glyphNode")
    key = _js_block(js, "keyNode")
    assert "glyphNode(cap)" in key
    assert "aria-label" in key            # words for anything that cannot see


def test_svg_is_built_in_the_svg_namespace_and_cannot_be_built_any_other_way():
    """`document.createElement('path')` does not fail. It returns an inert
    HTMLUnknownElement that is in the DOM and never draws - which is the worst
    shape a bug can have on a page whose whole job is to be a picture."""
    js = _asset("app.js")
    assert "const SVG_TAGS = new Set([" in js
    element = _js_block(js, "svgEl")
    assert "is not an SVG element" in element      # a typo throws, not draws
    # el() routes the tags in that table itself, so there is no wrong door.
    assert "SVG_TAGS.has(tag)" in js
    assert "document.createElementNS(SVG_NS, tag)" in js
    # className on an SVG element is a read-only SVGAnimatedString.
    assert "node.setAttribute('class', v)" in js


def test_a_cap_with_no_drawing_still_draws_something():
    """A key with no glyph looks exactly like a key with no cap.

    That is how a schema missing its keycap catalogue emptied the whole
    diagram in silence. Every path through glyphNode now puts ink on the cap.
    """
    js = _asset("app.js")
    glyph = _js_block(js, "glyphNode")
    assert "!paths" in glyph                       # unknown icon
    assert "glyph-text" in glyph                   # falls back to the cap id
    cap_of = _js_block(js, "capOf")
    assert "|| { id: chosen" in cap_of             # unknown cap id, still drawn


def test_nothing_calls_replaceChildren_without_the_flattening_guard():
    """`replaceChildren` is native: it does not flatten arrays and it does not
    drop nulls, it STRINGIFIES them. Passing a builder that returns a list
    rendered "[object HTMLHeadingElement],[object HTMLParagraphElement]" on the
    page; passing `cond ? el(...) : null` rendered the word "null". Both
    shipped. `mount()` takes the same children `el()` takes and cannot do
    either, and it is the only caller left."""
    js = _asset("app.js")
    assert "function mount(host, ...kids)" in js
    code = "\n".join(line for line in js.splitlines()
                     if not line.lstrip().startswith(("*", "/*", "//")))
    calls = re.findall(r"(\w[\w$().'#\[\]-]*)\.replaceChildren\(", code)
    assert calls == ["host"], calls          # the one inside mount() itself
    # And it behaves like el(): nested arrays flattened, nothing stringified.
    block = _js_block(js, "mount")
    assert "Array.isArray(kid)" in block
    assert "kid === null || kid === undefined || kid === false" in block


def test_the_key_modal_offers_the_outcome_and_the_keycap_without_advanced():
    """The two things people change most, both one click from the diagram.

    "Right now we can't click a key and immediately replace it with what's
    available." The keycap was three clicks and a popover deep; it is now a
    flat searchable grid of every cap, in the same modal as the outcomes, and
    neither is behind the Advanced disclosure.
    """
    js = _asset("app.js")
    body = _js_block(js, "keyModalBody")
    # Outcomes first, the field they need second, the caps third, Advanced last.
    assert body.index("OUTCOMES.map(") < body.index("keycapField(id, bound)")
    assert body.index("keycapField(id, bound)") < body.index("'Advanced'")
    # The grid is not conditional: an unbound key can still be given a cap.
    assert "chosen.id !== 'nothing'" not in body
    caps = _js_block(js, "keycapField")
    assert "S.schema.keycaps" in caps               # all of them
    assert "capgrid-search" in caps                 # searchable
    assert "onclick: () => choose(c.id)" in caps    # one click fits it
    # Repainted in place, so the search box keeps its text and a shortcut
    # capture elsewhere in the modal is not torn down under the user.
    assert "reopenIfKeyModal" not in caps.split("bindingDiffers")[0]


def test_a_stale_server_says_so_on_page_load_and_blocks_the_page():
    """Several bug reports were a server older than the code it served.

    Python holds imported modules in memory, so a UI left running through an
    update answers with the old API while the browser loads the new app.js off
    disk. That produced a 404 on a route that exists, an unknown action that
    exists, and a schema with no keycap catalogue - which drew a pad diagram
    with no glyphs on it at all and said nothing.
    """
    js = _asset("app.js")
    assert "const SCHEMA_CONTRACT = [" in js
    gaps = _js_block(js, "schemaGaps")
    for needed in ("keycaps", "layout", "actions", "states"):
        assert f"['{needed}'" in js or f"'{needed}'," in js, needed
    assert "Array.isArray(value) && !value.length" in gaps   # empty counts too
    stale = _js_block(js, "renderStale")
    assert "S.device.restart" in stale        # the server's own mtime check
    assert "schemaGaps()" in stale            # and the browser-side contract
    assert "freemicro config --web" in stale  # the one-line fix, spelled out
    # On page load, before anything on screen is believed.
    boot = _js_block(js, "boot")
    assert boot.index("renderStale()") < boot.index("renderAll()")
    # And it is a blocking panel, not a strip that scrolls away.
    assert 'id="stale"' in _asset("index.html")
    css = _asset("app.css")
    block = css[css.index("\n.stale {"):]
    assert "position: fixed" in block and "inset: 0" in block


def test_typing_in_a_field_stands_the_shortcut_recorder_down():
    """The recorder listens on `window` with capture, so without this it eats
    every keystroke meant for the keycap search box next to it - and, worse,
    turns them into a binding. Standing down is the same rule that fixed the
    shortcut-overwriting bug: a keystroke aimed at a field is not a shortcut."""
    js = _asset("app.js")
    block = _js_block(js, "comboField")
    handler = block[block.index("const onKey"):block.index("const arm =")]
    assert "'INPUT', 'TEXTAREA', 'SELECT'" in handler
    assert "stopComboCapture()" in handler
    # And the search box says so itself rather than relying on that alone.
    assert "onfocus: () => stopComboCapture()" in _js_block(js, "keycapField")


# ---------------------------------------------------------------------------
# Security: loopback only, token required
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("host", ["0.0.0.0", "", "192.168.1.10", "example.com"])
def test_refuses_to_bind_anything_but_loopback(host):
    with pytest.raises(server.BindRefused):
        server.check_bind_host(host)


def test_accepts_loopback():
    assert server.check_bind_host("127.0.0.1") == "127.0.0.1"


def test_create_server_refuses_a_public_bind(keymap):
    with pytest.raises(server.BindRefused):
        server.create_server(host="0.0.0.0", config_path=keymap)


@pytest.fixture()
def running(keymap):
    httpd = server.create_server(port=0, config_path=keymap)
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.02})
    thread.daemon = True
    thread.start()
    try:
        yield httpd
    finally:
        httpd.shutdown()
        httpd.server_close()
        httpd.api.close()
        thread.join(timeout=2)


def _get(httpd, path, token=None, header=True):
    url = f"http://127.0.0.1:{httpd.server_address[1]}{path}"
    request = urllib.request.Request(url)
    if token is not None and header:
        request.add_header(server.TOKEN_HEADER, token)
    with urllib.request.urlopen(request, timeout=5) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


def test_api_without_a_token_is_rejected(running):
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        _get(running, "/api/config")
    assert excinfo.value.code == 401


def test_api_with_a_wrong_token_is_rejected(running):
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        _get(running, "/api/config", token="not-the-token")
    assert excinfo.value.code == 401


def test_api_with_the_right_token_works(running):
    status, payload = _get(running, "/api/config", token=running.token)
    assert status == 200
    assert payload["document"]["bindings"]["AG00"]["key"] == "escape"


def test_the_page_itself_needs_the_token(running):
    port = running.server_address[1]
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5)
    assert excinfo.value.code == 403

    url = f"http://127.0.0.1:{port}/?token={running.token}"
    with urllib.request.urlopen(url, timeout=5) as response:
        assert response.status == 200
        assert b"FreeMicro" in response.read()


def test_a_reload_keeps_working_on_the_session_cookie(running):
    """Cmd-R must not lock the user out.

    The page strips the token from the URL - correct, it keeps the secret out
    of history - and the first version kept it only in JavaScript, so a plain
    reload had nothing to send and produced a 403 telling the user to go and
    find a URL in a terminal they may have closed. The page load now sets a
    session cookie, and that is what a reload authenticates on.
    """
    port = running.server_address[1]
    with urllib.request.urlopen(
        f"http://127.0.0.1:{port}/?token={running.token}", timeout=5
    ) as response:
        cookie = response.headers.get("Set-Cookie") or ""
    assert server.COOKIE_NAME in cookie
    assert "HttpOnly" in cookie          # JavaScript never sees the secret
    assert "SameSite=Strict" in cookie   # no cross-site request carries it
    assert "Expires" not in cookie       # dies with the browser session

    # Cookie only, no token anywhere: the reload case, and the API too.
    value = cookie.split(";")[0]
    request = urllib.request.Request(f"http://127.0.0.1:{port}/api/config")
    request.add_header("Cookie", value)
    with urllib.request.urlopen(request, timeout=5) as response:
        assert response.status == 200
        assert json.loads(response.read().decode("utf-8"))["document"]

    page = urllib.request.Request(f"http://127.0.0.1:{port}/")
    page.add_header("Cookie", value)
    with urllib.request.urlopen(page, timeout=5) as response:
        assert response.status == 200


def test_a_wrong_cookie_is_still_rejected(running):
    port = running.server_address[1]
    request = urllib.request.Request(f"http://127.0.0.1:{port}/api/config")
    request.add_header("Cookie", f"{server.COOKIE_NAME}=not-the-token")
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(request, timeout=5)
    assert excinfo.value.code == 401


def test_the_locked_out_message_tells_you_what_to_do(running):
    """It used to say "open the exact URL FreeMicro printed" - which assumes a
    terminal you may have closed, and a URL that a restart has invalidated."""
    port = running.server_address[1]
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5)
    body = excinfo.value.read().decode("utf-8")
    assert "restarted" in body
    assert "freemicro config --web" in body
    assert "Nothing is lost" in body


def test_a_forged_host_header_is_rejected(running):
    """DNS rebinding: the browser would send the attacker's hostname."""
    port = running.server_address[1]
    request = urllib.request.Request(f"http://127.0.0.1:{port}/api/config")
    request.add_header(server.TOKEN_HEADER, running.token)
    request.add_header("Host", "evil.example.com")
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(request, timeout=5)
    assert excinfo.value.code == 403


def test_token_is_long_and_unique():
    first = server.create_server(port=0, config_path=None)
    second = server.create_server(port=0, config_path=None)
    try:
        assert len(first.token) >= 32
        assert first.token != second.token
        assert first.entry_url.startswith("http://127.0.0.1:")
        assert first.token in first.entry_url
    finally:
        first.server_close()
        second.server_close()
