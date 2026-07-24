# Factory defaults of the Codex Micro

> What OpenAI/Work Louder actually ship, so FreeMicro can match it out of the box.
> Extracted 2026-07-23 from the ChatGPT desktop app bundle
> (`/Applications/ChatGPT.app/Contents/Resources/app.asar`, 197,636,359 bytes,
> mtime 2026-07-22) on a machine the project owner owns.
>
> **These are interface facts and default values recorded for interoperability.**
> No vendor source code, comments, or files are reproduced or vendored here. Where
> a literal constant is quoted it is the minimum needed to interoperate — a colour
> integer, a timeout, an enum value. The vendor implementation is marked
> proprietary Work Louder material; treat it as read-only reference, never as a
> source to copy from.
>
> Companion docs: [`PROTOCOL.md`](PROTOCOL.md) (the wire format, verified on
> hardware) and [`LED-STRATEGY.md`](LED-STRATEGY.md) (how we got here).

## How to re-verify any claim in this file

Byte offsets below are absolute offsets into `app.asar`. `grep` will hang on this
file; use Python `bytes.find()` instead:

```python
d = open("/Applications/ChatGPT.app/Contents/Resources/app.asar","rb").read()
i = d.find(b"function jf(e){switch(e){case`working`")   # -> 4795660
print(d[i-200:i+600].decode("utf-8","replace"))
```

The archive is a standard asar: 16-byte header, `<I` at offset 12 gives the JSON
index size, index follows, file data starts after it. Extracting the index lets
you pull individual files (`/.vite/build/codex-micro-service-*.js`,
`/node_modules/@worklouder/device-kit-oai/**`, `/webview/assets/codex-micro-*.js`)
and read them directly, which is what was done for everything below.

Offsets are from **this** build. They will move on the next app update; the
anchor strings are the durable part.

---

## 1. Summary table — semantic state to lighting

The device has three independent lighting surfaces. The vendor app drives all
three from one derived model, and *most of the pad is dark most of the time*.

### 1a. Agent Keys (`v.oai.thstatus`, one entry per key, ids 0–5)

| Thread status | Onboarding label | Colour (hex) | Colour (int) | Effect | Speed | Brightness |
|---|---|---|---|---|---|---|
| `idle` | White – Idle | `#FFFFFF` | `16777215` | solid (1) | 0 | user brightness |
| `working` | Blue – Thinking | `#304FFE` | `3166206` | solid (1) | 0 | user brightness |
| `unread` | Green – Complete | `#00FF4C` | `65356` | solid (1) | 0 | user brightness |
| `awaiting-approval` | Amber – Requires input | `#FF6D00` | `16739584` | solid (1) | 0 | user brightness |
| `awaiting-response` | Amber – Requires input | `#FF6D00` | `16739584` | solid (1) | 0 | user brightness |
| `error` | Red – Error | `#FF0033` | `16711731` | solid (1) | 0 | user brightness |
| `off` (no thread assigned) | Off – No Assigned Agent | `#000000` | `0` | off (0) | 0 | **0** |

**The selected key is the exception:** whichever Agent Key corresponds to the
currently-selected thread is sent `effect: breath (4)` at `speed: 0.4` instead of
solid/0. Same colour. `syncKeysLighting` and `syncAmbientLighting` are sent as
`0` (false) on every entry, always.

**Confidence: Confirmed.** Colour map at byte `4795660` (main process,
`/.vite/build/src-DVXSULz2.js`) and a byte-identical duplicate at `36559265`
(webview bundle). Thread-lighting builder at byte `2326803`–`2327100` region and
`2342600`+ (`/.vite/build/codex-micro-service-*.js`). Legend strings at
`61787836` (`White – Idle`) and `61788195` (`Green – Complete`).

### 1b. Ambient / underglow (`v.oai.rgbcfg` → `ambient`)

| Condition | Colour | Effect | Speed |
|---|---|---|---|
| Voice `recording` | `#2E8B57` / `3050327` | snake (2) | 0.4 |
| Voice `processing` | `#FFFFFF` / `16777215` | snake (2) | 0.4 |
| Voice `completed` | `#FFFFFF` / `16777215` | solid (1) | 0 |
| Selected thread is `working` | that status' colour (`#304FFE`) | snake (2) | 0.4 |
| Within 4 s of the selection changing | selected status' colour | solid (1) | 0 |
| Otherwise | `#000000` | off (0) | 0 |

Voice states take precedence over thread state. **Confidence: Confirmed** —
ambient builder at byte `2342600`+; voice colours literal at byte `2342833`.

### 1c. Key backlight (`v.oai.rgbcfg` → `keys`)

| Condition | Colour | Effect | Speed |
|---|---|---|---|
| Within 4 s of the selection changing | same colour as ambient at that moment | solid (1) | 0 |
| Otherwise | `#000000` | off (0) | **0** |

**The key backlight is off by default and essentially always.** It only lights as
a brief confirmation flash when you change which thread is selected. This is the
single most surprising factory behaviour and the easiest one for FreeMicro to get
wrong by "helpfully" lighting the keys. **Confidence: Confirmed** (byte
`2342600`+).

### 1d. The "all off" payload

Used for the keys side whenever the selection flash is not showing, for the
ambient side when nothing is happening, and for the whole device on auto-dim and
on app quit:

```json
{"e": 0, "b": 0, "s": 0, "m": 0, "c": 0}
```

**Confidence: Confirmed** — object literal at byte `2326882`.

---

## 2. How a thread's status is derived

The colour map is keyed on a status string that the app computes per thread.
Reproducing the derivation matters more than the colours, because it decides
*when* each colour appears.

For a **local** (on-device Codex) thread, in this precedence order:

1. `status === "error"` → `error`
2. pending chip is an approval request → `awaiting-approval`
3. pending chip is a response request → `awaiting-response`
4. `status === "loading"` → `working`
5. thread has unread output → `unread`
6. otherwise → `idle`

For a **remote** (cloud task) thread:

1. latest turn status `failed` → `error`
2. latest turn status `pending` or `in_progress` → `working`
3. task has an unread turn → `unread`
4. otherwise → `idle`

No thread assigned to the slot → `off`.

**One override:** if a slot is the selected thread **and** the app window is
focused **and** the derived status would be `unread`, it is downgraded to `idle`.
Rationale is obvious — you're looking at it, so it isn't unread.

**Confidence: Confirmed** — `/webview/assets/codex-micro-slot-signals-*.js`,
status resolver and slot builder.

Note that "Green – Complete" in the onboarding legend is the `unread` status, not
a distinct "done" state. Green means *finished and you haven't looked at it yet*.
It disappears the moment you look. FreeMicro's `done` state should map to
`unread` (green) and should clear the same way, or the pad will sit green forever.

**Also note:** `pulsing` is a field on the slot model that the thread-lighting
builder honours (it forces breath, same as `selected`), but **nothing in this
build ever sets it to true**. It is dead in the shipping app. Confidence:
Confirmed by exhaustive search — 13 occurrences of `pulsing` in the archive, all
either this consumer, the model serializer, or unrelated CSS class names.

---

## 3. Default brightness

| Fact | Value | Confidence |
|---|---|---|
| Setting key | `codex-micro-lighting-brightness` | Confirmed (byte `4894277`) |
| Schema | integer, min 0, max 100 | Confirmed (byte `4795620` region) |
| Default | **100** | Confirmed (byte `4894277`) |
| Sent to device as | `brightness / 100` → float **1.0** | Confirmed (slot-signals lighting model) |
| UI label | "Brightness" / "Adjusts the brightness of all Codex Micro lighting" | Confirmed |

So the slider is 0–100 in the UI and a 0–1 float on the wire, exactly as the
protocol's `b` field expects. Factory default is full brightness.

The same brightness value is applied to the Agent Keys, the ambient ring and the
key backlight — there is no per-zone brightness. Slots with status `off` are sent
`brightness: 0` regardless of the setting.

`L = 100` in the service is **not** brightness. It is a 100 ms input-quiet
debounce: while HID or joystick events are arriving, lighting writes and battery
polls are deferred and coalesced, then flushed 100 ms after the last input.
**Confidence: Confirmed** (byte `2326803` for the constant block, deferral logic
immediately below).

---

## 4. Auto-dim

| Fact | Value | Confidence |
|---|---|---|
| Setting key | `codex-micro-lighting-auto-off` | Confirmed (byte `4894455`) |
| UI label | "Auto-dim" | Confirmed |
| Default | **`3-minutes`** (180000 ms) | Confirmed (byte `4894455`) |

Full option list and millisecond values (byte `36559662`, **Confirmed**):

| Option | UI label | ms |
|---|---|---|
| `off` | Off | `null` (never dim) |
| `30-seconds` | 30 seconds | 30000 |
| `1-minute` | 1 minute | 60000 |
| `3-minutes` | 3 minutes | 180000 |
| `10-minutes` | 10 minutes | 600000 |
| `30-minutes` | 30 minutes | 1800000 |
| `1-hour` | 1 hour | 3600000 |

### What "dim" actually sends

Not a brightness reduction. **Two full-off writes:**

1. `v.oai.rgbcfg` with both `keys` and `ambient` set to the all-off payload from §1d.
2. `v.oai.thstatus` with all six slots at `{c:0, b:0, e:0, s:0, sk:0, sa:0}`.

The device goes completely dark. **Confidence: Confirmed** — the dim path calls
the same two builders used on shutdown, with the slot list forced to the initial
all-`off` state and brightness `0`.

The app also clears its "last applied config" cache before dimming, so the next
wake always re-sends in full rather than being deduplicated away.

### What wakes it

Three things, all of which reset the inactivity timer:

1. **Any HID key event** from the pad (any key, any Agent Key, the encoder).
2. **Joystick movement with `distance > 0.1`.** Below that it does not count as
   activity.
3. **Any change to the lighting model itself** — i.e. a thread changing status,
   the selection moving, voice state changing, a brightness/auto-dim setting
   change. This matches the UI copy: "Turns lighting off after inactivity and back
   on when you use Codex Micro or an agent key changes color or state."

On wake the app re-applies the last lighting model in full and restarts the timer.
If a wake write fails, the dim timer is rescheduled rather than the state being
left inconsistent.

**Confidence: Confirmed** — activity threshold literal `R=.1` at byte `2326867`;
wake/dim state machine in the same file; UI copy at byte `16474138`.

---

## 5. Default knob (encoder) behaviour

The encoder is a **separate input surface from the thumbstick**. It reports as
three key ids on `v.oai.hid`, exactly like the keys. `v.oai.rad` is the analogue
thumbstick and never carries encoder data — see §6.

| Key id | Event | Meaning | Default binding (`composer-navigation`) |
|---|---|---|---|
| `ENC_CW` | `act: 2` | one detent clockwise | synthesise `ArrowUp` — "previous control or option" |
| `ENC_CC` | `act: 2` | one detent counter-clockwise | synthesise `ArrowDown` — "next control or option" |
| `ENC_CLK` | `act: 1` then `act: 0` | press / release | `Enter` on release; hold 500 ms → open Codex Micro settings |

Rotation is a discrete `act: 2` pulse — there is no press/release pair for turns,
and `ENC_CW`/`ENC_CC` are explicitly excluded from the press/release decoder.
**Confidence: Confirmed** (bridge event decoder,
`/webview/assets/codex-micro-bridge-*.js`).

### The app treats rotation as discrete increments, with no acceleration

This was checked specifically. **Confidence: Confirmed** by exhaustive search of
every Codex Micro bundle for acceleration, velocity, detent-accumulation or
rate-limiting logic on the encoder path — there is none.

- **One detent → exactly one synthetic keystroke.** No 1:N multiplication, no
  scaling by turn rate, no accumulator that fires on threshold.
- **No debounce on the action path.** A fast spin produces a fast burst of
  `ArrowUp`/`ArrowDown` events with nothing dropped or coalesced. Whatever
  smoothing exists is in the firmware's detent detection, not the host.
- **The only 180 ms timer is cosmetic.** After the last rotation event a 180 ms
  timer clears an on-screen "knob pulse" animation in the settings preview. It
  does not gate, delay, or suppress any input. Do not mistake it for a debounce.
- **No direction latch or reversal hysteresis.** Alternating CW/CC detents produce
  alternating keystrokes immediately.

FreeMicro should mirror this: map each `ENC_CW`/`ENC_CC` event to one action and
resist the temptation to add acceleration. It would be a deviation from factory
feel, and the pad's own detents already provide the tactile rate limit.

### Modes

| Fact | Value | Confidence |
|---|---|---|
| Enum | `composer-navigation` \| `reasoning` | Confirmed (byte `4796400` region) |
| **Default** | **`composer-navigation`** | Confirmed |
| Stored in | `codex-micro-layout` → `encoderMode` | Confirmed |

> The brief named "Reasoning only" as the default. It is **not** — it is the
> second option. The factory default is `composer-navigation`. "Reasoning only"
> is the `reasoning` mode, UI-labelled *"Reasoning only" / "Open and adjust
> reasoning effort"* (string ids `settings.codexMicro.knob.reasoning*`, byte
> `16473694`). Both are documented below since either could be what a user means
> by "the factory knob".

### `composer-navigation` (default)

| Gesture | Behaviour |
|---|---|
| Turn clockwise (`ENC_CW`) | "Move to the previous control or option" — synthesises `ArrowUp` |
| Turn counter-clockwise (`ENC_CC`) | "Move to the next control or option" — synthesises `ArrowDown` |
| Click | "Open or select the highlighted control" — synthesises `Enter` |
| Press and hold | Open Codex Micro settings |

Yes, clockwise maps to `ArrowUp` / *previous*. That is what the code and the UI
strings both say. **Confidence: Confirmed** (direction mapping in bridge; label
strings at byte `16473694`+).

### `reasoning` ("Reasoning only")

| Gesture | Behaviour |
|---|---|
| Turn clockwise | "Decrease reasoning effort" (opens the model picker with `powerSelectionDirection: decrease`) |
| Turn counter-clockwise | "Increase reasoning effort" (`powerSelectionDirection: increase`) |
| Click | "Open the slider or advanced options" |
| Press and hold | Open Codex Micro settings |

**Confidence: Confirmed** (byte `16473694`+ for labels, bridge for the dispatch).

### Timing constants

| Constant | Value | Meaning | Confidence |
|---|---|---|---|
| Long-press threshold | **500 ms** | Hold `ENC_CLK` this long → navigate to Codex Micro settings. Fires on a timer *during* the hold; the subsequent release is swallowed. | Confirmed (byte `61694421`) |
| Rotation pulse debounce | **180 ms** | After the last `act: 2` rotation event, a UI "knob pulse" indicator is cleared. | Confirmed (bridge) |
| Menu auto-dismiss | **1500 ms** | In `reasoning` mode, after interacting with the reasoning menu, `Escape` is sent after this idle period. | Confirmed (byte `61694380`) |
| Composer highlight lifetime | **2000 ms** | How long the navigation highlight stays on a control. | Confirmed (bridge) |
| Command feedback delay | **220 ms** | Delay before reporting triggered/unavailable status for a joystick-fired command. | Confirmed (byte `61694380`) |

There is **no** encoder tap/double-tap window. Click is a single discrete action.

---

## 6. Default analog stick bindings

The analogue thumbstick is a **wholly separate input surface from the encoder**
(§5). It reports `v.oai.rad` → `{a: angle, d: distance}`, both normalised 0–1.
Angle is in turns, not degrees. Hardware testing confirms it returns to exactly
`{"a": 0, "d": 0}` on release, which means the resting position sits inside every
deadzone below and no explicit centre-calibration is needed.

### Default mapping

| Direction | Command id | Confidence |
|---|---|---|
| Up | `composer.togglePlanMode` | Confirmed (byte `4796478`, duplicate at `36560422`) |
| Right | `navigateForward` | Confirmed |
| Down | `toggleSidebar` | Confirmed |
| Left | `navigateBack` | Confirmed |

Each direction may alternatively hold a `{type: "skill", skillName, skillPath}`
binding, or `null` for unassigned. The four above are the shipped defaults.

### Thresholds

| Constant | Value | Purpose | Confidence |
|---|---|---|---|
| **Action deadzone** | `distance < 0.5` → no direction | The command only fires past half deflection. | Confirmed (byte `26063949`) |
| **HUD deadzone** | `distance < 0.1` → on-screen HUD hidden | Confirmed (byte `61700745`) |
| **Lighting-wake threshold** | `distance > 0.1` | Confirmed (byte `2326867`) |
| **Suppression release** | `distance <= 0.1` | After a gesture is consumed, the stick must return inside 0.1 before it re-arms. | Confirmed (bridge) |

### Angle sectors

Evaluated in this order once `distance >= 0.5` (byte `26063659`, **Confirmed**):

| Sector | Angle range (turns) |
|---|---|
| up | `0.625 <= a < 0.875` |
| down | `0.125 <= a < 0.375` |
| left | `0.375 <= a < 0.625` |
| right | everything else (`a >= 0.875` or `a < 0.125`) |

Note the asymmetry: `right` is the fall-through, so it also catches out-of-range
angles. Each sector is a clean 0.25-turn quadrant with no hysteresis band.

### Edge-triggered, not repeating

A command fires **once**, on the transition into a new direction sector. Holding
the stick does not repeat, and returning through the same sector without leaving
it does not re-fire. **Confidence: Confirmed** (bridge: action is produced only
when the resolved direction is non-null *and* differs from the previous frame's
direction).

The joystick HUD stays visible for **600 ms** after the last event.

---

## 7. Default keycap / glyph assignments

### Physical key ids

- Agent Keys: `AG00`–`AG05` (six, top row, frosted)
- Action keys: `ACT06`, `ACT07`, `ACT08`, `ACT09`, `ACT10`, `ACT11`, `ACT12`
- Encoder: `ENC_CW`, `ENC_CC`, `ENC_CLK`

`ACT10` and `ACT11` are the two halves of one **double-width** slot, addressed in
software as `ACT10_ACT11`. Only `ACT10` produces an action; `ACT11` events are
discarded. **Confidence: Confirmed** (bridge decoder + layout slot resolver).

### Shipped default layout

`codex-micro-layout`, version 1 (byte `4796908`, duplicate at `36560852`,
**Confirmed**):

| Slot | Keycap | Resulting action |
|---|---|---|
| `ACT06` | `FAST` | `composer.toggleFastMode` |
| `ACT07` | `APPR` | `approval.approve` |
| `ACT08` | `REJ` | `approval.decline` |
| `ACT09` | `SPLIT` | `forkThread` |
| `ACT10_ACT11` | `MIC` | push-to-talk (special-cased, see §8) |
| `ACT12` | `CODEX` | `composer.submit` |

### Full glyph catalogue

All 37 keycap ids with their icon and default action (byte `61743514`,
**Confirmed**). `custom-shortcut` means the glyph has no built-in action — it is
a blank the user binds a command to.

| Keycap | Icon | Size | Default action |
|---|---|---|---|
| `FAST` | lightning-outline | single | `composer.toggleFastMode` |
| `APPR` | check-circle | single | `approval.approve` |
| `REJ` | x-circle | single | `approval.decline` |
| `SPLIT` | branch | single | `forkThread` |
| `MIC` | mic | **double** | push-to-talk (label "Push to talk") |
| `CODEX` | codex | single | `composer.submit` |
| `BUG` | bug | single | `feedback` |
| `OAI` | openai | single | open `https://developers.openai.com` |
| `TERM` | terminal | single | `toggleTerminal` |
| `DWN` | download | single | `copyConversationMarkdown` |
| `DEL` | trash | single | `archiveThread` |
| `NEW` | compose | single | `newTask` |
| `NAV` | pointer-outline | single | `openBrowserTab` |
| `MAGIC` | star | single | `toggleThreadPin` |
| `DIFF` | diff | single | `toggleReviewTab` |
| `PLAY` | play-outline | single | `environmentAction1` |
| `GIT` | diff | single | `git.commit` |
| `BRCH` | pull-request-draft | single | `toggleReviewTab` |
| `MRG` | pull-request-merged | single | `toggleReviewTab` |
| `PR` | pull-request | single | `git.createPullRequest` |
| `PAINT` | paint | single | `composer.addPhotos` |
| `LAB` | flask | single | `settings` |
| `PARTY` | confetti | single | `openSideChat` |
| `TIME` | clock | single | `manageTasks` |
| `MIND+` | brain-medium | single | `composer.increaseReasoningEffort` |
| `MIND-` | brain-outline | single | `composer.decreaseReasoningEffort` |
| `EMPT1` | empty | single | none (`custom-shortcut`) |
| `EMPT2` | empty | single | none (`custom-shortcut`) |
| `EMPT3` | empty | single | none (`custom-shortcut`) |
| `EMPT4` | empty | single | none (`custom-shortcut`) |
| `SETUP` | settings | single | `settings` |
| `FOLD` | folder-plus | single | `openFolder` |
| `UPL` | cloud-upload | single | `composer.addFiles` |
| `APPS` | all-products | single | `openSkills` |
| `YOLO` | empty | single | types `:yolo:` into the composer |
| `YEET` | empty | single | types `:yeet:` into the composer |
| `EMPT5` | empty | **double** | none (`custom-shortcut`) |

Note: the task brief guessed `PR` → "Create PR". Confirmed correct — the command
id is `git.createPullRequest`.

Only `MIC` and `EMPT5` are double-width, so `ACT10_ACT11` can only ever hold one
of those two.

### Resolution rules

1. A per-slot `commandId` override, if set, wins over the keycap's built-in action.
2. `MIC` **always** resolves to push-to-talk, ignoring any override.
3. Assigning a keycap that already occupies another slot **swaps** the two slots.
4. Action keys fire on key-down (`act: 1`) only. Push-to-talk is the sole
   exception — it uses both down and up.

**Confidence: Confirmed** (layout module action resolver and slot-assignment
helper).

### Agent-key source

| Fact | Value | Confidence |
|---|---|---|
| Setting key | `codex-micro-agent-source` | Confirmed (byte `4893990`) |
| Options | `pinned`, `recent`, `priority`, `custom` | Confirmed |
| **Default** | **`recent`** | Confirmed |

`recent` takes the six most-recently-updated threads (pinned threads and pinned
project threads are merged in, then the whole set is sorted by update time
descending, then sliced to 6). Slot index 0–5 maps to `AG00`–`AG05` in order.

---

## 8. Agent-key and MIC tap semantics

### Agent Keys (`AG00`–`AG05`)

Fires on key-down only. Behaviour:

| Gesture | Behaviour |
|---|---|
| **Single tap** | Select that slot's thread and open it *in the background* — the ChatGPT window is not raised. |
| **Double tap** | Same, plus raise/focus the ChatGPT window. |

Double-tap window: **350 ms**, and it must be the *same slot* **and** the *same
thread key*. Both conditions are required — tapping two different keys quickly is
never a double tap. **Confidence: Confirmed** (constant at byte `61677742`;
comparison in the bridge's tap resolver).

Onboarding copy corroborates: "Single tap a key to focus it in the background, or
double tap to bring Codex window to the front and center." (byte `61784803`).

**Context override:** while a menu, listbox or the add-context popover is open,
all Agent Key presses are swallowed *except* `AG00`, which sends `Escape`. During
that state the app also repaints the pad — see §9d. **Confidence: Confirmed.**

**Unassigned slot, `custom` source:** pressing an empty Agent Key while agent
source is `custom` creates and assigns a new thread to that key. Under the
default `recent` source, pressing an empty key does nothing.

### MIC / push-to-talk (`ACT10`)

A four-state machine, all thresholds **350 ms** (the same constant as the
double-tap window). UI copy: "Hold to record. Double-tap to keep recording; tap
again to stop" (byte `16471763`).

| From | Event | To | Emits |
|---|---|---|---|
| idle | press | pressed | **start** |
| pressed | release after **>= 350 ms** | idle | **stop** (classic hold-to-talk) |
| pressed | release before 350 ms | waiting-for-second-press (deadline = press + 350 ms) | — |
| waiting | 350 ms elapses | idle | **stop** |
| waiting | press | latched (keeps recording indefinitely) | — |
| latched | press | suppressing (350 ms) | **stop** |
| suppressing | press before deadline elapses | suppressing | — |
| suppressing | press after deadline | pressed | **start** |

**Confidence: Confirmed** (bridge state machine; constant at byte `61677742`).

### Voice state → lighting

The voice state that drives ambient lighting (§1b) is a separate small machine:

| State | Trigger |
|---|---|
| `recording` | dictation active |
| `processing` | transcription active |
| `completed` | dictation finished — held for **1000 ms** |
| `idle` | otherwise; `completed` decays to this after 1000 ms |

**Confidence: Confirmed** (byte `21259070`).

---

## 9. Connect / disconnect behaviour

### Discovery

| Fact | Value | Confidence |
|---|---|---|
| VID | `12346` = `0x303A` | Confirmed (byte `2326034`) |
| PID | `33632` = `0x8360` | Confirmed (same) |
| HID usage page filter | `65280` = `0xFF00` | Confirmed (same) |
| Device type reported to the kit | `Project2077` | Confirmed |
| Layout type | `universal` | Confirmed |

This independently corroborates the VID/PID/usage-page in
[`PROTOCOL.md`](PROTOCOL.md) from the vendor side.

On macOS the app does **not** use `node-hid`'s enumerator. It ships a native
addon (`hid-topology-watcher.node`) exposing `findCodexMicroInterfaces()` and a
topology-change watcher — the same macOS HID-collection problem `PROTOCOL.md`
documents, solved the same way (go under hidapi). On Linux it falls back to
`node-hid`'s `devicesAsync(VID, PID)` filtered on usage page `0xFF00`.

The HID handle is opened **non-exclusively on macOS**. That means FreeMicro and
the ChatGPT app *can* both hold the device open — and will then fight over the
LEDs, each overwriting the other. Quitting the ChatGPT app remains mandatory.
**Confidence: Confirmed** (device-kit transport open path).

### On connect

There is **no handshake, no capability negotiation, and no lighting reset**. The
sequence is:

1. Open the HID handle.
2. Subscribe to `v.oai.hid` and `v.oai.rad` notifications.
3. Mark the pad as "currently dark" internally, then **immediately push the
   current lighting model** — one `v.oai.rgbcfg` and one `v.oai.thstatus`.
4. Poll `device.status` for battery, then every **60000 ms** thereafter.

So the app *does* claim the lighting on connect, unconditionally, with whatever
state it computes at that moment. **Confidence: Confirmed** (byte `2332842`).

### On graceful stop (app quit, device settings teardown)

Explicitly blanks the pad before disconnecting:

1. `v.oai.rgbcfg` with keys and ambient both all-off (§1d).
2. `v.oai.thstatus` with all six slots at `{c:0, b:0, e:0, s:0, sk:0, sa:0}`.
3. Then disconnect.

**Confidence: Confirmed** (byte `2330636`).

### On unexpected disconnect (cable pulled)

Nothing is sent — it can't be. Internal state is reset and reconnect is scheduled.
Whatever was last written stays on the LEDs until the firmware decides otherwise.

### Reconnect / rescan timing

| Constant | Value | Purpose | Confidence |
|---|---|---|---|
| Transport reconnect backoff | `[1000, 2000, 5000, 10000]` ms, clamped at the last | After a transport failure | Confirmed (byte `2326803`) |
| Topology settle retries | `[250, 1000, 3000]` ms | After a USB topology change, re-scan on this ladder | Confirmed (same) |
| Topology fallback poll | `30000` ms | Only if the native watcher fails to load | Confirmed (same) |
| Battery poll | `60000` ms | Confirmed (same) |
| Selection-flash window | `4000` ms | How long the key backlight / ambient selection flash lasts | Confirmed (same) |
| Input-quiet debounce | `100` ms | Coalesce lighting writes while keys are being hammered | Confirmed (same) |

### Write discipline

Every lighting write goes through a serialised promise queue — never two in
flight. The app also caches the JSON of the last successfully-applied `rgbcfg`
and `thstatus` payloads and **skips the write entirely if nothing changed**. This
is worth copying: it is why the pad doesn't flicker under rapid state churn.

### Non-thread lighting overrides

Three states replace the normal model. Documented for completeness; FreeMicro has
no equivalent for the first two.

| State | Slots | Ambient |
|---|---|---|
| Onboarding animation running | scripted demo cycle | normal |
| Mini-game active | game state | snake, `error` red, brightness as set |
| Knob composer-navigation, control **selected** | unchanged | snake, `working` blue |
| Knob composer-navigation, surface **open** | all off **except `AG00` = `error` red** | snake, `unread` green |

That last row is the visual pair to the `AG00`-is-Escape rule in §8: when a menu
is open, `AG00` turns red to advertise itself as the back-out key.
**Confidence: Confirmed** (bridge lighting-model watcher).

---

## 10. Battery, charging, and transport — do they change the lighting?

Asked explicitly because the pad is battery-powered and untethered, and auto-dim
looks power-motivated. Both answers are clean negatives, and both are worth
stating plainly so nobody re-investigates.

### 10a. Battery and charging state: **no effect on lighting whatsoever**

**Confidence: Confirmed** — by exhaustive search of every Codex Micro bundle for
`battery`, `batteryPercentage` and `isCharging`. Every single occurrence is one
of: the `device.status` poll, the internal device-state object, the settings-page
readout, or a React memo key. **None of them is ever read by any lighting code.**

| Question | Answer |
|---|---|
| Does the app dim harder on battery? | **No.** Auto-dim is one timeout from one setting. It does not consult battery level or charging state. |
| Is there a low-battery indication on the pad? | **No.** No colour, no effect, no flash. |
| Does charging change anything on the pad? | **No.** |
| Does brightness scale with battery? | **No.** Brightness is the user setting, full stop. |

The **only** battery-conditional colour anywhere in the app is on screen, not on
the pad: the settings page renders the battery percentage in the error/red text
token when it is `<= 20%` **and** not charging. That is a CSS class on a `<span>`
in the app window. It never reaches the device.

So the answer to "is auto-dim power-motivated?" is: probably, in intent — but it
is implemented as a flat inactivity timeout with no power awareness at all.
FreeMicro can implement auto-dim exactly as §4 describes and be factory-correct
on battery and on USB alike.

`device.status` does expose `battery` and `is_charging`, and the app polls them
every 60 s purely to render the settings readout. If FreeMicro wants a
low-battery cue on the pad, that is a **new feature**, not factory parity, and it
should be off by default.

### 10b. Transport: lighting is **not** USB-conditioned

**Confidence: Confirmed.** The device kit computes an `isUsbConnection` flag
(from the HID release field — `(release & 3) === 0`) and stores it on every
discovered device record. It is **never read** by the Codex Micro service, the
lighting builders, the bridge, or anything on the lighting path. Grep the whole
archive: four occurrences, all either the type declaration or the two places that
*write* it. Nothing consumes it.

This matches the hardware finding that BLE round-trips input, `lights.preview`,
`v.oai.thstatus` and `device.status` just as USB does. Every default in this
document applies identically over BLE and USB. The only difference is write
framing, which is a transport-layer concern and is documented in
[`PROTOCOL.md`](PROTOCOL.md) (USB: 63-byte buffer, no prefix; BLE: 64-byte buffer
with report id `0x06` prefixed).

Two practical consequences for FreeMicro:

1. **Do not gate any lighting behaviour on transport.** Same colours, same
   timings, same dim policy, either way.
2. **Do not assume USB when choosing a discovery filter.** The vendor's own
   discovery matches on VID/PID/usage-page only and treats the transport flag as
   metadata.

---

## 11. Persistence across power cycle

| Claim | Status |
|---|---|
| `lights.preview` is applied immediately and **not** persisted to flash | **Confirmed** — vendor API doc comment at bytes `12388648` and `12714664` |
| The app never uses `lights.preview` for Codex Micro at all | **Confirmed** — the entire Codex Micro lighting path is `v.oai.rgbcfg` + `v.oai.thstatus`; no call to `sendLightingPreview` exists in the Codex Micro service |
| Whether `v.oai.rgbcfg` persists to flash | **Unknown** — the vendor docs are silent. The method is named "config" and is the counterpart to a "preview", which *suggests* persistence, but that is inference, not evidence |
| Whether `v.oai.thstatus` persists | **Unknown**, same |
| What the pad shows with no host software | **Inferred** — `PROTOCOL.md` records breath (effect 4) as the firmware idle default observed on hardware |

> ### Correction needed in `PROTOCOL.md`
>
> `PROTOCOL.md` currently says `v.oai.rgbcfg` ACKs but produces no visible change,
> and recommends `lights.preview` as the live path. The vendor app does the exact
> opposite: it drives **all** live lighting — keys, ambient, and per-key — through
> `v.oai.rgbcfg` and `v.oai.thstatus`, and never touches `lights.preview`.
>
> Both observations can be true at once. The most likely explanation is that
> `rgbcfg` only takes visible effect for zones the firmware isn't otherwise
> driving, or that the earlier hardware test sent a `keys`/`ambient` combination
> the firmware treats as a no-op. **This needs one more hardware probe** — send
> the exact `rgbcfg` + `thstatus` pair the app sends on connect, in that order,
> and watch. Until then, treat "rgbcfg does nothing" as unverified.
>
> A power-cycle test settles §11 in about a minute: send a distinctive `rgbcfg`,
> unplug, replug with no host software running, and look.

---

## 12. What FreeMicro should default to

### Recommended default config

```yaml
# Factory-parity defaults. Mirrors the Codex Micro's shipped behaviour.
lighting:
  brightness: 1.0            # vendor default 100/100
  auto_dim: 180              # seconds; vendor default "3-minutes"
  auto_dim_action: blank     # full off, not reduced brightness — matches vendor

  # Agent Keys (v.oai.thstatus), ids 0-5
  states:
    idle:      { color: "#FFFFFF", effect: solid,  speed: 0.0 }
    thinking:  { color: "#304FFE", effect: solid,  speed: 0.0 }   # vendor `working`
    done:      { color: "#00FF4C", effect: solid,  speed: 0.0 }   # vendor `unread`
    needs_input: { color: "#FF6D00", effect: solid, speed: 0.0 }  # vendor `awaiting-*`
    error:     { color: "#FF0033", effect: solid,  speed: 0.0 }
    unassigned:{ color: "#000000", effect: off,    speed: 0.0, brightness: 0.0 }

  # The selected key overrides effect/speed only. Colour is unchanged.
  selected_override: { effect: breath, speed: 0.4 }

  # Ambient / underglow (v.oai.rgbcfg -> ambient)
  ambient:
    default: { effect: off, color: "#000000" }
    when_selected_thinking: { effect: snake, color: "#304FFE", speed: 0.4 }
    selection_flash: { effect: solid, duration_ms: 4000 }

  # Key backlight (v.oai.rgbcfg -> keys)
  keys_backlight:
    default: { effect: off, color: "#000000", brightness: 0.0 }
    selection_flash: { effect: solid, duration_ms: 4000 }   # colour follows ambient

input:
  agent_key_double_tap_ms: 350
  encoder_long_press_ms: 500
  encoder_mode: composer-navigation
  joystick:
    action_deadzone: 0.5
    activity_deadzone: 0.1
    edge_triggered: true
    bindings: { up: plan-mode, right: forward, down: sidebar, left: back }

write_policy:
  input_quiet_debounce_ms: 100
  dedupe_identical_writes: true
  serialise_writes: true
```

Three implementation notes that matter more than the colours:

1. **Dedupe and serialise.** The vendor app hashes the payload and skips
   no-op writes, and never has two writes in flight. Without this the pad
   flickers under fast state churn.
2. **Dim means dark.** Reducing brightness is a *different* behaviour from what
   the factory does and users will notice.
3. **`done` must decay.** Green is `unread`, not "completed". If FreeMicro lights
   green on completion and never clears it, the pad diverges from factory
   behaviour within minutes. Clear it when the user looks at the session.

### Should FreeMicro send lighting on startup?

**Recommendation: no. Leave the pad untouched until the user opts in, once,
explicitly. Then remember the answer and never ask again.**

The honest case for sending on startup is that it is what the vendor does — the
ChatGPT app claims the LEDs the instant it connects, without asking. Factory
parity is the project's stated principle, and this is factory behaviour.

The case against is stronger, for three reasons specific to *us*:

1. **We are not the only claimant.** macOS opens this device non-exclusively.
   A user who still has the ChatGPT app installed and running will get two
   processes writing the same LEDs on a 100 ms debounce, each undoing the other.
   The vendor app can assume it is the only driver. We cannot. Blowing away a
   working setup on first launch is the worst possible first impression, and the
   user has no way to tell which program is at fault.
2. **A silent LED takeover is not reversible by the user.** If FreeMicro paints
   the pad at startup and the user doesn't want that, there is no obvious undo —
   the vendor app's blank-on-quit only runs if *it* was the one that connected.
   The user is left with a pad stuck in our colours and no clue why.
3. **The failure modes are asymmetric.** Not lighting the pad costs one command
   (`freemicro render idle`) and a line in the README. Lighting it uninvited costs
   confusion, a bug report, and possibly a user who concludes the tool is
   invasive. Non-goal violations are cheap to fix; trust is not.

The parity principle is about *what colours mean*, not about seizing hardware on
launch. We honour it by making the opt-in produce output indistinguishable from
factory — same colours, same effects, same speeds, same dim timing, same
blank-on-exit. That is the promise worth keeping.

Concretely:

- **First run:** detect the pad, print what we found, and ask once. Default the
  prompt to "no".
- **After opt-in:** behave exactly as §12's config describes, including
  blanking the pad on clean exit (the vendor does this; it's good manners and it
  hands the device back cleanly).
- **Always:** refuse to drive the pad if the ChatGPT app is running, with a clear
  message saying why. Don't silently fight.
- **Never:** write lighting during `freemicro detect` or any read-only command.

---

## Open questions

| # | Question | How to close it |
|---|---|---|
| 1 | Does `v.oai.rgbcfg` visibly drive the base, contradicting `PROTOCOL.md`? | Send the app's exact connect-time pair (`rgbcfg` then `thstatus`) on hardware and watch |
| 2 | Does `rgbcfg`/`thstatus` survive a power cycle? | Set a distinctive colour, unplug, replug with no host software |
| 3 | What is the `m` ("magic") field for? | The app always sends `0`. Sweep 0–1 on hardware |
| 4 | Firmware idle default with no host | `PROTOCOL.md` says breath (4). Confirm the colour and speed |
| 5 | Does the firmware apply its own fade/ramp on effect changes? | Would explain why 100 ms debouncing is enough for the vendor |
| 6 | Does the *firmware* dim on battery, independently of the host? | The host does not (§10a). If the pad dims itself on battery, that is firmware behaviour and FreeMicro inherits it for free — leave the pad idle on battery and watch |

### Closed by this research

| Question | Answer |
|---|---|
| Exact status → colour integers behind `t.wl(status)` | **Answered.** §1a — all six, integer and hex |
| Does the app accelerate or debounce encoder rotation? | **No.** §5 — one detent, one action, no acceleration |
| Does the app condition lighting on battery or charging? | **No.** §10a — battery is display-only |
| Is any lighting default USB-specific? | **No.** §10b — the transport flag exists but is never read |
| Is there a low-battery colour on the pad? | **No.** §10a — the only battery colour rule is on screen |
| Is `pulsing` ever used? | **No.** §2 — dead field in the shipping build |

---

*Everything above was read from a bundle on hardware and software the project
owner owns, and is recorded as interface facts for interoperability. No vendor
source is reproduced or vendored in this repository.*
