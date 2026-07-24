"""Tests for the firmware compatibility classifier.

Two things matter here and both are about *not overreaching*: the classifier
must never mis-rank a version it does not understand, and it must never block
anything. Everything else is presentation.
"""

from __future__ import annotations

import pytest

from freemicro import compat


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "text,expected",
    [
        ("0.4.1", (0, 4, 1)),
        ("v0.4.1", (0, 4, 1)),
        ("V0.4.1", (0, 4, 1)),
        ("  0.4.1  ", (0, 4, 1)),
        ("0.5", (0, 5)),
        ("1", (1,)),
        ("1.2.3.4", (1, 2, 3, 4)),
        ("0.4.1-beta.2", (0, 4, 1)),
        ("0.4.1+cafebabe", (0, 4, 1)),
        ("0.4.1 (build 7)", (0, 4, 1)),
        ("10.0.0", (10, 0, 0)),
    ],
)
def test_parses_the_shapes_a_device_might_report(text, expected):
    assert compat.parse_version(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        None,
        "",
        "   ",
        "unknown",
        "banana",
        "0.4.x",
        "0..1",
        "0.4.1.",
        ".4.1",
        "-",
        "v",
        "0x4",
        "n/a",
        "\x00\x01",
    ],
)
def test_refuses_to_guess_at_malformed_versions(text):
    """A wrong comparison is worse than no comparison."""
    assert compat.parse_version(text) is None


def test_parse_accepts_non_string_input():
    assert compat.parse_version(1) == (1,)
    assert compat.parse_version(0.4) == (0, 4)


def test_format_round_trips():
    assert compat.format_version(compat.parse_version("0.4.1")) == "0.4.1"
    assert compat.format_version(None) == ""


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

def test_the_known_good_version_is_recognised():
    for version in compat.KNOWN_GOOD:
        assert compat.classify(version) == compat.KNOWN_GOOD_STATUS
        assert compat.classify(f"v{version}") == compat.KNOWN_GOOD_STATUS


def test_trailing_zeros_do_not_change_the_answer():
    """0.4.1 and 0.4.1.0 are the same firmware."""
    assert compat.classify("0.4.1.0") == compat.KNOWN_GOOD_STATUS


@pytest.mark.parametrize("text", ["0.4.2", "0.5.0", "1.0.0", "0.5"])
def test_newer_firmware_is_flagged_as_newer(text):
    assert compat.classify(text) == compat.NEWER


@pytest.mark.parametrize("text", ["0.4.0", "0.3.9", "0.1", "0"])
def test_older_firmware_is_flagged_as_older(text):
    assert compat.classify(text) == compat.OLDER


def test_unparseable_and_missing_are_distinct():
    assert compat.classify("banana") == compat.UNPARSEABLE
    assert compat.classify(None) == compat.MISSING
    assert compat.classify("") == compat.MISSING


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------

def test_known_good_report_is_ok_and_names_the_version():
    report = compat.check("0.4.1")
    assert report.ok
    assert report.status == compat.KNOWN_GOOD_STATUS
    assert "0.4.1" in report.message


def test_newer_report_is_calm_names_both_versions_and_asks_for_a_report():
    report = compat.check("0.5.0")
    assert not report.ok
    assert "0.4.1" in report.message and "0.5.0" in report.message
    assert compat.REPORT_URL in report.message
    # Tone check: this is information, not an alarm.
    lowered = report.message.lower()
    assert "unsupported" not in lowered
    assert "refus" not in lowered


def test_every_status_produces_a_non_empty_message():
    for text in ("0.4.1", "0.9.9", "0.1.0", "banana", None):
        report = compat.check(text)
        assert report.message.strip()
        assert report.status in {
            compat.KNOWN_GOOD_STATUS,
            compat.NEWER,
            compat.OLDER,
            compat.UNPARSEABLE,
            compat.MISSING,
        }


def test_nothing_ever_blocks_on_a_version_mismatch():
    """The whole point: warn, don't gate."""
    for text in ("0.4.1", "99.0.0", "0.0.1", "garbage", None, "", 12345):
        assert compat.check(text).blocks is False


def test_check_status_reads_the_device_status_reply():
    report = compat.check_status({"version": "0.4.1", "battery": 80})
    assert report.ok
    assert compat.check_status({}).status == compat.MISSING
    assert compat.check_status(None).status == compat.MISSING
    # A reply that is not a mapping at all must not explode.
    assert compat.check_status(["0.4.1"]).status == compat.MISSING


def test_report_serialises_for_the_diagnostics_bundle():
    data = compat.check("0.5.0").to_dict()
    assert data["reported"] == "0.5.0"
    assert data["parsed"] == "0.5.0"
    assert data["status"] == compat.NEWER
    assert data["verified_on"] == list(compat.KNOWN_GOOD)
    assert data["message"]


def test_summary_line_is_one_line_for_every_status():
    for text in ("0.4.1", "0.5.0", "0.1.0", "banana", None):
        summary = compat.summary_line(compat.check(text))
        assert summary and "\n" not in summary


def test_known_good_list_is_self_consistent():
    """A typo in KNOWN_GOOD would silently break every classification."""
    assert compat.KNOWN_GOOD
    for version in compat.KNOWN_GOOD:
        assert compat.parse_version(version) is not None
    assert compat.newest_known_good() == compat.parse_version(compat.KNOWN_GOOD[-1])
    assert compat.VERIFIED_BEHAVIOUR
