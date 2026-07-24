# Adversarial design audit

> Originally read on 2026-07-23 against the working tree at 20:55. **Reconciled
> on 2026-07-24 against the current code:** most of the defects below have since
> been fixed. Each finding now carries a status tag - **FIXED**, **OPEN** or
> **PARTIAL** - and every FIXED entry cites the file that now handles it. The
> prose describing what was wrong is kept on purpose; it just no longer asserts
> a defect that is closed. The "Still open" list immediately below is the honest
> remaining set.
>
> Method of the original pass: read `PROTOCOL.md`, `FACTORY-DEFAULTS.md`,
> `AGENT-KEYS.md`, `CUSTOMIZING.md`, `README.md`, then every module under
> `src/freemicro/`, then the tests for the claims they make. Findings are grouped
> by the four failure patterns.
>
> Note the doc reuses the labels F2, F3 and F4 across two patterns each; the
> heading text disambiguates them, and the tags below are per-heading.

---

## Still open

The findings below are the ones still true in the code as of 2026-07-24, most
important first. Everything not listed here has been fixed and is tagged
**FIXED** in place, with the resolving file cited.

1. **F16 - unbounded caches.** `focus._TTY_CACHE` (no eviction),
   `ProcessLiveness._alive` (never pruned on process death),
   `codex_micro._LIVE_CALLBACKS` (grows one ctypes callback per `stream()`), and
   the `_mode` CFString that leaks when `Device.stream()` is called twice. The
   only unbounded structures in a process meant to run for months.
2. **F17 - interval scheduling on the wall clock.** `LightingOwner` was moved to
   `time.monotonic`, but `ConfigWatcher`, `CodeWatcher` and `run_with_reconnect`
   still schedule their intervals on `time.time()`, so a backwards clock
   correction can stall the config watcher and the restart watcher. PARTIAL.
3. **F15 - idle poll cost.** `--interval` still defaults to 0.25 s and each tick
   still walks the state directory more than once and stats the whole package;
   roughly four state scans a second at rest.
4. **F3 (device) / F18 - the reconnect loop polls and does not back off.** Pad
   presence is re-checked once a second instead of via the IOKit removal
   callback, and after a drop the loop goes silent for a bare
   `time.sleep(retry_interval)` with no `[1, 2, 5, 10]` s ladder.
5. **Menubar double directory scan.** `menubar.status.resolved_state()` calls
   `store.resolved_state()` and then `len(store.sessions())`, two full scans of
   the state directory per menu-bar poll.
6. **F6 - a pointer hard-failure is unsurfaced and retried per sample.** `Pointer`
   now accepts an `on_error`, but `Bridge` still constructs it without one, and
   `Pointer.update` restarts the loop on the next moving sample without checking
   `loop.error`, so a machine where `move_mouse` cannot work spawns a thread per
   sample. `doctor` still says only fn bindings and hold-to-talk break.
7. **F9 - `freemicro status` cannot name a project.** It still prints
   `state / session_id / age` with a raw UUID, and never uses `describe_claim()`
   or the slot table, both of which exist.
8. **F10 - a hook on a full or unwritable disk raises into Claude Code.**
   `cmd_hook` guards only the JSON parse; `_store()` and `store.update()` are
   unguarded and `main()` has no top-level handler.
9. **F13 - two independent config watchers reload the same file** on different
   paths (`ConfigWatcher` and `LightingOwner._check_config`).
10. **F8 - the mic-shortcut docs still contradict the code.** The shipped default
    is now `ctrl+cmd+o` on `ACT10`, but `README.md` and `docs/CUSTOMIZING.md`
    still tell the reader to set the four-key combo the web UI refuses.
11. **F4 (PreCompact) - a graded event that is never installed.** `PreCompact`
    counts as a working event and gets a 600 s grace, but it is absent from
    `hooks_install.HOOK_EVENTS`, so Claude Code never fires it.
12. **F14 - coexistence heals in one direction only,** and `CUSTOMIZING.md`
    oversells it as "heals itself".
13. **F19 - a corrupt `config.json` is swallowed silently** and surfaced nowhere,
    while a corrupt `keymap.json` is fatal.
14. **F20 - `cmd_run` and `cmd_keys` call `lock.acquire()` without checking it.**

---

## Pattern 1: a timer standing in for a fact we already have

The big one in this category (30-minute session TTL versus the stored `pid`) has
landed. What is left is smaller but real.

### F1. A `hold` whose release never arrives leaves the key physically down

**FIXED.** Severity was severe: this was the one the brief flagged as needing a
specific check, and at the time it was not handled anywhere.

> **Resolved:** the process that presses the modifiers now guarantees the
> key-ups on every exit path. `quartz.hold_chord` registers each held keycode in
> a module-level `_held` list (`input/quartz.py:178-296`) and
> `quartz.release_all()` sends the key-ups for all of them; `install_release_guard()`
> chains `SIGINT`/`SIGTERM`/`SIGHUP` to `release_all()` and registers it with
> `atexit` (`input/quartz.py:190-256`). On the bridge side,
> `Bridge.release_held_keys()` and `Bridge.close()` clear the held state and call
> the backend release (`input/bridge.py:455-495`), and `_run_pipeline`'s
> `finally` now calls `bridge.close()` (`cli.py:1496`, also `cli.py:2054`). The
> config-reload path releases before swapping via the `joystick` setter
> (`input/bridge.py:436-453`). The four loss modes below are the record of what
> was wrong.

`Bridge.fire(input_id, pressed=False)` is the only path that releases a held
chord (`src/freemicro/input/bridge.py:215-228`). It fires only when the pad
sends `act: 0` for that exact input, and only if the *currently loaded* config
still binds that input to a `hold` kind. `quartz.hold_chord`
(`src/freemicro/input/quartz.py:155-181`) is completely stateless: it posts
key-down events for the modifiers and the key and returns. Nothing anywhere
records that a chord is down.

There are at least four ways to lose the release:

1. **Ctrl-C while holding the mic key.** `_run_pipeline`'s `finally`
   (`src/freemicro/cli.py:1755-1760`) closes the renderers and the device. It
   never calls `bridge.close()` at all, and `Bridge.close()`
   (`src/freemicro/input/bridge.py:143-145`) only stops the pointer loop anyway.
2. **Bluetooth drop mid-hold.** `dropped()` (`src/freemicro/cli.py:1670-1677`)
   resets the renderer list and prints a line. No release.
3. **Config reload mid-hold.** `adopt()` (`src/freemicro/cli.py:1630-1641`)
   swaps `bridge.config`. If the new file changed `ACT10` away from `hold`, the
   later release hits `action.kind not in HOLD_KINDS` and returns `None`.
4. **A crash or a `CodeWatcher` re-exec while the key is down.**

**How the owner would write it up:** *"I held the mic key, my Mac went to sleep
/ the pad dropped / I hit Ctrl-C, and now Ctrl and Cmd are stuck down. Every
letter I type is a keyboard shortcut. Quitting FreeMicro does not fix it; I had
to tap Ctrl and Cmd by hand to clear it."*

Note that the default binding is `ctrl+cmd+o` (`src/freemicro/default_keymap.json`,
`ACT10`), so the stuck state is two modifiers plus a letter: the machine becomes
effectively unusable until the user works out what happened.

**Fix.** The bridge must own held state, not the pad. Keep
`self._held: dict[str, Action]`; add on press, remove on release, and add
`Bridge.release_all()` that releases everything still in it. Call it from
`dropped()`, from `adopt()` *before* swapping the config, from
`_run_pipeline`'s `finally`, from `cmd_keys`'s `finally`, and from an
`atexit` handler for the crash case (the pointer loop already does exactly this,
`src/freemicro/input/pointer.py:358`). A watchdog is the wrong shape here: the
release is a fact we get for free from the config we already hold.

### F2 is in Pattern 2 but belongs here too

**FIXED.**

> **Resolved:** `PointerLoop.stop()` now calls `engine.reset()`, which clears
> `_precision` (`input/pointer.py:381-398`, `211-218`), and `Bridge.close()`
> calls `pointer.close()` -> `loop.stop()` on every exit path
> (`input/bridge.py:476-495`), so a lost release no longer latches the cursor at
> `precision_scale`. The original description follows.

`joystick.precision_key` has the same shape as F1 in miniature. Precision mode
latches on key-down and off on key-up
(`src/freemicro/input/bridge.py:174-179`). Lose the release and the cursor is
stuck at `precision_scale` (default 0.25) forever. The parser already refuses
`ENC_CW`/`ENC_CC` for precisely this reason (`padconfig.py`, "dial detents are
momentary and have no release, so precision mode would latch on forever"), which
proves the failure mode was understood; the disconnect case was not covered.
`PointerEngine.reset()` does clear `_precision`, but it is only called from
`PointerLoop.stop()`, which nothing calls on disconnect.
**Severity: medium.** Same fix: clear it in `release_all()`.

### F3. Pad presence is polled once a second when IOKit will tell us

**OPEN.** Confirmed: `run_with_reconnect`'s inner `_tick` still calls
`device_present()` on a one-second wall-clock gate (`device/__init__.py:136-150`)
and no IOKit removal callback is registered.

`run_with_reconnect` calls `device_present()` every second
(`src/freemicro/device/__init__.py:136-147`), and `device_present()` runs a full
`IOServiceGetMatchingServices` enumeration over every `IOHIDDevice` on the
machine (`src/freemicro/device/codex_micro.py:522-558`, `592-606`). IOKit
publishes device removal directly (`IOServiceAddMatchingNotification` with
`kIOTerminatedNotification`, or `IOHIDManagerRegisterDeviceRemovalCallback`), on
the same run loop the code is already pumping.

**How the owner would write it up:** *"The pad takes up to a second to notice it
has been unplugged, and `freemicro daemon` is enumerating every HID device on
this Mac 86,400 times a day to find that out."*

**Severity: low** on correctness, **medium** on the battery cost noted under
[Long-running behaviour](#long-running-behaviour). **Fix:** register the removal
callback on the run loop already created in `Device.stream()`; keep the poll as
a slow backstop at 10-30 s rather than 1 s.

### F4. `PreCompact` is classified but is never installed as a hook

**OPEN.** Confirmed: `state/hooks.py:67` still counts `PreCompact` among the
working events, and `hooks_install.HOOK_EVENTS` (`hooks_install.py:34-46`) still
lists only `SessionStart`, `UserPromptSubmit`, `PreToolUse`, `PostToolUse`,
`Notification`, `Stop`, `SessionEnd` - `PreCompact` is not among them.

`state/hooks.py:65` counts `PreCompact` as a working event and
`state/engine.py:140` gives it the 600-second tool grace, but `PreCompact` is
not in `hooks_install.HOOK_EVENTS` (`src/freemicro/hooks_install.py:34-46`), so
Claude Code never fires it. The 600-second grace for a compaction therefore
never applies, and a long compaction is retired by the 120-second working TTL
instead. `_hook_drift()` (`src/freemicro/cli.py:291-318`) compares installed
events against `HOOK_EVENTS`, so it cannot see this either.

**How the owner would write it up:** *"Claude spent four minutes compacting and
the key went white halfway through."*

**Severity: low-medium.** **Fix:** either add `PreCompact` to `HOOK_EVENTS` or
delete it from the two frozensets. Two lists of "events we care about" is the
underlying defect.

---

## Pattern 2: two copies of the same truth

Most of this pattern has now been collapsed to a single copy each. What remains
open here is F13 (two config watchers) and the docs half of F8.

### F2. Three more places build a `StateStore` with two of four TTLs

**FIXED.**

> **Resolved:** there is one store-construction path now. `cli._store()` is a
> thin call to `default_store(cfg)` (`cli.py:38-46`), `menubar.status.resolved_state()`
> builds `default_store()` (`menubar/status.py:99-114`), and `default_store()`
> passes all four TTLs (`state/engine.py:1276`). No surface hand-rolls a
> two-of-four store any more, so `working_ttl_seconds: 0` is honoured everywhere
> the store is read. The original description follows.

The web UI's copy was found and fixed, with a docstring explaining exactly why it
mattered (`src/freemicro/webui/api.py:56-81`). Three other copies were not
touched:

| Where | Passes | Missing |
|---|---|---|
| `src/freemicro/cli.py:38` `_store()` | `ttl_seconds`, `done_ttl_seconds` | `working_ttl_seconds`, `tool_ttl_seconds` |
| `src/freemicro/menubar/status.py:99` `resolved_state()` | same two | same two |
| `src/freemicro/state/engine.py:1188` `default_store()` | all four | - |

`cli._store()` is used by `cmd_hook`, `cmd_emit`, `cmd_status` and
`_run_pipeline`. So inside a single `freemicro run` process there are **two
stores reading the same directory with different rules**: the tick's
`store.resolved_state()` uses `_store()` (defaults for working/tool) while
`MicroLedsRenderer` uses `default_store()` (the config's values). The `state:`
line `run` prints and the colour on the pad can disagree, in the same process,
at the same instant.

**How the owner would write it up:** *"I set `working_ttl_seconds: 0` in
`config.json` because the docs say 0 switches the check off. `freemicro status`
still says `idle` two minutes into a long silent job, and the console line
disagrees with the pad."*

`docs/AGENT-KEYS.md:130` and `docs/CUSTOMIZING.md` both document `0` as "switch
the check off". It does not, on three of the five surfaces.

**Severity: high.** **Fix:** delete `cli._store()` and
`menubar.status.resolved_state()`'s local construction and call
`default_store()`. `default_store()`'s own docstring already says "Constructing
this took four copies of the same three lines across the code base; one of them
drifting is exactly how you get a renderer reading a different directory than
the hooks write." That sentence is now describing the present tense.

### F3. The LED renderer's slot resolver drops two of three TTLs

**FIXED.**

> **Resolved:** the renderer no longer hand-picks TTLs. It builds the resolver
> with `SlotResolver.for_store(store, config=config)` (`renderers/micro_leds.py:465`),
> a constructor that reads all of the store's TTLs off the store itself, so a
> raised or disabled working TTL is honoured and cannot be silently undone by the
> resolver. The original description follows.

`src/freemicro/renderers/micro_leds.py:211-214`:

```python
self._resolver = SlotResolver(
    config=config,
    done_ttl_seconds=getattr(store, "done_ttl_seconds", 0.0),
)
```

`SlotResolver` also takes `working_ttl_seconds` and `tool_ttl_seconds`
(`src/freemicro/agentkeys.py:536-539`) and defaults them to the module
constants. `resolve_slots` re-applies `effective_state` to records the store has
*already* decayed, so the stricter of the two rules wins. If the user raises or
disables the working TTL, the store honours it and the resolver immediately
undoes it.

**How the owner would write it up:** *"`freemicro status` says the API repo is
still working, and its Agent Key is white."* That is the exact class of bug the
`effective_state` docstring says must not happen: *"Somewhere in the codebase
disagreeing about this is how the pad ends up showing one thing and the status
command another."*

**Severity: high.** **Fix:** pass all three, or better, have `SlotResolver` take
the store and read them off it, so a fourth TTL added later cannot be forgotten
a fifth time.

### F5. The joystick now has two contradictory "up" directions in one process

**FIXED.**

> **Resolved:** the discrete wheel was made to agree with the factory sector
> table. `JOYSTICK_INPUTS` is now `("JOY_RIGHT", "JOY_DOWN", "JOY_LEFT", "JOY_UP")`
> (`padconfig.py:89`), so pushing up (angle ~0.75) resolves to `JOY_UP`. The old
> order is kept as `_LEGACY_JOYSTICK_INPUTS` only to detect and warn a user whose
> file still carries it (`padconfig.py:95-96`, `1226-1230`). The test that
> encoded the wrong convention was corrected too (see F22). The original
> description follows.

`src/freemicro/input/pointer.py:34-49` states the orientation plainly: angle
0.75 is up, matching `FACTORY-DEFAULTS.md` §6 which was read out of the shipped
vendor binary. The docstring then says, of the discrete wheel:

> Note that this disagrees with FreeMicro's *discrete* default wheel
> (`JOYSTICK_INPUTS`), which places `JOY_UP` at 0.25 - an assumption of
> maths-convention (y-up) axes that predates the factory capture and was never
> checked against hardware. That wheel is left alone deliberately.

`src/freemicro/padconfig.py:71` is still
`("JOY_RIGHT", "JOY_UP", "JOY_LEFT", "JOY_DOWN")`, i.e. `JOY_UP` at 0.25. Both
conventions now ship, in the same process, reading the same `a` value.

The consequence is concrete: switch `joystick.mode` to `directions` and push the
stick up; per the factory sector table that is angle ~0.75, which
`direction_for` maps to index 3, `JOY_DOWN`, which the shipped default binds to
`{"action":"mouse","y":40}` (`default_keymap.json`). **Pushing up moves the
cursor down, and pushing left/right is correct**, which is the classic signature
of an inverted vertical axis and the hardest kind to diagnose because half of it
works.

`README.md:392` already carries a troubleshooting row for this
("Joystick fires the wrong direction | Tune `joystick.origin` / `directions`"),
which is evidence the symptom has been seen and worked around with a tuning knob
rather than fixed.

**Severity: high**, because a documented, deliberately-preserved contradiction
will be discovered by the next person who tries `mode: directions`.

**Fix.** Pick the factory table, which is the only measured one, and make the
wheel `("JOY_RIGHT", "JOY_DOWN", "JOY_LEFT", "JOY_UP")`. The stated reason for
not doing so ("changing it would silently repoint everybody's existing flick
bindings") applies to a user base of one, and the migration is a one-line
warning at load time when `directions` is absent from the user's file. Leaving
two conventions in the tree because one of them is wrong is not backwards
compatibility, it is a second copy waiting to fire.

### F8. The mic shortcut is in four places, and the two in the docs are the one the code rejects

**OPEN (docs).** The code side is consistent: the shipped `default_keymap.json`
now sets `ACT10` to `hold ctrl+cmd+o` and `ACT11` to `none`, and `starters.py`
still refuses the four-key combo. But the docs were not reconciled -
`README.md:251` still shows `"ACT10": { "action": "hold", "key": "ctrl+option+cmd+d" }`,
and `docs/CUSTOMIZING.md` still tells the reader to bind `ctrl+option+cmd+d` on
`ACT11` (`:183-199`). The instruction to set a combo the web UI rejects survives.

| Source | Key | Combo |
|---|---|---|
| `src/freemicro/default_keymap.json` `ACT10` | ACT10 (ACT11 = `none`) | `ctrl+cmd+o` |
| `src/freemicro/webui/starters.py:53,92`, `webui/keycaps.py:210` | ACT10 | `ctrl+cmd+o` |
| `README.md:199, 228` | 199 says ACT10, 228 says "ACT11, the mic key" | `ctrl+option+cmd+d` |
| `docs/CUSTOMIZING.md:52, 125, 137` | 52 says ACT10, 125/137 say ACT11 | `ctrl+option+cmd+d` |

The combo in the docs is four keys. `starters.combo_problem`
(`src/freemicro/webui/starters.py:108-124`) exists specifically to refuse it:
Wispr Flow accepts a maximum of three, and `tests/test_webui.py:636` asserts
that `combo_problem("wispr", "ctrl+option+cmd+d")` returns a complaint.
`docs/WEB-UI.md:636` records the same finding.

**How the owner would write it up:** *"I followed the README, set
Ctrl+Option+Cmd+D in Wispr Flow, and the mic key does nothing. Then the web UI
told me that combo cannot be registered - so why does the README tell me to use
it?"* This is the mic-shortcut-in-three-places bug from the brief, with a fourth
place added and a documented "this cannot work" verdict now sitting next to the
instruction to do it.

**Severity: medium**, near-certain to be hit. **Fix:** the shipped
`default_keymap.json` is the only copy that should exist. README and CUSTOMIZING
should quote it, or better, tell the reader to run `freemicro keys --list`.
While you are in there, `README.md:228` and `CUSTOMIZING.md:121` say the mic is
`ACT11`; the shipped default deliberately silences `ACT11` and acts on `ACT10`.

### F11. The LED fallback palette is not the factory palette the docs promise

**FIXED.**

> **Resolved:** there is one palette now. `padconfig.FACTORY_PALETTE`
> (`padconfig.py:310`) holds the factory hex values and the shipped defaults are
> derived from it (`padconfig.py:327`); `micro_leds` falls back to
> `FACTORY_PALETTE` for any omitted state (`renderers/micro_leds.py:37`), so a
> deleted `idle` block now falls back to factory white rather than the Apple
> near-black. The original description follows.

`renderers/base.py:11-17` defines `PALETTE` as Apple system colours:
idle `(40,40,48)`, working `(0,122,255)`, waiting `(255,149,0)`, done
`(52,199,89)`, error `(255,59,48)`. `micro_leds._fallback_light`
(`src/freemicro/renderers/micro_leds.py:84-91`) uses that palette for any state
the user's `lighting.states` omits, and `docs/CUSTOMIZING.md:209` says exactly
that: *"Any state you omit falls back to FreeMicro's canonical palette, lit
solid."*

But four lines earlier the same document says *"The shipped colours are the
**exact factory values**"*, and the factory values are `#FFFFFF`, `#304FFE`,
`#FF6D00`, `#00FF4C`, `#FF0033` (`FACTORY-DEFAULTS.md` §1a). None of the five
fallbacks match. `idle` is the worst: factory white becomes a near-black slate
`#282830` at brightness 1.0.

**How the owner would write it up:** *"I deleted the `idle` block from
`lighting.states` because the docs say omitted states fall back to the factory
palette, and now my idle keys are almost black instead of white."*

The colour is now in five places: `PALETTE`, `default_keymap.json`,
`FACTORY-DEFAULTS.md` §1a, `AGENT-KEYS.md` "Colours", `CUSTOMIZING.md`
"Factory parity". Four agree.

**Severity: medium.** **Fix:** one module-level `FACTORY_PALETTE` that both the
fallback and the shipped JSON derive from, with a test asserting they are equal.
(The screen renderer, the only surface the Apple colours were ever right for, is
gone; the menu bar dot is the last non-pad reader of `PALETTE` and it should
match the pad.)

### F12. `PROTOCOL.md` still tells the reader to use the method the code refuses to use

**FIXED.**

> **Resolved:** `docs/PROTOCOL.md` now speaks with one voice. The lighting table
> marks `lights.preview` as "does nothing on v0.4.1; use `v.oai.rgbcfg`"
> (`PROTOCOL.md:161`), and a boxed correction states plainly "Use `v.oai.*`, not
> `lights.preview`" and records that the earlier opposite claim was wrong
> (`PROTOCOL.md:178-189`). The contradicting "verified on hardware" and "open
> question" passages are gone. The original description follows.

`padconfig.py:88-97`, `micro_leds.py:9-18` and `default_keymap.json` all agree:
`rgbcfg` works, `lights.preview` lights nothing on v0.4.1. `PROTOCOL.md` says
this too, twice and emphatically (lines 161, 178-193). It then contradicts
itself three more times in the same file:

* line 208: *"**Verified on hardware, over USB and Bluetooth:** `lights.preview`
  drives the base"*
* lines 214-228: a "Open question" box asserting *"FreeMicro drives lighting
  through `lights.preview` + `v.oai.thstatus`"*, which is false of the code.
* lines 266-267: *"Treat `rgbcfg` as stored configuration and use
  `lights.preview` (plus `v.oai.thstatus`) for live state."*

`FACTORY-DEFAULTS.md` §11 even contains a "Correction needed in PROTOCOL.md"
box, which is a note-to-self that outlived its own fix.

**How the owner would write it up:** *"I read PROTOCOL.md end to end to remind
myself which method drives the LEDs and came away not knowing."*

**Severity: medium.** This is `lights.preview` surviving in four docs, again,
now inside a single document. **Fix:** delete lines 195-228 and 258-267's
conclusion; keep one paragraph recording that `lights.preview` was tried,
returns `{"result": null}`, and does nothing.

### F13. Two independent config watchers reload the same file

**OPEN.** Confirmed: `ConfigWatcher` is still created in `_run_pipeline`
(`cli.py:1879-1880`, polled at `1941`) and `LightingOwner._check_config` still
stats and reloads `config.source` independently (`lighting_owner.py:600`,
`728`). Both remain, on different paths.

`ConfigWatcher` (`src/freemicro/staleness.py:695-761`) and
`LightingOwner._check_config` (`src/freemicro/lighting_owner.py:572-591`) both
`stat` a config path each tick and both reload and re-apply it. `_run_pipeline`
handles the duplicate output by demoting the owner's config events to verbose
(`cli.py:1643-1654`), which is a symptom being managed rather than a cause being
removed. They also watch **different paths**: the owner uses `config.source`
(which is the *packaged* `default_keymap.json` when the user has no config of
their own), the watcher uses `staleness.config_watch_path()` (which points at
`~/.freemicro/keymap.json` even before it exists). So on a `pip install -U`, a
user with no personal config gets `[lighting] reasserted lighting (config
changed)` because the shipped default's mtime moved.

**Severity: low.** **Fix:** one watcher, owned by the run loop, that hands the
new config to everyone including the lighting owner.

---

## Pattern 3: correct behaviour that is invisible

### F4. One write failure kills lighting for the life of the process, silently

**FIXED.**

> **Resolved:** the permanent latch is gone. `MicroLedsRenderer` now counts
> consecutive failures and schedules a retry on the vendor's `[1, 2, 5, 10]` s
> backoff ladder rather than setting a `_failed` flag that never clears
> (`renderers/micro_leds.py:362-363`, `911-977`, ladder documented at
> `:164`). It says so once on the first failure instead of once per tick, and a
> later successful write clears the counter. The original description follows.

`MicroLedsRenderer._send` sets `self._failed = True` on any exception from
`device.send()` (`src/freemicro/renderers/micro_leds.py:388-395`). Nothing ever
resets it: `available()` and `render()` both return early forever afterwards
(`lines 237, 254`), and `_send` prints nothing, logs nothing and returns `False`
which `render()` discards.

`Device.send_report` raises `DeviceError` on any non-zero `IOHIDDeviceSetReport`
(`src/freemicro/device/codex_micro.py:350-351`), so a single transient write
error is enough. The only recovery is a full disconnect and reconnect, because
`rebuild()` constructs a fresh renderer (`cli.py:1598-1619`). A pad that stays
on the bus but hiccups once has its lighting dead until the process restarts.

**How the owner would write it up:** *"The LEDs stopped changing at some point
this afternoon. `freemicro run` is still going, keys still work, `status` is
correct, and the log says nothing at all. Restarting it fixes it."*

**Severity: high** because the failure is permanent, the process looks healthy,
and the log is empty.

**Fix:** count consecutive failures, print one line on the first
(`  [lighting] write failed: ... - retrying`), and clear the latch on the next
successful `device.status` round trip or after a backoff. A permanent latch
needs at minimum to say so once.

### F6. A pointer failure is swallowed, and then retried forever

**OPEN (partial infrastructure).** `PointerLoop` now accepts an `on_error` and
calls it when `step()` raises (`input/pointer.py:331-338`, `415-417`), but the
wiring that would make it visible is still missing: `Bridge` constructs the
`Pointer` with no `on_error` (`input/bridge.py:394`), and `Pointer.update`
restarts the loop on the next moving sample without checking `loop.error`
(`input/pointer.py:470-477`), so a machine where `move_mouse` hard-fails still
spawns and kills a thread per sample. `doctor` still reports the Quartz-missing
case as "Everything works except fn bindings and hold-to-talk" (`cli.py:1123`),
which the `mouse` action and the default joystick mode contradict.

`PointerLoop._run` catches any exception from `step()`, records it in
`self.error`, calls `on_error` if there is one, and returns
(`src/freemicro/input/pointer.py:382-390`). `Pointer.__init__` constructs the
loop **without** an `on_error`
(`src/freemicro/input/pointer.py:421-423`), and `Bridge` constructs the
`Pointer` itself with no way to pass one
(`src/freemicro/input/bridge.py:117-119`). `cli.py` never touches the pointer at
all. So `loop.error` is written and never read by any non-test code.

Worse, the thread does not stay dead: `Pointer.update` restarts the loop on the
next sample whose vector is moving
(`src/freemicro/input/pointer.py:446-447`), and `start()` clears `self.error`
and registers another `atexit` handler each time. The pad streams samples
continuously while the stick is deflected, so a machine where `move_mouse`
cannot work spawns and kills a thread per sample for as long as the stick is
held.

This is reachable: `AppleScriptBackend.move_mouse` is the base-class
`raise NotImplementedError` (`src/freemicro/input/actions.py:102-103`), and
`best_backend()` returns `AppleScriptBackend` whenever Quartz will not load
(`src/freemicro/input/actions.py:300-309`). `freemicro doctor` reports that case
as *"Everything works except fn bindings and hold-to-talk"*
(`src/freemicro/cli.py:861`), which is now wrong twice over: the `mouse` action
and the entire default joystick mode also need CGEvent.

**How the owner would write it up:** *"On my other Mac the joystick does nothing
at all. No error, no log line, doctor says everything is fine except fn keys."*

**Severity: high.** **Fix:** pass an `on_error` that prints once through the run
loop's normal channel; do not auto-restart after a hard failure (check
`loop.error` in `Pointer.update`); and add pointing to doctor's Quartz warning.

### F7. The tuning workflow both docs promise does not exist

**FIXED.**

> **Resolved:** `keys --dry-run` now prints the velocity. The dry-run handler
> calls `joystick_line(message, bridge)` (`cli.py:1474`), which in pointer mode
> returns `bridge.pointer.preview(angle, distance).describe()` - the live px/s
> next to the raw sample (`input/bridge.py:870-892`). `gamma` and `max_speed`
> can now be tuned as the docstrings promise. The original description follows.

`PointerVector.describe()` says it is *"Printed by `freemicro keys --dry-run` so
`gamma` and `max_speed` can be chosen the only way they ever really get chosen:
by feel, watching numbers move"* (`src/freemicro/input/pointer.py:117-119`), and
`default_keymap.json`'s joystick comment says *"Run `freemicro keys --dry-run`
and watch the live angle, distance and px/s while you push the stick - that is
how these numbers get chosen."*

`cmd_keys`'s dry-run handler prints only `angle=` and `distance=`
(`src/freemicro/cli.py:1203-1212`). It calls `joystick_sample()`, not
`bridge.pointer.preview()`. There is no px/s anywhere in the CLI. `cli.py` was
never updated when the pointer landed, which is also why `bridge.close()` is
never called (see F1).

**How the owner would write it up:** *"The comment in my own keymap tells me to
watch px/s in `--dry-run`. There is no px/s."*

**Severity: medium**, certain to be hit the first time anyone tunes the stick.
**Fix:** print `bridge.last_vector.describe()` in the dry-run handler; it is a
one-line change and the method already exists.

### F9. `freemicro status` cannot answer the two questions the Agent Keys raise

**OPEN.** Confirmed against `cmd_status` (`cli.py:507-540`): it still prints
`f"  {s.state.value:8} {s.session_id}  ({int(s.age())}s ago)"` - a raw UUID and
`state.value` - and neither the slot table (`focus.current_slots()`) nor
`describe_claim()` is wired in, though both exist. Liveness still prints first.

`cmd_status` (`src/freemicro/cli.py:342-374`) prints liveness, the resolved
state, and a list of `state / session_id / age`. Session ids are UUIDs. It never
prints:

* **which project is on which Agent Key.** `docs/AGENT-KEYS.md:293-311` specifies
  this in detail ("The single most useful missing line"), including the exact
  code. `focus.current_slots()` and `AgentSlot.to_dict()` both exist and are
  ready. It was never wired.
* **why a session looks the way it does.** `SessionState.describe_claim()`
  returns `"idle (was working; last turn was interrupted)"`, and
  `docs/AGENT-KEYS.md:133` claims *"`status` reports such a session as `idle`,
  and the record remembers what it claimed so a reader can say `idle (was
  working)`"*. A grep for the consumers of `describe_claim`, `kept_by_process`,
  `process_alive` and `interrupted` finds exactly one, `webui/api.py:303`. The
  CLI, the menu bar and the LEDs surface none of it.

**How the owner would write it up:** *"AG02 is amber. Which repo is that?
`freemicro status` gives me five UUIDs and no answer."* And: *"I built the whole
interrupted-turn detection and the only way to see it is to open the web UI."*

**Severity: medium.** **Fix:** the six lines in `AGENT-KEYS.md:293-311`, plus
`s.describe_claim()` instead of `s.state.value` in the session list, plus the
project basename instead of the session id.

### F14. Coexistence only heals in one direction, and the docs oversell it

**OPEN.** Confirmed: `LightingOwner._check_vendor` still reasserts only on the
running -> not-running transition (`lighting_owner.py:556-570`), the heartbeat is
still off by default, and `CUSTOMIZING.md` still presents this as "heals itself".

`LightingOwner._check_vendor` reasserts on the transition running -> not running
(`src/freemicro/lighting_owner.py:556-570`). While ChatGPT is *running*, nothing
reasserts: the heartbeat is off by default and the renderer's frame dedupe
(`micro_leds.py:257-261`) means an unchanged state is never re-sent. So the
practical behaviour with both apps open is: ChatGPT repaints, and FreeMicro's
colours do not come back until the agent state changes, which can be hours.

`docs/CUSTOMIZING.md:263-275` presents this as *"Let it heal itself (on by
default)"* with a table of triggers. Only one of the four listed triggers fires
in the common case, and the one that matters ("ChatGPT quits") requires quitting
the app the user was told they do not have to quit.

**Severity: medium.** `coexist_advice()` does print a suggestion at startup, so
this is undersold rather than hidden. **Fix:** say plainly in CUSTOMIZING that
with both apps running the pad shows whichever wrote last until your agent state
changes, and that `--coexist` is the only configuration with no repaint window.

---

## Pattern 4: unexamined constants

**Mixed.** The table is kept as the provenance record. Two rows now point at
fixed findings: the `PALETTE` row (see F11, now `FACTORY_PALETTE`) and the
`JOYSTICK_INPUTS` order row (see F5, now the factory order). Still open from this
table: `retry_interval` has no backoff ladder (see F18), `--interval` is still
0.25 s (see F15), and `joystick.deadzone` is still allowed to be exactly `0`,
which wedges the tracker (the "> 0" fix at the end of this section is not applied
as of 2026-07-24).

Every default in the tree, with provenance. "Invented" means I could find no
measurement or `FACTORY-DEFAULTS.md` reference for it.

| Constant | Value | Where | Provenance | 10x / 0.1x | Verdict |
|---|---|---|---|---|---|
| `DEFAULT_TTL_SECONDS` | 1800 s | `state/engine.py:93` | Invented, but now a backstop only | 10x: unvouchable records linger 5 h. 0.1x: no change for pid-vouched records | OK since the liveness fix |
| `DEFAULT_DONE_TTL_SECONDS` | 180 s | `state/engine.py:101` | Mirrors vendor auto-dim (a different mechanism) | 10x: green for half an hour, pad stops matching factory. 0.1x: you miss it | Weakly justified, see below |
| `DEFAULT_WORKING_TTL_SECONDS` | 120 s | `state/engine.py:125` | **Measured** (11 timestamps recorded in the docstring) | 10x: interrupted sessions stay blue 20 min. 0.1x: blue flickers off during normal gaps | Good, keep |
| `DEFAULT_TOOL_TTL_SECONDS` | 600 s | `state/engine.py:133` | Claude Code's max Bash timeout | 10x: a wedged tool holds blue for 100 min. 0.1x: long builds go white | Good |
| `PID_START_TOLERANCE_SECONDS` | 5 s | `state/engine.py:436` | Reasoned from `ps` truncation | Fine either way | Good |
| `DEFAULT_LIVENESS_CACHE_SECONDS` | 1.0 s | `state/engine.py:421` | Invented | 10x: a closed tab's key stays lit 10 s. 0.1x: 24 syscalls/s | Fine, worth a comment on the upper bound |
| `DEFAULT_IDENTITY_CACHE_SECONDS` | 60 s | `state/engine.py:429` | Invented | 10x: a recycled pid can resurrect a session for 10 min | Fine |
| `_TTY_CACHE_SECONDS` | 15 s | `focus.py:120` | Invented, with a good reason written down | 10x: key press can hit a recycled tab | Fine |
| `PS_TIMEOUT_SECONDS` | 2.0 s | `state/engine.py` | Invented | 0.1x on a loaded machine: tty lookups start failing, keys silently degrade to app-focus | Fine |
| `MAX_TTY_HOPS` | 8 | `state/engine.py:241` | Reasoned | - | Fine |
| `JoystickTracker.REARM_RATIO` | 0.75 | `input/bridge.py` | **Invented.** The vendor uses a fixed 0.1 re-arm floor (`FACTORY-DEFAULTS.md` §6, "Suppression release") | 0.1x: a stick resting at 0.5 never re-arms | Diverges from a measured value for no stated reason |
| `joystick.deadzone` | 0.6 | `padconfig.py:237` | Near the factory 0.5, not equal to it | - | Undocumented divergence |
| `joystick.pointer_deadzone` | 0.1 | `padconfig.py:243` | **Factory** (§6 HUD deadzone) | - | Good |
| `joystick.max_speed` | 1200 px/s | `padconfig.py:245` | Invented | - | Unverifiable without hardware; fine as a default |
| `joystick.gamma` | 2.0 | `padconfig.py:248` | Invented ("TrackPoint-like") | - | Fine |
| `joystick.tick_hz` | 90 | `padconfig.py:251` | Invented | 0.1x (9 Hz, below `TICK_HZ_MIN`): rejected. 10x (900): rejected | Fine, and bounded |
| `TICK_HZ_MIN/MAX` | 20 / 480 | `padconfig.py:109-110` | Invented, reasoned | - | Fine |
| `precision_scale` | 0.25 | `padconfig.py:256` | Invented | - | Fine |
| `DEFAULT_STALE_SECONDS` | 0.25 s | `input/pointer.py:67` | Invented, well reasoned | 10x: a dropped BLE packet slides the cursor 2.5 s | Good |
| `DEFAULT_MAX_STEP` | 0.05 s | `input/pointer.py:75` | Invented | - | Good |
| `QUIET_SECONDS` | 0.1 s | `lighting_owner.py:392` | **Factory** (§3 input-quiet debounce) | - | Good |
| `reassert.poll_seconds` | 3.0 | `padconfig.py:153` | Invented | 0.1x: a `pgrep` fork every 0.3 s | See long-running |
| `reassert.heartbeat_seconds` | 0 (off) | `padconfig.py:150` | Reasoned at length | - | Good |
| `--interval` (run/watch/daemon) | 0.25 s | `cli.py` parser | Invented | See long-running | Too fast for what it does |
| `retry_interval` | 2.0 s | `device/__init__.py:98` | Invented; vendor uses a `[1,2,5,10]` s backoff (§9) | 0.1x: reopen attempts 5x/s while unplugged | No backoff, diverges from the measured vendor ladder |
| `staleness.CHECK_SECONDS` | 2.0 s | `staleness.py:825` | Invented | See long-running | Too fast for a filesystem walk |
| `staleness.SETTLE_SECONDS` | 2.0 s | `staleness.py:820` | Reasoned | 0.1x: restart into a half-written `pip install` | Good |
| `staleness.MAX_RESTARTS` / `RESTART_WINDOW` | 3 / 600 s | `staleness.py:814-815` | Invented, reasoned | - | Good |
| `GRACE_SECONDS` | 2.0 s | `staleness.py:60` | Reasoned from `ps` resolution | - | Good |
| `LOG_CAP_BYTES` | 1 MiB | `daemon.py:43` | Invented | - | Fine |
| `THROTTLE_SECONDS` | 10 | `daemon.py:47` | launchd minimum | - | Fine |
| `HOOK_TIMEOUT_SECONDS` | 10 | `hooks_install.py:51` | Invented | 0.1x: a slow disk kills the hook | Fine |
| menubar `POLL/PROCESS/DEVICE` | 2 / 10 / 60 s | `menubar/status.py:49-51` | Invented | - | Fine |
| `menubar STALE_AFTER_SECONDS` | 300 s | `menubar/model.py:53` | **Invented, and a fifth staleness rule.** Unrelated to any of the four store TTLs | 10x: menu bar calls nothing stale | Should derive from the store |
| `webui CAPTURE_SECONDS` | 120 s | `webui/padlink.py:63` | Invented | - | Fine |
| `PALETTE` | Apple system colours | `renderers/base.py:11-17` | **Invented**, and contradicts the factory palette | - | See F11 |
| `JOYSTICK_INPUTS` order | R, U, L, D | `padconfig.py:71` | **Invented** (maths-convention axes), contradicts factory §6 | - | See F5 |

Two entries deserve a note beyond the table:

**`DEFAULT_DONE_TTL_SECONDS = 180`.** The docstring says "180s mirrors the
vendor's auto-dim timeout". That is a coincidence, not a derivation: auto-dim is
an *inactivity blank of the whole pad*, and this is *how long green means
unread*. The vendor's green clears on a real signal (you opened the thread), and
`AGENT-KEYS.md:105-113` is honest that FreeMicro has no such signal. But it now
*does* have one within reach: `focus.py` knows how to identify a project's
terminal tab, and `plan_for_session` already resolves a tty. "The user selected
that tab" is checkable, cheaply, with the same AppleScript vocabulary already in
the tree. That is a Pattern 1 opportunity the codebase has already paid for.

**`joystick.deadzone = 0.6` is allowed to be `0`.** `_parse_joystick` validates
`0.0 <= deadzone <= 1.0` (`padconfig.py:412-413`). At exactly `0`,
`JoystickTracker.update` can never re-arm (`distance < 0` is never true), so the
stick fires once and is dead until restart. **Severity: low. Fix:** require
`> 0`.

---

## Long-running behaviour

Nothing here has run for more than a couple of hours. What a week of
`freemicro daemon` looks like:

### F15. The idle cost is four full state-directory scans a second

**OPEN.** Confirmed: `--interval` still defaults to 0.25 s (`cli.py:2150`,
`2195`), and the per-tick work below is unchanged - the state directory is still
scanned more than once per tick and `package_mtime()` still walks the package.

At the default `--interval 0.25`, each tick of `_run_pipeline`
(`src/freemicro/cli.py:1697-1729`) does:

1. `store.resolved_state()` -> `sessions()` -> `glob("*.json")` + read + parse
   every session file;
2. `renderer.render(state)` -> `self.slots()` -> `store.sessions()` again, a
   second full scan of the same directory (`micro_leds.py:219`);
3. `owner.poll()` -> a clock read, and every 3 s one `pgrep -x ChatGPT` fork
   plus one `stat`;
4. `config_watcher.poll()` -> one `stat` per second;
5. `watcher.poll()` -> every 2 s, `package_mtime()`, which is an `os.walk` of
   the entire installed package with a `stat` per file
   (`staleness.py:149-173`; the package includes `webui/static/app.js` and
   friends, so roughly 50-60 stats);
6. once a second, `device_present()`, a full IOKit HID enumeration
   (`device/__init__.py:142-145`).

Per day, idle, that is roughly 690,000 state-directory scans, 28,800 `pgrep`
forks, 43,200 package walks (about 2.5 million `stat` calls) and 86,400 IOKit
enumerations. None of it is wrong; all of it is a poll where a signal exists
(hook writes a file; ChatGPT quitting; IOKit removal; package mtime after an
install).

**How the owner would write it up:** *"`freemicro daemon` is the top entry in
Activity Monitor's Energy tab and I have not touched the pad in an hour."*

**Severity: medium.** **Fix, in order of value:** (a) call `store.sessions()`
once per tick and pass the list to the renderer, which removes half the file I/O
for free; (b) raise `--interval` to 1.0 s, since the hook -> LED latency budget
is human-scale and nothing else uses the tick; (c) skip `package_mtime()` unless
the package directory's own mtime changed; (d) use the IOKit removal callback
(F3).

### F16. Two caches and one list grow without bound

**OPEN.** Confirmed on 2026-07-24, all four parts: `focus._TTY_CACHE`
(`focus.py:119`) is still a plain dict with no eviction (only `clear_tty_cache`,
test-only); `ProcessLiveness._alive` (`state/engine.py:652`) is still not pruned
on death (`_started` is popped at `:676`, `_alive` is not); `_LIVE_CALLBACKS`
(`codex_micro.py:309`) still grows one callback per `stream()` by design
(`:481`); and `Device.stream()` still assigns `self._mode = _cfstr(...)` each call
(`codex_micro.py:454`) releasing it only in `close()` (`:508-510`), so a second
`stream()` on one `Device` leaks the first `CFString`.

* `focus._TTY_CACHE` (`focus.py:119`) has an expiry per entry but no eviction.
  `clear_tty_cache()` exists and is called by tests only. In a daemon it
  accumulates one entry per distinct pid ever asked about.
* `ProcessLiveness._alive` (`state/engine.py:558`) keeps an entry for every pid
  it has ever answered about, including dead ones. `_started` is popped on
  death; `_alive` is not.
* `codex_micro._LIVE_CALLBACKS` (`codex_micro.py:309`) grows by one ctypes
  callback per `Device.stream()` call, deliberately and correctly (releasing one
  is a segfault). The docstring says it is "bounded by the number of streams a
  process opens", which for a daemon reconnecting on every sleep/wake is
  unbounded in practice. Each object is small; over months it is still a leak.
* `Device.stream()` also creates a fresh `_cfstr("kCFRunLoopDefaultMode")` on
  every call and releases it only in `close()`
  (`codex_micro.py:454`, `508-510`). Calling `stream()` twice on one `Device`
  (which `request()` does) leaks the first `CFString`.

**Severity: low** individually, but they are the only unbounded structures in a
process designed to run for months. **Fix:** prune expired entries when either
cache exceeds a few hundred keys; release the previous `_mode` at the top of
`stream()`.

### F17. Everything is wall-clock, and one thing should not be

**PARTIAL.** `LightingOwner` was moved to a monotonic clock (its `clock`
parameter now defaults to `time.monotonic`, `lighting_owner.py:461`). Still
wall-clock, so still open: `ConfigWatcher` (`clock` defaults to `time.time`,
`staleness.py:710`), `CodeWatcher` (`staleness.py:957`), and `run_with_reconnect`
(`device/__init__.py:110-162`, all interval maths on `time.time()`). A backwards
clock correction still stalls those three.

`staleness.PROCESS_STARTED = time.time()` (`staleness.py:54`) and every
comparison against a file mtime use wall time, which is correct because mtimes
are wall time. But `LightingOwner`, `ConfigWatcher`, `CodeWatcher` and
`run_with_reconnect` all schedule *intervals* on `time.time()` too. A backwards
NTP correction or a manual clock change pushes `_next_probe`, `_next_check` and
`_beat_at` into the future, and the lighting owner, the config watcher and the
code watcher all stop working until real time catches up. macOS sleep does not
affect this, but a laptop that wakes to a corrected clock can.
`PointerLoop` gets this right (`pointer.py:395-398`, `time.monotonic`).

**Severity: low.** **Fix:** interval scheduling on `time.monotonic()`; keep
`time.time()` only where an mtime is on the other side of the comparison.

### F18. The reconnect loop stops ticking for two seconds after a drop

**OPEN.** Confirmed: after a stream ends, `run_with_reconnect` still does a bare
`time.sleep(retry_interval)` (`device/__init__.py:162`) with no tick-while-waiting
and no `[1, 2, 5, 10]` s backoff ladder.

`run_with_reconnect` ticks while waiting for a *missing* pad
(`device/__init__.py:120-130`) but does a bare `time.sleep(retry_interval)`
after a stream ends (`line 162`). During those two seconds no state change is
printed, the config watcher is blind, and a `CodeWatcher` restart decision
cannot be taken. There is also no backoff: a pad that is present but
un-openable (Input Monitoring revoked while running) is retried every 2 s
forever with no message after the first.

**Severity: low.** **Fix:** reuse the tick-while-waiting loop for both paths, and
adopt the vendor's `[1, 2, 5, 10]` s ladder from `FACTORY-DEFAULTS.md` §9.

---

## Unhappy paths

### F10. A hook on an unwritable or full disk raises into Claude Code

**OPEN.** Confirmed against the current `cmd_hook` (`cli.py:396-441`): it still
guards only `json.load` (`cli.py:400-401`), and `_store(cfg)`, `store.update(...)`
and `store.clear(...)` all run unguarded. `main()` is still
`return args.func(args)` with no top-level handler (`cli.py:2311-2322`), so a
write failure still reaches the hook's stderr on every event.

`cmd_hook` guards exactly one failure, JSON parsing
(`src/freemicro/cli.py:233-236`, *"Never break Claude Code because of a hook
parse error"*). Everything after it is unguarded:

* `_store(cfg)` -> `StateStore.__post_init__` -> `self.directory.mkdir(...)`
  (`state/engine.py:961`) raises `PermissionError` or `OSError` if
  `~/.freemicro` is read-only, missing on a mount that has not come back, or on
  a full disk.
* `store.update(...)` re-raises any write failure by design
  (`state/engine.py:1025-1040`).
* `main()` has no top-level handler (`cli.py`, end of file): it is
  `return args.func(args)`.

So the traceback goes to the hook's stderr on **every single hook event**, seven
events per turn.

**How the owner would write it up:** *"My disk filled up and now every Claude
Code turn prints a Python traceback about `freemicro hook`. I had to uninstall
the hooks to get work done."*

**Severity: medium**, and it violates the module's own stated rule twice over.

**Fix:** wrap the body of `cmd_hook` in `try/except Exception: return 0`. The
hook has exactly one job and no output; there is no failure it should ever
surface. Optionally write the reason to `$FREEMICRO_HOOK_LOG` if that is set.

### F19. A corrupt `config.json` is silent, a corrupt `keymap.json` is fatal, and only one of those is right

**OPEN.** Confirmed: `Config.load` still swallows a broken `config.json` and
returns defaults with no error recorded on the object (`config.py:76-78`), and
neither `doctor` nor `status` surfaces the parse failure.

`Config.load` swallows a broken `config.json` and returns defaults
(`src/freemicro/config.py:76-78`, *"A broken config should never stop the light
from working"*). `padconfig.load` raises (`padconfig.py:545-562`, *"running on
the old defaults while your changes vanish is far worse than an error
message"*). Both arguments are good and they contradict each other. The
consequence: a user who fat-fingers `config.json` gets a 30-minute TTL and a
180-second green back with no warning anywhere, including from `doctor`, which
checks the pad config and never mentions `config.json`.

**Severity: low-medium.** **Fix:** keep the tolerant load, but record the parse
error on the `Config` object and have `doctor` and `status` print it.

### F20. `freemicro run` and `keys` acquire the pad lock but ignore whether they got it

**OPEN.** Confirmed: `cmd_keys` (`cli.py:1433`) and `cmd_run` (`cli.py:1814`)
still call `lock.acquire()` without checking the result, relying on the earlier
`_pad_is_taken` check a few lines up. (Other call sites, e.g. `cli.py:774`, do
check.)

`cmd_run` (`cli.py:1530-1535`) and `cmd_keys` (`cli.py:1176-1181`) call
`lock.acquire()` without checking the return value. In practice `_pad_is_taken()`
was checked a few lines earlier, so the window is small, but two `freemicro run`
invocations started in the same second both proceed and both open the device.
The lock's whole purpose is to make that impossible.

**Severity: low.** **Fix:** `if not lock.acquire(): return _pad_is_taken(...)`.

### Checked and found sound

* **Two FreeMicro processes.** `PadLock` is an `flock`, so a killed holder
  releases it in the kernel; `reclaim_stale_lock` explains rather than instructs
  (`staleness.py:632-675`). Good.
* **Pad yanked mid-write.** `_send` catches and retries on a backoff;
  `_apply_exit_state` catches and returns. The old permanent latch was F4, now
  fixed; nothing crashes.
* **Session whose terminal died.** `plan_for_session` fails closed to "do
  nothing" and `tab_script` never launches an app (`focus.py:276-331`). Good,
  and the AppleScript injection surface is pattern-checked at two layers.
* **macOS sleep/wake.** The pad drops, `run_with_reconnect` reopens, `rebuild()`
  makes a fresh renderer, `owner.attach()` reasserts. Correct.
* **Permissions revoked while running.** Degrades to F18's silent retry loop. Not
  ideal, not dangerous.
* **`SessionEnd` firing twice.** Explicitly handled as idempotent
  (`state/engine.py`, `clear()`).

---

## Assertions that encode an assumption rather than a requirement

### F21. `test_ttl_expires_stale_sessions` now passes by accident

**FIXED.**

> **Resolved:** the fixture no longer reaches for the real machine. The store
> fixture now injects a stubbed `terminal_probe` and a fake `liveness`
> (`tests/test_state_engine.py:141-142`), the shared liveness cache is
> monkeypatched (`:128`), and the TTL assertion was rewritten as
> `test_ttl_expires_a_session_nothing_vouches_for` (`:180`), which tests the
> liveness-aware behaviour rather than a fake-clock accident. No test in the file
> shells out to real `ps` for this any more. The original description follows.

`tests/test_state_engine.py:83-89` asserts that a record older than the TTL is
deleted from disk. The `store` fixture (`tests/test_state_engine.py:44-49`) uses
the **real** `ProcessLiveness` and the **real** `current_terminal` probe, so:

* `store.update("old", ...)` records `pid = os.getppid()`, a live process, and
  `updated_at = 1000.0` from the fake clock;
* `sessions()` sees `quiet=True`, asks `verdict(verify=True)`, finds the pid
  alive, then shells out to real `ps` for its start time;
* the real start time is around 1.7e9, which is not `<= 1000.0 + 5.0`, so the
  identity check fails and the record is deleted.

The test passes because the fake clock starts at 1000 and the wall clock does
not. Set the fixture's `Clock` to a realistic epoch value and the record is
correctly *kept* by the liveness rule, and the test fails. It therefore asserts
the old behaviour and proves nothing about the new one, while also shelling out
to `ps` in a suite whose `conftest.py` opens with *"no test ever touches real
hardware"* and whose sibling tests carefully inject `terminal_probe`.

**Severity: medium** (a test that passes for the wrong reason is worse than no
test). **Fix:** give the fixture a stubbed `terminal_probe` and an injected
`liveness`, like `open_store()` in the same file already does, and split the
assertion into "a record with no pid is swept" and "a record whose pid is gone is
swept".

### F22. `test_joystick_fires_once_per_flick` encodes the wrong "up"

**FIXED.**

> **Resolved:** with F5 fixed, the tests were corrected to the factory table.
> `test_joystick_fires_once_per_flick` now expects `JOY_DOWN` at angle 0.25
> (`tests/test_bridge.py:125-129`), and the direction test asserts the full
> factory sector map including `(0.75, "JOY_UP")` with a comment citing
> `FACTORY-DEFAULTS.md` §6 (`tests/test_bridge.py:133-142`, `178`). The original
> description follows.

`tests/test_bridge.py:105-111` asserts `tracker.update(0.25, 0.7) == "JOY_UP"`,
twice. Angle 0.25 is *down* per `FACTORY-DEFAULTS.md` §6 and per
`input/pointer.py`'s own orientation note. The test restates
`JOYSTICK_INPUTS`'s ordering rather than checking it against the hardware fact,
which is exactly why F5 has survived. `tests/test_padconfig.py:208-212` has the
same shape with placeholder names A/B/C/D, which is fine because it is testing
the wheel arithmetic, not the orientation.

**Severity: medium.** **Fix:** with F5 fixed, this test becomes
`update(0.75, 0.7) == "JOY_UP"` and gains a comment pointing at
`FACTORY-DEFAULTS.md` §6.

### F23. Tests that restate the implementation

**PARTIAL.** The third bullet is resolved: the release-on-shutdown behaviour now
exists (F1) and is covered - `release_all` / `release_held_keys` / `close()`
appear in `tests/test_bridge.py` and `tests/test_actions.py`. The first two
bullets (a `direction_for` test that restates list order, and the absence of a
docs test that would have caught F8) are lower-value and unchanged.

Lower value, listed for completeness:

* `tests/test_padconfig.py:208-212` asserts `direction_for` returns the list in
  list order. That is the definition of the function.
* `tests/test_webui.py:633` asserts the Wispr starter uses `ctrl+cmd+o` with the
  comment "verified working on hardware". Good test; the problem is that
  `README.md:228` disagrees and no test covers the docs (F8). A docs test that
  greps for `ctrl+option+cmd+d` outside `WEB-UI.md` would have caught it.
* There is no test anywhere that a `hold` action is released on shutdown or on
  disconnect. `tests/test_actions.py:232-237` proves `perform` then `release`
  produces down-then-up, which is the happy path only. F1's four failure modes
  are untested because the behaviour does not exist.

---

## Docs that claim what the code does not do

**Mixed as of 2026-07-24.** The rows tied to F5, F7, F11 and F12 are now
resolved (see those findings). Still true: the two mic-shortcut rows
(`README.md:251`, `CUSTOMIZING.md`), the F9 rows about `status`, the F6 doctor
row, and the F14 coexistence row. The `default_keymap.json` joystick-comment /
`pointer.py` px/s rows are resolved by F7.


| Doc | Claim | Reality |
|---|---|---|
| `README.md:228` | "The shipped default puts `Ctrl+Option+Cmd+D` on `ACT11`, the mic key" | Default is `ctrl+cmd+o` on `ACT10`; `ACT11` is `none`. The combo named is one the web UI refuses (F8) |
| `docs/CUSTOMIZING.md:121-137` | `ACT11` is the mic key, bind `ctrl+option+cmd+d` | Same (F8) |
| `docs/CUSTOMIZING.md:148-167` | Joystick section describes flicks only, with `deadzone`/`origin`/`directions` | Default mode is now `pointer`; those four bindings do not fire. `mode`, `max_speed`, `gamma`, `tick_hz`, `precision_key`, `invert_y` are undocumented |
| `docs/CUSTOMIZING.md:209` + `:213` | Omitted states fall back to "FreeMicro's canonical palette"; shipped colours are "exact factory values" | The fallback palette is not the factory palette (F11) |
| `docs/AGENT-KEYS.md:130` | "Set either value to `0` to switch the check off" (of `working_ttl_seconds` / `tool_ttl_seconds`) | True for the store, false for `status`, the menu bar and the Agent Keys (F2, F3). `CUSTOMIZING.md:229-231` makes the same promise for `done_ttl_seconds`, which *is* honoured everywhere |
| `docs/AGENT-KEYS.md:133-134` | "`status` reports such a session as `idle` ... a reader can say `idle (was working)`" | `cmd_status` prints `s.state.value` only (F9) |
| `docs/AGENT-KEYS.md:293-311` | Specifies the slot display for `freemicro status`, with code | Not implemented (F9) |
| `docs/PROTOCOL.md:208, 214-228, 266-267` | `lights.preview` drives the base; FreeMicro uses it for live state | The code uses `rgbcfg`; the same file says so twice elsewhere (F12) |
| `default_keymap.json` joystick comment | "watch the live angle, distance and px/s" in `--dry-run` | No px/s is printed (F7) |
| `src/freemicro/input/pointer.py:117` | `PointerVector` "Printed by `freemicro keys --dry-run`" | Never printed (F7) |
| `docs/CUSTOMIZING.md:263-275` | Reassert "heals itself" with four triggers | Only the ChatGPT-quit trigger fires in normal use (F14) |
| `src/freemicro/cli.py:861` (doctor) | Without Quartz, "Everything works except fn bindings and hold-to-talk" | The `mouse` action and the default joystick mode also stop working, silently (F6) |

### Code the docs do not mention

* `joystick.mode`, `pointer_deadzone`, `max_speed`, `gamma`, `tick_hz`,
  `precision_key`, `precision_scale`, `invert_y` (all in `padconfig.py:235-259`)
  appear nowhere in `docs/`. The only prose is the `_comment` block inside
  `default_keymap.json`, which a user who ran `keys --init` before the feature
  landed will never see.
* `SessionState.process_alive` / `kept_by_process` / `interrupted` are consumed
  only by `webui/api.py` (F9).
* `FREEMICRO_HOOK_LOG` (`cli.py:213`) is documented in no file; it is the tool
  that found the interrupt behaviour in the first place and there is no way for
  a user to discover it. It also appends without any size cap.

---

## Summary of recommended fixes, in order

Status as of 2026-07-24. Done items are struck through in words rather than
markup.

1. **F1 - DONE.** Held chords are tracked in `quartz._held` and released on
   disconnect, reload, exit, signal and `atexit`.
2. **F2, F3 - DONE.** One store construction (`default_store`), and
   `SlotResolver.for_store` reads the TTLs off the store.
3. **F4 - DONE.** Lighting no longer latches off silently; it retries on a
   backoff and says so once.
4. **F5 - DONE.** The discrete wheel matches the factory sector table, and the
   test was fixed (F22).
5. **F6 - STILL OPEN (partial), F7 - DONE.** The vector now prints in
   `--dry-run`; pointer errors are still unsurfaced because `Bridge` builds the
   `Pointer` without an `on_error` and `update` restarts on error.
6. **F10 - STILL OPEN.** `cmd_hook` still guards only the JSON parse; wrap the
   whole body and add a top-level handler in `main()`.
7. **F8 - partial, F11 - DONE, F12 - DONE.** Palette and lighting method are one
   copy each and the PROTOCOL prose is fixed; the mic-shortcut docs
   (`README.md`, `CUSTOMIZING.md`) still name the rejected combo.
8. **F9 - STILL OPEN.** `freemicro status` still prints session UUIDs; wire it to
   `focus.current_slots()` and `describe_claim()`.
9. **F15 - STILL OPEN.** Per-tick file I/O and poll rate are unchanged.
10. **F21 - DONE.** The TTL test now tests the liveness-aware behaviour.

The current open set, ordered, is the "Still open" list at the top of this file.
