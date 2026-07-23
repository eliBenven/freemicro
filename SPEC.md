# FreeMicro — Spec / Design Doc

> An open-source bridge that lets an arbitrary coding agent — first target
> **Claude Code** — fully drive a macro pad's status LEDs and inputs, including
> the **OpenAI Codex Micro's Agent-Key LEDs**, without the ChatGPT desktop app.

**Status:** Draft v0.1 · pre-hardware · reference impl in `src/freemicro/`
**Owner:** Eli · **Target agent (first):** Claude Code (terminal)

---

## 1. Problem

The Codex Micro is a Work Louder Creator Micro 2 macro pad with a Codex firmware
profile. Two things are bolted to OpenAI's walled garden:

1. **Agent-Key status LEDs** — the top-row RGB (idle/thinking/done/needs-input/
   error) is *pushed from the Codex platform via the ChatGPT desktop app*. It is
   not standard HID and does nothing for other agents out of the box.
2. **The "agentic" bindings** (reasoning dial, skill joystick, push-to-talk) are
   wired to Codex/ChatGPT-app behaviors.

The physical **inputs** are standard USB HID and already work anywhere. So the
gap to close is: (a) drive the LEDs from an arbitrary agent's real state, and
(b) ship a clean input mapping for that agent. First target: Claude Code.

## 2. Goals / Non-goals

**Goals**
- Drive the Micro's Agent-Key LEDs from **Claude Code** lifecycle state.
- Ship a recommended Claude Code **input layout** (terminal-first).
- **Always** deliver a "done / needs-you" signal, even if the pad's LEDs turn
  out to be non-drivable (graceful fallback to an external light or screen).
- Generalize: any agent (Claude Code, Codex CLI, Cursor) × any VIA/QMK RGB pad.
  Positioning: *macro pads as agent status surfaces* — an unoccupied niche.
- Be a good OSS citizen: MIT, documented, crowdsourced hardware capability DB.

**Non-goals**
- Reverse-engineering or redistributing OpenAI/Work Louder firmware.
- Replacing Work Louder Input for basic keystroke macros (we complement it).
- Local inference or anything touching the Codex model itself.

## 3. Prior art (from research, 2026-07)

- **OpenMicro** (`github.com/stephenleo/OpenMicro`, MIT) — rebuilds Codex Micro
  behavior for **Claude Code and Codex** via auto-installed hooks, with
  agent-state LEDs. **But it is a gamepad tool end to end:** it reads HID
  *gamepad* input, and the DualSense is the only pad it can write LEDs back to.
  The Micro is a keyboard-class device, so **neither end of OpenMicro fits our
  hardware.** → **Prior art and pattern reference, NOT a base or fork target.**
- **VibeSignal** (`github.com/yzhao062/vibesignal`) — Claude Code hooks → status,
  rendered to commercial busylights (blink(1)/Luxafor/BlinkStick) + screen.
  Great state-layer reference; does **not** talk to the Micro.
- **M5Stack Core2 open-source firmware** — reproduces Codex Micro features on a
  *different* dev board (ESP32-class), not the shipping pad.
- **Bawankule, "Codex Micro Alternatives"** — writes up the QMK raw-HID +
  Claude Code hooks method. Confirms the approach.
- **explainx.ai** — step-by-step Claude Code sound + traffic-light via hooks.
- A **commercial clone vendor** (`codex-micro.com`) sells a CM2-class pad
  "pre-configured for Codex, Claude Code, or Cursor" with agent-status
  lighting — **paid, closed-source, and a clone rather than the shipping Micro.**

**Verdict:** nobody has *publicly and openly* driven the **actual shipping Codex
Micro's** Agent Keys from Claude Code. Doing so — open-source, on the real pad —
is the novel, ownable position.

## 4. The big open question (disputed in public)

Whether the **shipping Codex Micro exposes a writable raw-HID channel** (VIA/QMK)
is **contested** across launch coverage:
- Some outlets: it's VIA-capable (real-time remap, no reflash); coverage notes
  customization runs "through the open-source VIA tool."
- One: Work Louder says CM2 uses proprietary **Input**, *not* QMK/VIA; OpenAI
  doesn't document VIA for the Micro.
- Another: the configurator may be embedded in the ChatGPT app.

This is the single fact the whole LED half hinges on, and it is **unresolved
until we probe the physical unit.** Hence Milestone 0. Note: even "VIA remap
works" does **not** guarantee "the VIA lighting command drives the *Agent
Keys*" — those LEDs are pushed by the desktop app and may sit on a separate
channel. Confirm with a write test, not just enumeration.

**Research update:** the chassis is a Work Louder Creator Micro 2, which is
VIA-capable with open QMK firmware in the upstream tree, on an RP2040 with an
unbrickable UF2 bootloader. So the *hardware* supports every path we need; only
the *Codex firmware profile's* lockdown is unknown. Three concrete ways to drive
the Agent Keys — VIA raw-HID (no reflash), sniff-and-replay the app's protocol,
or reflash open QMK — are analyzed and ranked in
[`docs/LED-STRATEGY.md`](docs/LED-STRATEGY.md), including the policy decision on
whether to relax the reverse-engineering non-goal for the sniff path.

## 5. Architecture

Four decoupled layers. State and input are solved; the renderer is the work.

```
Claude Code ──hooks──▶ [State Engine] ──▶ [Renderer Registry] ──▶ hardware
                           │                     ├── micro-via   (best-effort)
   normalized states:      │                     ├── micro-qmk   (reflash)
   idle/working/           │                     ├── busylight   (reliable)
   waiting/done/error      │                     └── screen      (guaranteed)
                           ▼
                    per-session store,
                    priority-resolved
```

### 5.1 State Engine — `src/freemicro/state/`
- Claude Code hooks → normalized states:
  - `UserPromptSubmit`, `PreToolUse`, `PostToolUse` → **working**
  - `Notification` (permission prompt) → **waiting**
  - `Stop` → **done**; `Stop` w/ error → **error**
  - `SessionEnd` → **idle**
- One JSON file per session; resolve by priority `waiting > error > done >
  working > idle`; TTL drops stale sessions.

### 5.2 Renderer Registry — `src/freemicro/renderers/`
Auto-select the best available target; **screen fallback always present** so the
alert never depends on the pad.
- `micro-via` — set LEDs over the VIA lighting raw-HID protocol. **No reflash.**
  Likely global colour, not per-key. *(experimental until M0/M1)*
- `micro-qmk` — custom firmware `raw_hid_receive` → per-key Agent-Key colours.
  **Requires reflash + open bootloader.** *(true per-key; M3)*
- `busylight` — blink(1)/Luxafor/etc via `busylight-core`. Reliable.
- `screen` — always-on-top chip + console fallback. Guaranteed.

### 5.3 Input Layer — `presets/`, `src/freemicro/input/`
- Ship a recommended **terminal Claude Code** layout for Work Louder Input, plus
  a VIA-importable `keyboard.json` where VIA works.
- Optional custom **QMK keymap** for people who reflash.

### 5.4 Config — `src/freemicro/config.py`
Adopt OpenMicro's config *shape* (per-layer `color` + `bindings`, `workflows`)
for familiarity and possible interop. `~/.freemicro/config.json`.

## 6. Open questions → **Milestone 0: detection spike** (day the pad arrives)

Run `freemicro detect` and publish the answers (this report alone is a useful
community artifact — nobody's posted it):

1. Does **usevia.app** / Vial detect the pad? (writable channel y/n)
2. **VID / PID** and interfaces (`hid.enumerate()`); is there a `usage_page
   0xFF60` raw interface?
3. LED write path: does the **VIA lighting protocol** move the Agent Keys? Is it
   per-key addressable or global only?
4. Is the **bootloader** open for custom QMK?
5. **Contention:** must the ChatGPT app be quit for the host to own the LEDs?

## 7. Roadmap

- **M0 — Detection spike.** Answer §6. Publish a hardware-capability report.
- **M1 — Status-LED MVP.** Finish the LED write for whichever path the pad
  supports; light the Micro or fall back. *Blocker: M0 answers.*
- **M2 — Input layer.** Ship the terminal Claude Code layout + VIA `keyboard.json`.
- **M3 — Generalize.** Harness abstraction for any agent; renderer for any
  VIA/QMK RGB pad; optional QMK keymap.
- **M4 — OSS launch.** Docs, MIT license, crowdsourced capability DB.

## 8. Risks

- **Firmware locked (no writable channel).** → Fallback renderers still deliver
  the core value; document the limitation honestly. Value survives.
- **OpenAI/WL firmware updates change behavior.** → Version-pin + capability DB.
- **Contention with the ChatGPT app over the LEDs.** → Detect; document "quit
  Codex app for host control."
- **Trademark.** Don't ship "Codex" in the project name (nominative use only).

## 9. Naming

Avoid "Codex" (OpenAI mark) in the repo/package name. Chosen: **FreeMicro**.
Description may say "for the OpenAI Codex Micro" (nominative use) — the *name*
is ours.

## 10. License & layout

MIT (matches OpenMicro, keeps interop clean).

```
freemicro/
  README.md  SPEC.md  LICENSE  CONTRIBUTING.md  CODE_OF_CONDUCT.md
  src/freemicro/  state/ renderers/ detector/ input/ config.py cli.py
  presets/   claude-code.input.json  claude-code.keyboard.json (VIA)
  firmware/  qmk-keymap/            (optional, M3)
  hardware/  capabilities.json      (crowdsourced DB)
  hooks/     install helper
  tests/     unit tests for state/renderers/detector
```

## 11. References

- OpenMicro — https://github.com/stephenleo/OpenMicro
- VibeSignal — https://github.com/yzhao062/vibesignal
- Bawankule, Codex Micro Alternatives — https://www.adityabawankule.io/blog/openai-codex-micro-alternatives
- explainx, Claude Code sound/traffic-light — https://explainx.ai/blog/claude-code-sound-notification-approval-hook-2026
- QMK raw HID — https://docs.qmk.fm
- VIA — https://usevia.app
