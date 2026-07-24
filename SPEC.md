# FreeMicro - Spec / Design Doc

> An open-source bridge that lets an arbitrary coding agent - first target
> **Claude Code** - fully drive a macro pad's status LEDs and inputs, including
> the **OpenAI Codex Micro's Agent-Key LEDs**, without the ChatGPT desktop app.

**Status:** v0.2 · **inputs and LEDs verified on a physical unit (2026-07-23,
firmware v0.4.1, macOS)** · reference impl in `src/freemicro/`
**Owner:** Eli · **Target agent (first):** Claude Code (terminal)
**Wire protocol:** [`docs/PROTOCOL.md`](docs/PROTOCOL.md) - the authoritative
record of what the hardware actually does.

---

## 1. Problem

The Codex Micro is a Work Louder Creator Micro 2 macro pad with a Codex firmware
profile. Two things are bolted to OpenAI's walled garden:

1. **Agent-Key status LEDs** - the top-row RGB (idle/thinking/done/needs-input/
   error) is *pushed from the Codex platform via the ChatGPT desktop app*. It is
   not standard HID and does nothing for other agents out of the box.
2. **The "agentic" bindings** (reasoning dial, skill joystick, push-to-talk) are
   wired to Codex/ChatGPT-app behaviors.
3. **The keys emit no scancodes.** ⚠️ This spec originally assumed the physical
   inputs were "standard USB HID that already work anywhere." **That is wrong,
   and it is the most important thing the hardware taught us.** Every key,
   the dial press and the joystick arrive as vendor JSON-RPC notifications on the
   pad's `0xFF00` collection. Without software listening there, the pad types
   nothing at all.

So the gap to close was bigger than assumed: (a) drive the LEDs from an arbitrary
agent's real state, **and (b) read the inputs at all** and map them for that
agent. Both are now done, on real hardware. First target: Claude Code.

## 2. Goals / Non-goals

**Goals**
- Drive the Micro's Agent-Key LEDs from **Claude Code** lifecycle state.
- Ship a recommended Claude Code **input layout** (terminal-first).
- Give each open project its own Agent Key, lit with that project's own state,
  and make pressing that key jump to it. This is the product; §5.3 is why the
  once-goal of "always deliver the signal even without the pad" is now a
  **non-goal**.
- Positioning: *the Codex Micro as an agent control surface*, an unoccupied
  niche. Other agents (Codex CLI, Cursor) are a new classifier in `state/`.
- Be a good OSS citizen: MIT, documented, crowdsourced hardware capability DB.

**Non-goals**
- **Redistributing** OpenAI/Work Louder firmware. (Still a hard non-goal.)
- ~~Reverse-engineering OpenAI/Work Louder firmware.~~ **Relaxed 2026-07-23
  by owner grant** for **interoperability recon on owned hardware** only:
  observing and replaying the app→pad LED protocol on the `0xFF00` vendor
  channel (Path B) is now in scope. We still never redistribute their firmware,
  and this covers the LED-control hop only, not the model or app internals.
- Replacing Work Louder Input for basic keystroke macros (we complement it).
- Local inference or anything touching the Codex model itself.

## 3. Prior art (from research, 2026-07)

- **OpenMicro** (`github.com/stephenleo/OpenMicro`, MIT) - rebuilds Codex Micro
  behavior for **Claude Code and Codex** via auto-installed hooks, with
  agent-state LEDs. **But it is a gamepad tool end to end:** it reads HID
  *gamepad* input, and the DualSense is the only pad it can write LEDs back to.
  The Micro is a keyboard-class device, so **neither end of OpenMicro fits our
  hardware.** → **Prior art and pattern reference, NOT a base or fork target.**
- **VibeSignal** (`github.com/yzhao062/vibesignal`) - Claude Code hooks → status,
  rendered to commercial busylights (blink(1)/Luxafor/BlinkStick) + screen.
  Great state-layer reference; does **not** talk to the Micro.
- **M5Stack Core2 open-source firmware** - reproduces Codex Micro features on a
  *different* dev board (ESP32-class), not the shipping pad.
- **Bawankule, "Codex Micro Alternatives"** - writes up the QMK raw-HID +
  Claude Code hooks method. Confirms the approach.
- **explainx.ai** - step-by-step Claude Code sound + traffic-light via hooks.
- A **commercial clone vendor** (`codex-micro.com`) sells a CM2-class pad
  "pre-configured for Codex, Claude Code, or Cursor" with agent-status
  lighting - **paid, closed-source, and a clone rather than the shipping Micro.**

**Verdict:** nobody has *publicly and openly* driven the **actual shipping Codex
Micro's** Agent Keys from Claude Code. Doing so - open-source, on the real pad - is the novel, ownable position.

## 4. The big open question - ANSWERED 2026-07-23 (live on hardware)

> **Yes.** A shipping unit was probed, then read from, then lit. Raw probe JSON:
> [`hardware/probes/codex-micro-303a-8360_2026-07-23.json`](hardware/probes/codex-micro-303a-8360_2026-07-23.json).
> The full wire protocol is documented in [`docs/PROTOCOL.md`](docs/PROTOCOL.md).
> Verdict below; the pre-probe debate is preserved underneath for provenance.

**Final answer, verified visually on the pad:**

- **Inputs: solved.** Every key, the dial (press *and* rotation, `ENC_CW` /
  `ENC_CC`) and the analogue thumbstick read live off the `0xFF00` vendor
  collection (Report ID 6, JSON-RPC).
- **LEDs: solved.** `lights.preview` drives the backlight and underglow in real
  time; `v.oai.thstatus` sets each of the six Agent Keys independently.
  `v.oai.rgbcfg` ACKs without any visible change on our unit - **but the vendor
  app appears to use it for everything and never call preview**, so this is an
  open contradiction, not a settled fact. FreeMicro defaults to what it has
  watched work and exposes the other as `lighting.method`.
- **Access: macOS IOKit.** `hidapi` cannot open this device at all. This
  supersedes the earlier "capture must be done on Linux/Windows" conclusion:
  plain userland macOS works, given **Input Monitoring**.
- **Both transports work.** USB and Bluetooth LE are equally capable - input,
  lighting and RPC all verified wirelessly. The *only* difference is write
  framing (63 bytes bare on USB, 64 bytes with the report id prefixed on BLE),
  selected from the IOKit `Transport` property.
- **Write return codes are meaningless.** A wrongly framed write returns
  `kIOReturnSuccess` and is silently discarded. The only trustworthy check is a
  `device.status` round trip, which is what `freemicro doctor` does.
- **No sniffing was needed in the end.** The device answers a documented-shaped
  JSON-RPC interface; we enumerated its methods and wrote our own client. Path B
  (USB capture) is retired - see [`docs/PATH-B-CAPTURE.md`](docs/PATH-B-CAPTURE.md).

**What the pad actually is:** enumerates as **VID `0x303A` / PID `0x8360`**, mfr
`Work Louder`, product `Codex Micro`. `0x303A` is **Espressif's** VID → the MCU
is **ESP32-family (likely ESP32-S3), *not* the RP2040** the research assumed.

**The three-way verdict:**
- **Path A (VIA raw-HID, no reflash): ❌ blocked.** No `0xFF60` interface exists.
  The Codex profile does not expose a VIA/QMK writable channel.
- **Path C (reflash open QMK): ❌ likely invalid.** The open `work_louder/micro`
  QMK target is **RP2040-only**; mainline QMK does not run on ESP32. The
  "unbrickable UF2 / `RPI-RP2` drive" premise below does **not** apply to this
  silicon (ESP32 uses the `esptool` serial ROM bootloader instead).
- **Path B (own the `0xFF00` vendor channel): ✅ SHIPPED - and it turned out not
  to need sniffing.** The pad exposes a vendor-defined HID interface on
  `usage_page 0xFF00` speaking JSON-RPC. Enumerating its methods was enough; the
  USB-capture plan in [`docs/PATH-B-CAPTURE.md`](docs/PATH-B-CAPTURE.md) is
  retired. This is now the *input* path as well as the LED path.
- **Path F (screen / busylight fallback): ⛔ withdrawn 2026-07-23.** It existed
  to insure Paths A/B/C against failure. Path B shipped and is verified on
  hardware over both transports, so the insurance had nothing left to insure,
  and it was never free (§5.3). The alert depends on the pad, deliberately.

**Platform note (corrected):** the M0 probe's conclusion that macOS userland
"cannot open" the `0xFF00` interface was **wrong in its diagnosis**. `hidapi`
fails because it models each HID top-level collection as its own openable path,
while macOS vends a **single** `IOHIDDevice` (primary usage = keyboard) that
merely *contains* `0xFF00`. Going straight to IOKit
(`IOServiceGetMatchingServices` → `IOHIDDeviceCreate` → `IOHIDDeviceOpen`) works
from plain userland with **no entitlement and no kext** - only the user's
**Input Monitoring** grant. No Linux/Windows capture was required.

---

### Pre-probe debate (preserved for provenance)

Whether the **shipping Codex Micro exposes a writable raw-HID channel** (VIA/QMK)
was **contested** across launch coverage:
- Some outlets: it's VIA-capable (real-time remap, no reflash); coverage notes
  customization runs "through the open-source VIA tool."
- One: Work Louder says CM2 uses proprietary **Input**, *not* QMK/VIA; OpenAI
  doesn't document VIA for the Micro.
- Another: the configurator may be embedded in the ChatGPT app.

This is the single fact the whole LED half hinges on, and it is **unresolved
until we probe the physical unit.** Hence Milestone 0. Note: even "VIA remap
works" does **not** guarantee "the VIA lighting command drives the *Agent
Keys*" - those LEDs are pushed by the desktop app and may sit on a separate
channel. Confirm with a write test, not just enumeration.

**Research update (pre-probe, now partly falsified):** the research assumed the
chassis was a Work Louder Creator Micro 2 - VIA-capable, open QMK firmware, on an
**RP2040 with an unbrickable UF2 bootloader**. ⚠️ **The 2026-07-23 probe
falsified the MCU assumption:** the shipping unit is **ESP32-based (VID
`0x303A`)**, so the RP2040/UF2/QMK-reflash reasoning (Path C) does not hold for
this silicon. The three paths - VIA raw-HID (A), sniff-and-replay (B), reflash
QMK (C) - are still analyzed in [`docs/LED-STRATEGY.md`](docs/LED-STRATEGY.md),
now annotated with the probe verdict (A blocked, C invalid, **B is the path**).
The reverse-engineering non-goal was **relaxed by owner grant** (see §2), so
Path B recon is authorized.

## 5. Architecture

Five decoupled layers. The device layer is shared by input *and* output, because
on this hardware both travel the same vendor HID channel.

```
Claude Code ──hooks──▶ [State Engine] ──▶ [Renderer Registry] ──▶ [Device] ──▶ pad LEDs
                           │                  └── micro-leds  (the pad)      ▲
   normalized states:      │                                                 │
   idle/working/           ▼                                                 │
   waiting/done/error   per-session store,     [Input Bridge] ◀── key/joystick events
                        priority-resolved             │
                                                      ▼
                                          your keymap ──▶ frontmost app
```

### 5.1 State Engine - `src/freemicro/state/`
- Claude Code hooks → normalized states:
  - `UserPromptSubmit`, `PreToolUse`, `PostToolUse` → **working**
  - `Notification` (permission prompt) → **waiting**
  - `Stop` → **done**; `Stop` w/ error → **error**
  - `SessionEnd` → **idle**
- One JSON file per session; resolve by priority `waiting > error > done >
  working > idle`; TTL drops stale sessions.

### 5.2 Device Layer - `src/freemicro/device/`
Owns the pad's `0xFF00` vendor collection. Deliberately outside both `input/` and
`renderers/`, because the *same channel* carries key events up and lighting
commands down - modelling it as an input concern would be a lie the code would
eventually punish us for.
- `codex_micro.py` - IOKit open, transport-aware framing, run-loop pump.
  Framing and decoding are pure functions, tested without hardware.
- Disconnects are treated as **normal**: `run_with_reconnect` reopens instead of
  exiting, because the pad drops on sleep, range, battery and nudged cables.
- Crash safety is explicit: the ctypes input callback is held in a
  module-level list forever, can never raise, and send-only paths register no
  callback and pump no run loop.
- `lighting.py` - message builders for `lights.preview`, `v.oai.thstatus` and
  (documented but unused for live state) `v.oai.rgbcfg`.
- One **shared handle per process**, so `freemicro run` never contends with
  itself. `FREEMICRO_NO_DEVICE=1` makes the pad look absent - used by the tests.

### 5.3 Renderer Registry - `src/freemicro/renderers/`
One renderer, and a registry so a second one is a new module and nothing else.
- `micro-leds` - the Codex Micro's backlight, underglow and Agent Keys.
  **Verified on hardware, USB and Bluetooth.** Priority 100, so it wins whenever
  the pad is present - but it is **opt-in**: `lighting.enabled` defaults to
  false, because macOS shares this device and two processes silently repainting
  the same LEDs is the worst possible first impression. Colours default to the
  exact factory values (`docs/FACTORY-DEFAULTS.md`).

**Removed 2026-07-23** (`docs/PRODUCT-REVIEW.md` §7): `screen`, `busylight`,
`micro-via` and `micro-qmk`, together with the "any agent in, any RGB surface
out" positioning that justified them. They existed so that *the alert never
depends on the pad*, written while the LED path was unproven. The LED path is
now proven on hardware over both transports, and the fallbacks were not paying
for themselves: the `screen` renderer's Tk window could not open on any machine
and could `abort()` the process trying, `busylight` had never been executed by
anyone, and the two VIA/QMK renderers targeted hardware the shipping probe ruled
out. The pad is the display. `freemicro run` prints each state change to the
terminal, which is all the `screen` renderer ever actually delivered. A name in
`renderers.prefer` that used to resolve is now reported by the CLI with what
replaced it, never silently ignored.

### 5.4 Input Layer - `src/freemicro/input/`
The pad's keys emit no scancodes, so this layer is what makes it work at all.
- `keys.py` - key names → macOS keystrokes. Pure, so bad names fail at load time.
- `actions.py` - an extensible action registry (`text`, `key`, `shell`,
  `applescript`, `none`) plus delivery backends. A new kind is one decorated
  function. Delivery is injectable, so tests never type anything.
- `bridge.py` - protocol events → bound actions, including joystick edge
  detection with hysteresis.
- The bindings themselves live in `padconfig.py` / `default_keymap.json`, and
  the web UI (`freemicro config --web`) is the supported way to edit them.

### 5.4a Preflight - `freemicro doctor`
Every failure mode on this device is invisible: macOS drops synthetic events
silently, a wrongly framed write returns success, and a contending app just
quietly wins. `doctor` checks the platform, the config, the transport, the open,
and - crucially - a real `device.status` round trip, then names the one
permission it cannot check for itself.

### 5.5 Config
Two files, split by how often you touch them:
- `~/.freemicro/keymap.json` - **the pad config**: every binding, the joystick,
  and every LED colour/effect. `freemicro keys --init` writes an annotated
  starter. Loader: `src/freemicro/padconfig.py`. JSON rather than TOML because
  `tomllib` is 3.11+ and the core must stay dependency-free on 3.9; see
  [`docs/CUSTOMIZING.md`](docs/CUSTOMIZING.md).
- `~/.freemicro/config.json` - runtime prefs (renderer order, TTL).
  `src/freemicro/config.py`.

Validation is **strict about actions, lenient about input ids**: a bad action
field is fatal (you want to know now), an unrecognised input id is a warning, so
a future firmware key is bindable without a FreeMicro release.

## 6. Open questions - answered

Milestone 0 asked five questions. All five now have answers:

1. *Does VIA/Vial detect the pad?* **No.** There is no `0xFF60` channel.
2. *VID/PID and interfaces?* **`0x303A:0x8360`**, ESP32-family silicon, with a
   vendor collection on `0xFF00` (Report ID 6, 63-byte input *and* output).
3. *Does the VIA lighting protocol move the Agent Keys?* **N/A** - no VIA
   channel. The Agent Keys move via `v.oai.thstatus`, individually addressable.
4. *Is the bootloader open for custom QMK?* **Moot** - `sys.bootloader` exists and
   reboots to DFU, but mainline QMK does not target this silicon. ⚠️ Never call
   `sys.bootloader` in normal operation; it disconnects the device.
5. *Must the ChatGPT app be quit?* **Yes, in practice** - it contends for the
   same channel.

Still open: whether `v.oai.rgbcfg` really drives the lights (our hardware said no,
the vendor app's behaviour says yes - exposed as `lighting.method` rather than
guessed), the literal `act` value on encoder ticks, non-macOS host support, the
`magic` lighting field, whether a synthetic `fn` triggers dictation apps, and
whether any of this survives a firmware update.

## 7. Roadmap

- [x] **M0 - Detection spike.** Capability report published.
- [x] **M1 - Status-LED MVP.** `micro-leds` drives the real pad from agent state.
- [x] **M2 - Input layer.** Full key/joystick bridge with a user-owned keymap.
- [ ] **M3 - Generalize.** Harness abstraction for any agent; per-Agent-Key
      status for multiple concurrent sessions; other VIA/QMK RGB pads.
- [ ] **M4 - Reach.** Linux/Windows host support for the vendor channel; grow the
      capability DB; a daemon + IPC layer so a menubar app, the CLI and the hooks
      can share the single device owner rather than fight for it.

## 8. Risks

- **Firmware update changes the protocol.** → Version-pin observations in the
  capability DB and keep `available()` honest, so a pad that stops answering
  reports itself instead of going quietly dark. This is now the top risk, since
  everything else is verified.
- **Single-unit sample.** Everything is confirmed on one pad on firmware v0.4.1.
  → Crowdsource Hardware Reports.
- **macOS-only pad support.** → Documented plainly, in the README and in
  `doctor`. There is no non-pad signal to fall back to, so on Linux and Windows
  FreeMicro currently does nothing; a port is a well-isolated PR.
- **Contention with the ChatGPT app over the channel.** → Document "quit the
  ChatGPT app for host control."
- **Trademark.** Don't ship "Codex" in the project name (nominative use only).
- **Provenance.** Everything we know is our own measurement of our own device,
  documented as interface facts. **No vendor source is copied or vendored.**

## 9. Naming

Avoid "Codex" (OpenAI mark) in the repo/package name. Chosen: **FreeMicro**.
Description may say "for the OpenAI Codex Micro" (nominative use) - the *name*
is ours.

## 10. License & layout

MIT (matches OpenMicro, keeps interop clean).

```
freemicro/
  README.md  SPEC.md  LICENSE  CONTRIBUTING.md  CODE_OF_CONDUCT.md
  src/freemicro/
    device/      codex_micro.py (transport) lighting.py (LED messages)
    input/       keys.py actions.py bridge.py
    renderers/   base.py micro_leds.py
    state/       engine.py hooks.py
    detector/    probe.py
    padconfig.py default_keymap.json config.py cli.py
  docs/      PROTOCOL.md  CUSTOMIZING.md  LED-STRATEGY.md  LAUNCH.md
  hardware/  capabilities.json      (crowdsourced DB) + probes/
  hooks/     install helper
  tests/     protocol, padconfig, actions, bridge, LEDs, state, hooks
```

## 11. References

- OpenMicro - https://github.com/stephenleo/OpenMicro
- VibeSignal - https://github.com/yzhao062/vibesignal
- Bawankule, Codex Micro Alternatives - https://www.adityabawankule.io/blog/openai-codex-micro-alternatives
- explainx, Claude Code sound/traffic-light - https://explainx.ai/blog/claude-code-sound-notification-approval-hook-2026
- VIA - https://usevia.app
- **Codex Micro wire protocol - [`docs/PROTOCOL.md`](docs/PROTOCOL.md)** (this
  project's own measurements; believed to be the first public write-up)
- Customizing bindings and lighting - [`docs/CUSTOMIZING.md`](docs/CUSTOMIZING.md)
