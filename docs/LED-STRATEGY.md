# Driving the Agent-Key LEDs — strategy & research

> How FreeMicro can light the Codex Micro's Agent Keys from Claude Code, ranked
> by fidelity, effort, risk, and reversibility. Research synthesis as of
> 2026-07; **the probe on a physical unit is still the decider** — this doc
> narrows the search, it doesn't replace Milestone 0.

## What we're up against

The six top-row Agent Keys are **not** driven by anything standard. From launch
coverage and OpenAI's own docs, the chain is:

```
Codex platform ──JSON-RPC thread-state events──▶ ChatGPT desktop app ──RGB HID reports──▶ pad LEDs
                 (idle/thinking/running/                (the "bridge")
                  awaiting/done/error)
```

The ChatGPT desktop app is a JSON-RPC **client** of the Codex App Server. It
reads thread-state events and **translates them into RGB LED control signals
sent to the pad over USB/BT.** So the last hop — app → pad — is almost certainly
ordinary HID reports on a vendor channel. That hop is the thing we want to own.

## What the research established (and the one thing it didn't)

**Established:**

1. **The chassis is open.** The Codex Micro is a **Work Louder Creator Micro 2**
   with a Codex firmware profile. The base CM2 is **VIA-capable, per-key RGB,
   and its firmware lives in the open-source QMK tree**
   (`qmk/qmk_firmware/keyboards/work_louder/micro`, plus community forks). That
   means the *hardware* exposes exactly the raw-HID + RGB-matrix machinery our
   `micro-via` / `micro-qmk` renderers target.
2. **The MCU is unbrickable to flash.** The Work Louder Micro line is RP2040.
   RP2040 has a mask-ROM UF2 bootloader (BOOTSEL / double-tap reset → an
   `RPI-RP2` USB drive). You cannot brick it, and you can always flash back.
   → A reflash path is **safe and reversible.**
3. **The app→pad hop is sniffable.** Solid-red-vs-solid-green USB captures
   (USBPcap+Wireshark on Windows, `usbmon` on Linux) reliably expose an RGB
   report format; `usbrply` turns a capture straight into replay code. This is a
   well-trodden technique for exactly this kind of vendor LED protocol.

**Not established (the M0 unknown):** whether the **Codex firmware profile**
leaves the VIA/raw-HID channel *open* or *locks it down* relative to stock CM2,
and whether the Agent-Key LEDs sit on the normal RGB-matrix range or a separate
one. No public teardown or VIA-detection report exists yet. `freemicro detect`
+ a 10-minute sniff answers this on day one.

## The paths, ranked

### Path A — VIA lighting over raw HID (no reflash) — *try first*
**What:** If the Codex profile keeps the QMK/VIA channel, set the LEDs with the
documented VIA lighting commands. Already implemented in
`src/freemicro/renderers/micro_via.py`.
**How:** `freemicro detect` → look for `usage_page 0xFF60`. If present, run
**`freemicro verify-leds`** — it drives the LEDs through every state, asks
whether the Agent Keys moved (per-key or global? app-quit needed?), and saves a
report for the capability DB. (This harness is built and tested; it just needs
the pad.)
**Effort:** minutes. **Risk:** low. **Reversible:** N/A (no firmware change).
**Legality:** clean — documented open protocol on your own device.
**Catch:** VIA lighting is often **global**, not per-key; and the Codex profile
may have disabled the channel, or the Agent Keys may be a separate LED range the
global command doesn't touch. Detect tells us fast.

### Path B — Sniff & replay the app's own LED protocol — *highest fidelity*
**What:** Capture the exact HID reports the ChatGPT app sends when state changes
(idle→thinking→done…), reverse the report format, and replay those bytes from
FreeMicro driven by Claude Code state. This drives the **real Agent Keys exactly
as the app does — per-key, correct ranges** — because we speak the pad's actual
LED protocol.
**How:** Windows VM + ChatGPT app + USBPcap; force each state, diff captures
byte-by-byte (report id, command, key index, RGB); rebuild as `hid.write()`
reports; wire into a new `micro-sniffed` renderer.
**Effort:** an afternoon. **Risk:** medium (protocol may checksum / sequence).
**Reversible:** N/A. **Contention:** you must **quit the ChatGPT app** so it
isn't fighting us for the HID channel (document this).
**⚠️ Policy flag:** this is **protocol reverse-engineering**, which the current
[`SPEC.md` §2](../SPEC.md) lists as a **non-goal**. It's generally defensible as
interoperability on hardware you own, but may bump OpenAI/Work Louder ToS.
**This needs an explicit decision from the owner before we build it** (see
below). We would still never redistribute their firmware.

### Path C — Reflash stock open QMK + our `raw_hid_receive` — *most reliable*
**What:** If the Codex profile locks everything, flash the **open-source Work
Louder QMK firmware** with our `raw_hid_receive` handler (already scaffolded in
`firmware/qmk-keymap/keymap.c`). We then own every LED forever, true per-key.
**How:** BOOTSEL/double-tap → `RPI-RP2` drive → drop the `.uf2`. Flash the
stock Codex/WL firmware back anytime to restore app behavior.
**Effort:** an hour. **Risk:** low (RP2040 can't be bricked). **Reversible:**
yes, fully. **Legality:** clean — it's Work Louder's *own open-source* firmware,
no reverse-engineering.
**Cost:** you lose the Codex-app-driven behavior while our firmware is on — fine
for a Claude-Code-first user, and reversible.

### Path F — Fallback renderers (already shipped) — *the guarantee*
`busylight` and `screen` deliver the signal regardless of what A/B/C conclude.
The alert never depends on the pad. This is why we can pursue A–C as upside, not
as a single point of failure.

## Recommended sequence when the pad arrives

1. `freemicro detect --json` → is there a `0xFF60` channel? Save to `hardware/`.
2. If yes → **Path A**: `freemicro verify-leds`. Do the Agent Keys move?
   Per-key or global? It records the verdict automatically.
3. Regardless → do a **red/green sniff** of the ChatGPT app (Path B recon) — even
   if you don't ship the replay, the capture *documents the real protocol* and
   is a community first for the capability DB.
4. If A is blocked or only global, decide between **B** (speak their protocol,
   per-key, policy call) and **C** (reflash open QMK, clean, reversible).
5. Update `hardware/capabilities.json` and the renderer that won.

## Decisions this needs from the owner

- **Relax the "no reverse-engineering" non-goal to allow Path B?** The
  sniff-and-replay is the only path that drives the *stock* Agent Keys per-key
  without reflashing. It's interop on owned hardware, but it's a line the spec
  currently draws. Yes/no changes what we build.
- **Is reflashing (Path C) acceptable** as the shipped recommendation if the
  channel is locked? It's clean and reversible but replaces the Codex profile.
- **BT vs USB:** LEDs also work over Bluetooth. USB is far easier to sniff and
  drive; recommend scoping to USB first.

## Sources

- Codex Micro Agent Keys require the ChatGPT desktop app as the LED bridge —
  https://www.techtimes.com/articles/320670/20260716/openai-codex-micro-ships-today-agent-keys-only-work-chatgpt-desktop.htm
- Codex App Server JSON-RPC thread-state → app → RGB signals —
  https://learn.chatgpt.com/docs/features/codex-micro ,
  https://rohitai.com/blog/openai-codex-micro-agent-control-surface
- Creator Micro 2 is VIA-capable, per-key RGB — https://worklouder.cc/creator-micro-2
- Work Louder Micro firmware is in open-source QMK —
  https://github.com/qmk/qmk_firmware/blob/master/keyboards/work_louder/micro/readme.md ,
  https://github.com/ForsakenRei/qmk-worklouder-micro , https://github.com/qmk/qmk_firmware/pull/19555
- RP2040 unbrickable UF2 bootloader / flashing — https://docs.qmk.fm/flashing ,
  https://docs.keeb.supply/basics/firmware/flashing/
- USB HID LED sniff/replay method — https://botmonster.com/self-hosting/reverse-engineer-usb-devices-with-wireshark-and-python/ ,
  https://pypi.org/project/usbrply , https://wiki.wireshark.org/CaptureSetup/USB
- QMK raw HID — https://docs.qmk.fm/features/rawhid
