"""Tests for the hook installer and the read-only detector."""

from __future__ import annotations

import json

from freemicro.detector import probe
from freemicro.detector.probe import CapabilityReport, HidInterface
from freemicro.hooks_install import HOOK_EVENTS, build_settings, install_hooks


def test_install_creates_all_events(tmp_path):
    path = tmp_path / "settings.json"
    install_hooks(settings_path=path)
    data = json.loads(path.read_text())
    for event in HOOK_EVENTS:
        assert event in data["hooks"]
        commands = [
            h["command"]
            for group in data["hooks"][event]
            for h in group["hooks"]
        ]
        assert any("freemicro" in c for c in commands)


def test_install_is_idempotent(tmp_path):
    path = tmp_path / "settings.json"
    install_hooks(settings_path=path)
    first = path.read_text()
    install_hooks(settings_path=path)
    second = path.read_text()
    assert first == second  # running twice must not duplicate entries


def test_install_preserves_existing_settings(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"model": "opus", "hooks": {"Stop": []}}))
    install_hooks(settings_path=path)
    data = json.loads(path.read_text())
    assert data["model"] == "opus"  # untouched
    assert data["hooks"]["Stop"]  # our entry was appended


def test_build_settings_is_pure():
    original = {"hooks": {}}
    build_settings(original)
    assert original == {"hooks": {}}  # input not mutated


def test_probe_never_raises():
    # With or without hidapi installed, probe returns a report and no error.
    report = probe()
    assert isinstance(report.notes, list)
    # to_json must always be serializable
    json.loads(report.to_json())


def _iface(usage_page, usage=1, product="Codex Micro", mfr="Work Louder",
           vid=0x303A, pid=0x8360, interface_number=0):
    return HidInterface(
        vendor_id=vid,
        product_id=pid,
        usage_page=usage_page,
        usage=usage,
        interface_number=interface_number,
        product_string=product,
        manufacturer_string=mfr,
    )


def test_vendor_channel_detected_when_no_via_channel():
    """The shipping Codex Micro: 0xFF00 vendor channel, no 0xFF60 VIA channel."""
    report = CapabilityReport(
        hidapi_available=True,
        interfaces=[_iface(0x0001, usage=6), _iface(0xFF00)],
    )
    assert report.has_raw_channel is False
    vendor = report.candidate_vendor_channels
    assert len(vendor) == 1
    assert vendor[0].usage_page == 0xFF00
    # Serialized report advertises the vendor channel for the capability DB.
    data = json.loads(report.to_json())
    assert data["candidate_vendor_channels"][0]["usage_page"] == "0xFF00"


def test_host_vendor_interfaces_are_not_mistaken_for_a_pad():
    """A host's own 0xFF00 interfaces (e.g. Apple) must not count as a pad channel."""
    report = CapabilityReport(
        hidapi_available=True,
        interfaces=[
            _iface(0xFF00, product="", mfr="Apple", vid=0x0, pid=0x0,
                   interface_number=-1),
        ],
    )
    assert report.candidate_pads == []
    assert report.candidate_vendor_channels == []
