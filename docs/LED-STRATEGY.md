# Driving the Agent-Key LEDs - strategy & research

> **⛳ SOLVED, 2026-07-23.** The LEDs are driven, verified visually on a shipping
> unit. The authoritative record of *how* is
> **[`PROTOCOL.md`](PROTOCOL.md)**; the implementation is
> `src/freemicro/renderers/micro_leds.py`. This document is kept as the
> **decision record** - what we expected, what was wrong, and why the path we
> took won. Read it for provenance, not for instructions.

## The answer, in one box

| | |
|---|---|
| **Live lighting** | `lights.preview` → `backlight` (keycaps) + `underglow` (base strip), applied immediately, not persisted |
| **Agent Keys** | `v.oai.thstatus` → an array of six entries, each independently coloured |
| **Stored config** | `v.oai.rgbcfg` → ACKs `{"ok":1}` but **produces no visible change**; not used for live state |
| **Transport** | The same `0xFF00` / Report ID 6 vendor channel the keys arrive on |
| **Host access** | macOS IOKit, plain userland, **Input Monitoring** grant only |
| **Effects** | 0 off · 1 solid · 2 snake · 3 rainbow · 4 breath *(firmware idle default)* · 5 gradient · 6 shallowBreath |

**No USB sniffing was needed.** The device answers a JSON-RPC interface; we
enumerated its methods and wrote an independent client. The capture playbook in
[`PATH-B-CAPTURE.md`](PATH-B-CAPTURE.md) is retired.

## What the probe overturned

A shipping unit was probed with `freemicro detect`
([raw JSON](../hardware/probes/codex-micro-303a-8360_2026-07-23.json)). It
**overturned three core assumptions** in the research below:

| | Research assumed | Reality |
|---|---|---|
| **MCU** | RP2040 (Work Louder CM2) | **ESP32-family**, VID `0x303A`, PID `0x8360` |
| **Bootloader** | UF2 / `RPI-RP2`, unbrickable | ESP32 serial ROM; `sys.bootloader` reboots to DFU ⚠️ |
| **VIA `0xFF60`** | maybe present | **absent** → Path A dead |
| **QMK reflash** | clean fallback | `work_louder/micro` is RP2040-only → **Path C invalid here** |
| **Inputs** | "standard USB HID, already work anywhere" | **wrong** - no scancodes; vendor JSON-RPC events only |
| **macOS access** | needs an entitled `IOHIDManager`; capture on Linux | **wrong** - plain IOKit works; `hidapi` is what fails |

**Verdict:** **Path A ❌ blocked · Path C ❌ invalid on ESP32 · Path B ✅ shipped
(and simpler than planned) · Path F ✅ still the guarantee.** Path annotations
are inline below.

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
sent to the pad over USB/BT.** So the last hop - app → pad - is almost certainly
ordinary HID reports on a vendor channel. That hop is the thing we want to own.

## What the research established (and the one thing it didn't)

**Established:**

1. **The chassis is open.** The Codex Micro is a **Work Louder Creator Micro 2**
   with a Codex firmware profile. The base CM2 is **VIA-capable, per-key RGB,
   and its firmware lives in the open-source QMK tree**
   (`qmk/qmk_firmware/keyboards/work_louder/micro`, plus community forks). That
   means the *hardware* exposes exactly the raw-HID + RGB-matrix machinery the
   `micro-via` / `micro-qmk` renderers targeted. (Both were deleted on
   2026-07-23: the shipping unit is ESP32, not RP2040, and it exposes no
   `0xFF60` channel. See `SPEC.md` §5.3.)
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

### Path A - VIA lighting over raw HID (no reflash) - ❌ ~~try first~~ BLOCKED (2026-07-23: no `0xFF60` channel)
**What:** If the Codex profile keeps the QMK/VIA channel, set the LEDs with the
documented VIA lighting commands. Was implemented in
`src/freemicro/renderers/micro_via.py`, deleted 2026-07-23 once this was
confirmed blocked.
**How:** `freemicro detect` → look for `usage_page 0xFF60`. If present, run
`freemicro render done` and watch the keys.
**Effort:** minutes. **Risk:** low. **Reversible:** N/A (no firmware change).
**Legality:** clean - documented open protocol on your own device.
**Catch:** VIA lighting is often **global**, not per-key; and the Codex profile
may have disabled the channel, or the Agent Keys may be a separate LED range the
global command doesn't touch. Detect tells us fast.

### Path B - Own the `0xFF00` vendor channel - ✅ **SHIPPED** (2026-07-23)
**What we planned:** capture the ChatGPT app's HID reports with USBPcap, diff
them byte-by-byte, and replay the opaque bytes.
**What actually happened:** the channel turned out to speak **JSON-RPC**, not an
opaque binary format. Enumerating the method table was enough - no capture, no
byte diffing, no replay of anyone's traffic. We wrote our own client against a
documented-shaped interface.
**Result:** `lights.preview` for live backlight/underglow, `v.oai.thstatus` for
per-Agent-Key colour. Both verified visually. `v.oai.rgbcfg` ACKs but does
nothing observable, so it is documented and unused.
**Effort:** an afternoon, as estimated - spent on method discovery rather than
packet analysis. **Risk:** a firmware update could change the interface.
**Contention:** **quit the ChatGPT app**; it fights for the same channel.
**Policy:** the [`SPEC.md` §2](../SPEC.md) non-goal was relaxed by owner grant
for interop recon on owned hardware. In the end the work was interface
enumeration, and we reproduce **no vendor code** - only descriptions of observed
behaviour.

**One trap worth recording:** the earlier write-test conclusion that "the pad
accepts writes but ignores every payload" was correct *and* misleading. Writes
were being accepted because the framing was right; nothing happened because the
payload was not JSON the firmware recognised. Brute-forcing 63 opaque bytes was
never going to work - the bytes were never opaque.

### Path C - Reflash stock open QMK + our `raw_hid_receive` - ❌ ~~most reliable~~ INVALID on ESP32 (2026-07-23: not RP2040; QMK `work_louder/micro` won't run here)
**What:** If the Codex profile locks everything, flash the **open-source Work
Louder QMK firmware** with our `raw_hid_receive` handler (was scaffolded in
`firmware/qmk-keymap/keymap.c`, deleted 2026-07-23 once this path was ruled
invalid). We would then own every LED forever, true per-key.
**How:** BOOTSEL/double-tap → `RPI-RP2` drive → drop the `.uf2`. Flash the
stock Codex/WL firmware back anytime to restore app behavior.
**Effort:** an hour. **Risk:** low (RP2040 can't be bricked). **Reversible:**
yes, fully. **Legality:** clean - it's Work Louder's *own open-source* firmware,
no reverse-engineering.
**Cost:** you lose the Codex-app-driven behavior while our firmware is on - fine
for a Claude-Code-first user, and reversible.

### Path F: fallback renderers, ⛔ withdrawn 2026-07-23
`busylight` and `screen` were the insurance: they delivered the signal
regardless of what A/B/C concluded, so they could be pursued as upside rather
than as a single point of failure. That was the right call while the LED path
was unproven. **Path B shipped and is verified on hardware over USB and
Bluetooth**, so the insurance had nothing left to insure, and it was never free:
the `screen` renderer's window could not open on any machine and had aborted a
process outright, and `busylight` had never been run by anyone. Both renderers
are deleted. The pad is the display; `freemicro run` prints each state change to
the terminal. See `SPEC.md` §5.3 and `docs/PRODUCT-REVIEW.md` §7.

## Lessons worth keeping

1. **Check whether the payload is text before treating it as binary.** The
   single biggest time sink was assuming an opaque RGB report format on a channel
   that was speaking JSON the whole time.
2. **When `hidapi` "can't open" a macOS device, suspect `hidapi`, not macOS.**
   It models each HID top-level collection as an openable path; macOS vends one
   `IOHIDDevice` containing them all. IOKit opens it from plain userland.
3. **Hold each colour for a second or two when testing lighting by eye.** Every
   lighting call replaces the previous one, so a fast sweep looks like only its
   final frame. This cost real debugging time.
4. **Acknowledgement is not effect.** `v.oai.rgbcfg` returns `{"ok":1}` and
   changes nothing visible. Never treat an ACK as proof the thing worked.
5. ⚠️ **`sys.bootloader` reboots the pad into DFU and disconnects it.** Never
   call it during normal operation.

## Still open

- **Bluetooth.** The LEDs work over BT too; FreeMicro only drives USB so far.
- **Non-macOS hosts.** The channel is plain USB HID, so Linux (hidraw) and
  Windows should both be reachable - nobody has written it yet.
- **The `magic` lighting field.** Exposed in config, purpose uncharacterized.
- **Per-Agent-Key session status.** `v.oai.thstatus` addresses all six keys
  individually; mapping six concurrent Claude Code sessions onto them is M3.

## Sources

- Codex Micro Agent Keys require the ChatGPT desktop app as the LED bridge - https://www.techtimes.com/articles/320670/20260716/openai-codex-micro-ships-today-agent-keys-only-work-chatgpt-desktop.htm
- Codex App Server JSON-RPC thread-state → app → RGB signals - https://learn.chatgpt.com/docs/features/codex-micro ,
  https://rohitai.com/blog/openai-codex-micro-agent-control-surface
- Creator Micro 2 is VIA-capable, per-key RGB - https://worklouder.cc/creator-micro-2
- Work Louder Micro firmware is in open-source QMK - https://github.com/qmk/qmk_firmware/blob/master/keyboards/work_louder/micro/readme.md ,
  https://github.com/ForsakenRei/qmk-worklouder-micro , https://github.com/qmk/qmk_firmware/pull/19555
- RP2040 unbrickable UF2 bootloader / flashing - https://docs.qmk.fm/flashing ,
  https://docs.keeb.supply/basics/firmware/flashing/
- USB HID LED sniff/replay method - https://botmonster.com/self-hosting/reverse-engineer-usb-devices-with-wireshark-and-python/ ,
  https://pypi.org/project/usbrply , https://wiki.wireshark.org/CaptureSetup/USB
- QMK raw HID - https://docs.qmk.fm/features/rawhid
