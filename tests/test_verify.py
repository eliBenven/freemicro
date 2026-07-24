"""Tests for the active LED write-test harness (Path A)."""

from __future__ import annotations

import json

from freemicro import verify
from freemicro.renderers.base import Renderer
from freemicro.state.engine import AgentState


def test_no_writable_channel_reports_cleanly(tmp_path):
    # No hardware in CI -> no writable renderer available.
    result = verify.run_led_verify(
        interactive=False, hold=0, out_dir=tmp_path, sleep=lambda *_: None
    )
    assert result["renderer"] is None
    assert result["verdict"] is None
    # A report file is always written and is valid JSON.
    report_path = result["report_path"]
    assert report_path is not None
    data = json.loads(open(report_path).read())
    assert "detect" in data
    assert any("fallback" in n.lower() or "locked" in n.lower() for n in result["notes"])


def test_never_raises_without_hidapi(tmp_path):
    # Whatever the environment, the harness returns a dict and writes a report.
    result = verify.run_led_verify(
        interactive=False, hold=0, out_dir=tmp_path, sleep=lambda *_: None
    )
    assert isinstance(result, dict)
    assert set(result) >= {"writable_channel", "renderer", "verdict", "notes", "detect"}


class _FakeLed(Renderer):
    """A stand-in writable renderer to exercise the write + verdict path."""

    name = "micro-qmk"  # overrides the real one in the registry for this test
    priority = 99
    experimental = True

    rendered: list = []

    def available(self) -> bool:
        return True

    def render(self, state) -> None:
        _FakeLed.rendered.append(state)

    def close(self) -> None:
        pass


def test_write_path_and_verdict(tmp_path, monkeypatch):
    # Swap the registered micro-qmk renderer for our fake so we don't need hardware.
    monkeypatch.setitem(verify.REGISTRY, "micro-qmk", _FakeLed)
    _FakeLed.rendered = []

    answers = iter(["y", "k", "y"])  # moved? -> per-key? -> app quit?
    result = verify.run_led_verify(
        interactive=True,
        hold=0,
        out_dir=tmp_path,
        sleep=lambda *_: None,
        prompt=lambda _q: next(answers),
        sequence=[AgentState.WORKING, AgentState.DONE],
    )

    assert result["renderer"] == "micro-qmk"
    # Every state in the sequence was written to the pad.
    assert AgentState.WORKING in _FakeLed.rendered
    assert AgentState.DONE in _FakeLed.rendered
    assert result["verdict"] == {
        "agent_keys_moved": True,
        "granularity": "per-key",
        "chatgpt_app_quit_required": True,
    }


def test_verdict_negative_short_circuits(tmp_path, monkeypatch):
    monkeypatch.setitem(verify.REGISTRY, "micro-qmk", _FakeLed)
    _FakeLed.rendered = []
    result = verify.run_led_verify(
        interactive=True,
        hold=0,
        out_dir=tmp_path,
        sleep=lambda *_: None,
        prompt=lambda _q: "n",  # LEDs did not move
        sequence=[AgentState.WORKING],
    )
    assert result["verdict"]["agent_keys_moved"] is False
    assert result["verdict"]["granularity"] is None
