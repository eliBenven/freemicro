"""Active LED write-test — the day-one "does it actually light up?" verdict.

Unlike :mod:`freemicro.detector` (which is strictly read-only), this module
*writes* to the pad: it drives the writable LED renderers through the state
sequence so a human can watch the Agent Keys, then records the outcome to a
report file for the crowdsourced capability database.

This is Path A from ``docs/LED-STRATEGY.md``. It never touches firmware and
never speaks a proprietary protocol — only the documented VIA/QMK raw-HID
lighting commands our renderers already implement. If no writable channel is
present, it says so cleanly and points at the fallback renderers.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from freemicro.config import config_home
from freemicro.detector import probe
from freemicro.renderers import REGISTRY
from freemicro.state.engine import AgentState

# Renderers that actually write LEDs to the pad, best first.
WRITABLE_LED_RENDERERS = ("micro-qmk", "micro-via")

_DEFAULT_SEQUENCE = (
    AgentState.WORKING,
    AgentState.WAITING,
    AgentState.DONE,
    AgentState.ERROR,
    AgentState.IDLE,
)


def _writable_renderers() -> list:
    out = []
    for name in WRITABLE_LED_RENDERERS:
        cls = REGISTRY.get(name)
        if cls is None:
            continue
        try:
            renderer = cls()
            if renderer.available():
                out.append(renderer)
            else:
                renderer.close()
        except Exception:
            continue
    return out


def run_led_verify(
    *,
    interactive: bool = True,
    hold: float = 1.5,
    out_dir: Path | None = None,
    clock=time.time,
    sleep=time.sleep,
    prompt=input,
    sequence=_DEFAULT_SEQUENCE,
) -> dict:
    """Drive the writable LED renderer through the states and record the result.

    Returns a dict summarizing the run and writes it to a JSON report under
    ``out_dir`` (default ``~/.freemicro/reports``). Never raises for missing
    hardware or dependencies — those become ``notes``.
    """
    report = probe()
    result: dict = {
        "writable_channel": report.has_raw_channel,
        "renderer": None,
        "verdict": None,
        "report_path": None,
        "detect": json.loads(report.to_json()),
        "notes": list(report.notes),
    }

    renderers = _writable_renderers()
    if not renderers:
        result["notes"].append(
            "No writable LED renderer is available: the pad may be locked, "
            "absent, or driving its Agent Keys over a channel we don't yet "
            "speak. Fallback renderers still carry the signal. Next options are "
            "in docs/LED-STRATEGY.md (Path B: sniff & replay; Path C: reflash)."
        )
    else:
        primary = renderers[0]
        result["renderer"] = primary.name
        for state in sequence:
            primary.render(state)
            deadline = clock() + hold
            while clock() < deadline:
                primary.render(state)  # keep the write/GUI alive during the dwell
                sleep(min(0.05, hold))
        if interactive:
            moved = _ask_yes(prompt, "Did the Agent-Key LEDs change colour? [y/N] ")
            granularity = None
            app_quit = None
            if moved:
                g = prompt("Per-key colour or one global colour? [k/g] ").strip().lower()
                granularity = "per-key" if g.startswith("k") else "global"
                app_quit = _ask_yes(
                    prompt, "Did you have to quit the ChatGPT app first? [y/N] "
                )
            result["verdict"] = {
                "agent_keys_moved": moved,
                "granularity": granularity,
                "chatgpt_app_quit_required": app_quit,
            }
        for renderer in renderers:
            renderer.close()

    result["report_path"] = _write_report(result, report, out_dir)
    return result


def _ask_yes(prompt, question: str) -> bool:
    try:
        return prompt(question).strip().lower().startswith("y")
    except EOFError:
        return False


def _write_report(result: dict, report, out_dir: Path | None) -> str:
    directory = Path(out_dir) if out_dir else (config_home() / "reports")
    directory.mkdir(parents=True, exist_ok=True)
    candidates = report.candidate_pads
    tag = candidates[0].vid_pid.replace(":", "-") if candidates else "unknown"
    path = directory / f"led-verify-{tag}.json"
    path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    return str(path)
