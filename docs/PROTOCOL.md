# Codex Micro wire protocol (reverse-engineered, verified on hardware)

> Verified against a shipping unit, firmware **v0.4.1**, 2026-07-23.
> Documented as **interface facts** for interoperability. No vendor code is
> reproduced here; FreeMicro's implementation is written independently.

## Transport

The pad works over **both USB and Bluetooth LE**, but the two transports do
**not** frame writes identically — this is the single most expensive gotcha in
this document.

| | USB | Bluetooth LE |
|---|---|---|
| Product string | `Codex Micro` | `Codex Micro #1` |
| `Transport` property | `USB` | `Bluetooth Low Energy` |
| HID collections | 6 | 4 |
| Report descriptor | 275 bytes | 216 bytes (adds a FEATURE item) |
| **Write buffer** | **63 bytes, no report-id prefix** | **64 bytes, report id `0x06` prefixed** |
| `reportID` argument | `6` | `6` |
| Input events | ✅ | ✅ |
| Lighting / RPC | ✅ | ✅ *(only with the 64-byte prefixed framing)* |

Getting this wrong is silent: a malformed BLE write still returns
`kIOReturnSuccess` from `IOHIDDeviceSetReport`, and the device simply discards
it. Feature-type reports are rejected outright over BLE (`0xE00002F0`).

**Reliable self-test:** send `device.status` and wait for a reply. A reply proves
the host→device path is framed correctly; success return codes prove nothing.

> **Verified wireless, 2026-07-23.** With the 64-byte prefixed framing, key,
> joystick and encoder events stream in over BLE *and* `v.oai.rgbcfg` +
> `v.oai.thstatus` visibly drive the LEDs — confirmed by eye with the cable
> unplugged. An earlier conclusion that "LEDs require USB" was **wrong**: the
> writes were malformed, not the transport incapable.

- USB HID, **VID `0x303A` / PID `0x8360`** (Espressif silicon — *not* RP2040).
- Vendor collection: **usage page `0xFF00`, Report ID 6**, 63-byte Input *and*
  Output reports. There is **no VIA/QMK `0xFF60` channel**.
- **Write framing differs by transport** — the single nastiest trap here:

  | Transport | Output buffer | Length | `reportID` arg |
  |---|---|---|---|
  | USB | `[0x02][len][json…]` | 63 | 6 |
  | Bluetooth LE | `[0x06][0x02][len][json…]` | **64** | 6 |

  Report type is **Output** either way (Feature is rejected over BLE with
  `0xE00002F0`). Messages are `\r\n` terminated; long ones span several reports.

> ### ⚠️ Success return codes prove nothing
>
> A **wrongly framed write still returns `kIOReturnSuccess`** and is silently
> discarded by the device. `IOHIDDeviceSetReport` returning 0 tells you the OS
> accepted the buffer, not that the firmware understood it. The only trustworthy
> health check is a **`device.status` round trip** — send it and wait for a
> reply. `freemicro doctor` does exactly that; do the same in any new code.
> Trusting the return code cost this project hours.
- Payload is **JSON-RPC**. Compact form `{"m":…,"p":…,"id":…}` and standard
  `{"jsonrpc":"2.0","method":…,"params":…,"id":…}` are both accepted.
- Unknown method -> `{"error":{"code":404,"message":"Method not found"}}`.

### macOS access note

`hidapi`'s `open_path()` **always fails** on this device: hidapi models each HID
top-level collection as its own path, while macOS vends a **single**
`IOHIDDevice` (primary usage = keyboard) that *contains* `0xFF00` as a secondary
collection. Use IOKit directly: `IOServiceGetMatchingServices("IOHIDDevice")` ->
match VID/PID -> `IOHIDDeviceCreate` -> `IOHIDDeviceOpen` ->
`IOHIDDeviceRegisterInputReportCallback` / `IOHIDDeviceSetReport`.
Requires **Input Monitoring** permission.

## Device -> host notifications

| Method | Params | Meaning |
|---|---|---|
| `v.oai.hid` | `{k, act, ag}` | Key event. `k`=key id, `act` 1=down 0=up, `ag`=agent index |
| `v.oai.rad` | `{a, d}` | **Joystick** position. `a`=angle, `d`=distance, both normalized 0–1 |

**Key ids** (all arrive via `v.oai.hid`, with `act` 1=down / 0=up):

| Id | Input |
|---|---|
| `AG00`–`AG05` | the six Agent Keys |
| `ACT06`–`ACT12` | the seven action keys |
| `ENC_CLK` | encoder (dial) press |
| **`ENC_CW`** | **encoder rotated clockwise** — one event per detent |
| **`ENC_CC`** | **encoder rotated counter-clockwise** |

The action keys are **fixed switch positions**. The **keycaps are physically
swappable** — the pad ships with a tray of ~35 interchangeable caps — so *which
icon sits on which position is the owner's choice, not a property of the
hardware*. Do not hard-code a cap-to-id mapping.

Two facts about the positions themselves *are* hardware, and do generalise:

| Ids | What is fixed |
|---|---|
| `ACT06`–`ACT09` | four single-width switch positions |
| **`ACT10` + `ACT11`** | a **double-width** slot. One wide keycap spans **both** switches, so **both ids fire on every press** — bind them identically. The factory addresses this slot as `ACT10_ACT11` |
| `ACT12` | one single-width switch position |

### Factory keycap arrangement (verified from an unopened unit)

| Position | Factory cap |
|---|---|
| `ACT06` | **FAST** ⚡ |
| `ACT07` | **APPR** ✓ (approve) |
| `ACT08` | **REJ** ⊗ (reject) |
| `ACT09` | **SPLIT** ⤳ |
| `ACT10`+`ACT11` | **MIC** (double-width) |
| `ACT12` | **CODEX** |

Confirmed two ways: the vendor app's shipped layout (`docs/FACTORY-DEFAULTS.md`)
and a photograph of a boxed unit. **Default to this**, and let the user say
otherwise — the unit this protocol was otherwise captured on had been
rearranged by its owner to `LAB / PR / NAV / PLAY / MIC / TERM`, which is why a
cap-to-id mapping must never be treated as hardware.

### Physical arrangement of the whole pad

```
row 1:  [ ◯ dial ]  [ AG ]  [ AG ]  [ ● joystick ]
row 2:  [ AG ]      [ AG ]  [ AG ]  [ AG ]
row 3:  [ ACT06 ]   [ACT07] [ACT08] [ ACT09 ]
row 4:  [ ◉ haptic ] [ ═══ ACT10+ACT11 ═══ ] [ ACT12 ]
          + 3 LEDs
```

The pad also carries a **haptic pad** (tap + circle) beside three small white
indicator LEDs. It switches the pad's **Bluetooth host profile** (three slots;
the pad advertises as `Codex Micro #1/#2/#3`, and `device.status.profile_index`
reports the active one, zero-indexed). It emits **only standard HID** (report
ids 1/2), never `v.oai.hid`, so **FreeMicro cannot bind it** — it is the one
input on the device we do not own.

> **`act` on encoder rotation is not reliably `1`.** Detent events have been
> seen carrying other `act` values. Since a tick is momentary and has no
> matching release, treat `ENC_CW`/`ENC_CC` as firing on *any* `act` and keep the
> press/release filter for the real keys. Filtering ticks on `act == 1` silently
> swallows every dial turn, which looks exactly like a dead dial.

The encoder reports **discrete tick events**, not an analogue position. `v.oai.rad`
is a **separate** analogue thumbstick: it sweeps `a` (angle) with `d` (distance)
and returns to exactly `{"a":0,"d":0}` on release, which is how it is
distinguished from the dial.

> The keys do **not** emit ordinary keyboard scancodes. Nothing happens unless
> software listens on this channel — which is why the pad appears inert without
> the vendor app.

## Host -> device methods

### Complete base method table

| Method | Purpose |
|---|---|
| `sys.version` | firmware version |
| `sys.bootloader` | ⚠️ reboots into DFU — **the device disconnects**. Never call during normal operation; gate behind an explicit confirmation if you expose it at all |
| `sys.selftest` | enter self-test / diagnostics |
| `device.status` | `{version, profile_index, layer_index, battery, is_charging}` (read-only) |
| `lights.preview` | documented as real-time lighting — but **does nothing on v0.4.1**; use `v.oai.rgbcfg` |
| `ui.active_screen` | query active screen |
| `ui.home_accent_color` | home-screen accent colour |
| `mp.write_info` / `mp.write_artwork` | media metadata / artwork |
| `fs.list` / `fs.read` / `fs.write` / `fs.writebin` / `fs.readbin` / `fs.delete` | device filesystem |
| `host.focused_app` | tell the device which desktop app is focused |
| `wlsdk.<name>` | custom-widget messages |

Legacy text protocol equivalents also exist: `version`, `bootloader`, `selftest`.

### Vendor (OpenAI) methods — ✅ these are the ones that work

| Method | Params | Effect |
|---|---|---|
| **`v.oai.rgbcfg`** | `{ambient:{…}, keys:{…}}` | **The underglow/chassis glow + key backlight.** Verified visually. ACKs `{"ok":1}` |
| **`v.oai.thstatus`** | `[{id, c, b, e, s, sk, sa}, …]` | **Per-Agent-Key colour** (ids 0–5). Verified visually |

> ### ⚠️ Use `v.oai.*`, not `lights.preview`
>
> `lights.preview` is a real method in the firmware's table and returns
> `{"result": null}` — but on this device **it produces no visible change**,
> for either zone, over USB or Bluetooth, with the vendor app quit.
> `v.oai.rgbcfg` drives the glow; `v.oai.thstatus` drives the Agent Keys.
> This matches the vendor app, which calls the two `v.oai.*` methods and
> **never** calls `lights.preview`.
>
> An earlier revision of this document claimed the opposite — that `rgbcfg`
> acknowledged without doing anything, so `lights.preview` should be used.
> That was wrong. The original `rgbcfg` test predated the discovery that
> Bluetooth needs 64-byte report-id-prefixed framing, so those writes were
> malformed and silently discarded; a false conclusion was then generalised
> from them. Verify lighting by **eye**, and only after confirming the framing
> with a `device.status` round trip.

### `lights.preview` — documented, but inert on this firmware

```json
{"m":"lights.preview","p":{
  "backlight":{"effect":1,"brightness":1.0,"speed":0,"color":16711680},
  "underglow":{"effect":1,"brightness":1.0,"speed":0,"color":16711680}},"id":1}
```

`backlight` = under the keycaps, `underglow` = the base/underside strip. Unlike
the `v.oai.*` calls these use **full field names**: `effect` (required),
`brightness` 0–1, `speed` 0–1, `magic` 0–1, `color` packed `0xRRGGBB` integer.
Replies `{"result": null}`.

> **Verified on hardware, over USB *and* Bluetooth:** `lights.preview` drives the
> base, and `v.oai.thstatus` sets each of the six Agent Keys independently. Both
> confirmed visually. Note that each call *replaces* the previous state, so a
> rapid sequence looks like only its final frame — hold a colour for a second
> or two when testing by eye.

> ### 🔶 Open question: `rgbcfg` vs `lights.preview`
>
> Our hardware testing found `v.oai.rgbcfg` acknowledges (`{"ok":1}`) without any
> visible change, which is why FreeMicro drives lighting through
> `lights.preview` + `v.oai.thstatus`. But analysis of the vendor app finds it
> uses `v.oai.rgbcfg` + `v.oai.thstatus` for **all** lighting and never calls
> `lights.preview` at all.
>
> Both cannot be the whole truth. The likeliest explanation is a false negative
> on our side: each lighting call replaces the last, so a fast test sequence
> shows only its final frame — a mistake this project made repeatedly.
>
> **Unresolved.** FreeMicro defaults to the path it has actually watched work,
> and switching is one config key (`lighting.method: "rgbcfg"`). Do not treat
> either as settled until someone runs an isolated, slow, single-write probe.

### `v.oai.*` lighting side object

The vendor methods use **minimized** field names, unlike `lights.preview`:

| Field | Meaning |
|---|---|
| `e` | effect (see enum) |
| `b` | brightness, `0`=off … `1`=full |
| `s` | speed, `0`=stopped … `1`=fast |
| `c` | color as an **integer** `0xRRGGBB` |
| `m` | "magic" (purpose not yet characterized) |

In `v.oai.rgbcfg` the two sides are `ambient` (outer/base) and `keys` (under the
keycaps). A `v.oai.thstatus` entry adds `id` (Agent-Key index 0–5), `sk` (sync
keys backlight, 0/1) and `sa` (sync ambient, 0/1).

### Effects

| Value | Effect |
|---|---|
| 0 | off |
| 1 | solid |
| 2 | snake |
| 3 | rainbow |
| 4 | breath *(firmware default — the "breathing" seen when idle)* |
| 5 | gradient |
| 6 | shallowBreath (0.5 -> 1 brightness) |

### Example — stored config

```json
{"m":"v.oai.rgbcfg","p":{
  "ambient":{"e":1,"b":1,"s":0,"c":16711680},
  "keys":   {"e":1,"b":1,"s":0,"c":16711680}}}
```
Device acknowledges with `{"result":{"ok":1},"id":null,"method":"v.oai.rgbcfg"}`
— but **nothing visibly changes**. Treat `rgbcfg` as stored configuration and
use `lights.preview` (plus `v.oai.thstatus` for the Agent Keys) for live state.

## How FreeMicro implements this

| Concern | Module |
|---|---|
| Transport, framing, IOKit | `src/freemicro/device/codex_micro.py` |
| Lighting message builders | `src/freemicro/device/lighting.py` |
| Key/joystick routing | `src/freemicro/input/bridge.py` |
| Agent-state LEDs | `src/freemicro/renderers/micro_leds.py` |

Framing and every lighting message are pure functions, so the exact bytes are
asserted in `tests/test_protocol.py` with no hardware attached.

## Safety

- ⚠️ **Never call `sys.bootloader`** in normal operation: it reboots the pad into
  DFU and disconnects it. Gate it behind an explicit confirmation if you expose
  it at all.
- `fs.write` / `fs.delete` touch the device filesystem and have **no documented
  restore path** on this silicon. FreeMicro leaves them out of scope.
- The IOKit input-report callback plus a CFRunLoop is a real crash surface: if
  the pad drops mid-callback and the ctypes callback has been collected, the
  interpreter segfaults. Keep a strong module-level reference to it, never let an
  exception escape the callback, and prefer **fire-and-forget writes with no
  callback and no run loop** for send-only paths.

*Everything above is a description of observed behaviour on hardware we own,
written for interoperability. No vendor source code is reproduced or vendored
in this repository.*
