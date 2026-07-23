# Changelog

All notable changes to FreeMicro are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **State engine** (`freemicro.state`): Claude Code hooks → five normalized
  states, per-session store with priority resolution and TTL expiry.
- **Renderer registry** (`freemicro.renderers`): `screen` (guaranteed),
  `busylight` (reliable), `micro-via` and `micro-qmk` (experimental), with a
  screen fallback that is always kept reachable.
- **Detector** (`freemicro.detector`): read-only HID capability probe for
  Milestone 0.
- **Sniff & replay** (`freemicro.capture`, `freemicro.protocol`, `freemicro
  learn`, `micro-sniffed` renderer): capture the ChatGPT app's own LED HID
  reports per Codex state and replay them from Claude Code for an exact
  replication of the Agent-Key behaviour. Parses tshark/Wireshark JSON or plain
  hex captures, maps Codex states (thinking/running→working, awaiting→waiting,
  …) to FreeMicro states, stores literal per-state frames, and optionally infers
  RGB byte offsets from solid-colour captures. Full procedure in
  `docs/SNIFF-RUNBOOK.md`. (Owner-authorized protocol interop; no firmware is
  extracted or redistributed.)
- **LED write-test** (`freemicro.verify` / `freemicro verify-leds`): actively
  drives the writable LED renderers through the state sequence (Path A from
  `docs/LED-STRATEGY.md`), captures a human verdict (did the Agent Keys move?
  per-key or global? app-quit needed?), and writes a report for the capability
  DB. Uses only the documented VIA/QMK path — no firmware, no proprietary
  protocol.
- **CLI** (`freemicro`): `detect`, `install`, `hook`, `watch`, `demo`, `emit`,
  `render`, `renderers`, `status`. Hooks are installed with an absolute
  interpreter path so they resolve under Claude Code's minimal environment.
- **`freemicro demo`**: plays the full state sequence on the real renderer so
  the end-to-end loop is visible with no agent or hardware attached.
- **Presets**: recommended Claude Code terminal layout (Work Louder Input) and
  a VIA skeleton.
- **Firmware**: optional QMK keymap reference for per-key Agent-Key colours.
- **Crowdsourced hardware DB** scaffold (`hardware/capabilities.json`).
- Test suite (57 tests) covering the state engine, hook classifier, renderer
  selection, hook installer, detector, the LED write-test harness, capture
  parsing / learning / replay, and a full-session end-to-end pipeline (hook
  events → store → resolved state).

### Not yet verified
- Driving the **shipping Codex Micro's Agent Keys** — blocked on the Milestone 0
  hardware probe. Tracked in `SPEC.md` §4.

[Unreleased]: https://github.com/eliBenven/freemicro
