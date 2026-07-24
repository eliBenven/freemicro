# Customizing FreeMicro

Everything the pad does - every key, the joystick, and every LED colour - lives
in one file you own. Nothing here requires editing Python.

```sh
freemicro keys --init     # write a starter config you can edit
freemicro keys --list     # print the resolved config and every option
freemicro keys --dry-run  # press keys and watch what *would* happen
```

## Where the config lives

`freemicro keys --init` writes `~/.freemicro/keymap.json`. FreeMicro looks in
this order and uses the first file it finds:

1. `--config /path/to/file.json` on the command line
2. `$FREEMICRO_KEYMAP`
3. `~/.freemicro/keymap.json`  ← what `--init` writes
4. `$XDG_CONFIG_HOME/freemicro/keymap.json` (defaults to `~/.config/…`)
5. the annotated default shipped inside the package

`freemicro keys --list` always prints which one won, so you never have to guess.

> **Why JSON and not TOML?** FreeMicro's core is dependency-free on Python 3.9,
> and `tomllib` only landed in 3.11 - TOML would force a `tomli` dependency on
> exactly the people least likely to want one. JSON is stdlib everywhere, it is
> already the format of `config.json` and `capabilities.json`, and it
> round-trips losslessly for `--init`. To buy back the readability TOML would
> have given us, the shipped default is heavily commented: `_readme` at the top
> and a `comment` field on any binding, both ignored by the loader.

## Bindings

One entry per input id:

| Input ids | What they are |
|---|---|
| `AG00` - `AG05` | the six top-row Agent Keys. One project each - see [`AGENT-KEYS.md`](AGENT-KEYS.md) |
| `ACT06` - `ACT12` | the seven action keys. On the unit we tested: `ACT06` LAB, `ACT07` PR, `ACT08` NAV, `ACT09` PLAY, `ACT10`+`ACT11` MIC (one double-width keycap over two switches), `ACT12` TERM |
| `ENC_CLK` | dial press |
| `ENC_CW` / `ENC_CC` | dial rotation, one event per detent |
| `JOY_UP` `JOY_DOWN` `JOY_LEFT` `JOY_RIGHT` | thumbstick flicks |

Don't know which physical key is which id? Run `freemicro keys --dry-run` and
press it - the id is printed.

```json
"bindings": {
  "AG00": { "action": "focus_session", "label": "agent 1" },
  "ACT09": { "action": "text", "text": "continue", "submit": true, "label": "play" },
  "ACT10": { "action": "hold", "key": "ctrl+option+cmd+d", "label": "mic" },
  "ACT12": { "action": "app",  "name": "Terminal", "cycle": true, "label": "term" },
  "AG05":  { "action": "none" },
  "AG04":  "/review"
}
```

A plain string is shorthand for `{"action": "text", "text": "…"}`.
`label` names the binding in logs; `comment` is free text (a string, or a list
of lines) and is ignored by the loader; `light` is
[what the pad shows while the key is held](#the-pad-changes-colour-while-a-key-is-held).

### Action kinds

| `action` | Required | Optional | What it does |
|---|---|---|---|
| `text` | `text` | `submit` | Types the text. `submit: true` presses Return after. |
| `key` | `key` | - | Presses a keystroke. |
| `hold` | `key` | - | Holds the key down for as long as you hold the pad key. True push-to-talk. |
| `shell` | `command` | `cwd`, `wait` | Runs a shell command. Fire-and-forget unless `wait: true`. |
| `applescript` | `script` | - | Runs arbitrary AppleScript. The escape hatch. |
| `app` | `name` | `cycle` | Focuses an app. `cycle: true` cycles its windows if it's already frontmost. |
| `focus_session` | - | `slot`, `project`, `fallback` | Raises the terminal tab running that Agent Key's project. The default on `AG00` - `AG05`; see [`AGENT-KEYS.md`](AGENT-KEYS.md). |
| `mouse` | - | `x`, `y`, `absolute`, `click`, `count` | Moves the pointer and/or clicks. |
| `none` | - | - | Explicitly unbind an input. |

`freemicro keys --list` prints this table from the live registry, so it can never
drift from what your build actually supports.

### Key names

Combos are written with `-` or `+`, modifiers first:
`escape`, `ctrl-r`, `shift-tab`, `cmd+shift+k`, `ctrl+option+cmd+d`.

* **Modifiers:** `cmd`/`command`/`meta`, `ctrl`/`control`, `alt`/`opt`/`option`,
  `shift`.
* **Named keys:** `return`, `enter`, `tab`, `space`, `delete`, `forward-delete`,
  `escape`, `home`, `end`, `page-up`, `page-down`, `up`, `down`, `left`,
  `right`, `f1` - `f12`, `help`.
* **Anything printable** works as itself: `a`, `7`, `/`. Because `-` and `+`
  separate a combo, write those keys by name: `minus`, `plus`, `equals`,
  `comma`, `period`, `slash`, `backslash`, `semicolon`, `quote`, `grave`.
* **`fn`** works too (`fn-space`), but only through the CGEvent backend - AppleScript cannot express it at all, and FreeMicro says so rather than failing
  silently. Whether a *synthetic* fn triggers third-party dictation apps is
  **unverified**.

FreeMicro uses Quartz `CGEvent` when it can and falls back to AppleScript
`System Events` otherwise. CGEvent is what makes `fn` and `hold` possible, and it
avoids spawning a subprocess per keystroke. Both need the same Accessibility
grant; `freemicro doctor` prints which one you're on.

A misspelled key name is caught when the config loads, not silently ignored when
you press the key.

### Chords: two keys bound as one

A binding key with a `+` in it binds the two keys **pressed together**:

```json
"bindings": {
  "AG00": { "action": "none", "label": "chord key" },
  "AG01": { "action": "focus_session" },
  "AG00+AG01": { "action": "shell", "command": "gh pr create --fill", "label": "ship" }
}
```

Order does not matter: `"AG00+AG01"` and `"AG01+AG00"` are the same chord, and
writing both is an error rather than a silent overwrite. Chords are **two keys
only** (see below), and they cannot use `ENC_CW`, `ENC_CC` or the four `JOY_*`
ids: those report one event and no release, so they are never *held* alongside
anything.

#### The rule, and what it costs

Key-down for the first key arrives before anything can know a second one is
coming. So if `AG00` is bound on its own *and* in a chord, something has to
decide which you meant. FreeMicro decides like this:

| Your key | What happens when it goes down |
|---|---|
| in no chord | fires immediately. **Zero added latency**, always. |
| in a chord, no binding of its own | fires nothing, waits for nothing. It stands by as a chord partner for as long as you hold it. **Zero added latency.** |
| in a chord **and** bound on its own | held back for `chords.settle_ms` (default **45 ms**). A partner inside that window fires the chord and the solo binding never runs. Otherwise the solo binding runs. |

Only the third row pays anything, and it pays 45 ms once. Release the key
before the window is up and it fires straight away, so a quick tap never waits
the full window either. A partner arriving *after* the solo binding already
fired is not a chord: it is two presses, and both run. That is what stops one
press from doing two things.

The zero-latency way to build a chord is therefore to give one key
`{"action": "none"}` and treat it as a shift key, which is what the example
above does. Reach for the settle window only when you want both keys useful on
their own.

```json
"chords": { "settle_ms": 45 }
```

Set it to `0` to switch deferring off completely: nothing is ever held back, and
chords then only work through a key with no binding of its own. FreeMicro warns
at load time if that leaves a chord that can never fire.

Releases follow the same resolution. Both key-ups of a chord are swallowed, so
a chord never leaks a stray solo release; if the chord's action is a `hold`, the
first of the two key-ups lets go, because there is no coherent meaning to
holding a chord you have half let go of.

#### Two keys, not three

Three-key chords are **refused**, with an error naming the limit. Not because
three fingers is hard, but because a third key would have to be waited for: on
seeing `AG00+AG01` the pad could not act until it knew `AG02` was not coming,
which is a second settle window paid by every two-key chord. Thirteen keys
already give seventy-eight pairs.

### Adding a new action kind

One decorated function in `src/freemicro/input/actions.py`:

```python
@action("notify", summary="Post a macOS notification.", required=("body",))
def _run_notify(act, backend):
    backend.run_applescript(f'display notification "{act.params["body"]}"')
```

It is then loadable from config, validated, listed by `--list`, and covered by
`--dry-run` - no dispatch `if` chain to touch.

## The mic key / push-to-talk

`ACT11` is the mic key on the unit we tested; `freemicro keys --dry-run` confirms
it on yours. The shipped default puts a dictation shortcut there:

```json
"ACT11": { "action": "key", "key": "ctrl+option+cmd+d", "label": "mic - dictation" }
```

Assign that **same** shortcut inside your dictation app and the key toggles it:

* **Wispr Flow** → Settings → Shortcuts → set the toggle-dictation hotkey to
  `Ctrl+Option+Cmd+D`. Nothing collides with that combo by default.
* Any other dictation tool with a configurable hotkey works the same way.

For **true push-and-hold**, use the `hold` action instead:

```json
"ACT11": { "action": "hold", "key": "ctrl+option+cmd+d", "label": "talk" }
```

The pad reports release as well as press, so FreeMicro presses the key when you
press the pad key and releases it when you let go. (This needs the CGEvent
backend; AppleScript's `keystroke` is press-and-release in one go.)

If you'd rather have the pad launch the app instead of toggling it, use a shell
action: `{"action": "shell", "command": "open -a 'Wispr Flow'"}`.

### The pad changes colour while the mic is live

Push-to-talk with no light is a key you have to trust. Give the binding a
`light` and the pad tells you:

```json
"ACT10": {
  "action": "hold", "key": "ctrl+cmd+o", "label": "mic",
  "light": { "color": "#2E8B57", "effect": "snake", "speed": 0.4,
             "zones": ["underglow"] }
}
```

`freemicro start` and the web UI's key editor write both halves for you when
you pick a hold-style dictation app. `freemicro keys --list` prints every light
in your config and exactly when each one goes out.

**This is not the mic key's feature.** Any binding may carry a `light`: hold a
key while a slow shell command runs, make a key a torch, mark whichever key you
are most likely to press by accident. The mic is just the one the default ships.

| Field | Values |
|---|---|
| `color` | required. `"#RRGGBB"`, `"#f0a"`, `"0xRRGGBB"`, `[r, g, b]`, or a packed integer |
| `effect` | `off`, `solid`, `snake`, `rainbow`, `breath`, `gradient`, `shallow-breath`. Default `solid` |
| `brightness`, `speed`, `magic` | `0` - `1`, as in `lighting.states` |
| `zones` | `underglow` (the default), `backlight`, `agent_keys`. Any combination |
| `timeout_seconds` | `120` by default, max `600`. See [below](#it-never-sticks) |

#### It is a layer, not a repaint

The light **claims the zones it names and nothing else**, for as long as the key
is down, and gives them straight back. Three consequences, and all three are the
point:

* **Your projects stay visible.** The default zone is the underglow precisely
  because the six Agent Keys are carrying one project each, and that is exactly
  what you still want to see while you are talking to one of them. It is also
  where the vendor puts its own recording colour
  ([`FACTORY-DEFAULTS.md`](FACTORY-DEFAULTS.md) §1b).
* **Letting go shows the truth as it is *then*.** Nothing is saved and put back.
  If a project finished mid-sentence, the pad is already green when you release,
  not green a moment later.
* **Auto-dim cannot blank the pad mid-hold.** Holding a key is activity; the
  three-minute timer does not run while a light is up.

#### It never sticks

A release can be lost - a Bluetooth drop mid-hold, the machine sleeping, a
key-up eaten in a burst - so a key-up is never the only thing that can end it:

* **The pad disconnecting ends it at once.** A key on a pad that is gone is not
  held, and the run loop knows that without guessing.
* **The clock ends it regardless**, after `timeout_seconds` (default **120**),
  and `freemicro run` says so. 120 s is long enough that no real hold reaches
  it and short enough that a stuck light clears itself while you are still at
  the desk wondering about it - and it is under the 180 s auto-dim, so a lost
  release can never outlive the pad's own dimming.

There is **no** "never" setting, on purpose. That is the same guarantee
`quartz.release_all()` gives for the modifier keys a `hold` leaves down: the
process that made a claim on your hardware discharges it itself, on every path,
including the ones nobody remembers to write.

#### Why sea green, and not red

Red is the recording idiom everywhere. Here it is already taken: `error` is
`#FF0033`. A pad that goes red when you talk *and* red when your agent breaks
has two meanings for one colour, and the one you would least want to miss is the
one that stops being believed.

`#2E8B57` is what the ChatGPT app itself drives while its voice state is
`recording` (§1b), so this is factory parity rather than a colour somebody
liked - the same principle as the five state colours. It is also clearly apart
from all five: the nearest is `done` `#00FF4C`, and `#2E8B57` is far darker,
desaturated and blue-shifted, it lands on a **different physical surface**, and
it *animates* where every state colour is solid.

#### Toggle dictation cannot be lit honestly

If your dictation app uses a **toggle** shortcut, FreeMicro sees the tap that
starts recording and then sees nothing at all - the tap that stops it looks
identical to the one that started it, and no message ever says "recording
ended". So there is no moment at which the light could correctly go out.

The options were to guess with a timeout, or to not claim what we cannot know.
A guess is wrong in both directions: too short and the pad goes dark while you
are still talking, too long and it says you are recording after you have
stopped. A microphone indicator that is wrong is worse than no indicator, so
FreeMicro does not ship one.

A `light` on a non-`hold` binding is therefore allowed but lasts exactly as long
as your finger, and the config layer warns at load time, `freemicro keys --list`
says so, and the web UI says so in the editor. For dictation: use
`{"action": "hold"}` and set your dictation app's **push-to-talk (hold)**
shortcut to the same combo. That is one setting in the other app, and it buys
you a light that is always right.

Lights on `ENC_CW`, `ENC_CC` and the four `JOY_*` ids are a **load error**, not
a warning: those report one event and no release, so nothing could ever turn
the light off.

### While a `hold` key is down, the other keys stop typing

`hold` presses **real modifier keys** and keeps them there. That is the whole
point, and it means that while you are dictating, every keystroke any other pad
key would send is silently modified into a different one. With
`ctrl+cmd+o` held, `{"action": "text", "text": "continue"}` does not type
`continue`: it sends `ctrl+cmd+c`, `ctrl+cmd+o`, `ctrl+cmd+n`, and so on, each
of which is a live macOS or app shortcut. The MIC keycap is double-width and
sits right next to the other action keys, so brushing one mid-sentence is an
ordinary accident.

So FreeMicro **refuses the second press** rather than sending something you did
not ask for. It is not silent: the press is printed by `freemicro keys
--dry-run` and by `freemicro run` as

```
  ACT09     play: type 'continue' + Return  [NOT SENT - ACT10 is holding ctrl+cmd+o]
```

Only actions that reach the outside world through the keyboard are refused
(`text`, `key`, `hold`, `applescript`, and any action kind added later, which is
assumed to type until it says otherwise). Actions that cannot be changed by a
held modifier are left alone, because suppressing them would just make the pad
feel broken:

| Refused while a `hold` is down | Allowed |
|---|---|
| `text`, `key`, `hold`, `applescript`, `answer_permission` | `app`, `focus_session`, `mouse`, `shell`, `none` |

The refusal lifts the instant you let go of the held key, and a refused press is
never queued: an action that arrives half a second late, after you have moved
on, is its own kind of surprise.

## Joystick

The pad reports the stick as an angle (0-1 of a full turn) and a distance
(0-1), and returns to exactly `{a:0, d:0}` when you let go. There are two ways
to use that, chosen with `joystick.mode`.

### `pointer` (the default): an analogue cursor

```json
"joystick": {
  "mode": "pointer",
  "pointer_deadzone": 0.1,
  "max_speed": 1200,
  "gamma": 2.0,
  "tick_hz": 90,
  "precision_key": "",
  "precision_scale": 0.25,
  "invert_y": false
}
```

This is the red TrackPoint nub from a ThinkPad. **How far you push sets the
cursor's speed, not how far it jumps**, and it keeps moving for as long as you
hold it - the cursor is driven by a steady internal tick, not by pad events, so
holding a direction steady does not stall it.

| Field | What it does |
|---|---|
| `pointer_deadzone` | How far the stick must move before the cursor does. Small on purpose: it only has to reject the stick's own slop. |
| `max_speed` | Pixels per second at **full** deflection. |
| `gamma` | The shape of everything in between. `1` is linear and twitchy; `2` is TrackPoint-like; `3` is very gentle near centre. It does not change your top speed. |
| `tick_hz` | How often the cursor moves. Not how often the pad reports. |
| `precision_key` | Hold this input id (e.g. `"ACT12"`) to drop to `precision_scale` of full speed for pixel work. While pointing, that key does not run its normal binding. |
| `invert_y` | Flip up and down, if pointing comes out upside down on your unit. |

**Tuning.** Run `freemicro keys --dry-run` and push the stick: it prints the
live angle, distance and the resulting px/s. Push to the deflection that feels
like normal cursor speed and read the number - that is your `max_speed`. Then:

* **Too fast / overshoots** -> lower `max_speed` (try 800).
* **Twitchy near centre, hard to land on a target** -> raise `gamma` (try 2.5
  or 3) before touching `max_speed`. That buys precision in the middle of the
  range without giving up your top speed.
* **Slow to cross the screen but fine up close** -> raise `max_speed`, leave
  `gamma`.
* **Creeps when you are not touching it** -> raise `pointer_deadzone` a little.
* **Feels dead / needs a shove to start** -> lower it.

If the pad goes quiet for a quarter second - a dropped Bluetooth packet, a
disconnect, a sleep - the cursor stops on its own. It never keeps drifting.

### `directions`: four bindable flicks

```json
"joystick": {
  "mode": "directions",
  "deadzone": 0.6,
  "origin": 0.0,
  "directions": ["JOY_RIGHT", "JOY_DOWN", "JOY_LEFT", "JOY_UP"]
}
```

A flick fires **once** when distance crosses `deadzone`, and re-arms only after
the stick clearly returns to centre - so resting near the threshold can't
machine-gun keystrokes into your terminal.

`directions` lists the input ids starting at angle 0 and stepping by equal
fractions of a turn, which for four ids is right, **down**, left, up. That is
the order the hardware itself uses: angle `0.75` is up, because macOS screen
coordinates grow downward. `origin` rotates the whole wheel if your stick's zero
sits somewhere else.

Want eight directions? List eight ids. The wheel adapts.

### The two deadzones are not the same number

`deadzone` (0.6) guards an **action**, and is large because crossing it types
something into your terminal. `pointer_deadzone` (0.1) guards **motion**, and
only has to reject slop. The vendor firmware splits them the same way. Setting
the pointer's to 0.6 is the quickest way to make pointing feel dead.

## Lighting

**FreeMicro ships with lighting off.** Turn it on once:

```sh
freemicro lights --enable     # and --disable to hand the pad back
```

This is deliberate. macOS opens this HID device non-exclusively, so the ChatGPT
desktop app may also be writing these LEDs. Taking over your hardware should be
something you decide, not something that happens to you. You do **not** have to
quit anything to run FreeMicro - see
[Running alongside the ChatGPT app](#running-alongside-the-chatgpt-app).

```json
"lighting": {
  "enabled": true,
  "method": "rgbcfg",
  "zones": ["agent_keys"],
  "on_exit": "off",
  "states": {
    "working": { "color": "#304FFE", "effect": "solid",
                 "brightness": 1.0, "speed": 0.0 }
  }
}
```

| Field | Values |
|---|---|
| `zones` | `backlight` (under the keycaps), `underglow` (base strip), `agent_keys` (the six top keys, set individually) |
| `color` | `"#RRGGBB"`, `"#f0a"`, `"0xRRGGBB"`, `[r, g, b]`, or a packed integer |
| `effect` | `off`, `solid`, `snake`, `rainbow`, `breath`, `gradient`, `shallow-breath` |
| `brightness`, `speed` | `0` - `1` |
| `magic` | `0` - 1, an uncharacterized firmware field - exposed for tinkerers |
| `on_exit` | `off` (default - blanks the pad and hands it back, like the vendor app does on quit), `breath`, `leave`. Applied however FreeMicro stops: Ctrl-C, `launchctl bootout`, logout, `pkill` |
| `auto_dim_seconds` | `180` (default, the factory's three minutes). Seconds of inactivity before the pad goes **dark**, not dimmer. `0` (or `"off"`) never dims |
| `auto_dim_alerts` | `false` (default): `waiting` and `error` stay lit through the timeout. `true` dims them too, which is exactly what the factory does |
| `enabled` | `false` (the default) turns the LED renderer off entirely |
| `method` | `rgbcfg` (default - the one verified to light this hardware) or `preview`, which firmware v0.4.1 accepts and ignores. Debugging only; see [`PROTOCOL.md`](PROTOCOL.md) |
| `reassert` | When we re-send lighting something else overwrote - see [Running alongside the ChatGPT app](#running-alongside-the-chatgpt-app) |

States are `idle`, `working`, `waiting`, `done`, `error`. Any state you omit
falls back to the **factory colour** for it, lit solid at full brightness: the
same value the shipped config spells out, so deleting a state you are happy with
changes nothing.

A binding's own
[`light`](#the-pad-changes-colour-while-a-key-is-held) layers over all of this
while its key is held. It may claim a zone `lighting.zones` does not list - the
mic default claims the underglow - and that zone is then sent dark in every
other frame, which is what the factory does with the underglow too.

### Factory parity

The shipped colours are the **exact factory values**
([`FACTORY-DEFAULTS.md`](FACTORY-DEFAULTS.md)), so turning lighting on looks like
the pad you bought:

| State | Colour | Factory meaning |
|---|---|---|
| `idle` | `#FFFFFF` | Idle |
| `working` | `#304FFE` | Thinking |
| `waiting` | `#FF6D00` | Requires input |
| `done` | `#00FF4C` | **Unread**, not "completed" |
| `error` | `#FF0033` | Error |

Two factory behaviours worth keeping if you edit these:

* **Green decays.** It means *unread*, so it clears after
  `state.done_ttl_seconds` (default 180s, in `~/.freemicro/config.json`). Set it
  to `0` to keep green until something else changes it - but the pad will then
  sit green forever after your first finished task, which the real hardware never
  does.
* **The factory keeps most of the pad dark.** Default `zones` is `agent_keys`
  alone for that reason; adding `backlight` or `underglow` is a visible
  divergence, not a bug.
* **The pad blanks itself after three minutes.** `auto_dim_seconds` copies the
  factory's auto-dim, including what "dim" means there: a full off, not a lower
  brightness. It matters more here than it does for the vendor, because `idle`
  is white at full brightness and idle is what a live project shows most of the
  time. Any key, dial detent or joystick nudge wakes it, and so does any change
  in what the pad is showing.
  * The one deliberate divergence: `waiting` and `error` do **not** dim, because
    the moment an amber key is worth the most is the moment you are away from
    the desk and nothing is resetting the timer. Set `auto_dim_alerts: true` for
    exact factory behaviour.
  * A key being **held** stops the timer outright, so the pad cannot go dark
    while a `light` is up. Holding a key is the least ambiguous activity there
    is, and the factory's own wake rule is "any HID event".

Test a palette by eye:

```sh
freemicro lights done                 # show one state
freemicro lights --cycle --hold 2     # walk all five
freemicro lights done --color '#FF00FF' --effect breath --speed 0.8
```

> **Hold each colour for a second or two when testing.** Every lighting call
> *replaces* the previous one, so a rapid sequence looks to a human like only its
> final frame. This costs people a lot of debugging time.

### Running alongside the ChatGPT app

Both programs drive the same LEDs over the same channel and the last write wins.
Three facts decide what to do about it:

* **Your keys are never affected.** macOS shares this device for *reading* - both apps see every press, detent and joystick sample. Only writes contend.
  Anything that disables key input because ChatGPT is open is a bug.
* **The vendor app is event-driven, not continuous.** It writes when its own
  state changes and then stops, so FreeMicro's colours persist in between.
* **Re-sending is free.** Every lighting call replaces the previous one, so
  sending the same state again is idempotent.

So there are two ways to live with it, and you can use both.

**1. Let it heal itself (on by default).** FreeMicro re-sends its current
lighting whenever it plausibly lost the field:

| Trigger | Why |
|---|---|
| ChatGPT quits | The field is ours again - this is the big one |
| The pad reconnects | It may have been repainted while we were gone |
| `keymap.json` changes | Reloaded and re-applied without restarting `run` |
| A slow heartbeat | **Off by default**, see below |

`freemicro run` says so when it happens (`[lighting] reasserted lighting
(ChatGPT quit)`), so a pad that repaints itself is never unexplained magic.

```json
"lighting": {
  "reassert": {
    "enabled": true,
    "heartbeat_seconds": 0,
    "poll_seconds": 3.0
  }
}
```

The heartbeat defaults to `0` - off - on purpose. Every lighting call replaces
the last, so a periodic re-send *restarts* animated effects: a `breath` idle
colour would visibly hitch on every beat. It also puts permanent background
traffic on the channel that carries your key events. Set it to `5` if you run
both apps constantly, use only `solid` effects, and prefer self-healing to
precision. Reasserts never run while a keypress burst is in flight, heartbeat or
not.

**2. Own a zone the vendor leaves alone (no conflict at all).**

```sh
freemicro lights --coexist      # same as: freemicro lights --zones backlight
freemicro lights --zones agent_keys    # back to per-key status
```

The ChatGPT app keeps the **key backlight** dark essentially always - it flashes
it for ~4 s when you change the selected thread and otherwise sends all-off
([`FACTORY-DEFAULTS.md`](FACTORY-DEFAULTS.md) §1c). If FreeMicro drives only
`backlight`, the two of you never write the same zone, so nothing can be
overwritten in either direction.

The trade-off is real: the backlight sits *under* the keycaps, so agent state
reads as **one colour glowing through the whole pad** rather than six independent
per-project lights. You lose the "three keys lit means three live projects"
glance; you gain colours nothing ever repaints. `agent_keys` remains the default
because the per-key detail is the better default when nothing is competing for
it.

## Runtime prefs (a different file)

`~/.freemicro/config.json` holds runtime preferences - renderer `prefer` order,
state TTL - and is separate from the pad config on purpose: one file is *yours to
edit constantly*, the other you set once.

## Environment variables

| Variable | Effect |
|---|---|
| `FREEMICRO_HOME` | Move `~/.freemicro` somewhere else |
| `FREEMICRO_KEYMAP` | Point at a specific pad config file |
| `FREEMICRO_NO_DEVICE` | Pretend no pad is attached - keeps the test suite off your real hardware |
| `XDG_CONFIG_HOME` | Where the XDG search path looks |
