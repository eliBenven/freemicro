# Changelog

All notable changes to FreeMicro are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

> **Nothing has been released yet.** Everything below is the content of the
> first public release, `0.1.0`, and stays under *Unreleased* until that tag
> exists - a changelog that claims a version nobody can install is the first
> thing that makes a project untrustworthy.

## [Unreleased]

The arc of this release, honestly: FreeMicro began as a state engine and a
renderer registry with **no way to drive the actual pad**. The Codex Micro's
LED protocol was unknown, so it shipped four fallback renderers while the
investigation ran. It ends with that protocol reverse engineered, published, and
**verified on hardware over both USB and Bluetooth** - every key, the dial, the
thumbstick and all three LED zones - plus a loop that runs at login without a
terminal open. The fallbacks are gone with it: they insured a risk that no
longer exists, and one of them could abort the process.

### Added - talking to the pad

- **`freemicro.device`** - the pad's `0xFF00` vendor JSON-RPC channel, over
  **USB and Bluetooth**. Transport-aware write framing (63 bytes with no
  report-id prefix on USB, 64 bytes with `0x06` prefixed on BLE), a pure and
  therefore testable framer and decoder, a single shared device handle per
  process, and a reconnect loop that treats disconnects as normal rather than
  fatal.
- **`docs/PROTOCOL.md`** - the wire protocol, written up as interface facts and
  believed to be its first public documentation: methods, key ids, the lighting
  objects, the effect enum, and the per-transport framing trap. Plus
  `docs/CUSTOMIZING.md` and `docs/FACTORY-DEFAULTS.md`.

### Added - input

- **Input bridge** (`freemicro.input`): every key, the dial press, the dial's
  rotation ticks and the analogue thumbstick, routed to user-configured actions.
  The pad emits no scancodes, so this is what makes it do anything at all.
- **CGEvent keystroke backend** with AppleScript fallback: adds `fn` support,
  true hold-to-talk, mouse control, and no subprocess per keystroke.
- **Eight action kinds** (`text`, `key`, `hold`, `shell`, `applescript`, `app`,
  `mouse`, `none`) in a registry where adding a ninth is one decorated function,
  validated at config-load time.

### Added - lighting

- **`micro-leds` renderer**: drives the backlight, underglow and the six Agent
  Keys from agent state via `lights.preview` + `v.oai.thstatus`. **Opt-in** - `freemicro lights --enable` - because macOS shares this device and FreeMicro
  should not seize LEDs somebody else may be driving.
- Factory-matching default colours, and a decay on `done` (factory green means
  *unread*, not *completed*).

### Added - the loop is proven, and it runs without you

- **`freemicro selftest`**: fires a whole synthetic Claude Code session through
  the *exact command recorded in your settings*, as a subprocess with the event
  JSON on stdin, against a throwaway state directory - then asserts the resolved
  state for each event **and** the LED protocol messages for each state. This
  closes the project's oldest gap: every piece was unit-tested and the assembly
  never was. `--json` for CI, `--in-process` when iterating on the classifier.
- **`freemicro start`**: guided setup end to end - both macOS permissions
  (detected, explained, and the right System Settings pane opened on request),
  the pad, vendor-app contention, the config, the LED opt-in (default still
  **no**), the hooks (installed *and verified*), the daemon, and a closing light
  show. Idempotent, and every prompt has a default so `--yes` never hangs.
- **`freemicro daemon install|uninstall|status|logs`**: a launchd LaunchAgent
  that starts at login, restarts if it dies, and logs to a size-capped file. The
  pad emits no scancodes, so without something listening the hardware is dead - this is what stops that meaning "keep a terminal open forever".
- **Pad lock** (`~/.freemicro/pad.lock`): only one process can usefully hold this
  device, so `run`, `keys` and the daemon take an `flock` and *name whoever has
  it* instead of fighting. Being an `flock`, a killed process can never leave a
  stale lock behind. `--take-pad` overrides.
- **`freemicro.permissions`**: Input Monitoring via `IOHIDCheckAccess` and
  Accessibility via `AXIsProcessTrusted` - read-only, prompt-free, and they work
  with no pad attached, so "did you grant it?" is no longer inferred from a
  failure three layers down. `doctor` reports "macOS hasn't decided yet" as the
  distinct third state it really is.
- **`freemicro doctor`**: preflight for both macOS permissions, transport,
  battery, and a real `device.status` round trip - the only honest write test on
  a device where success return codes mean nothing.
- **`freemicro run`**: keys in and lights out in one process, one device handle.
- **User-owned pad config** (`~/.freemicro/keymap.json`, `freemicro keys --init`):
  every input and every LED colour/effect in one annotated JSON file. JSON
  rather than TOML so the core stays dependency-free on Python 3.9.

### Added - release readiness

- **`freemicro.compat`**: records the firmware the protocol was actually
  verified on (v0.4.1) and classifies whatever a pad reports as known-good,
  newer, older or unparseable. It warns and explains; it never gates.
- **`freemicro.diagnostics`**: a redacted diagnostic bundle for bug reports - OS, Python, transport, firmware, battery, permissions, renderer availability
  and config *structure*, in text and JSON. The contents of `shell` and
  `applescript` bindings and every path outside the config directory are
  stripped before anything is printed, so the output is safe to paste into a
  public issue.
- **`SECURITY.md`** and **`docs/SECURITY-MODEL.md`**: what a config can actually
  do, what the two macOS permissions really grant and to whom, a threat model
  (malicious preset, local process on the web UI, unattended daemon, config
  tampering), and a concrete design for preset trust that must ship alongside
  shareable presets.
- **`docs/RELEASING.md`**: version bump through PyPI and GitHub release, with
  the exact commands.
- **macOS CI.** The product is macOS-only and CI was Linux-only, so every IOKit,
  CGEvent and permissions path was untested. There is now a `macos-latest`
  job (3.11 - 3.13, plus 3.9/3.10 on the x86_64 runner) that runs headless - no
  hardware, no synthetic keystrokes, and a step that fails the build if anything
  imports tkinter. Linux is kept across 3.9 - 3.13 to prove the pure-logic layers stay
  portable. Ruff runs as its own job, and a packaging job installs the built
  wheel into a clean venv and runs it.
- **Issue templates** for bug reports (asking for `freemicro doctor --report`)
  and hardware reports (VID/PID *and firmware version*, feeding the crowdsourced
  capability database and `compat.KNOWN_GOOD`).

### Added - the original scaffold

The layers that predate the hardware work, and are still the backbone:

- **State engine** (`freemicro.state`): Claude Code hooks → five normalized
  states, per-session store with priority resolution and TTL expiry.
- **Renderer registry** (`freemicro.renderers`): originally five renderers with
  a guaranteed non-pad fallback. Four were deleted before release; see
  **Removed**. What survives is the registry and `micro-leds`.
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
  DB. Uses only the documented VIA/QMK path - no firmware, no proprietary
  protocol.
- **CLI** (`freemicro`): `detect`, `install`, `hook`, `demo`, `emit`, `render`,
  `renderers`, `status`. Hooks are installed with an absolute interpreter path
  so they resolve under Claude Code's minimal environment.
- **`freemicro demo`**: plays the full state sequence on the real renderer so
  the end-to-end loop is visible with no agent or hardware attached.
- **Crowdsourced hardware DB** scaffold (`hardware/capabilities.json`).
- Test suite (57 tests) covering the state engine, hook classifier, renderer
  selection, hook installer, detector, the LED write-test harness, capture
  parsing / learning / replay, and a full-session end-to-end pipeline (hook
  events → store → resolved state).

### Changed

- **Hook installation is now absolute, quoted, and self-repairing.** The command
  is resolved from the binary you actually invoked (then the interpreter's
  `bin/`, then `PATH`) and `shlex`-quoted, so a virtualenv in a path with a space
  no longer produces hooks that silently do nothing. Re-running `freemicro
  install` after moving or reinstalling *rewrites* the stale entry rather than
  leaving it. Entries carry a `timeout`, and `--uninstall` removes exactly ours.
- **`freemicro install` verifies itself** by running the selftest, unless you
  pass `--no-verify`. "Installed" and "working" are not the same claim.
- **`freemicro doctor`** now also checks Input Monitoring directly, whether the
  hooks are installed *and still point at this binary*, and the daemon.
- **`freemicro daemon install` refuses to claim success it hasn't seen**: it
  waits for a stable pid (launchd reports one for a process already dying) and,
  if the daemon never comes up, reads its log and explains why. It specifically
  detects a binary installed under `~/Desktop`/`~/Documents`/`~/Downloads`, which
  macOS blocks background agents from reading - an error whose actual message
  (`PermissionError … pyvenv.cfg`) is unguessable.
- **Packaging**: `Development Status :: 4 - Beta` (the loop is verified on
  hardware and covered by a re-runnable self-test; not Stable until more than one
  unit and firmware have been through it), macOS and Python 3.9 - 3.13 classifiers,
  project URLs, and an explicit `artifacts` entry so the shipped keymap can never
  be dropped from the wheel. `pipx` is now the recommended install.
- Lighting defaults to `lights.preview` rather than `v.oai.rgbcfg`, with the
  choice exposed as one config key (`lighting.method`), because the two paths
  genuinely disagree - see the open question in `docs/PROTOCOL.md`.

### Removed

- **Four of the five renderers, and the positioning that justified them.**
  `screen`, `busylight`, `micro-via` and `micro-qmk` are gone, with `--no-screen`,
  `freemicro watch`, the `busylight` extra, the Tk probe and its
  `~/.freemicro/tk-probe.json` cache, and doctor's Tk check. They existed so
  that "the alert never depends on the pad", written while the LED path was
  unproven; it is now verified on hardware over both transports. They were not
  free: `screen`'s window could not open on **any** machine (nothing ever called
  the probe that would have let it), it had already aborted a process outright,
  and doctor warned about it with a blank reason. `busylight` had never been run
  by anyone; blink(1) and Luxafor owners are better served by VibeSignal, which
  does that job properly. `micro-via` and `micro-qmk` targeted VIA/QMK pads: the
  Codex Micro exposes no `0xFF60` channel and QMK does not run on its ESP32.
  `freemicro run` prints each state change to the terminal, which is all the
  `screen` renderer ever actually delivered. A `renderers.prefer` entry naming a
  removed renderer, and `freemicro watch` or `--no-screen` on a command line,
  each get a sentence saying what replaced them.
- **`firmware/qmk-keymap/`**: firmware for a reflash this silicon cannot take.
- **`presets/`**: a VIA skeleton still carrying `"vendorId": "0x0000"` and a
  Work Louder Input layout whose bindings predate being able to read the pad
  directly. Both were fossils; the web UI (`freemicro config --web`) is how you
  configure the pad now.
- `micro_sniffed`: its premise (sniffing an opaque binary RGB protocol and
  replaying it) is obsolete now that the channel is known to speak JSON-RPC.
- `docs/PATH-B-CAPTURE.md` retired to a decision record; no USB capture was
  needed.

### Fixed

- **Every surface now decays a session by the same clocks.** The CLI, the menu
  bar, the web UI and the pad each built their own `StateStore` from a
  hand-written list of TTLs, and three of the lists were short, so
  `working_ttl_seconds: 0` and `tool_ttl_seconds` were ignored by the console
  line while the pad honoured them, and the two could disagree about the same
  session in the same process at the same instant. The timers now travel as one
  `DecayPolicy` and there is a single construction, `default_store()`, that
  every reader calls. `working_ttl_seconds: 0` switches the check off
  everywhere, exactly as the docs have always said.
- **"LEDs require USB" was wrong.** That conclusion came from malformed
  Bluetooth writes, not from an incapable transport: BLE needs a 64-byte buffer
  with the report id prefixed. With correct framing, input, lighting and RPC all
  work wirelessly - confirmed by eye with the cable unplugged.
- **Encoder rotation is no longer silently swallowed.** Ticks have been seen
  carrying `act` values other than `1`, so `ENC_CW`/`ENC_CC` now fire on any
  value. Filtering on `act == 1` dropped every dial turn and looked exactly like
  a dead dial.
- **Tk can no longer take the process down**, because FreeMicro no longer
  imports Tk at all. On some macOS/Python combinations `tkinter.Tk()` calls
  `abort()`, which no `try`/`except` can catch; the subprocess probe written to
  survive that went out with the renderer it defended.
- **The IOKit input-report callback keeps a strong module-level reference**, so
  a pad that drops mid-callback can no longer segfault the interpreter.

### Security

- LED control is **off by default**. FreeMicro does not take over a shared
  device uninvited.
- The web UI refuses to bind anything that is not loopback - a `ValueError`,
  with no flag to override it - requires a per-run bearer token on every
  request, and validates the `Host` header to close DNS rebinding.
- Diagnostic reports redact `shell` and `applescript` contents and personal
  paths, so they are safe to paste into a public issue.
- `sys.bootloader`, which reboots the pad into DFU and disconnects it, is
  documented and deliberately never sent.
- The security model and the preset-trust design are written down *before*
  shareable presets exist, because the day presets land, downloading one becomes
  remote code execution.

### Known limitations

- **Pad support is macOS-only.** The vendor channel is reached through IOKit;
  `hidapi` cannot open this device. There is no non-pad display to fall back to,
  so on Linux and Windows FreeMicro currently shows you nothing. Support is
  unimplemented, not impossible.
- **One unit, one firmware.** Everything was verified on a single pad running
  v0.4.1. `freemicro.compat` says so out loud when yours differs, and a
  [Hardware Report](.github/ISSUE_TEMPLATE/hardware_report.yml) is the single
  most valuable contribution anyone can make.
- **`v.oai.rgbcfg` is unresolved.** It acknowledges without any visible change
  on our hardware, while the vendor app appears to use it for everything.
- **Synthetic `fn` is unverified** against third-party dictation apps.
- **There is no preset trust check yet.** A config you did not write is a
  program you did not read - see `docs/SECURITY-MODEL.md`.

[Unreleased]: https://github.com/eliBenven/freemicro/compare/main...HEAD
