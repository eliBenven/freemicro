# The web config UI

A local page for editing `~/.freemicro/keymap.json` by pointing at the pad
instead of typing JSON.

Until this existed, remapping a key or changing an LED colour meant hand-editing
a config file. For a tactile object with thirteen keys, a dial and a stick that
is the wrong medium: you cannot see which block of JSON is the key under your
finger, and you certainly cannot see what `#304FFE` looks like on frosted
plastic. So the UI draws the pad, you click the thing you want to change, and -
if the pad is plugged in - the colour you pick appears on the hardware while you
drag the slider.

It is a tool you start, use and stop. Not a service, not a daemon, nothing
running in the background.

```sh
freemicro config --web        # the command this wants to be; see "Wiring" below
```

- Standard library only. No Electron, no npm, no build step, no dependencies.
- Python 3.9 compatible, like the rest of the core.
- Binds `127.0.0.1` only, on a random port, behind a per-run token.

---

## The shape of the page

The owner's verdict on the first design was *"a million fields and a million
ways to do everything"*, next to the vendor's settings window, which is about
six controls and a picture. He was right, and the fix was not more tabs.

**The pad is the interface.** The front page is one column:

| On the front page | |
|---|---|
| The pad diagram | Large, accurate, showing your keycaps and your colours. It is the object, not an illustration. |
| One status line | Connected / transport, or the one thing that is wrong and what to do about it. |
| Five colour swatches | Click to preview a state on the diagram; click again to edit that colour. |
| **Layout** | One chooser holding the built-in starters and every layout you have saved, plus "Save current as…". |
| **Lights** | On or off. |
| **Brightness** | One slider for the whole pad. |
| **Agent keys follow** | Most recent / pinned / manual / mirror. |
| Advanced | Closed. |

That is six interactive things plus the pad. Save and Revert appear in the
header only when there is something to save or revert.

**Click a key → one modal**, in this order: what it should do (in outcomes, not
action kinds), the one field that answer needs, *what the pad shows while the
key is held*, which keycap is fitted, and an Advanced disclosure for everything
else.

The first four are all **on screen at once**, and each is one click. That is
the point of the modal, and it took a second pass to get right - see *"Cannot
change a key quickly enough"* below.

### "While this key is held" is one switch, not a lighting panel

Turning it on writes the vendor's own recording look - `#2E8B57`, snake, on the
underglow ([`CUSTOMIZING.md`](CUSTOMIZING.md#the-pad-changes-colour-while-the-mic-is-live)).
A colour well and the three zones are on screen; effect, speed, brightness and
the timeout are one disclosure deeper, because nobody turning this on for a mic
key has an opinion about any of them yet.

The sentence under the switch **changes with the action kind**, and that is the
part worth keeping. FreeMicro can only see a key that is *held*: a toggle
dictation shortcut starts on a tap and stops on an identical one, so a light
there would go out while the mic was still live. So a binding that is not a
hold gets told exactly that, in the editor, at the moment somebody is about to
make the mistake - and the switch still works, because "lit while my finger is
on it" is a legitimate thing to want. The UI declines to *imply* something it
cannot deliver rather than declining to do what it was asked.

The list of kinds it can honestly track comes from `HOLD_KINDS` over the API,
not from a copy of it in the JavaScript, so a new action kind with a release
starts being tracked here without anyone editing this page.

### What moved behind a disclosure

Nothing was removed. Everything below is one click from where it belongs:

| Was on screen | Now |
|---|---|
| Action-kind picker per input | Key modal → Advanced (the outcomes cover the common six) |
| Every action parameter | Key modal → Advanced |
| Keycap picker | Key modal, fourth |
| A held key's light: effect, speed, brightness, timeout | Key modal → "While this key is held" → Advanced (the switch and the colour are on screen) |
| Per-state lighting table (colour, effect, brightness, speed, magic) | Click a swatch → colour + effect; speed and magic under Advanced there |
| `lighting.method` | **Gone from the UI.** `rgbcfg` is correct on this firmware and `preview` is a documented dead end; it is a debugging escape hatch, not a choice |
| Zones | One toggle in Advanced: "Leave the Agent Keys to the ChatGPT app" |
| Agent-key slots | Advanced (and only when the policy actually uses them) |
| Joystick deadzone / origin / direction order | Advanced (deadzone only - the rest has a good default) |
| Dictation target | Advanced |
| Starter presets section | Folded into the Layout chooser |
| Five tabs and a side panel | Deleted |

### Outcomes, not our vocabulary

`focus_session` is what the config calls it. The user is offered *"Jump to this
project's terminal"*. The six outcomes - jump to a terminal, type something,
press a shortcut, hold to talk, open an app, do nothing - cover every binding
in every starter. `shell`, `applescript`, `mouse` and raw parameters live one
disclosure deeper, where someone looking for them will find them and nobody
else has to read them.

## Configure the pad with the pad

Two capture paths, and between them a user can set up the whole pad without
typing a key name or ever meeting the id `ACT10`.

**Pressing a pad key opens that key.** The page starts listening as soon as the
pad is free, so a press selects that input and opens its editor - and lights it
up on the diagram, which doubles as proof that FreeMicro is receiving the pad
at all. Flicking the stick or turning the dial selects those inputs the same
way. **A press cannot fire its binding while the page is listening**: capture
reads `v.oai.hid` and never constructs a `Bridge` (see `webui/padlink.py`), so
nothing is typed, no app is switched and no shell command runs. The status line
says so in as many words, because a user about to press a key wired to
something destructive deserves to be told.

**Shortcuts are pressed, not typed.** A `key` or `hold` field is a live capture
box that is already listening when it appears: press the combo, see it, confirm
with *Use this*. Escape cancels the capture without closing the modal; Tab and
⌘W are never swallowed. Nothing reaches the document until you accept - the
rule from the data-loss bug, preserved. A folded-away *"type it instead"* field
remains for combos you cannot press here. The Wispr Flow three-key limit is
checked **while you press**, not at save.

## Swapping and moving

- **Drag one key onto another on the diagram to swap** what they do, keycap and
  all. Hold Option to copy instead. This costs no screen space, which is what
  makes it the right kind of feature for a page being shrunk - and it mirrors
  the physical act of moving a keycap to another switch.
- The same operation without a pointer: every key's Advanced panel has
  **"Swap this key with…"**, a searchable list of the other inputs.
- Undo is the header's **Revert**, and the previous file is always kept as
  `.bak`.

## Layouts

A layout is a whole pad under a name. The four starters appear as read-only
built-ins; **Save current as…** makes one of your own in
`~/.freemicro/layouts/<name>.json`. Switching shows the same pre-apply diff as
before, then writes through the ordinary delta + fingerprint save path, so it
can never clobber a concurrent edit.

A layout carries **bindings and keycaps only** - not your colours, stick
settings or comments. Switching layouts is a change of what the pad does, not a
reset of everything you have tuned.

## What it can do

| | |
|---|---|
| **Starter layouts** | Four complete, opinionated pads applied in one click, each with a diff you see *before* it is applied and an undo afterwards. Most people should never hand-configure sixteen inputs. |
| **Pad diagram** | The real 4x4 layout - dial and stick in the top corners, six Agent Keys, four action keys, the double-width MIC over `ACT10`+`ACT11`, `ACT12` = TERM, and the firmware-owned haptic profile control. Click anything to edit it. |
| **Every action kind** | Read live from `freemicro.input.actions.REGISTRY`, so a new action kind appears in the UI without the UI being touched. |
| **App picker** | "Open an app" offers the applications actually installed on this Mac, searchable, and tells you *while you type* when a name matches nothing. |
| **Dictation chooser** | Wispr Flow, macOS Dictation or your own shortcut, with the hold-versus-toggle trap explained in one sentence. |
| **Validation** | Every save goes through `padconfig.parse` - the same function the CLI uses. If it would not load, it does not get written, and the reason appears next to the field that caused it. |
| **LED editing** | Colour, effect, brightness and speed per agent state, with the exact factory colours as one-click presets. |
| **Live preview** | Sends the look you are editing to the pad immediately, through the existing lighting layer. |
| **Identify a key** | Listens for `v.oai.hid` and highlights the key you actually pressed, so you never have to guess which id is which. |
| **Safe writes** | Atomic (temp file + rename), with the previous version kept as `keymap.json.bak`. |

### Starter layouts

`webui/starters.py`. Each starter is a **complete** `bindings` map, not a patch,
so "apply" has a meaning you can hold in your head: *the pad now does this.*

| | |
|---|---|
| **Claude Code essentials** | The default. Agent keys for the commands you actually type, PLAY to unstick a stalled agent, LAB for a deep review, MIC push-to-talk, TERM to your terminal. |
| **Dictation-first** | MIC is hold-to-talk; almost nothing types on your behalf. |
| **Git workflow** | status / diff / branch / log / push on the agent keys, `gh` on PR and NAV. |
| **Minimal** | Only PLAY and TERM. Build the rest yourself. |

Applying one never happens silently:

1. **See what changes** lists every id that would differ, old value struck
   through, new value beside it, with counts.
2. **Apply** only touches the in-memory document. `bindings` is replaced;
   lighting, stick geometry, comments and unknown keys are left alone.
3. **Undo** is offered immediately, and nothing has reached the disk until you
   press Save - which still keeps the previous file as `.bak`.

The test suite pushes every starter through the real `padconfig` parser, so a
starter that would not load cannot ship.

### Everything knowable is a dropdown

If the valid values can be enumerated, you pick from them. Free text is the
exception, and every exception is a value that genuinely cannot be listed.

| Chosen, not typed | From |
|---|---|
| Action kind | `input.actions.REGISTRY`, with each kind's own summary inline |
| Application | `/Applications`, `/System/Applications`, `~/Applications` |
| Keycap | the 37-glyph vendor catalogue, searchable, drawn as icons |
| Key combo | modifier chips + the base keys `input/keys.py` accepts, with **press-the-keys capture** as the first-class path |
| Effect | the seven named effects, described in words |
| Colour | picker + the factory presets by name, hex validated live |
| Agent-Key slot | the live project list, never a session UUID |
| Dictation target | Wispr Flow / macOS Dictation / custom |
| Brightness, speed, magic | sliders with a live readout |

Genuinely free text - the literal `text` an action types, a `shell` command, an
`applescript` body, a label - stays free, and shows its validation result.

Every dropdown filters from the first keystroke, is driveable with
arrows/Enter/Escape, and shows a value that exists but is currently unavailable
(an app that is not installed) **disabled with the reason** rather than hiding
it. That last one matters: a setting that vanishes from its own editor looks
like data loss.

### The keycaps are user data

The pad ships with a tray of ~35 interchangeable caps and people rearrange them.
So *which glyph is on which switch* is something only the user knows:

- the catalogue lives in `webui/keycaps.py`, transcribed from
  `docs/FACTORY-DEFAULTS.md` §7 (id, icon, factory command);
- the arrangement is stored as a **top-level `keycaps` object**, `{"ACT06":
  "FAST"}`. Not inside the binding: `padconfig.parse` hands every unrecognised
  *binding* field to `validate_params`, so a `"keycap"` there would make the
  whole config fail to load. Unknown top-level keys are preserved and ignored,
  which is exactly what presentation metadata wants;
- a pad nobody has configured shows the **factory arrangement** - `FAST` `APPR`
  `REJ` `SPLIT` `MIC` `CODEX` - drawn faintly, because that is a guess about
  someone's desk. Setting a cap makes it solid;
- the glyphs are hand-authored inline SVG, built with `createElementNS` in the
  SVG namespace. No icon font, no CDN (the CSP forbids both), no emoji. See
  *"Every glyph was missing"* below for why the namespace is worth a paragraph;
- the picker is a **flat grid of all 37 caps**, in the modal, searchable, one
  click to fit. Not a dropdown: the cap and the outcome are the two things a
  key is opened to change, so neither is allowed to be a popover deep;
- the six Agent Keys have no cap in the factory arrangement, because the
  physical ones are blank frosted status keys. They draw no glyph until you
  fit one, and the same grid is there to fit one with;
- suggestions run **both ways** - bind `gh pr create` and it offers the PR cap;
  fit the APPR cap and it offers to bind Return, which answers a Claude Code
  permission prompt. Offered, never applied: the cap in your hand is the truth.

### The MIC cap is two switches

The wide MIC keycap physically sits over two switches and the pad reports
**`ACT10` and `ACT11` on every press**. So:

- the diagram draws one double-width cap labelled `ACT10+ACT11`;
- the action goes on `ACT10` and `ACT11` is bound to `none`, deliberately.
  Binding **both** fires the key twice per press - for a push-to-talk hold that
  means the combo goes down and straight back up under your finger, which is
  exactly the "it just doesn't work" this page exists to prevent. The factory
  decoder discards the `ACT11` half for the same reason;
- a config where the second half still does something gets a red badge on the
  cap, an explanation, and a one-click *"Silence ACT11"*.

### The round control at the bottom left is not a key

It is a **haptic Bluetooth-profile switch**: tap or circle it to move between
the pad's three host profiles (`Codex Micro #1/#2/#3`;
`device.status.profile_index` reports the active one, zero-indexed), with three
small white LEDs beside it showing which is live. It emits only standard HID
(reportID 1 keyboard, 2 consumer) and **never** `v.oai.hid`, so FreeMicro cannot
see it and cannot bind it.

It is drawn anyway, marked *not bindable*, and clicking it explains why -
because a control that exists on the object and is missing from the picture is
how someone concludes the tool is broken. `ACT12` is the separate narrow TERM
key to the right of MIC.

### The diagram is a mirror, not a picture

The six Agent Keys are drawn in the colour they are configured to show, dimmed
by their brightness and blanked by effect `off`. Pick a state chip above the pad
and the whole diagram repaints as that state. Change a colour and it updates as
you drag.

**Only the six Agent Keys have individual RGB.** They are frosted and addressed
one at a time over `v.oai.thstatus`. The action keys are tan and opaque - no LED
of their own - and everything else you see glowing is the *global* backlight and
underglow, one colour each over `lights.preview`. The UI therefore offers
per-key colour on the Agent Keys, global colour on the two zones, and nothing at
all on the action keys. A colour control that cannot change anything is worse
than no control.

---

## Security model

Be honest about what this process is: **it edits a file that describes
keystrokes, shell commands and AppleScript, and it can send them.** Anything
that can reach this API can write itself a binding. That is the whole threat
model, and it is why the surface is closed down three ways.

### 1. Loopback only, and there is no flag to change that

`check_bind_host()` raises `BindRefused` for anything that is not `127.0.0.1`,
`::1` or `localhost`. `0.0.0.0` is refused explicitly. There is deliberately no
option to expose this on a network - a config UI reachable from the LAN is a
remote-code-execution endpoint wearing a nice stylesheet.

### 2. A per-run token, then a session cookie

32 bytes from `secrets.token_urlsafe`, generated at startup, printed once in the
URL. Every `/api/` request needs it, compared with `hmac.compare_digest`.

Localhost is **not** a trust boundary on a shared or multi-user machine: any
process that can open a socket can reach a bare localhost server. The token
means it also has to guess a secret it never sees.

The page strips the token from the address bar the instant it loads
(`history.replaceState`), so it does not linger in history, in a copied URL or
in a `Referer`. The **page load sets a session cookie** and everything after it
authenticates on that:

```
freemicro_session=<token>; Path=/; HttpOnly; SameSite=Strict
```

`HttpOnly` means the page's JavaScript never touches the secret - strictly
better than the first version, which held it in memory and therefore **lost it
on Cmd-R**, locking the user out of their own config page with a message
telling them to find a URL in a terminal they may have closed. Reload, back,
forward and a second tab now all simply work. No `Expires`, so the session dies
with the browser. No `Secure`, because this is plain HTTP on loopback and the
flag would stop the cookie being sent at all.

Honestly: any process that can read this user's browser cookie jar could reuse
the session. That is no worse than the situation it replaces - a process
running as this user can simply edit `keymap.json` directly - and the token is
still required to obtain the cookie in the first place.

### 3. Host-header pinning

Requests whose `Host` is not a loopback name are rejected. This closes DNS
rebinding, where an attacker's page re-resolves their own domain to `127.0.0.1`
so the browser will let it talk to us. Combined with the token (which such a
page cannot read, being a different origin) that path is closed twice.

### Also true

- Responses carry `Cache-Control: no-store`, `Referrer-Policy: no-referrer`,
  `X-Content-Type-Options: nosniff` and a strict `Content-Security-Policy`
  (`default-src 'none'`, no inline script or style, no external anything).
- The page never uses `innerHTML`. Every string that reaches the DOM - including
  labels and comments out of your own config - is set as text.
- Static assets (`/app.css`, `/app.js`) are served without the token. They
  contain no user data; the config does, and that needs the token.
- Nothing is sent anywhere. There is no telemetry and no outbound network use of
  any kind.

### What it does not defend against

Someone who can already read your terminal, your process list or your home
directory. If an attacker can see the printed URL, they have already won by
easier routes. The token raises the bar from "any local process" to "a process
that can read your terminal output".

---

## Sharing the pad

Only one process can usefully hold this device. macOS opens the Codex Micro
*non-exclusively*, so "did the open succeed?" proves nothing - two processes can
both hold it and will then overwrite each other's LEDs on every state change,
with no way for you to tell which is at fault
(`docs/FACTORY-DEFAULTS.md` §9).

So the UI looks for another claimant **before** opening, and refuses if it finds
one:

1. a lock file at `~/.freemicro/pad.lock` naming a live pid,
2. another `freemicro` process that drives the pad (`run`, `keys`, `watch`,
   `daemon`),
3. the ChatGPT desktop app, which drives the same LEDs on the same channel.

Any of those and live preview and identify mode are disabled - and this is the
part that matters, because refusing correctly while looking broken is worse than
failing:

First, the refusal is scoped to what is genuinely broken. **Reading input and
writing LEDs do not contend the same way**: macOS opens this device
non-exclusively and two processes read every key, the encoder and the stick
simultaneously - verified on hardware with the ChatGPT app open. Only *lighting
writes* fight, because both processes push to the same channel and the last
write wins. So `contention_detail()` answers per capability:

| Situation | Input | Lighting |
|---|---|---|
| Another FreeMicro process holds the lock | blocked | blocked |
| The ChatGPT desktop app is running | **works** | works, warned: a preview may be overwritten |
| A lock file nobody is holding | reclaimed silently | reclaimed silently |

Coexisting with the vendor app is therefore a **supported** configuration for
input and a degraded-but-usable one for lighting. Disabling identify mode
because ChatGPT was open - as this page used to - meant a first-time user
pressed keys and saw nothing.

Second, whatever *is* unavailable says so:
**a banner across the top of the pad diagram, on page load, without any
interaction.** It names the cause, says what to do about it,
and says explicitly that editing and saving still work. The page re-checks every
three seconds, so quitting the ChatGPT app makes the banner disappear on its
own, no reload. Every control that needs the hardware also carries the reason
in its tooltip and reports it if you click it anyway.

That behaviour is not cosmetic. "Correct refusal, invisible reason" had by then
cost this project four separate rounds of *"the pad does nothing"* bug reports
where nothing was wrong.

### A stale lock is reclaimed, not reported

The pid in the lock file is a hint, not evidence - pids get reused, and a killed
owner leaves a file claiming a pid that now belongs to something else. So the
owner also holds an **`flock`** on it for as long as it lives, and the kernel
drops that the moment the process dies, however it died. `lock_is_held()` tests
it non-blockingly; a lock nobody holds is deleted silently and reported as a
one-line reassurance, not an error. Telling a user to go and delete a file they
did not create is a bug report waiting to happen.

When the UI *does* take the device it writes `~/.freemicro/pad.lock`:

```json
{"pid": 12345, "owner": "freemicro web UI", "since": 1690000000.0}
```

and removes it on exit. It is advisory, not a kernel lock: the point is to let
cooperating processes notice each other. **A daemon should write and respect the
same file** - `freemicro.webui.padlink.lock_path()`, `read_lock()` and
`contention()` are there to be reused rather than reimplemented.

Identify mode also stops itself after two minutes, so a browser tab left open
overnight does not hold the pad hostage.

---

## Wiring

### The command I want

```
freemicro config --web
```

Entry point:

```python
from freemicro.webui import serve

serve(open_browser=True)      # blocks until Ctrl-C
```

Full signature:

```python
def serve(
    open_browser: bool = True,
    host: str = "127.0.0.1",   # anything else raises BindRefused
    port: int = 0,             # 0 = a random free port
    config_path: Optional[Path] = None,   # honour --config if given
) -> None
```

Suggested argparse wiring, to match how `cmd_config` already takes `--config`:

```python
cf.add_argument(
    "--web", action="store_true",
    help="open the visual pad configurator in your browser",
)
cf.add_argument(
    "--no-browser", action="store_true",
    help="with --web, print the URL instead of opening a browser",
)
```

and in `cmd_config`:

```python
if args.web:
    from freemicro.webui import serve

    serve(
        open_browser=not args.no_browser,
        config_path=Path(args.config).expanduser() if args.config else None,
    )
    return 0
```

`serve()` prints the URL, the file it will write, and a one-line warning about
the token before it blocks. It handles `KeyboardInterrupt` itself, releases the
pad and drops the lock.

A top-level `freemicro web` would work just as well if that reads better
alongside the other commands - the entry point does not care.

### Packaging

The package includes `src/freemicro/webui/static/{index.html,app.css,app.js}`.
Hatch's wheel target already takes `packages = ["src/freemicro"]`, which picks
up package data, so **no `pyproject.toml` change is needed** - confirmed by
building the wheel and listing it:

```sh
python -m build --wheel && unzip -l dist/*.whl | grep static
```

All three assets are present. Still worth re-checking if anyone adds an
`exclude` rule to the build target.

---

## Wiring required - a `freemicro layout` command

`webui/layouts.py` is deliberately UI-free: it reads and writes
`~/.freemicro/layouts/*.json` and knows nothing about HTTP. The CLI surface it
is shaped for, for whoever owns `cli.py`:

```sh
freemicro layout list                 # names, plus which one matches the config
freemicro layout save <name>          # keep the current pad under a name
freemicro layout use <name>           # switch to it
freemicro layout delete <name>
```

`layouts.catalogue()`, `save()`, `apply_to()` and `delete()` are the four calls
that needs; `apply_to()` returns a document to hand to
`configio.save_document(path, document, base=..., expect=...)` so the CLI gets
the same non-clobbering write the UI does.

## Wiring required - the Agent-Key slots

The Agents tab is **designed and rendered but not connected**. It is shown with
a standing warning in the UI saying exactly that, because the alternative was to
build half a feature and let people believe it worked.

### What the owner asked for

Each Agent Key stands for **one chat/session**, not for the single winning
state. The six keys follow "the six most recent, pinned, or explicitly chosen"
sessions - the same model as the factory pad, whose settings read *"choose which
chats the six agent keys follow"* and which defaults to the six most recently
updated (`docs/FACTORY-DEFAULTS.md` §7, `codex-micro-agent-source` = `recent`).

### The config shape the UI writes

A new **top-level** section, deliberately outside `lighting` because it is about
*which session* a key follows, not about colour:

```json
{
  "agent_keys": {
    "policy": "recent",
    "slots": ["", "", "", "", "", ""]
  }
}
```

| Field | Meaning |
|---|---|
| `policy` | `recent` \| `pinned` \| `manual` |
| `slots` | Six entries, one per Agent Key in `AG00` - `AG05` order. Each is a session id, or `""`. |

- **`recent`** (default, factory parity) - `slots` is ignored; fill 0 - 5 from
  `StateStore.sessions()` newest first.
- **`pinned`** - the named slots keep their key; empty slots fill from the
  remaining most-recent sessions, skipping any already placed.
- **`manual`** - only named slots light. An empty slot is sent
  `{c: 0, b: 0, e: 0}` - the factory's "no assigned agent", which is off, not
  dim (`docs/FACTORY-DEFAULTS.md` §1a).

`padconfig.parse` ignores unknown top-level keys, so this section round-trips
today and the CLI loads a config containing it without complaint. It is simply
read by nothing.

### What has to be built to make it real

1. **`padconfig`** - parse and validate `agent_keys` into a dataclass
   (`policy` in three values, `slots` a six-element list of strings), exposed on
   `PadConfig`. Warn, do not fail, on a slot naming a session that is not
   running: sessions come and go.
2. **A slot resolver** - something like
   `resolve_slots(config, sessions) -> List[Optional[SessionState]]`, pure and
   testable, given the policy and `StateStore.sessions()` output. This is the
   whole feature; everything else is plumbing.
3. **`renderers/micro_leds.py`** - send six *different* `thread_entry` values
   instead of `all_agent_keys(...)`. `freemicro.device.lighting.thread_entry`
   and `thstatus_message` already take a per-key list, so the protocol layer
   needs nothing: it is `messages_for()` that currently collapses all six to one
   colour.
4. **Optional, for factory parity** - the selected key gets `effect: breath` at
   `speed: 0.4`, same colour (`docs/FACTORY-DEFAULTS.md` §1a). FreeMicro has no
   concept of a "selected" session yet, so this is a later decision, not a gap.

Once `PadConfig` exposes the parsed section, this UI needs **no changes** - it
already writes the shape above, and `Api.schema()` flips one flag
(`agent_slots_wired`) to drop the warning banner.

### Also needed for the picker to be useful

`GET /api/sessions` reads `StateStore.sessions()` directly, which is fine, but
it means the manual picker can only offer sessions that are **currently live**.
A session id you pinned last week shows as *"(not running)"* and is preserved.
If long-lived pinning matters, sessions need a stable, human-meaningful name -
`SessionState.title` exists but nothing populates it in the hook path today.

---

## Anything else that stayed out

- **No `pyproject.toml`, `cli.py` or `README.md` edits.** File ownership was
  split for this work; the entry point above is ready to be wired by whoever
  owns the CLI.
- **Keycap legends are per unit.** `webui/layout.py` carries the legends
  confirmed on hardware (`ACT06` LAB, `ACT07` PR, `ACT08` NAV, `ACT09` PLAY,
  `ACT10`+`ACT11` the wide MIC, `ACT12` TERM). They are cosmetic - FreeMicro
  binds ids - but a unit that differs wants that table edited. Identify mode
  tells you what your pad actually sends.
- **Per-surface colours are not expressible yet.** The hardware has three
  independent surfaces - the six Agent Keys (`v.oai.thstatus`, one colour
  *each*), the underglow and the key backlight (both `v.oai.rgbcfg`, one colour
  each) - but `lighting.states[name]` in the config carries a single look per
  state, applied to whichever `zones` you enable. The UI says so plainly rather
  than implying a control it cannot save; giving each surface its own colour
  needs a `padconfig` schema change, which this work does not own.
- **`sk` / `sa` (make an Agent Key drive the backlight or underglow) are not
  offered**, for the same reason: `padconfig` would reject the fields, so a
  control for them could not be saved.

---

## Two bugs this page shipped with, and what was actually wrong

Worth writing down, because both were "looks right in the code, does nothing on
the desk".

### Clicking the diagram did nothing

Three separate causes, all now closed:

1. **Real dead zones on the round controls.** The dial and the stick had no
   handler of their own; only the small nubs inside them were clickable. Over
   half of the dial's face and a large part of the stick's swallowed clicks
   silently. There is now **one delegated listener on `#pad`**, and a click that
   lands on a control's body - or in the gutter beside the round cap -
   resolves to the nearest input inside that cell. If you can see it, you can
   click it, and there is a test for it.
2. **Handlers were per-node closures created inside `renderPad()`, and the
   header's handlers were attached at the *end* of an async `boot()`.** Any
   exception, anywhere in the render chain, left a page that looked finished
   with no listeners on anything - and nothing on screen said so. Listeners are
   now attached in `wire()`, synchronously, before the first `fetch`; the
   delegated pad listener survives every redraw; and `window.onerror` /
   `unhandledrejection` put a loud red panel on the page.
3. **The first paint was gated on the hardware probe.** `boot()` awaited
   `/api/device` before drawing anything - an endpoint that shells out to as
   many as three `pgrep` calls (5 s timeout each) and walks IOKit. The page now
   draws from the config, then asks the hardware.

`do_GET` in `server.py` also had no `try/except` (unlike `do_POST`), so an
exception there dropped the connection instead of answering, and the page
reported only "Failed to fetch". It now returns a 500 with the message in it.

### A save overwrote a working shortcut - the worst bug here so far

Reported as *"on save it's overwriting wispr flow settings"*: a mic binding of
`ctrl+cmd+o`, the combo actually registered in Wispr Flow, came back as
`ctrl+cmd+g` after a save. `ctrl+cmd+g` appears **nowhere** in this project's
source, which is the clue that mattered - it was never a bad default, it was
the page writing back its own drifted memory.

Two defects, both mine, and both fixed:

**1. The shortcut recorder captured on keystroke.** "Press the keys" armed a
listener on `window` with `capture: true` for six seconds and wrote the first
key it saw *straight into the document* - no accept step, no cancel, and it
fired even while the user was typing in another field. Any stray press became
the binding. It now holds the captured combo aside, shows it, and writes
nothing until **Use this** is pressed; Escape cancels; the listener is removed
the moment capture ends.

**2. Save serialised the entire in-memory document.** So one stray value, in a
binding the user never opened, was written over the file along with everything
else - and the same mechanism would clobber anything the CLI, a preset or a
second tab had changed in the meantime. A save now sends three documents and
writes only the difference:

| | |
|---|---|
| `base` | what the page loaded |
| `document` | what the page holds now |
| *(on the server)* | what the file says at the instant of writing |

`configio.delta()` extracts the changed **leaves** - `bindings.ACT06.label`, not
`bindings` - and `configio.merge_onto()` applies exactly those onto the file.
Anything untouched keeps what the file has, whoever wrote it. Editing one colour
can no longer rewrite sixteen bindings, because the write never contains them.

Three more layers, in the order they catch things:

- **Before writing**, any save that would change more than one setting shows
  every leaf it is about to write, old value → new, with Cancel. That alone
  would have made this bug visible instead of silent: the panel names
  `bindings.ACT10.key  ctrl+cmd+o → ctrl+cmd+g` in plain sight.
- **A content fingerprint** taken at load and checked at save. An *unrelated*
  change by another process merges cleanly and says so; the **same** leaf
  changed in both places is a refusal, not a guess - the page shows what
  clashed and offers "Reload the file (lose my edits)" or "Keep my edits
  (overwrite the file)" by name. Nothing is written either way until you pick.
- **After writing**, the toast names what changed, and Revert sits beside it.
  The `.bak` is written before the new file, always, and is what recovered the
  owner's shortcut here.

Preset previews were already pure - `buildPreview()` only reads, and
`applyStarter()` mutates only after the user confirms - but the same `base` and
fingerprint now guard that path and the Revert path too.

### The page contradicted the pad

The UI showed three projects as `working` while the last two keys sat dark.
The pad was right. Claude Code emits no hook when you press Escape to
interrupt, so a cancelled session goes on *claiming* to be working; the store
retires that claim on a timer, in one place, so the LEDs, the slot resolver and
`freemicro status` cannot disagree. This page built its own `StateStore`
passing only two of the four TTLs, so it ignored the user's configured decay
and reported the stale claim as the current state.

There is now a single `_store()` helper that every session-shaped endpoint uses
and that passes **every** TTL from `Config`. `/api/sessions` reports the
effective state as the state, and offers the retired claim as detail
(`claim`, `stale`, `claim_text` from `describe_claim()`) rather than as the
answer. A test asserts the UI's payload matches `StateStore.sessions()` for
every session.

### Live preview lit nothing

`padlink.preview()` and `api.preview()` defaulted to `method="preview"`.
`lights.preview` is a real firmware method that answers `{"result": null}` and
changes **nothing visible** - not the underglow, not the backlight, over USB or
Bluetooth, with the vendor app quit. Live preview was therefore dead on arrival
while reporting success. Both now default to `rgbcfg`, which drives
`v.oai.rgbcfg` for the two global surfaces and `v.oai.thstatus` for the Agent
Keys, and a test pins the exact methods that go down the wire.

### The server can outlive its own code

Python holds imported modules in memory, so a UI left running while FreeMicro is
updated underneath it serves the old code - which produced a genuinely baffling
"unknown action 'focus_session'" for an action that plainly existed on disk.
`/api/device` compares the newest `.py` mtime in the package against the one
recorded at startup and reports it as `restart`.

That was a tinted strip at the top of the page, and it was not enough. An
evening's worth of bug reports - `/api/layouts/save` returning 404 for a route
that exists, and *"the icons are not on the page"* - were all one stale process.
So it is now a **blocking panel, on page load**, and there are two detectors,
because they fail at different ages:

| | |
|---|---|
| The server's mtime check | Precise. Only exists in a server new enough to have it. |
| `SCHEMA_CONTRACT` in `app.js` | What *this page* needs `/api/schema` to contain. Runs in the browser, so it works against a server of any age - including one that predates the mtime check. |

Static assets are read from disk on every request, so the browser always gets
the *new* `app.js` from an *old* process. That asymmetry is the whole bug, and
it is also what makes the browser-side contract check possible.

The panel names what is missing, gives the one-line fix as a command, and has a
single way past it: *"Carry on anyway, and expect things to be wrong"*, for
whoever is editing FreeMicro's own source and knows exactly why the server is
one save behind. Take that door and the strip stays for as long as the mismatch
does.

### Every glyph was missing

Reported as *"get the icons on the page, we still don't have them"*, and the
cause was the section directly above: a **stale server**. `/api/schema` from a
process older than `webui/keycaps.py` answers without a `keycaps` catalogue, so
`capById()` matches nothing, `capOf()` returns `null`, and `keyNode()` appends
no glyph node at all. Every key on the diagram draws blank, nothing throws,
nothing is logged, and the page otherwise works perfectly. Proven by deleting
`S.schema.keycaps` in a live page: 6 glyphs → 0, no other visible change.

Three things changed, in the order they now catch it:

1. **The blocking panel above**, which is the actual fix - `keycaps` is in
   `SCHEMA_CONTRACT`, so this exact failure now names itself on page load.
2. **`glyphNode()` always puts ink on the cap.** An unknown icon, or a cap id
   from a newer catalogue, draws its own name in the same stroke colour rather
   than nothing. A key with no glyph looks exactly like a key with no cap,
   which is what made this invisible.
3. **SVG cannot be built through the wrong door.** `document.createElement`
   does not fail on `<path>` - it returns an inert `HTMLUnknownElement` that
   sits in the DOM and never draws. `el()` is namespace-aware via `SVG_TAGS`
   and routes those tags to `createElementNS` itself; `svgEl()` says so
   explicitly and throws on a tag that is not SVG; and `class` is set with
   `setAttribute` for SVG, because `className` there is a read-only
   `SVGAnimatedString`. That was not this bug, but it is the same shape of bug
   and it was one refactor away.

### Cannot change a key quickly enough

Reported as *"right now we can't click a key and immediately replace it with
what's available"*. Both halves of the answer were technically present and both
were too slow: the keycap was a dropdown (click the picker, type, click an
option, close), and the six outcomes were a full-height stack that pushed the
caps below the fold.

Now: click a key → one modal → outcomes in two columns, the single field that
answer needs, and **all 37 keycaps as a flat searchable grid**, all visible
together. One click on a cap fits it and the diagram updates; the grid repaints
*itself* rather than rebuilding the modal, so the search box keeps its text and
its caret and a shortcut capture elsewhere in the modal is not torn down under
the user's fingers. Advanced still holds shell, applescript, mouse, raw
parameters and swap-with.

One consequence worth naming: the keycap search box and the shortcut recorder
are now on screen at the same time, and the recorder listens on `window` with
`capture: true`. So it **stands down** when a keystroke is aimed at an input,
textarea, select or contenteditable, instead of swallowing it. That is the same
rule that fixed the shortcut-overwriting bug - a keystroke aimed at a field is
not a shortcut - and it was previously only half applied.

### `replaceChildren` is not `el()`

`Node.replaceChildren()` is a native DOM method, and it behaves nothing like
this file's own `el()`: it does **not** flatten arrays and it does **not** drop
nulls. Every argument that is not a Node goes through `String()` and lands on
the page as text. So:

| Passed | Rendered |
|---|---|
| `dictationSection()`, which returns an array | `[object HTMLHeadingElement],[object HTMLParagraphElement],…` |
| `condition ? el(...) : null` | the literal word `null` |

Both shipped. Neither throws. The first was found by eye and fixed by spreading
that one call; auditing the remaining nineteen call sites found **two more**
live instances of the second form, in `judgeApp()` (the "did you mean" row) and
in the conflict panel (the "changed in both places" line) - both of which
printed `null` to a user whenever the optional part was absent.

The class is now closed rather than the instances: **`mount(host, ...kids)`**
takes exactly what `el()` takes - nested arrays, `null`, `false`, plain strings
- and is the only caller of `replaceChildren` in the file. A test asserts that.

### The refusal was invisible

See *Sharing the pad*, above. `contention()` already returned excellent prose;
nothing put it in front of the user. Now it is a banner across the diagram,
polled, self-clearing.

---

## Tests

`tests/test_webui.py`, no hardware and no browser required:

- config round trip - load, edit, save, reload, with comments and unknown keys
  intact, and the CLI's own loader reading the result;
- backup and atomicity - the `.bak` copy, and a refused save leaving the file
  byte-identical;
- validation - every class of bad action rejected with the config layer's own
  message, and bad lighting rejected too;
- the layout matching the hardware (4 columns, round controls in row 1's
  corners, the six Agent Keys as row 1's middle two plus all of row 2, MIC two
  units wide over `ACT10`+`ACT11`, `ACT12` = TERM, the haptic control drawn but
  not bindable, only the Agent Keys lit);
- every starter layout loading through the real `padconfig` parser, and
  `apply_to()` leaving lighting and comments alone;
- the app enumerator finding real bundles and `resolve()` refusing a name that
  is not installed;
- the source-level invariants that keep the click path alive: the pad's listener
  is delegated and attached before the first render, no per-key `onclick`
  closures, no `await` between the start of `boot()` and the first paint;
- the invariants behind the four bugs above: SVG is built in its own namespace
  and `svgEl()` throws on a tag that is not one; a cap with no drawing still
  draws its own name; `mount()` is the only caller of `replaceChildren`; the
  key modal puts the outcomes *and* the 37-cap grid in front of Advanced; the
  stale-server panel is checked before the first paint, names the fix, and is
  `position: fixed` over the whole page; and the shortcut recorder stands down
  when a keystroke is aimed at a field;
- the agent-slot section round-tripping without breaking `padconfig.load`;
- token auth - missing, wrong and correct, on both the page and the API;
- host-header rejection;
- the bind refusal for `0.0.0.0` and friends;
- graceful degradation with no pad, which the repo's conftest guarantees by
  setting `FREEMICRO_NO_DEVICE=1`.
