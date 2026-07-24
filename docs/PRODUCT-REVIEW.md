# Product review

> Written 2026-07-23 by a reviewer who used the repo as a stranger would: read
> `README.md` first, followed it from a clean shell, read the shipped
> `default_keymap.json` as if the pad had just come out of the box, then read
> the code to check whether the docs were telling the truth.
>
> This is not a bug hunt. Everything below can be true while every test passes.
> The question here is whether this is the right product and whether it is good.
>
> Findings are ranked by impact on a real user. Each one names what is wrong,
> why it matters, what I would do instead, and how confident I am.

---

## The one sentence

> **Your Codex Micro shows which of your Claude Code projects needs you, and
> pressing a key takes you to it.**

That is the product. It is a good product, it is genuinely novel, and nothing
else on the market does it. Everything else in this repo is either support for
that sentence or scope creep away from it.

Here is how the four user-facing surfaces score against it:

| Surface | Serves the sentence? |
|---|---|
| `README.md` headline | **No.** "Your OpenAI Codex Micro, driven by Claude Code. No ChatGPT desktop app." says what it replaces, not what it does for you. The word "project" appears nowhere in the README except in "open-source project" and "pyproject.toml". |
| `README.md` subhead | **No.** Four claims in one sentence, and the best one is missing: "Six Agent Keys that light up with *your* agent's real state" is singular. The whole point is that it is *plural* and each key is a different repo. |
| `freemicro run` (default behaviour) | **Yes.** Keys in, per-project lights out. This is the one thing that is right. |
| Web UI front page | **Half.** The pad diagram is excellent and the "pad is the interface" decision was correct. But the page is about assigning bindings, not about which project is on which key. |
| Shipped `default_keymap.json` | **Half.** `AG00`-`AG05` are `focus_session`, which is right. The seven action keys, the dial and the stick all serve a different product. |
| `freemicro status` | **No.** Prints `working  27961705-b2ca-4c36-be4e-10f59b5a73b6 (0s ago)`. A session UUID. The one command whose job is "what is my pad showing" cannot name a single project. |

The hedge that does the damage is this blockquote, third paragraph of the
README:

> **It's not just the Micro.** FreeMicro is a four-layer pipeline: *agent hooks
> to state engine to renderer registry to hardware*. Any agent in, any RGB
> surface out [...] **The alert never depends on the pad.**

That paragraph is the reason `screen`, `busylight`, `micro-via` and `micro-qmk`
exist, the reason `watch` / `render` / `emit` / `renderers` / `demo` are in the
command table, and the reason `firmware/qmk-keymap/` is in the repo. It is a
second product, it has zero verified users, and it is stated before the reader
has been told what the first product does. Cut it.

---

## 1. The README sells a different product than the one that got built

**Confidence: very high.** This is the highest-impact finding in the document.

Three of the four best things in this project are invisible from the front door:

* **Per-project Agent Keys.** The single best idea here, worked out in detail in
  `docs/AGENT-KEYS.md` with a slot-stability rule that is genuinely thoughtful.
  The README does not mention it. A reader gets "six keys that light up with
  your agent's state" and reasonably concludes it is six copies of one status
  light, which is what it *used* to be and what the doc explicitly calls a
  mistake that was fixed.
* **The web config UI.** `grep -n "\-\-web\|web UI\|browser" README.md` returns
  nothing. `docs/FEATURE-OPPORTUNITIES.md` ranks it #10 and calls it "the
  flagship demo and the strongest single answer to why not just use the
  official app". It is built, it works, and no stranger will ever find it.
* **The menu bar item.** Not in the README, not in the command table, not
  offered by `freemicro start`. It is the only surface that tells you when the
  thing has silently stopped working, which is the #1 way this product dies in
  week two (see §4).

Meanwhile the README spends a full table and eight bullet points on the honest
status of `v.oai.rgbcfg`, the `magic` field, and encoder `act` values. That
material is excellent and belongs in `PROTOCOL.md`, where it already is.

**What I would do:**

* Rewrite the headline to the one sentence, then show it. A picture of three
  keys, three colours, three repo names beats every paragraph currently on the
  page.
* Add a "What it looks like in a day" section built from the trace already
  written in `docs/AGENT-KEYS.md` ("What a user with three projects open
  actually sees"). It is the best writing in the repo and it is in a file nobody
  will open.
* Promote `freemicro config --web` and `freemicro menubar` into the quickstart.
  Two commands, both built, both invisible.
* Move the four-layer pipeline paragraph, the renderer table and half of "Honest
  status" out of the README. Keep the honesty; move the location.

---

## 2. The shipped defaults are one person's preferences, and two of them do not work

**Confidence: very high on the facts, high on the recommendation.**

A stranger unboxes a Codex Micro. It has the factory keycaps on it: `FAST`,
`APPR`, `REJ`, `SPLIT`, `MIC`, `CODEX`. Those caps have printed meanings that
`docs/FACTORY-DEFAULTS.md` §7 documents with confidence "Confirmed". Then they
install FreeMicro and get this:

| Key | Cap they are looking at | What FreeMicro does |
|---|---|---|
| `ACT06` | `FAST` (lightning) | Types a 90-character prompt: *"/review this session's work in depth: what changed, what is risky, what is untested"* and presses Return |
| `ACT07` | `APPR` (check circle) | Types *"Open a pull request for this branch. Review the diff first and tell me anything that should block it."* and presses Return |
| `ACT08` | `REJ` (x circle) | Focuses Google Chrome |
| `ACT09` | `SPLIT` (branch) | Types "continue" and presses Return |
| `ACT12` | `CODEX` | Focuses Terminal.app |
| `ENC_CW` | dial | Types `/effort up` and presses Return |

Five separate problems, in order of severity:

### 2a. `/effort up` and `/effort down` are not valid commands

```
$ claude --effort bogus -p "hi"
Warning: Unknown --effort value 'bogus' - ignoring it and using the default
effort. Valid values: low, medium, high, xhigh, max.
```

The ladder is `low, medium, high, xhigh, max`. There is no `up` and no `down`.
The shipped default binds both dial directions to an invalid command **and sets
`submit: true`**, and the config's own comment says "One event per detent.
Turning fast sends several in a row." So a single flick of the dial submits a
burst of malformed slash commands into the user's agent. That costs tokens, it
pollutes the transcript, and on a bad day it is a prompt.

This is the worst binding in the file, and it is worth asking how it shipped:
nobody turned the dial and read what happened. FreeMicro *already reads*
`effort.level` off every hook payload (`state/hooks.py:_effort_level`, and I can
see `{"level": "high"}` in 4,420 captured events), so it knows the current rung
and could step the real ladder. It sends `up` instead.

### 2b. The default assumes the user owns Wispr Flow

`ACT10` ships as `hold ctrl+cmd+o`, and `webui/starters.py` sets
`DEFAULT_DICTATION = "wispr"`. Wispr Flow is a paid third-party app. A user who
does not have it presses the biggest key on the pad and nothing happens, with no
feedback anywhere. The `DICTATION_CHOICES` structure in `starters.py` is already
the right design and already has three options; the default just has to stop
picking one of them on the user's behalf.

### 2c. Chrome and Terminal are hardcoded, and FreeMicro already knows better

`ACT08` is `{"action": "app", "name": "Google Chrome"}` and `ACT12` is
`{"action": "app", "name": "Terminal"}`. FreeMicro reads `TERM_PROGRAM` in
`focus.py` to do exact-tab targeting for the Agent Keys. It knows the user is on
iTerm2 or Ghostty or Warp, and then binds a key to Terminal.app anyway. Same for
the browser: macOS exposes the default handler for `http`.

### 2d. The keycap comment in the shipped config contradicts itself

From `default_keymap.json`, `_readme`:

```
PHYSICAL LAYOUT of this unit's action keys, confirmed on hardware:
  ACT06 = LAB (flask)   ACT07 = PR (branch)    ACT08 = NAV (cursor)
  ACT09 = PLAY          ACT10 = TERM (prompt)  ACT11 = MIC (voice)
  ACT10 + ACT11 = MIC (one wide keycap, two switches), ACT12 = TERM
```

`ACT10` is TERM on line 5 and half of MIC on line 6. `ACT12` is TERM on line 6
and nothing on line 5. More importantly: *this unit's* keycaps are the owner's,
not the factory's, and the file presents them as the layout. A stranger reading
their own config is told their `ACT06` is a flask when the cap in their hand
says `FAST`.

### 2e. Every default that touches the agent types words into it

Four of seven action keys, plus both dial directions, submit text. That means a
stranger's first exploratory press of an unfamiliar key sends someone else's
prompt wording to their agent, in their repo, at their expense, in a transcript
they will keep.

**The principle I would adopt, and state in the docs:** *the shipped default
never puts words in the user's agent's mouth.* Every default binding is a
keystroke, a window focus, or nothing. Text bindings are something the user
opts into, in the web UI, where they can read what they are about to say.

### 2f. One more thing worth noticing about who wrote these defaults

Every one of the 4,420 hook payloads in `~/.freemicro/hook-events.jsonl` carries
`"permission_mode": "bypassPermissions"`. The person who chose these defaults
runs an agent that never asks permission. That is exactly why `APPR` and `REJ`
ended up bound to a browser and a PR prompt: amber almost never fires on his
machine. For everyone running default permissions, amber is the most common
non-idle state on the pad, and the pad cannot answer it. See §4.

### The default keymap I would ship

Design rules, in order:

1. **The cap tells the truth.** Every binding does what the printed keycap says,
   translated into Claude Code.
2. **Nothing types text.** No default sends a prompt.
3. **Nothing assumes a purchase.** No default depends on an app the user may not
   own.
4. **Nothing is destructive or irreversible.** The worst outcome of a curious
   press is a wasted keystroke.

```json
{
  "version": 1,
  "bindings": {
    "AG00": { "action": "focus_session", "label": "project 1" },
    "AG01": { "action": "focus_session", "label": "project 2" },
    "AG02": { "action": "focus_session", "label": "project 3" },
    "AG03": { "action": "focus_session", "label": "project 4" },
    "AG04": { "action": "focus_session", "label": "project 5" },
    "AG05": { "action": "focus_session", "label": "project 6" },

    "ACT06": {
      "action": "key",
      "key": "shift-tab",
      "label": "FAST - cycle permission mode",
      "comment": "The factory FAST key is composer.toggleFastMode. The Claude Code equivalent of 'stop stopping and go' is Shift+Tab, which cycles normal -> auto-accept edits -> plan mode."
    },
    "ACT07": {
      "action": "key",
      "key": "1",
      "when": "waiting",
      "label": "APPR - approve",
      "comment": "Claude Code's permission prompt numbers its options; 1 is Yes. Guarded by `when` so it can never type a stray digit into your composer."
    },
    "ACT08": {
      "action": "key",
      "key": "escape",
      "label": "REJ - reject / interrupt",
      "comment": "Escape declines a permission prompt AND interrupts a running turn. One key, one meaning: stop. Correct in every state, harmless when nothing is happening."
    },
    "ACT09": {
      "action": "key",
      "key": "cmd+t",
      "label": "SPLIT - branch this project",
      "comment": "The factory SPLIT key forks a thread. A new terminal tab inherits this project's directory, so this is 'start a second agent on the same repo' - which is what the Agent Keys are for."
    },
    "ACT10": {
      "action": "none",
      "label": "MIC - pick your dictation app",
      "comment": "Unbound on purpose. `freemicro start` asks which dictation app you use and writes the right shortcut; the web UI has the same three choices. A guessed shortcut for an app you do not own is a key that silently does nothing."
    },
    "ACT11": {
      "action": "none",
      "label": "MIC (second half - intentionally ignored)",
      "comment": "The wide MIC cap spans two switches and the pad reports both. Binding both fires the key twice."
    },
    "ACT12": {
      "action": "key",
      "key": "return",
      "label": "CODEX - send",
      "comment": "The factory CODEX key is composer.submit. Send."
    },

    "ENC_CW": {
      "action": "key",
      "key": "up",
      "label": "dial - previous",
      "comment": "Factory parity: clockwise is ArrowUp / previous (FACTORY-DEFAULTS 5). Walks prompt history and menu options."
    },
    "ENC_CC": { "action": "key", "key": "down", "label": "dial - next" },
    "ENC_CLK": { "action": "key", "key": "return", "label": "dial press - confirm" },

    "JOY_LEFT":  { "action": "key", "key": "cmd+shift+[", "label": "previous terminal tab" },
    "JOY_RIGHT": { "action": "key", "key": "cmd+shift+]", "label": "next terminal tab" },
    "JOY_UP":    { "action": "key", "key": "page-up",   "label": "scroll back" },
    "JOY_DOWN":  { "action": "key", "key": "page-down", "label": "scroll forward" }
  },

  "lighting": {
    "enabled": true,
    "zones": ["agent_keys"],
    "method": "rgbcfg",
    "on_exit": "off",
    "auto_dim_seconds": 180
  },

  "agent_keys": { "policy": "recent", "slots": ["", "", "", "", "", ""] }
}
```

I ran everything except the four fields marked below through the real parser
(`freemicro keys --list --config ...`); the bindings validate.

**Three things this map needs that do not exist yet, all small:**

| Needed | Why | Where it already is |
|---|---|---|
| `when` on a binding (state gate) | `APPR` is only safe if it cannot fire outside a permission prompt | `FEATURE-OPPORTUNITIES` §2.3, rated **S**. `StateStore.resolve()` already produces the value |
| `page-up` / `page-down` parseable | `input/keys.py` splits combos on `-`, so `page-up` is read as modifier `page` + key `up` and rejected. All three multi-word key names (`page-up`, `page-down`, `forward-delete`) are unwritable, and the parser's own error message lists them as valid options | One-line fix: match the whole token against `KEY_CODES` before splitting |
| `auto_dim_seconds` honoured | See §5 | Not implemented at all |

Interim substitutes if you want to ship today: `APPR` becomes
`{"action": "key", "key": "return"}` (approves the prompt, submits the composer,
harmless on an empty one), and the stick's up/down stay unbound with a comment.

**Confidence:** very high that the current defaults are wrong. High on `REJ` =
`escape` and `CODEX` = `return`, which are exact. Medium on `FAST` = `shift-tab`
and `SPLIT` = `cmd+t`, which are the best available translations rather than
exact matches. `APPR` = `1` rests on Claude Code's permission prompt accepting
digit selection, which I believe but did not put a physical prompt in front of.

---

## 3. The default install produces a pad that does nothing you can see

**Confidence: high.**

`lighting.enabled` is `false` in the shipped config. `freemicro start` asks
"Let FreeMicro drive the LEDs from agent state?" with default **no**, and
`--yes` explicitly refuses to say yes ("The dangerous default stays no"). So the
complete, sanctioned, scripted path is:

```
pipx install ...
freemicro start --yes
```

and it ends with a pad whose lights do not work. The final step even says so:
`Skipping the light show (LED control is off)`. And `_show_next_steps()` does
not mention `freemicro lights --enable` at all, so the user is not told how to
fix the thing they just failed to get.

The reasoning in `FACTORY-DEFAULTS.md` §12 is well argued and was correct when
it was written. It has since been overtaken by the code. FreeMicro now has
`lighting_owner.py` (668 lines), `vendor_app_running()`, per-zone coexistence,
and a `--coexist` mode that provably cannot collide. The premise of the
argument, "we cannot tell whether anything else is driving the pad", is no
longer true.

**What I would do:** default `lighting.enabled` to whether the ChatGPT desktop
app is running at first-run time.

* Not running: enable, say so in one line, mention `--disable`.
* Running: leave off, explain the collision, and offer `--coexist` in the same
  breath. This is the *only* case the original argument was actually about.

The failure mode the original reasoning worried about ("blowing away a working
setup") only exists in the second branch, and the second branch is detectable.

**Confidence: high.** The counter-argument is that a light appearing uninvited
is a trust cost. That is real, but it is smaller than the cost of a product
whose headline feature is off after a guided setup that never showed it to you.

---

## 4. The loop is not closed: the pad tells you it needs you, and cannot answer

**Confidence: very high. This is what makes someone abandon it in week two.**

Here is the week-two experience for a user on default permissions:

1. Agent Key AG02 goes amber.
2. You look up. Good. The product worked.
3. You reach for the pad. There is nothing on it that answers a permission
   prompt.
4. You press AG02, which raises the terminal.
5. You move your hand back to the keyboard and press `1`.

Step 5 is the whole problem. The pad saved you the glance and then handed the
work back. After a fortnight the honest summary is "it is a notification light
that costs a USB port", and a notification light is a solved problem.

FreeMicro is closer to solving this than any other tool could be, and it already
has every piece:

* `notification_type: "permission_prompt"` is in the payload and parsed
  (`state/hooks.py:PERMISSION_PROMPT`). I confirmed both `permission_prompt`
  and `idle_prompt` appear in the captured log.
* The resolved state is available synchronously to the key bridge.
* `FEATURE-OPPORTUNITIES` §2.3 already specifies state-gated bindings and rates
  it **S**, with the note: *"Approve/reject as the same physical key that is
  currently glowing amber is the entire point of a pad next to your keyboard."*
  That sentence is correct and it is the roadmap.

**What I would do:** ship `when` on bindings, bind `APPR` and `REJ` per §2, and
make it the headline of the README. "The key that is glowing amber is the key
that approves it" is the demo. Nothing else in `FEATURE-OPPORTUNITIES` is worth
building before this.

Two smaller things in the same family:

* `idle_prompt` notifications are classified as `None` (no state change). That
  event means "I have been sitting here waiting for you to type for a while",
  which is a genuine needs-you. Worth a look; low confidence that it should be
  amber, high confidence it should not be silently discarded without a comment
  saying why.
* Nothing on the pad distinguishes "waiting on a permission prompt" from
  "waiting on you to type". Both are amber. Effect is free
  (`FEATURE-OPPORTUNITIES` §4.3) and would separate them.

---

## 5. There is no auto-dim, and the pad is a desk object

**Confidence: high.**

`grep -rni "auto_dim\|auto-dim" src/` returns exactly one hit, and it is a
comment in `state/engine.py` explaining that the *done* TTL happens to be the
same number.

The factory blanks the entire pad after three minutes of inactivity. This is
documented as Confirmed in `FACTORY-DEFAULTS.md` §4, with the option list, the
millisecond values, the exact payloads it sends, and the three things that wake
it. §12 puts `auto_dim: 180` in the recommended config and says the promise
worth keeping is "same colours, same effects, same speeds, **same dim timing**,
same blank-on-exit".

FreeMicro implements every part of that sentence except the dim timing. And it
matters more here than it does for the vendor, because FreeMicro's `idle` is
`#FFFFFF` at `brightness: 1.0` and `idle` is what a live project shows most of
the time. Six live projects means six full-brightness white LEDs, on your desk,
forever, including at 1 a.m., including while you sleep in the same room,
including on battery.

Copying half of a design is worse than copying neither half. The factory's
full-brightness white is only tolerable *because* it goes dark after three
minutes.

**What I would do:** implement §4 exactly as documented. Blank on inactivity
(not reduce brightness, per the doc), wake on any HID event or any change in the
lighting model, default 180 seconds, and expose it as one config key and one
line in the web UI's Lights section. It is a timer and two writes the code
already knows how to send.

**Also missing in the same category:** a low-battery cue. The pad is verified
fully functional over Bluetooth, `device.status` already returns `battery` and
`is_charging`, and `doctor` already prints them. A wireless status light that
dies silently mid-session is a status light you stop trusting. Off by default
per `FACTORY-DEFAULTS` §10a, but it should exist.

---

## 6. Nothing can remove FreeMicro, and stopping it can leave your pad stuck lit

**Confidence: very high on both halves.**

There is no `freemicro uninstall`. There are two partial removals in two
different places (`freemicro install --uninstall` for the hooks, `freemicro
daemon uninstall` for the LaunchAgent), and nothing that names the full
footprint or removes it.

The full footprint, from the code:

| What | Where | Removed by |
|---|---|---|
| 7 hook entries | `~/.claude/settings.json` | `freemicro install --uninstall` |
| LaunchAgent plist | `~/Library/LaunchAgents/com.freemicro.daemon.plist` | `freemicro daemon uninstall` |
| Pad config + backup | `~/.freemicro/keymap.json`, `keymap.json.bak` | nothing |
| Engine config | `~/.freemicro/config.json` | nothing |
| Session state | `~/.freemicro/state/` | nothing |
| Slot assignment | `~/.freemicro/slots.json` | nothing |
| Menubar status cache | `~/.freemicro/status.json` | nothing |
| Locks | `~/.freemicro/pad.lock`, `menubar.lock` | nothing |
| Daemon logs | `~/.freemicro/logs/` | nothing |
| Tk probe cache | `~/.freemicro/tk-probe.json` | nothing |
| Saved layouts | `~/.freemicro/layouts/` | nothing |
| Raw hook log | `~/.freemicro/hook-events.jsonl` | nothing. **This is 11.5 MB on the developer's own machine right now, and unbounded** |
| Input Monitoring grant | macOS TCC | nothing, and nothing tells the user it is still listed |
| Accessibility grant | macOS TCC | nothing, same |
| **The LEDs themselves** | the pad | see below |

### The pad can be left lit with nothing driving it

There is no signal handler anywhere in `src/`:

```
$ grep -rn "SIGTERM\|signal\." src/freemicro
src/freemicro/renderers/micro_via.py:12:screen fallback carries the signal.
```

`lighting.on_exit: "off"` is applied in `renderer.close()`, which runs in the
`finally` of `_run_pipeline`. `launchctl bootout` sends `SIGTERM`, and Python's
default `SIGTERM` disposition terminates the process without unwinding, so the
`finally` never runs. I confirmed this empirically. Same for logout, shutdown,
`launchctl kill`, and any `pkill`.

So `freemicro daemon uninstall` prints "The pad is free for `freemicro run`
again" and leaves the pad glowing in FreeMicro's last colour, with nothing
driving it, and no software installed that could turn it off. That is the exact
scenario `FACTORY-DEFAULTS.md` §12 identifies as the worst outcome: *"A silent
LED takeover is not reversible by the user [...] The user is left with a pad
stuck in our colours and no clue why."*

### What a complete uninstall covers

```
freemicro uninstall [--keep-config] [--dry-run]
```

1. **Blank the pad first**, before anything else is removed, while the code that
   knows how is still installed. Print that it did.
2. Stop and remove the LaunchAgent, and any menubar login item.
3. Remove FreeMicro's hooks from `~/.claude/settings.json`, leaving everyone
   else's, and say "restart Claude Code".
4. Remove `~/.freemicro/` entirely, or with `--keep-config` keep only
   `keymap.json` and `layouts/` and say where they are.
5. **Print what it cannot remove**, with the exact path: the two TCC entries
   under Privacy and Security, and `pipx uninstall freemicro` (or the venv).
   Offer to open the Privacy pane, the same way `start` does.
6. `--dry-run` lists all of it and touches nothing.

Independently of the command: **install a `SIGTERM` handler** that raises
`KeyboardInterrupt` (or sets the run loop's stop flag) so the existing `finally`
does its job. Three lines, and it fixes logout, shutdown, `daemon uninstall`,
and every stale `pad.lock` at once.

Also: `hook-events.jsonl` needs a size cap and a rotation, or it should be
written to a temp dir. An 11.5 MB append-only log of full hook payloads,
including `cwd` and every field the payload carries, sitting in a user's home
directory is a debugging tool that outstayed its debugging session.

---

## 7. The screen renderer's window cannot open on any machine, and the README claims it can

**Confidence: very high. This is the clearest delete in the repo.**

The brief asked whether the screen renderer still earns its place. The answer is
sharper than expected: **the GUI half of it has never run, anywhere, including
on the developer's own machine, and cannot.**

```
$ grep -rn "tk_is_safe\|probe=True" src/ tests/
src/freemicro/diagnostics.py:300:        "tk_window": tk_is_safe(),
src/freemicro/cli.py:862:    if tk_is_safe():
src/freemicro/renderers/screen.py:68:def tk_is_safe(probe: bool = False) -> bool:
src/freemicro/renderers/screen.py:167:        if not tk_is_safe():
```

Nothing in the codebase ever calls `tk_is_safe(probe=True)`. Without it, the
function returns `False` unless a cache file exists, and the cache file is only
written by the probe. The cycle never starts:

```
$ ls ~/.freemicro/tk-probe.json
ls: /Users/Eli/.freemicro/tk-probe.json: No such file or directory
```

So `ScreenRenderer._ensure_window()` returns `False` on every machine on the
planet, forever. The 100 lines of Tk window code are unreachable. This is also
why `freemicro doctor` prints a warning with a blank explanation:

```
  [warn]  screen fallback can't open a window - using the console
          
          -> Harmless. Status prints to your terminal instead.
```

The blank line is `tk_unsafe_reason()`, which is `""` because nothing ever
probed. Doctor is warning about a capability it never tested.

Meanwhile the README says:

| `screen` | Always-on-top chip; ANSI console fallback | No | ✅ **Guaranteed** - always available |

The "always-on-top chip" does not exist. What actually ships is one ANSI status
line printed to the terminal you already have open, and only when `run` is not
headless.

**And it is guarding a failure mode that no longer exists.** The renderer's
entire justification is "the alert never depends on the pad", written when the
LED path was unproven. LEDs are now verified over USB and Bluetooth, on
hardware, in both directions.

**What I would do: delete `renderers/screen.py`.** With it goes `tk_is_safe`,
`tk_unsafe_reason`, the subprocess probe, the disk cache, the doctor check, the
`--no-screen` flag, the `"screen" not in seen` special case in
`renderers/base.py:select()`, the README row, and the "guaranteed fallback"
claim.

Keep exactly one thing: `freemicro run` should print `state: working` when the
state changes. It already does that (`cli.py:1721`), and it is conditional on
the screen renderer *not* being present, so removing the renderer makes that
line unconditional, which is what you want.

The counter-argument is "it costs nothing to keep". It does not cost nothing. It
cost a hard `abort()` crash this week, it costs a subprocess probe and a disk
cache written to defend against that crash, it costs a warning in `doctor` that
cannot be explained, and it costs a false claim in the README. Its actual value
is a status line, which is four lines of code.

Two smaller notes on the way out: the screen palette (`renderers/base.py:PALETTE`)
uses Apple system colours, not the factory palette, so the chip and the pad
showed different blues and greens for the same state. And it is the reason
`freemicro watch` exists, which is a command for driving "non-pad targets".

### `busylight`

54 lines, an optional dependency, and nobody has ever run it. The code is cheap.
The *claim* is not: the README lists it as "✅ **Reliable**" and it is the second
supporting pillar of the "any RGB surface out" hedge in §1.

`SPEC.md` §3 already says "**VibeSignal** owns commercial busylights". I would
delete the renderer and put one line in the README pointing blink(1)/Luxafor
owners at VibeSignal, which does that job properly and is not competing with
this one. Focus is worth more than a checkbox on a comparison table.

If that feels too brave: keep the 54 lines, but change the README status from
"Reliable" to "Untested, PRs welcome", and take it out of the architecture
diagram. Claiming reliability for code no one has executed is the same class of
error as the Tk row.

**Confidence:** very high on deleting the screen renderer, medium-high on
deleting busylight.

---

## 8. Onboarding installs things without asking what the user wants

**Confidence: high.**

`freemicro start` runs eight steps. It asks four yes/no questions: open a
Settings pane, wait for the pad, drive the LEDs, install hooks, install the
daemon. It never asks the one question that determines what any of it is for:

> **What do you want the pad to do? Show you which project needs you? Do things
> when you press keys? Both?**

That answer changes almost everything downstream:

* **Lights only:** you need Input Monitoring, the hooks, the daemon, and
  `lighting.enabled`. You do not need Accessibility, and being sent to a second
  System Settings pane for a permission you will never use is pure friction.
* **Keys only:** you need Input Monitoring and Accessibility. You do not need
  the Claude Code hooks at all, and `step_hooks` returning failure currently
  makes the whole command exit non-zero (`return 0 if hooks_ok else 1`).
* **Both** (most people): the current flow, which is correct for them.

Two more things it should ask, both of which it has the machinery for and does
not use:

* **Which dictation app?** `webui/starters.py:DICTATION_CHOICES` has Wispr Flow,
  macOS Dictation, and "something else", each with setup text and a hold-versus-
  toggle warning. `start` guesses Wispr instead of asking. One question removes
  the single most common "the big key does nothing" case.
* **Which terminal and which browser?** `focus.py` reads `TERM_PROGRAM`. Offer
  the detected value as the default and write it into `ACT12`.

What it should **stop** asking or saying:

* **"Wait for it to appear?"** followed by a 60 second dotted progress line, when
  the whole flow works fine without a pad and says so. Detect, report, carry on.
* **The ChatGPT contention step as a gate.** `step_contention` returning False
  aborts the entire setup with "Stopped. Quit the ChatGPT app and run `freemicro
  start` again." That was right before `lighting_owner.py` existed. It is now an
  informational line plus an offer of `--coexist`.
* **The intro paragraph.** Six lines of prose about what the command is going to
  do, before it does it. Do it and narrate as you go; the section headers already
  narrate.

And what it should say at the end and does not: `_show_next_steps()` never
mentions `freemicro lights --enable` (even when lighting is off, which is the
default), `freemicro config --web`, or `freemicro menubar`. Three of the four
most useful things a new user could do next.

---

## 9. What is missing that a user would expect

Ranked by how surprising the absence is.

| Missing | Why the absence is surprising | Confidence |
|---|---|---|
| **Sound** | This is an ambient-awareness product and it has no audio option at all. `grep -rniE "afplay\|NSSound\|beep" src/` returns nothing. A pad that goes amber while you are looking at a different monitor has not told you anything. `afplay /System/Library/Sounds/*.aiff` is one subprocess and no dependency. It should be per-state, off by default except possibly `waiting`, and it needs a quiet-hours switch or people will disable it in a day. `SPEC.md` §3 even cites explainx.ai's "Claude Code sound + traffic-light via hooks" as prior art and then does not do the sound half | very high |
| **Answering a permission prompt from the pad** | See §4 | very high |
| **Auto-dim** | See §5 | high |
| **A complete uninstall** | See §6 | very high |
| **`freemicro status` naming projects** | It prints session UUIDs. `docs/AGENT-KEYS.md` "Wiring required" §1 calls this "the single most useful missing line", supplies the eight lines of code, and it is still not wired. Someone read that doc, wrote the code it needed, and skipped the one item it asked for | high |
| **Low-battery warning** | The pad is battery-powered and verified over Bluetooth. `device.status` returns `battery` and `is_charging`, and `doctor` already prints them. A wireless status light that dies without warning is a status light you stop trusting | high |
| **Telling you when it has stopped working** | If Input Monitoring is revoked, the daemon dies, or the pad drops for good, the pad simply goes dark and nothing anywhere says why. The menu bar item solves exactly this and no one is told it exists. Silence and "nothing needs you" are the same picture, which is the worst property a status display can have | high |
| **`freemicro pause` / do not disturb** | Available only as a menu bar click. Not in the CLI, not a config key, not a bindable action. On a call, in a demo, screen-sharing: you want one key that makes the pad shut up for an hour | medium |
| **A "what happened while I was away" answer** | The pad forgets. `done` decays after 180 s, `working` after 120 s. Come back from lunch and everything is white. `freemicro status` should be able to say "web finished 40 minutes ago" | medium |

---

## 10. What is actively annoying

Ordered by how often a user hits it.

**It teaches you its internals instead of doing what you asked.** `freemicro
keys --list` is the command you run to answer "what does my pad do". It answers
in 45 lines: your bindings, then `zones`, `on exit`, per-state
`#FFFFFF solid b=1 s=0`, joystick `deadzone`/`origin`/"directions (from angle 0,
by equal steps)", then the complete nine-row action-kind reference with required
and optional parameter lists, then a hex swatch line. Twenty of those lines are
about FreeMicro. `--list` should print bindings. `--list --all` can print the
rest.

**The vocabulary is the implementation's, not the user's.** `zones`, `method`,
`rgbcfg`, `preview`, `effect: shallow-breath`, `magic`, `action kinds`,
`bindings`, `renderers`, `priority=100`, `on_exit`, `origin`, `REARM_RATIO`. The
web UI already solved this ("Outcomes, not our vocabulary", `docs/WEB-UI.md`) and
the CLI did not get the memo. The six outcomes in the web UI are the right
vocabulary everywhere.

**Every failure lectures.** `_pad_is_taken` prints five lines with three arrows.
`_open_pad` prints five. `_set_lighting_zones` prints eight, including a
paragraph about the trade-off between per-key detail and colours nothing
overwrites. Individually each is defensible and well written. Collectively the
product explains itself constantly, and text people stop reading is text that
stops working. The pattern to adopt: **one line of what happened, one line of
what to do, and a `--why` flag or a doc link for the paragraph.**

**`freemicro status` leads with developer concerns.** On my run, the first nine
lines were three "started before the code it loaded changed" warnings about a
running process, a web UI, and a menu bar. That is a hot-reload staleness check.
The user asked what state their agent is in. Answer that first.

**The error message recommends key names the parser rejects.**

```
$ ... "key": "page-up"
Config error: binding for 'JOY_UP': unknown modifier 'page' in 'page-up'
$ ... "key": "pageup"
unknown key 'pageup'; use one of: ..., page-down, page-up, period, ...
```

`page-up`, `page-down` and `forward-delete` are listed in
`docs/CUSTOMIZING.md` and in the error message itself, and all three are
unwritable because `-` is a combo separator. The tool is telling you to type
something it will refuse.

**The shipped config file is a manual.** `default_keymap.json` opens with a
40-line `_readme` array, and individual bindings carry six-line `comment` arrays.
The JSON-versus-TOML rationale in `CUSTOMIZING.md` explains that the comments
buy back readability. They do not: they push the actual configuration below the
fold and the first thing a user reads is a contradiction about which keycap is
on `ACT10` (§2d). Cut `_readme` to five lines and one URL. The web UI is the
documentation now.

**Doctor warns with a blank reason** (§7).

**The daemon permission dance.** Grant Input Monitoring to the launchd binary,
then `freemicro daemon uninstall && freemicro daemon install`. The README is
honest that this cannot be removed, and it is still the worst moment in the
product. At minimum, `daemon logs` and the menu bar should offer a single
"re-register the daemon" action so the user does not have to type two commands
in the right order.

---

## 11. What would make someone abandon it in week two

Unsentimentally, in order of likelihood:

1. **The loop is not closed.** Amber tells you, and you still reach for the
   keyboard. Two weeks in, the value is "a light" and lights are cheap. (§4)
2. **It goes quiet and does not say so.** Any of: the daemon lost its Input
   Monitoring grant, the pad dropped and did not come back, a Claude Code update
   changed a payload field. The pad goes dark, dark is indistinguishable from
   idle, you stop looking at it, and a status display you have stopped looking at
   is uninstalled. The menu bar fixes this and is undiscoverable. (§9)
3. **Six white LEDs at full brightness, all night.** No auto-dim, no brightness
   schedule, no quiet hours. The pad becomes a thing you unplug in the evening,
   and things you unplug do not get plugged back in. (§5)
4. **The keys type things they did not ask for.** Someone spins the dial in week
   one, sends four `/effort up` messages into a real session, and concludes the
   pad is not safe to touch. After that it is a light, not a control surface,
   and see #1. (§2a)
5. **The first Claude Code update after install.** Hook payload fields are the
   dependency this product is built on and it does not own them. There is a
   `_hook_drift()` check for missing *events*, which is good, and nothing at all
   for changed *fields*. `notification_type`, `prompt_id`, `background_tasks`,
   `effort.level` and `reason` are all load-bearing and all undocumented
   externals. A monthly `freemicro selftest` nag, or a check that fires when a
   state has not been seen in N days, would catch it.
6. **They wanted a macro pad.** Someone who bought the Micro to type things
   discovers that the seven action keys ship with two prompts, two app switchers,
   a dictation shortcut for an app they do not own, and a dial that submits
   invalid commands. They open the web UI (if they find it) and rebuild it by
   hand, which is fine, or they do not, which is not. (§2)

---

## Cut this

Deletions, ordered by value returned per line removed.

| Cut | Lines | Why |
|---|---|---|
| **`renderers/screen.py`** and everything supporting it: `tk_is_safe`, `tk_unsafe_reason`, the subprocess probe, `~/.freemicro/tk-probe.json`, the doctor check, `--no-screen`, the `select()` special case, the README row | ~300 | The window cannot open on any machine (§7). It guards a failure mode that hardware verification closed. Keep four lines that print `state: working` |
| **The "any agent in, any RGB surface out" positioning** | 1 paragraph, but it is load-bearing | It is why four of the five renderers exist, why six of the eighteen CLI commands exist, and why the README's first three paragraphs are not about the product (§1) |
| **`renderers/busylight.py`** | 54 | Never run. Point blink(1) owners at VibeSignal, which `SPEC.md` already concedes owns that space |
| **`renderers/micro_via.py`, `renderers/micro_qmk.py`** | ~200 | Both target *other pads*. The README says the Codex Micro exposes no `0xFF60` channel and QMK does not run on its ESP32. Two renderers for hardware this project does not support, listed on the product's own renderer table |
| **`firmware/qmk-keymap/`** | a directory | Firmware for a path the project has documented as impossible on this silicon (`SPEC.md`, `LED-STRATEGY.md`) |
| **`presets/claude-code.keyboard.json`** | 12 | Still says `"vendorId": "0x0000"` and *"pending Milestone 0 confirmation of the pad's VID/PID"*. Milestone 0 is done; the VID/PID have been in `PROTOCOL.md` for days |
| **`presets/claude-code.input.json`** | 40 | A Work Louder Input preset whose bindings (dial to history, joystick to `Esc`/`EscEsc`/`@`/`!`) contradict everything the project learned once it could read the pad directly. It is a fossil from before the protocol was solved |
| **`freemicro watch`** | ~30 | "Lights only, no key bridge, for non-pad targets." With screen and busylight gone there are no non-pad targets |
| **`freemicro emit`, `freemicro render`, `freemicro renderers`** from the README command table | 0 | Keep the commands; they are useful for development. Take them out of a user-facing table that already has fifteen rows. `renderers` prints `priority=100` |
| **The `_readme` block in `default_keymap.json`, down to ~5 lines** | ~35 | It is longer than the configuration it documents, and its keycap table contradicts itself and the factory (§2d) |
| **The action-kind reference from `keys --list`** | 0 | Move behind `--all`. It is nine rows of internals in front of the answer to "what does my pad do" |
| **`step_intro()` and the `step_device` wait loop in `onboarding.py`** | ~25 | Narration before the work, and a 60 second dotted wait for a device the flow does not need |

Net effect: about 700 lines gone, one renderer instead of five, a README that
can open with the product instead of the architecture, and a comparison table
that stops claiming reliability for code nobody has executed.

---

## Summary

| # | Finding | Confidence |
|---|---|---|
| 1 | The README sells a different product than the one that got built. Per-project Agent Keys, the web UI and the menu bar are all invisible from the front door | very high |
| 2 | The shipped defaults are one person's prompts, and the dial binding submits an invalid command on every detent | very high |
| 3 | `lighting.enabled` defaults to false, so the sanctioned setup path ends with a pad whose lights do not work | high |
| 4 | The loop is not closed: amber tells you, and nothing on the pad can answer it. This is the week-two abandonment | very high |
| 5 | Auto-dim is documented as Confirmed factory behaviour, recommended in the project's own config, and not implemented | high |
| 6 | There is no uninstall, and `SIGTERM` leaves the pad lit with nothing driving it | very high |
| 7 | The screen renderer's window cannot open on any machine, and the README calls it Guaranteed | very high |
| 8 | Onboarding never asks what the user wants the pad for | high |
| 9 | No sound, on an ambient-awareness product | very high |
| 10 | The CLI talks in zones, methods, action kinds and priorities, and lectures on every failure | high |

The single highest-leverage change is **§4 plus §2 together**: ship `when` on
bindings, put approve on `APPR` and reject on `REJ`, and lead the README with
"the key that is glowing amber is the key that approves it". That is one small
feature, one config file, and one paragraph, and it converts this from a very
good status light into the thing it is actually for.
