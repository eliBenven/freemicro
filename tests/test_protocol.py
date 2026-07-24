"""Tests for the pad's wire protocol: framing, decoding and lighting messages.

These are the facts in ``docs/PROTOCOL.md`` turned into assertions. They need no
hardware, which is the whole reason the transport was split from the framing.
"""

from __future__ import annotations

import json

import pytest

from freemicro.device.codex_micro import (
    MAX_CHUNK,
    OPCODE_DATA,
    REPORT_BYTES,
    REPORT_BYTES_BLE,
    REPORT_ID,
    TRANSPORT_BLE,
    TRANSPORT_USB,
    FrameDecoder,
    frame_message,
    notification,
)
from freemicro.device.lighting import (
    AGENT_KEY_COUNT,
    EFFECTS,
    METHOD_PREVIEW,
    METHOD_THREAD_STATUS,
    LightingError,
    all_agent_keys,
    color_to_hex,
    effect_name,
    parse_color,
    parse_effect,
    parse_zone,
    preview_message,
    preview_zone,
    rgb_tuple,
    thread_entry,
    thstatus_message,
)


# ---------------------------------------------------------------------------
# Framing
# ---------------------------------------------------------------------------

def test_frame_message_shape():
    reports = frame_message('{"m":"x"}')
    assert len(reports) == 1
    report = reports[0]
    assert len(report) == REPORT_BYTES
    assert report[0] == OPCODE_DATA
    assert report[1] == len('{"m":"x"}') + 2  # payload + CRLF
    assert report[2:report[1] + 2] == b'{"m":"x"}\r\n'


def test_frame_message_splits_long_payloads():
    payload = "x" * (MAX_CHUNK * 2)
    reports = frame_message(payload)
    assert len(reports) == 3  # two full chunks plus the CRLF tail
    assert all(len(r) == REPORT_BYTES for r in reports)
    assert sum(r[1] for r in reports) == len(payload) + 2


def test_decoder_round_trips_a_framed_message():
    decoder = FrameDecoder()
    message = {"m": "v.oai.hid", "p": {"k": "AG02", "act": 1, "ag": 0}}
    out = []
    for report in frame_message(json.dumps(message, separators=(",", ":"))):
        out.extend(decoder.feed(report))
    assert out == [message]


def test_decoder_reassembles_across_reports():
    decoder = FrameDecoder()
    payload = json.dumps({"m": "x", "p": {"pad": "y" * 200}}, separators=(",", ":"))
    reports = frame_message(payload)
    assert len(reports) > 1
    out = []
    for report in reports:
        out.extend(decoder.feed(report))
    assert out == [json.loads(payload)]


def test_decoder_accepts_a_leading_report_id():
    """macOS hands the callback a buffer that starts with the report id."""
    decoder = FrameDecoder()
    body = frame_message('{"m":"ping"}')[0]
    out = decoder.feed(bytes([REPORT_ID]) + body)
    assert out == [{"m": "ping"}]


def test_decoder_ignores_junk_and_recovers():
    decoder = FrameDecoder()
    assert decoder.feed(b"\x00\x00\x00") == []       # wrong opcode
    assert decoder.feed(frame_message("not json")[0]) == []
    assert decoder.feed(frame_message('{"m":"ok"}')[0]) == [{"m": "ok"}]


def test_notifications_never_carry_an_id():
    # Including an id on a v.oai.* method gets a 404 back from the firmware.
    assert notification("v.oai.thstatus", []) == {"m": "v.oai.thstatus", "p": []}
    assert "id" not in notification("v.oai.rgbcfg", {})


# ---------------------------------------------------------------------------
# Colours and effects
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "value,expected",
    [
        ("#FF0000", 0xFF0000),
        ("ff0000", 0xFF0000),
        ("0xFF0000", 0xFF0000),
        ("#f00", 0xFF0000),
        ([255, 0, 0], 0xFF0000),
        ((0, 255, 0), 0x00FF00),
        (16711680, 0xFF0000),
    ],
)
def test_parse_color_accepts_every_reasonable_spelling(value, expected):
    assert parse_color(value) == expected


@pytest.mark.parametrize(
    "value", ["nope", "#12345", [1, 2], [300, 0, 0], -1, 0x1000000, True, None]
)
def test_parse_color_rejects_nonsense(value):
    with pytest.raises(LightingError):
        parse_color(value)


def test_color_round_trip():
    assert color_to_hex(parse_color("#34C759")) == "#34C759"
    assert rgb_tuple(0x34C759) == (0x34, 0xC7, 0x59)


@pytest.mark.parametrize(
    "value,expected",
    [("solid", 1), ("breath", 4), ("shallow-breath", 6), ("SHALLOW_BREATH", 6),
     ("breathing", 4), ("off", 0), (3, 3)],
)
def test_parse_effect(value, expected):
    assert parse_effect(value) == expected


def test_parse_effect_rejects_unknown():
    with pytest.raises(LightingError):
        parse_effect("disco")
    with pytest.raises(LightingError):
        parse_effect(99)


def test_effect_names_round_trip():
    for name, number in EFFECTS.items():
        assert effect_name(number) == name


def test_parse_zone_aliases():
    assert parse_zone("keys") == "backlight"
    assert parse_zone("ambient") == "underglow"
    assert parse_zone("agent-keys") == "agent_keys"
    with pytest.raises(LightingError):
        parse_zone("everything")


# ---------------------------------------------------------------------------
# Lighting messages
# ---------------------------------------------------------------------------

def test_preview_message_uses_full_field_names():
    """lights.preview is a base firmware method - it does not use v.oai's
    minimised keys."""
    zone = preview_zone("#FF0000", "solid", 1.0, 0.0)
    assert zone == {
        "effect": 1, "brightness": 1.0, "speed": 0.0, "color": 16711680,
    }
    message = preview_message(backlight=zone, underglow=zone, request_id=7)
    assert message["m"] == METHOD_PREVIEW
    assert set(message["p"]) == {"backlight", "underglow"}
    assert message["id"] == 7  # this method does take an id


def test_preview_message_can_target_one_zone():
    zone = preview_zone("#000000", "off", 0.0, 0.0)
    message = preview_message(backlight=zone)
    assert list(message["p"]) == ["backlight"]
    with pytest.raises(LightingError):
        preview_message()


def test_preview_zone_omits_magic_unless_asked():
    assert "magic" not in preview_zone("#FFFFFF")
    assert preview_zone("#FFFFFF", magic=0.5)["magic"] == 0.5


@pytest.mark.parametrize("field", ["brightness", "speed"])
def test_preview_zone_rejects_out_of_range(field):
    with pytest.raises(LightingError):
        preview_zone("#FFFFFF", "solid", **{field: 1.5})


def test_thread_entry_uses_minimised_field_names():
    entry = thread_entry(2, "#00FF00", "solid", 1.0, 0.0)
    assert entry["id"] == 2
    assert entry["c"] == 0x00FF00
    assert entry["e"] == 1
    assert entry["b"] == 1.0
    # sk/sa are only sent when explicitly requested.
    assert "sk" not in entry and "sa" not in entry


def test_thread_entry_rejects_bad_index():
    with pytest.raises(LightingError):
        thread_entry(AGENT_KEY_COUNT, "#FFFFFF")


def test_all_agent_keys_covers_every_key():
    message = all_agent_keys("#00FF00")
    assert message["m"] == METHOD_THREAD_STATUS
    assert [entry["id"] for entry in message["p"]] == list(range(AGENT_KEY_COUNT))
    assert "id" not in message  # notification form


def test_thstatus_needs_entries():
    with pytest.raises(LightingError):
        thstatus_message([])


# ---------------------------------------------------------------------------
# Transport-dependent framing
# ---------------------------------------------------------------------------

def test_usb_framing_has_no_report_id_prefix():
    report = frame_message('{"m":"x"}', TRANSPORT_USB)[0]
    assert len(report) == REPORT_BYTES
    assert report[0] == OPCODE_DATA


def test_ble_framing_prefixes_the_report_id_and_is_one_byte_longer():
    """Get this wrong and the write still returns success - silently discarded."""
    report = frame_message('{"m":"x"}', TRANSPORT_BLE)[0]
    assert len(report) == REPORT_BYTES_BLE
    assert report[0] == REPORT_ID
    assert report[1] == OPCODE_DATA
    assert report[2] == len('{"m":"x"}') + 2
    assert report[3:report[2] + 3] == b'{"m":"x"}\r\n'


def test_both_transports_carry_the_same_payload_bytes():
    payload = "y" * 200
    usb = frame_message(payload, TRANSPORT_USB)
    ble = frame_message(payload, TRANSPORT_BLE)
    assert len(usb) == len(ble)
    assert [r[1] for r in usb] == [r[2] for r in ble]


def test_decoder_round_trips_ble_framed_reports():
    decoder = FrameDecoder()
    message = {"m": "v.oai.hid", "p": {"k": "ENC_CW", "act": 2}}
    out = []
    for report in frame_message(
        json.dumps(message, separators=(",", ":")), TRANSPORT_BLE
    ):
        out.extend(decoder.feed(report))
    assert out == [message]
