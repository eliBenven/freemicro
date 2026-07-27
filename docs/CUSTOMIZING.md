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
  "ACT10": { "action": "hold", "key": "ctrl+cmd+o", "label": "mic" },
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
| `hold` | `key` | `latch`, `double_tap` | Holds the key down for as long as you hold the pad key. True push-to-talk. `latch: true` instead taps the key (for a toggle app): tap-tap keeps recording, tap again stops. `double_tap: <combo>` keeps the real hold and *also* fires a second, different shortcut on a double-tap. See [the mic key](#the-mic-key--push-to-talk). |
| `shell` | `command` | `cwd`, `wait` | Runs a shell command. Fire-and-forget unless `wait: true`. |
| `applescript` | `script` | - | Runs arbitrary AppleScript. The escape hatch. |
| `app` | `name` | `cycle` | Focuses an app. `cycle: true` cycles its windows if it's already frontmost. |
| `focus_session` | - | `slot`, `project`, `fallback`, `new_terminal`, `terminal` | Raises the terminal tab running that Agent Key's project; when the key is **empty** (no live project) opens a new terminal window instead. The default on `AG00` - `AG05`; see [`AGENT-KEYS.md`](AGENT-KEYS.md) and [below](#an-empty-agent-key-opens-a-terminal). |
| `layer` | `layer` | - | Makes the key a layer trigger: hold it for the named layer's bindings. Types nothing itself. See [Layers](#layers-hold-a-key-for-a-second-binding-set). |
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

### An empty Agent Key opens a terminal

An Agent Key with no live project - an **unlit** key - used to do nothing when
pressed. Now it opens a **new terminal window**, so a spare key is a way to start
work rather than a dead key. A key that *does* have a live project still focuses
that project's tab, unchanged; only the empty case is new.

It opens the top-level `terminal_app` (default `"Terminal"`, the one terminal
every Mac has). Set it once to yours:

```json
{
  "terminal_app": "iTerm2",
  "bindings": { "AG00": { "action": "focus_session", "label": "agent 1" } }
}
```

Terminal and iTerm2 get a real new window through their own AppleScript; the
common Cmd-N terminals (Ghostty, Warp, WezTerm, kitty, Alacritty, Hyper, Tabby,
Rio, VS Code, Cursor) are activated and sent Cmd-N; anything else FreeMicro does
not recognise is simply **activated** - the right app comes forward even if a new
window cannot be opened for it. It only ever opens a window: no `cd`, no launching
Claude Code, that is yours to do.

**The switch.** Opening a window is visible and non-destructive, so it is **on by
default** for empty keys. Turn it off globally with `"terminal_app": false` (every
empty key then stays inert), or for one key with
`{"action": "focus_session", "new_terminal": false}`. A single key can also name
its own terminal with `"terminal": "Ghostty"`, overriding the top-level default.

### Per-app profiles: one key, different jobs per app

The same key can sensibly mean different things depending on what is in front of
you: in a browser `ACT06` might open a tab, in your terminal it might clear the
screen. A `profiles` block lets the config say so. Each profile is a **partial
bindings map** that shadows the base bindings while a given app is frontmost:

```json
{
  "bindings": {
    "ACT06": {"action": "text", "text": "/clear", "submit": true}
  },
  "profiles": {
    "Google Chrome": {"ACT06": {"action": "key", "key": "cmd+t"}},
    "Terminal":      {"ACT06": "/clear"}
  }
}
```

A profile overrides **only the keys it names**. Everything it leaves out falls
through to the base `bindings`, so you write the difference, not the whole map.
A binding inside a profile takes exactly the same forms a base binding does, the
string shorthand included, and it is validated at load time the same way, with
the same warning if it names an input this build does not recognise.

**Matching.** The frontmost app is matched by name, case-insensitively, in this
order:

1. an **exact** match on the profile's key (`"Terminal"` matches the app
   `Terminal`);
2. otherwise a **substring** match, so a profile named `"Chrome"` matches
   `"Google Chrome"` because the key is contained in the app's name. When more
   than one profile matches this way, the **longest** key wins, so a specific
   `"Google Chrome"` profile beats a broad `"Chrome"` one.

An app with no matching profile, or a lookup that fails, simply uses the base
bindings. An empty or unknown profile is never an error.

**What stays global.** Profiles override single-key bindings only. Chords, the
thumbstick and all of the lighting are the same in every app. A chord's meaning
depends on two keys being held at once, and making that answer depend on the
frontmost app as well would double the timing logic for no real gain; a profile
that tries to bind a chord id (`"AG00+AG01"`) is refused with an error saying so.

**Holds and chords still behave.** A `hold` bound in a profile presses and
releases correctly even if you switch apps mid-hold: the binding is latched when
the key goes down and replayed when it comes up, so the key-up always matches the
key-down and a modifier can never be left stuck. A profile-bound key that also
takes part in a chord, participates in the modifier-bleed suppression, or opens a
double-tap window works exactly as its base counterpart would.

**Cost, and the freshness tradeoff.** Resolving a press is a dict lookup, never
an OS round trip: the run loop caches which app is frontmost and refreshes that
cache on its own tick, off the key path. The refresh is throttled by
`profile_poll_ms` (default `300`, a top-level number in milliseconds, `0` means
"every tick"). App switches are human-paced, so a third of a second is invisible
in practice; the price is that a press in the first few hundred milliseconds
after a switch can still act on the app you just left. Raise `profile_poll_ms`
to poll less often, lower it to act on a switch sooner. **A config with no
`profiles` never looks up the frontmost app at all**, so the feature costs
nothing until you use it.

`freemicro keys --list` prints every profile and, next to each override, the base
binding it replaces. In the web editor (`freemicro config --web`) the profiles
live under **Advanced -> Per-app profiles**, where you add an app from a picker
of what is installed and give any key an app-specific binding with the same
widgets the base bindings use.

### Layers: hold a key for a second binding set

A layer is a keyboard **Fn key** for your pad. A binding can be a *layer
trigger*, and while that key is physically held the keys a layer names resolve
to that layer; on release they revert. It is what lets one physical key carry two
jobs.

```json
{
  "bindings": {
    "ACT09":   {"action": "layer", "layer": "fn"},
    "ENC_CLK": {"action": "mouse", "click": "left"}
  },
  "layers": {
    "fn": {
      "ENC_CLK": {"action": "text", "text": "/effort", "submit": true}
    }
  }
}
```

Each layer is a **partial bindings map**, exactly like a profile: same action
forms, the string shorthand included, validated the same way at load time. A
layer overrides **only the keys it names** - everything else falls through to
your normal resolution.

**The trigger types nothing.** It is a pure modal switch: holding it switches the
layer on and holds nothing itself.

**Precedence: layer > profile > base.** When a layer is held, a key it names
resolves to the layer, ahead of any per-app profile, ahead of the base binding. A
key the layer does *not* name falls straight through to the profile-then-base
resolution it always had. The order is deliberate: a key you are **physically
holding down this instant** is the most immediate expression of intent there is -
the same reason a keyboard's Fn wins over everything - whereas a profile is
passive and automatic, following whatever app happens to be frontmost. If you
hold two layer keys at once, the most recently pressed wins, which reads like a
keyboard.

**It cannot latch on forever.** A layer stuck "on" because its key-up was lost
(a Bluetooth drop mid-hold) is the same failure class as a stuck `hold`, and it
is recovered by the same machinery, not a second copy of it: the next press of
the trigger reconciles it, and a 120-second max-hold cap is the backstop. A
layer can never stay on past that.

**It composes with everything else.** A `hold` reached *through* a layer still
presses on the way down and releases on the way up - even if you let go of the
layer key first - because the delivered action is latched on press and replayed
on release, exactly as the profile path does. A **chord** always resolves
globally, whatever layer is held; a layer, like a profile, overrides single keys
only and a layer that tries to bind a chord id is refused. A layer trigger may
carry a `light`, which is on for exactly as long as the layer is held.

**The recipe: the pointer's click back, and `/effort` on demand.** By default the
thumbstick moves the cursor and the dial press is a left click (see
[Joystick](#joystick)). This layer keeps that, and adds a hold-for-effort mode:
hold the fn key and the dial press opens Claude Code's `/effort` slider while the
dial turn adjusts it.

```json
{
  "bindings": {
    "ACT09":   {"action": "layer", "layer": "fn"},
    "ENC_CLK": {"action": "mouse", "click": "left"},
    "ENC_CW":  {"action": "key", "key": "up"},
    "ENC_CC":  {"action": "key", "key": "down"}
  },
  "layers": {
    "fn": {
      "ENC_CLK": {"action": "text", "text": "/effort", "submit": true},
      "ENC_CW":  {"action": "key", "key": "right"},
      "ENC_CC":  {"action": "key", "key": "left"}
    }
  }
}
```

Turn the dial to adjust effort, press Enter (the CODEX key) to keep it. To drive
effort with the **joystick** left/right instead of the dial, add `JOY_LEFT` and
`JOY_RIGHT` arrow overrides to the `fn` layer and set
`"joystick": {"mode": "directions"}` - in the default `pointer` mode the stick
moves the cursor and the flicks never fire. This recipe ships documented in the
default keymap's `_readme` but is not active; copy it in if you want it.

`freemicro keys --list` prints every layer, which key triggers it, and each
override next to the base binding it shadows.

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
"ACT11": { "action": "key", "key": "ctrl+cmd+o", "label": "mic - dictation" }
```

Assign that **same** shortcut inside your dictation app and the key toggles it:

* **Wispr Flow** → Settings → Shortcuts → set the toggle-dictation hotkey to
  `Ctrl+Cmd+O`. Keep it to **three keys or fewer**: Wispr Flow silently ignores
  a hotkey longer than that, so a four-key combo like `Ctrl+Option+Cmd+D` never
  registers and the mic looks dead. (This is the shipped default's combo for
  exactly that reason.)
* Any other dictation tool with a configurable hotkey works the same way.

For **true push-and-hold** (the app records while the shortcut is held), use the
`hold` action instead:

```json
"ACT11": { "action": "hold", "key": "ctrl+cmd+o", "label": "talk" }
```

The pad reports release as well as press, so FreeMicro presses the key when you
press the pad key and releases it when you let go. (This needs the CGEvent
backend; AppleScript's `keystroke` is press-and-release in one go.)

For an app whose shortcut **toggles** recording (tap to start, tap to stop), add
`"latch": true`. You then get the vendor MIC's own behaviour: hold to talk and
let go to stop, **or** tap the key twice quickly to latch recording on and tap
once more to stop.

```json
"ACT11": { "action": "hold", "key": "ctrl+cmd+o", "latch": true, "label": "talk" }
```

A latch **taps** the shortcut rather than holding modifiers down, so nothing is
held between the taps: other keys keep typing normally, and there is no
stuck-modifier hazard even across a latch that lasts minutes. Every threshold is
350 ms - the vendor's double-tap window ([`FACTORY-DEFAULTS.md`](FACTORY-DEFAULTS.md)
§8). Latch needs the CGEvent backend and an input with a release, so it is a
load error on the dial detents and joystick flicks.

#### One key, two dictation shortcuts (`double_tap`)

You can make a single mic key do push-to-talk on a **hold** *and* fire a
**second, different** shortcut on a **double-tap**. Add `double_tap` to a plain
`hold`:

```json
"ACT10": { "action": "hold", "key": "ctrl+cmd+o", "double_tap": "ctrl+cmd+u" }
```

* **Hold** the pad key → `ctrl+cmd+o` is physically held for as long as you hold,
  released when you let go. This is the plain push-to-talk hold, unchanged.
* **Double-tap** the pad key (two quick presses within 350 ms) → one tap
  (press-and-release, *not* a hold) of `ctrl+cmd+u`.

The recipe it exists for is **two Wispr Flow shortcuts in two modes on one key**:
set `Ctrl+Cmd+O` as Wispr's **push-to-talk (hold)** shortcut, and bind
`Ctrl+Cmd+U` in Wispr's **toggle** area. Then a hold talks in hold mode and a
double-tap flips the toggle-mode dictation on or off.

The hold is **never delayed** to watch for a second tap - that would put a
350 ms lag before push-to-talk starts recording, which would ruin it - so the
first tap of a double-tap briefly holds `ctrl+cmd+o` for under the window. That
blip is harmless: a toggle-mode app ignores its push-to-talk shortcut, and a
push-to-talk app records nothing meaningful in under 350 ms. The double-tap fires
on the **second press** (the moment the gesture is unambiguous and the first tap
is already released, so the second shortcut goes out with clean modifiers). It
fires **once per completed pair**: a triple-tap fires it once, a quadruple-tap
twice (on, then off).

The pad's `light`, if you add one, tracks the **hold** only - it comes on while
the key is down, exactly as for a plain hold. A double-tap does **not** light the
pad: the second shortcut targets a toggle app whose recording state FreeMicro
cannot see, so lighting it would be a guess.

`double_tap` and `latch` are two different models of the same gesture and cannot
be combined on one binding (a load error says so). Like `latch`, it needs an
input with a real release, so it is a load error on the dial detents and
joystick flicks.

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

#### A toggle can be lit honestly - if FreeMicro drives it

Watching a toggle from outside, FreeMicro cannot light it: it sees the tap that
starts recording, but the tap that stops looks identical and no message ever
says "recording ended", so there is no moment the light could correctly go out.
Put a plain `light` on a non-`hold` binding and it lasts exactly as long as your
finger - the config layer warns at load time, `freemicro keys --list` says so,
and the web UI says so in the editor.

`latch: true` removes that limit by making FreeMicro the thing that taps. It runs
the vendor state machine itself, so it always knows whether recording is on - a
latched mic reports live for the whole latch and goes dark the instant the stop
tap is sent, whether that stop came from your tap, the waiting window timing out,
or the pad disconnecting. So the honest answer for a toggle dictation app is a
**latching hold with a light**: `{"action": "hold", "key": "…", "latch": true,
"light": {…}}`.

For a push-to-talk (hold) app, a plain `{"action": "hold"}` with a light is
still the simplest right answer: the release you feel is the release FreeMicro
sees.

Lights (and latches) on `ENC_CW`, `ENC_CC` and the four `JOY_*` ids are a **load
error**, not a warning: those report one event and no release, so nothing could
ever turn the light off or resolve the tap-tap window.

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
  "invert_y": false,
  "tap_click": true,
  "tap_click_button": "left"
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
| `tap_click` | Tap-to-click (below). On by default; `false` turns it off. |
| `tap_click_button` | Which button a tap clicks: `left` (default), `right` or `middle`. |

**Tap the stick to click.** The analogue stick has no physical button, so a
quick **deflect-and-return** that never became real cursor movement is a left
click - the stick becomes a full trackpad. The exact rule, and it is chosen so a
fast intended *move* is never mistaken for a click:

* the push crosses the **action deadzone** (`deadzone`, `0.6` - a deliberate
  push, not the stick's resting slop),
* it **returns to centre within about 200 ms** of that crossing, and
* the cursor moved **no more than about 30 px** in total while it was out.

A push you *hold*, or one that has already carried the cursor across the screen
by the time it could return, fails the second or third test and is a move, not a
click. Tap-to-click is on by default and only ever active in `pointer` mode; set
`"tap_click": false` to turn it off, or `"tap_click_button"` to `right`/`middle`
to change the button.

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
| `effect` | `off`, `solid`, `snake`, `rainbow`, `breath`, `gradient`, `shallow-breath`, and FreeMicro's own `blink` (see [Blink](#blink-a-hard-on-off-in-software)) |
| `brightness`, `speed` | `0` - `1` |
| `magic` | `0` - 1, an uncharacterized firmware field - exposed for tinkerers |
| `theme` | A named palette - `factory`, `nord`, `solarized`, `high-contrast` - that sets all five colours at once. See [Colour themes](#colour-themes) |
| `flash_on` | States whose *entry* plays a brief attention flash. Default `["waiting", "error"]`; `[]` (or `false`) turns it off. See [Attention flash on entry](#attention-flash-on-entry) |
| `battery` | Reflect a low battery through the lighting. Off by default. See [Low-battery cue](#low-battery-cue) |
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

### Blink: a hard on/off, in software

The firmware has no hard blink - only the smooth `breath`. FreeMicro adds one
itself. Set `"effect": "blink"` on any state (or any binding `light`) and the
render loop toggles that light between its colour and off on its own clock; the
`speed` field sets the rate (0 slowest, 1 fastest).

```json
"states": { "error": { "color": "#FF0033", "effect": "blink", "speed": 0.5 } }
```

It is driven by the render loop that already ticks, not a second thread writing
the LEDs - the lighting code holds exactly one writer - so a blink composes with
everything else: one Agent Key can blink `error` while the other five sit solid
on their own projects, and letting a held mic key's layer up or down still works
frame by frame.

**Blink is an accessibility win, not just decoration.** State on the pad is
otherwise carried by *hue alone* - amber `waiting` versus green `done`, red
`error` versus green `done` - which is the classic red/green colourblind trap. A
distinct effect per state is a **redundant channel**: a key that blinks is
"error" whether or not you can tell its colour from its neighbour's. The
`high-contrast` theme below is built entirely on this idea.

Blink does not keep the pad awake: `auto_dim_seconds` still blanks it, and the
toggling stops when it does, so a blinking light on an empty desk goes dark like
anything else. (The two states you would most want to blink, `waiting` and
`error`, already stay awake by default through `auto_dim_alerts`.)

### Colour themes

A `theme` sets all five state colours at once, so you do not hand-pick each:

```json
"lighting": { "enabled": true, "theme": "nord" }
```

| Theme | What it is |
|---|---|
| `factory` | The palette the pad ships with. `"theme": "factory"` and no theme at all look the same |
| `nord` | The Nord editor palette (frost and aurora) |
| `solarized` | Solarized's accent colours |
| `high-contrast` | A colourblind-friendly palette (Okabe-Ito hues) that leans on a **distinct effect per state**, not hue: `waiting` breathes and `error` blinks, so the two states you least want to miss are told apart without seeing their colour at all |

A theme is a starting point, not a cage: an explicit `states` entry still wins
per state, and every state a theme does not set (or that no theme is chosen for)
falls back to the factory colour as before. You can pick a theme from the web
UI's Lights pane, where the pad diagram repaints in the theme's colours.

### Attention flash on entry

When a key (or the underglow/backlight) *enters* one of the `flash_on` states, it
plays a brief one-time pulse in that state's own colour - it draws your eye to
the new state without ever standing in front of it - and then settles into the
steady look. It fires only on the actual transition, never on every frame and
never on the first sight of a state at startup.

```json
"lighting": { "flash_on": ["waiting", "error"] }   // the default
"lighting": { "flash_on": [] }                      // turn it off
```

### Low-battery cue

The pad is battery-powered, and FreeMicro can reflect a low battery through the
lighting. It is **off by default** - the vendor app has no such cue, so this is a
FreeMicro extra, not factory parity.

```json
"lighting": {
  "battery": {
    "enabled": true,
    "threshold": 15,          // percent, at or below which the cue shows
    "zone": "underglow",      // where it lands - the underglow by default
    "color": "#FF6D00",
    "effect": "breath",       // a slow pulse; "blink" works too
    "poll_seconds": 60
  }
}
```

A charging pad is never "low" (the cue is a nudge to plug it in, and it already
is). The reading is taken from the cached `device.status` value that the menu bar
and daemon already refresh - **FreeMicro never issues the battery round trip from
the render loop.** That round trip shares the one vendor channel with the
lighting writes and key events, and the render loop is already inside one long
read pump; asking for battery there would fight the very writes it decorates. So
the cue reads a small cache file on a slow clock (`poll_seconds`, default 60) and
puts nothing on the channel.

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

**3. Split the six Agent Keys - give Codex some, keep the rest.**

If you run Codex *and* Claude Code, the ChatGPT app lights all six Agent Keys for
Codex and FreeMicro lights them for Claude Code, and you get a tug of war. You
cannot tell the ChatGPT app to use fewer keys - but you can tell FreeMicro to.
Set `agent_keys.keys` to the physical key indices (`0`-`5`) FreeMicro should
own, and it leaves the rest untouched:

```json
"agent_keys": {
  "policy": "recent",
  "keys": [3, 4, 5]
}
```

Now FreeMicro drives `AG03`-`AG05` (your Claude Code projects map onto those
three keys only) and never writes `AG00`-`AG02`, so Codex keeps them. It does
this by sending a **partial `thstatus`** - an array with entries for the owned
keys only. A `thstatus` that names fewer than six keys updates just those keys
and leaves the others as they are, which is what lets the ChatGPT app's colours
on the un-owned keys persist.

**The split covers presses as well as lighting.** An un-owned Agent Key is
Codex's *entirely*: FreeMicro ignores its presses. Pressing `AG00` while you own
`[3, 4, 5]` does nothing on FreeMicro's side - no focus, no new terminal window,
no bound action at all - so Codex's own action on that key stands alone rather
than getting a FreeMicro one on top of it. Your owned keys behave exactly as
before, new-terminal-on-empty included, so you can leave `terminal_app` on and
open a fresh terminal from a spare *owned* key while the Codex keys stay inert.

> This relies on the firmware doing **partial `thstatus` updates** (an array of
> three entries changes three keys, not all six). It matches the message shape
> in [`PROTOCOL.md`](PROTOCOL.md) and is how the split is built, but it wants a
> quick confirmation on real hardware.

Rules and defaults:

* **Omitting `keys` drives all six** - the default, byte-for-byte the behaviour
  before this option existed. An empty list `[]` or all six also means "drive
  everything". The shipped config sets no `keys`.
* `keys` must be whole numbers `0`-`5`, each at most once; anything else is a
  clear load error.
* **A binding on an un-owned key never fires** - the split covers presses, so an
  un-owned Agent Key runs no action, not even an explicit `shell` or `key` you
  bound to it deliberately (the key is Codex's, and a half-held split is worse
  than none). `freemicro keys --list` says which keys are Codex's, and the pad
  warns at load time if a binding names one, so it is never silently dropped.
* Everything composes with the split: the `mirror` policy, per-project slots, the
  mic activity light, the battery cue, blink and the attention flash all stay on
  the owned keys only. Auto-dim and blank-on-exit only darken the owned keys, so
  Codex's keys are never blanked by FreeMicro either.
* **Holding the split.** The ChatGPT app writes all six keys on its own model
  changes and would periodically clobber the keys you own. So while a subset is
  configured, FreeMicro turns on a modest reassert cadence by itself (every ~3 s)
  and re-sends its owned keys - just those keys, never the ones Codex has. Without
  this the pad would show your keys flicker to Codex's colour and stay there. The
  cadence is only on while a subset is set; with the default all-six config there
  is no heartbeat and no added traffic. The trade-off is the general heartbeat
  one - each re-send restarts an animated effect - but the owned-key states are
  `solid`, so a re-send is invisible; set `reassert.heartbeat_seconds` yourself to
  override the cadence.

`freemicro keys --list` prints which keys FreeMicro drives and which are left for
Codex.

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
