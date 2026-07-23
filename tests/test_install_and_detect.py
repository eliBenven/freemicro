"""Tests for the hook installer and the read-only detector."""

from __future__ import annotations

import json

from freemicro.detector import probe
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
