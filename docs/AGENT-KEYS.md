# The six Agent Keys

> Six projects, one key each. Glance to see which one needs you; press to land
> in it.

The pad's top row is six individually addressable RGB keys. Until now FreeMicro
lit all six with the single winning state — six copies of one status light — and
bound them to slash commands typed into whatever window happened to be focused.
Both were wrong, and both are fixed here:

* each key is coloured by **its own project's** state, and
* pressing a key **raises that project's terminal tab**.

---

## What a key is bound to

**A project directory. Not a session id.**

This is the decision everything else follows from, so it is worth being explicit
about why. A Claude Code "session" is a terminal process with a UUID that dies
when you close the tab; the next tab gets a new UUID. A pad bound to session ids
would need re-teaching several times a day, and a pad you have to keep
re-teaching is worse than no pad at all.

A project directory is stable for as long as the work is. It survives restarts,
`/clear`, a crashed tab, a reboot, a laptop lid. It is also how people actually
think: *"the API repo is waiting on me"*, never *"session 0f2c…9a is waiting on
me"*.

Hook events already carry `cwd`, so nothing new had to be plumbed. Several
sessions in one directory collapse into one project, and that project shows the
**highest-priority** state among them (`waiting > error > done > working >
idle`). If any tab in a repo needs you, that repo's key says so.

## The slot-stability rule

An indicator you cannot trust is noise. If the key that meant "the API repo"
silently became "the docs repo" because activity order shifted, you would have
to *read* the pad instead of glancing at it.

So slots are **sticky**, in this precedence:

1. **A pinned slot is absolute.** Under `pinned`/`manual` a configured slot
   always means that directory. Nothing live there? The key is dark — it is
   never lent to another project.
2. **An incumbent keeps its key.** A project holding slot *n* keeps slot *n* for
   as long as it stays live, however the activity order moves around it. Nothing
   is ever evicted for being less recent.
3. **Only vacated slots are refilled.** When a project goes stale (the store's
   TTL) or ends, its key frees, and the most-recently-active project without a
   key moves in — to the lowest free index, so keys fill left to right.
4. **A free key remembers its last project.** If a project disappears and its
   key has not been reused, coming back puts it on the same key.
5. **First run** fills `AG00`–`AG05` with live projects, most recent first —
   factory `recent` behaviour, so an empty config is immediately useful.

The consequence, stated plainly: **with more than six live projects the seventh
does not appear until a key frees.** Bumping an incumbent for whoever moved most
recently is exactly the unreadability rule 2 exists to prevent.

### What a user with three projects open actually sees

This is a real trace, not an illustration — it is what the resolver produces:

| Moment | AG00 | AG01 | AG02 | AG03–AG05 |
|---|---|---|---|---|
| Start work in `api` | `api` blue | dark | dark | dark |
| Open `web`, start a task | `api` blue | `web` blue | dark | dark |
| Open `docs` | `api` blue | `web` blue | `docs` blue | dark |
| `api` asks for permission | **`api` amber** | `web` blue | `docs` blue | dark |
| You press AG00 | *api's terminal tab comes to the front* | | | |
| `web` finishes | `api` amber | **`web` green** | `docs` blue | dark |
| 3 min later, unread green decays | `api` amber | `web` white | `docs` blue | dark |
| You close `docs`'s terminal | `api` amber | `web` white | dark *(remembers `docs`)* | dark |
| Start a fourth project | `api` amber | `web` white | **`new` blue** | dark |

Three things to notice:

* **Nothing ever moves.** `api` was the first project you touched, so it is
  `AG00` for the rest of the day. The pad becomes muscle memory.
* **Keys fill left to right as projects appear**, and only the *first* fill on a
  cold start is ordered by recency.
* **An unused key is off, not dim** — so the number of lit keys is the number of
  live projects, countable without reading anything.

The `docs` memory in row 8 lasts only until something else takes the key: reopen
`docs` before starting a fourth project and it lands back on `AG02`.

## Colours

Exactly the factory values (`docs/FACTORY-DEFAULTS.md` §1a), sent one
`v.oai.thstatus` entry per key:

| State | Colour | Meaning |
|---|---|---|
| idle | `#FFFFFF` | live, nothing happening |
| working | `#304FFE` | thinking |
| done | `#00FF4C` | finished and **unread** |
| waiting | `#FF6D00` | blocked on you |
| error | `#FF0033` | something broke |
| *empty* | `{c: 0, b: 0, e: off}` | no project on this key |

### Green decays, on purpose

The factory's green means **unread**, not "completed" — it clears when you look
at the thread. FreeMicro has no "the human looked at it" signal: Claude Code
fires no hook when you switch terminal tabs, and watching window focus would
mean a background accessibility poll for a cosmetic nicety. So the decay is a
timer (`state.done_ttl_seconds`, default 180 s), applied **per project** — one
repo going quiet never clears another repo's green. Without it the pad goes
green after your first task and stays green forever, which stops matching the
hardware within minutes.

### Blue expires too — an interrupt fires no hook

Press Escape to interrupt an agent and Claude Code emits **nothing** (confirmed
against 152 captured payloads). The last thing that session said was `working`,
so its key would sit blue — "still thinking" — until the 30-minute session TTL.
A status light nobody believes is worse than no status light, so a `working`
claim that stops being renewed is retired to `idle`:

| Situation | Grace | Why |
| --- | --- | --- |
| Quiet, nothing known to be running | `state.working_ttl_seconds`, 120 s | Real work emits a hook every few seconds; 120 s is Claude Code's own default Bash timeout |
| Last event was `PreToolUse` | `state.tool_ttl_seconds`, 600 s | A tool call is *expected* to be silent — a five-minute build must not blank its key |
| A background task (subagent) is running | never expires | The turn stopped; the work did not |
| `waiting` / `error` | never expires | You are the blocker; it waits as long as you do |

Set either value to `0` to switch the check off. The rule lives in one place
(`freemicro.state.engine.effective_state`) and is applied where sessions are
read, so the keys, the resolved state and `freemicro status` cannot disagree —
`status` reports such a session as `idle`, and the record remembers what it
claimed so a reader can say *"idle (was working)"* rather than either half.

`prompt_id` — the turn id every hook payload carries — settles the same question
exactly rather than statistically: a **new** `prompt_id` arriving while the
previous turn never received its `Stop` is proof the previous turn was
abandoned. That is recorded on the session (`interrupted`), which is why the
pad can say "the last turn was interrupted" instead of just "quiet". It cannot
replace the timer, though: after an interrupt no event arrives at all, so
nothing would fire until the user came back and typed something.

## Pressing a key focuses a terminal

An Agent Key press is a **window-focus action**, not a keystroke into the
current window. Typing `/resume` into whatever is focused is wrong exactly when
you reach for the pad: when you are not already looking at the right window.

How a tab is found, in preference order:

1. **By controlling tty.** Terminal.app exposes `tty` on every tab and iTerm2 on
   every session, so a tty match names **one exact tab**. That tab is selected
   and its window raised.

   Getting the tty is less obvious than it looks. A hook cannot read it off
   itself: Claude Code spawns hooks with pipes for stdin/stdout *and* outside
   the terminal's session, so `/dev/tty` fails to open and `ps` reports `??`
   for the hook process. Every session record therefore used to store
   `tty: ""`, and every Agent Key fell through to case 2 — the pad activated
   Terminal but could not pick the tab.

   The session's **pid** is the way in. The record already stores it, and
   `ps -o tty= -p <pid>` names the tab, walking up to the `claude` process when
   the hook itself has no terminal. That derivation runs at hook time, and
   again at focus time for any record whose stored `tty` is blank — so a
   session that has not re-emitted since is still reachable. `ps` prints
   `ttys003` where Terminal reports `/dev/ttys003`; both spellings are
   normalised to the second.

   A pid that has exited, or never had a terminal, yields nothing and the key
   falls back to case 2 or 3. It never guesses a tab.
2. **By app.** Emulator known (`TERM_PROGRAM`) but not scriptable — Ghostty,
   Warp, WezTerm, kitty, VS Code, Cursor — so the app is activated and nothing
   more. Right app, unknown tab.
3. **Nothing.** Not enough information, or the project is not running. The key
   does *nothing at all*, and `freemicro keys --dry-run` / `keys --list` says
   which case it is.

Case 3 is a feature. **Focusing the wrong window is worse than focusing
nothing**, because the next thing you type lands somewhere you did not intend
and you may not notice. Every path fails closed: the AppleScript matches on tty
or does nothing, and it never launches an app that is not already running
(`if application "X" is running` is checked first, so a keypress can never open
an empty Terminal window).

The tty is the only value from an on-disk record that reaches `osascript`. It is
pattern-checked (`^/dev/[A-Za-z0-9._/-]{1,64}$`) rather than escaped — a device
path has no legitimate reason to contain a quote or a newline.

### Known limits, stated precisely

* **Terminal.app and iTerm2 only** get exact-tab targeting. No other macOS
  terminal exposes a tab's tty to AppleScript. Everything else gets app-level
  focus, or nothing if it does not set `TERM_PROGRAM`.
* **tmux/screen/ssh**: the controlling tty belongs to the multiplexer's server
  or the remote host, so it will not match a local tab. The result is case 2 or
  3 — never the wrong window.
* **A daemonised Claude Code** (launchd, CI, a GUI client) has no controlling
  terminal. Nothing to raise, so nothing happens.
* **The tab must still exist.** A session record outlives a closed tab until its
  TTL; pressing that key does nothing rather than guessing.

## Configuration

A top-level section — deliberately outside `lighting`, because it is about
*which project* a key follows, not about colour. This is the exact shape the web
UI already writes.

```json
"agent_keys": {
  "policy": "recent",
  "slots": ["", "", "", "", "", ""]
}
```

| `policy` | Behaviour |
|---|---|
| `recent` *(default)* | The six most recently active projects fill the keys automatically, subject to the stability rule. Zero config. |
| `pinned` | Slots you name keep their key forever; the rest fill from `recent`, never stealing a pinned key. |
| `manual` | Exactly the slots you name. Unnamed keys stay dark. |
| `mirror` | The old behaviour: all six keys show the one winning state. |

`slots` is six entries in `AG00`–`AG05` order, each a directory (`~` is
expanded) or `""`/`null`. Pins are **preserved but ignored** under `recent` and
`mirror`, so switching policy back and forth never loses them.

A pin naming a directory that does not exist is a **warning, not an error** —
projects get archived and drives get unmounted, and refusing to start the whole
pad over one of six pins would be the wrong trade.

### Bindings

The shipped default binds every Agent Key to the new action kind:

```json
"AG00": { "action": "focus_session" }
```

The slot is filled in from the input id, so you never repeat the number printed
on the key. Two variations worth knowing:

```json
"AG03": { "action": "focus_session", "project": "~/code/api" }
"AG04": { "action": "focus_session", "fallback": false }
```

`project` nails one key to one repo regardless of policy. `fallback: false`
turns off case-2 app activation for that key: exact tab or nothing.

And it is still just a binding — anyone who wants `/resume` back writes
`{"action": "text", "text": "/resume", "submit": true}`.

## How the pieces fit

| Concern | Module |
|---|---|
| Config shape, validation, warnings | `padconfig.py` (`agent_keys`) |
| Projects, slots, the stability rule | `agentkeys.py` — **pure**, no I/O |
| Per-key `v.oai.thstatus` messages | `renderers/micro_leds.py` |
| Session `cwd` + terminal identity | `state/engine.py` |
| Shared slot assignment | `state/slots.py` |
| Finding and raising a terminal tab | `focus.py` |
| The `focus_session` action kind | `input/actions.py` |

`agentkeys.resolve_slots(config, sessions, previous=…, now=…)` is a pure
function; `SlotResolver` is the only stateful thing and all it holds is the
previous assignment. That is what makes slot stability testable directly rather
than inferred from LED traffic (`tests/test_agentkeys.py`).

### Why the assignment is written to disk

Two processes have to agree: the **renderer** decides what colour each key is,
and the **key press** has to jump to whatever that key was showing. If they
resolved independently they could disagree — a cold-started resolver fills by
recency, a warm one by incumbency — and pressing an amber key would land you in
the wrong project. So the renderer publishes the assignment to
`~/.freemicro/slots.json` and the key handler seeds from it. It is a cache, never
a source of truth: missing or corrupt costs a cold start and nothing else.

**Caveat:** only a *rendering* process publishes. With `lighting.enabled: false`
the pad shows nothing, so nothing is published, and each key press resolves cold
(recency order). That is coherent — there is no light to contradict — but it
means presses are only *sticky* while something is driving the LEDs.

---

## Wiring required

Everything above works today through `freemicro run` / `watch` / `daemon`. What
is left is CLI surface, in files this work does not own.

### 1. `freemicro status` should print the slot assignment

The single most useful missing line. `cli.py`, in `cmd_status`:

```python
from freemicro import focus

slots = focus.current_slots()          # never raises; [] if unreadable
if slots:
    print("Agent Keys:")
    for slot in slots:
        print(f"  {slot.key_id}  {slot.describe()}")
```

and in the `--json` branch, alongside `sessions`:

```python
"agent_keys": [slot.to_dict() for slot in slots],
```

`AgentSlot.to_dict()` already returns `index`, `key`, `path`, `label`, `state`,
`last_active`, `pinned`, `empty`, `sessions`.

### 2. `freemicro keys --list` already works

`focus_session` describes itself per binding ("focus web — iTerm2 tab
/dev/ttys009", "nothing on this key (…)"), so the dry-run and list output are
correct with no CLI change. Worth a line in the help text pointing at
`docs/AGENT-KEYS.md`.

### 3. Optional: `freemicro slots --clear`

`state.slots.clear()` forgets the assignment and lets the pad re-fill from
scratch. One line, occasionally handy after a big reshuffle. Not required.

### 4. The web UI can drop its warning banner

`webui/api.py` sets `"agent_slots_wired": False` with a comment pointing here.
The section is now parsed, validated and consumed, so that flag can flip to
`True`. Two notes for whoever owns that file:

* the UI's slot picker offers **session ids**; slots are now **project
  directories**. The picker should offer `session.cwd` values (deduplicated)
  rather than `session_id`, or pinning will write ids that never match.
* `padlink.preview(...)` and `api.preview` default `method="preview"`. That
  method lights nothing on firmware v0.4.1 — the default should be `"rgbcfg"`
  to match `padconfig.LightingConfig.method`, or live preview will appear
  broken.

### 5. Hook payload

`cmd_hook` passes `cwd` already, which is all that is required. Terminal
identity is captured inside `StateStore.update()` — deliberately, because the
hook process is the only place it is available, and doing it there means no
change to `cli.py`.

---

## Tests

`tests/test_agentkeys.py`, no hardware and no terminal:

* config parsing, the web UI's exact shape, `~` expansion, every rejection
  message, and warnings that do not fail the load;
* grouping — several sessions per project, priority within a project, per-project
  green decay, sessions with no `cwd`, duplicate basenames disambiguated;
* **slot stability** — incumbency across reordering, left-to-right filling, a
  seventh project that waits rather than evicting, a vacated key being refilled,
  a free key remembering its project, pins never lent out;
* the exact six-entry `v.oai.thstatus` payload with factory colours and dark
  empty slots, plus a repaint when one slot changes state while the winning
  state does not;
* focus — tty targeting for both scriptable emulators, the app fallback, the
  do-nothing case, and a hostile tty that never reaches AppleScript;
* terminal capture in `StateStore`, including old records that predate it.
