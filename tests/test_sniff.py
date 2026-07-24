"""Tests for sniff-and-replay: capture parsing, learning, and profile replay."""

from __future__ import annotations

import json

from freemicro import capture
from freemicro.protocol import CODEX_TO_AGENT, SniffedProtocol
from freemicro.renderers import REGISTRY
from freemicro.state.engine import AgentState


# -- capture parsing ----------------------------------------------------

def test_parse_hex_lines(tmp_path):
    f = tmp_path / "cap.txt"
    f.write_text("0b 00 ff 00 00\n# a comment\n0b:00:00:ff:00\n0b0000 00ff\n")
    frames = capture.parse_capture(f)
    assert frames[0] == [0x0B, 0x00, 0xFF, 0x00, 0x00]
    assert frames[1] == [0x0B, 0x00, 0x00, 0xFF, 0x00]
    assert len(frames) == 3


def test_parse_tshark_json(tmp_path):
    packets = [
        {"_source": {"layers": {"usbhid.data": "0b:00:ff:00:00"}}},
        {"_source": {"layers": {"usb.capdata": "0b:00:00:ff:00"}}},
        {"_source": {"layers": {}}},  # no payload -> skipped
    ]
    f = tmp_path / "cap.json"
    f.write_text(json.dumps(packets))
    frames = capture.parse_capture(f)
    assert frames == [[0x0B, 0x00, 0xFF, 0x00, 0x00], [0x0B, 0x00, 0x00, 0xFF, 0x00]]


def test_empty_capture(tmp_path):
    f = tmp_path / "empty.txt"
    f.write_text("")
    assert capture.parse_capture(f) == []


# -- learning -----------------------------------------------------------

def _write(tmp_path, name, frame):
    f = tmp_path / name
    f.write_text(" ".join(f"{b:02x}" for b in frame))
    return f


def test_learn_maps_codex_states_to_agent_states(tmp_path):
    caps = {
        "thinking": _write(tmp_path, "t.txt", [0x0B, 0, 0, 0, 255]),
        "awaiting": _write(tmp_path, "a.txt", [0x0B, 0, 255, 149, 0]),
        "done": _write(tmp_path, "d.txt", [0x0B, 0, 52, 199, 89]),
        "error": _write(tmp_path, "e.txt", [0x0B, 0, 255, 59, 48]),
        "idle": _write(tmp_path, "i.txt", [0x0B, 0, 40, 40, 48]),
    }
    profile = capture.learn(caps, vid_pid="574c:1df9")
    assert profile.vid_pid == "574c:1df9"
    # thinking -> working
    assert AgentState.WORKING.value in profile.frames_by_state
    assert AgentState.WAITING.value in profile.frames_by_state
    assert AgentState.DONE.value in profile.frames_by_state
    assert AgentState.ERROR.value in profile.frames_by_state


def test_learn_merges_thinking_and_running_into_working(tmp_path):
    caps = {
        "thinking": _write(tmp_path, "t.txt", [0x0B, 0, 1, 1, 1]),
        "running": _write(tmp_path, "r.txt", [0x0B, 0, 2, 2, 2]),
    }
    profile = capture.learn(caps)
    working = profile.frames_by_state[AgentState.WORKING.value]
    # Both distinct frames kept under the single "working" state.
    assert [0x0B, 0, 1, 1, 1] in working
    assert [0x0B, 0, 2, 2, 2] in working


def test_learn_skips_unknown_states(tmp_path):
    caps = {"bogus": _write(tmp_path, "b.txt", [1, 2, 3])}
    profile = capture.learn(caps)
    assert profile.frames_by_state == {}


def test_infer_layout_from_solid_colors(tmp_path):
    caps = {"thinking": _write(tmp_path, "t.txt", [0x0B, 0, 0, 0, 255])}
    colors = {
        "red": _write(tmp_path, "red.txt", [0x0B, 0, 255, 0, 0]),
        "green": _write(tmp_path, "grn.txt", [0x0B, 0, 0, 255, 0]),
        "blue": _write(tmp_path, "blu.txt", [0x0B, 0, 0, 0, 255]),
    }
    profile = capture.learn(caps, color_captures=colors)
    lay = profile.layout
    assert (lay.r_offset, lay.g_offset, lay.b_offset) == (2, 3, 4)
    assert lay.command == 0x0B and lay.command_offset == 0


# -- profile round-trip and replay -------------------------------------

def test_profile_roundtrip(tmp_path):
    profile = SniffedProtocol(
        vid_pid="574c:1df9",
        report_length=5,
        frames_by_state={"done": [[0x0B, 0, 52, 199, 89]]},
    )
    path = tmp_path / "protocol.json"
    profile.save(path)
    loaded = SniffedProtocol.load(path)
    assert loaded.frames_for(AgentState.DONE) == [[0x0B, 0, 52, 199, 89]]


def test_frames_for_unknown_state_is_empty():
    profile = SniffedProtocol(frames_by_state={"done": [[1, 2, 3]]})
    assert profile.frames_for(AgentState.WORKING) == []


def test_parametric_synthesis_when_no_literal_frame():
    from freemicro.protocol import ByteLayout

    profile = SniffedProtocol(
        report_length=6,
        frames_by_state={},
        layout=ByteLayout(report_id=0, command=0x0B, command_offset=1,
                          r_offset=2, g_offset=3, b_offset=4),
    )
    frame = profile.frames_for(AgentState.ERROR)
    assert frame  # synthesized from the palette
    r, g, b = 255, 59, 48  # PALETTE[ERROR]
    assert frame[0][2:5] == [r, g, b]


def test_codex_state_map_covers_all_agent_states():
    mapped = set(CODEX_TO_AGENT.values())
    assert mapped == set(AgentState)


# -- renderer registration ---------------------------------------------

def test_sniffed_renderer_registered_and_dormant_without_profile(monkeypatch, tmp_path):
    monkeypatch.setenv("FREEMICRO_HOME", str(tmp_path))
    assert "micro-sniffed" in REGISTRY
    r = REGISTRY["micro-sniffed"]()
    # No profile on disk -> not available, never raises.
    assert r.available() is False
    r.close()


def test_sniffed_renderer_is_highest_priority():
    assert REGISTRY["micro-sniffed"].priority > REGISTRY["micro-qmk"].priority
