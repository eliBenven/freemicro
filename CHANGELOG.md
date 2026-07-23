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
- **CLI** (`freemicro`): `detect`, `install`, `hook`, `watch`, `emit`,
  `render`, `renderers`, `status`.
- **Presets**: recommended Claude Code terminal layout (Work Louder Input) and
  a VIA skeleton.
- **Firmware**: optional QMK keymap reference for per-key Agent-Key colours.
- **Crowdsourced hardware DB** scaffold (`hardware/capabilities.json`).
- Test suite (35 tests) covering the state engine, hook classifier, renderer
  selection, hook installer, and detector.

### Not yet verified
- Driving the **shipping Codex Micro's Agent Keys** — blocked on the Milestone 0
  hardware probe. Tracked in `SPEC.md` §4.

[Unreleased]: https://github.com/eliBenven/freemicro
